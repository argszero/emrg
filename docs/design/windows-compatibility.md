# Windows 操作系统兼容改造

## 问题分析

### 现状

EMRG 的整个 IPC 层基于 **Unix domain socket** (`AF_UNIX`)，在 Windows 上完全不可用：

| 文件 | 行 | Unix socket 使用 |
|------|-----|------------------|
| `emrg/protocol.py` | 19 | `SOCKET_NAME = "emrgd.sock"` |
| `emrg/server/daemon.py` | 183-185 | socket 文件路径计算、过期文件清理 |
| `emrg/server/daemon.py` | 210 | `asyncio.start_unix_server(str(self.socket_path))` |
| `emrg/server/daemon.py` | 234-235 | 关闭时删除 socket 文件 |
| `emrg/client/app.py` | 123-131 | `socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)` 健康检查 |
| `emrg/client/app.py` | 146, 156 | `asyncio.open_unix_connection(str(sock))` 连接 + ping |
| `emrg/client/app.py` | 198-205 | `asyncio.open_unix_connection()` 主连接入口 |
| `emrg/server/daemon.py` | 165 | `asyncio.open_unix_connection()` BackgroundThread 演化连接 |

## 参考实现

### Claude Code 的做法

Node.js 的 `net.createServer()` 和 `net.connect()` **统一了 UDS 和 Named Pipe**，同一个 API 根据路径格式自动判断传输类型：

```typescript
// src/utils/claudeInChrome/common.ts:481-486
export function getSecureSocketPath(): string {
  if (platform() === 'win32') {
    return `\\.\pipe\claude-mcp-browser-bridge-${getUsername()}`
  }
  return join(getSocketDir(), `${process.pid}.sock`)
}

// 同一个 API，传不同格式的路径，底层自动选择传输方式
server.listen(`/tmp/xxx.sock`)       // → UDS
server.listen(`\\.\pipe\xxx`)        // → Named Pipe
socket.connect(`/tmp/xxx.sock`)      // → UDS  
socket.connect(`\\.\pipe\xxx`)       // → Named Pipe
```

所以 Claude Code 只需要 `if (platform() === 'win32') → 换路径` 就够了，bind/connect 调用是同一个函数。

### EMRG (Python) 的差异

Python 的 `asyncio` **没有统一** UDS 和 Named Pipe，需要调用不同的函数：

```python
# Unix: 专用函数
await asyncio.start_unix_server(handler, path="/tmp/xxx.sock")
await asyncio.open_unix_connection("/tmp/xxx.sock")

# Windows: ProactorEventLoop 下的 start_server/open_connection 支持 path= 参数
await asyncio.start_server(handler, path=r"\\.\pipe\xxx")
await asyncio.open_connection(r"\\.\pipe\xxx")
```

因此 EMRG 需要在封装层多做一层 `if os.name == "nt"` 分支来选择调用哪个函数。**思路和 Claude Code 一致（平台检测 + 各平台原生 IPC），但 Python API 的差异导致多一层分支。**

### Codex (OpenAI)

```rust
// codex-rs/uds/src/lib.rs
#[cfg(unix)]    → tokio::net::UnixListener / UnixStream (AF_UNIX)
#[cfg(windows)] → uds_windows crate (Windows 上的 Unix socket 兼容层)

// codex-rs/app-server-transport/src/transport/mod.rs
pub enum AppServerTransport {
    Stdio,                                      // stdin/stdout
    UnixSocket { socket_path: AbsolutePathBuf }, // UDS (Unix) / uds_windows (Windows)
    WebSocket { bind_address: SocketAddr },      // TCP WebSocket（备选）
    Off,
}
```

**方案**：编译期条件编译 + 多传输方式。Unix 用原生 AF_UNIX，Windows 通过 `uds_windows` crate 模拟 UDS。另有 WebSocket over TCP 作为备选，支持 `ws://IP:PORT` 绑定，并对非 loopback 连接增加 token 认证。

### 两者的共同点

| 特征 | Claude Code | Codex |
|------|-------------|-------|
| Unix 传输 | Unix Domain Socket (文件路径) | Unix Domain Socket (文件路径) |
| Windows 传输 | Named Pipe (`\\.\pipe\...`) | `uds_windows` crate（UDS 兼容层）|
| 平台分发方式 | 运行时 `platform() === 'win32'` | 编译期 `#[cfg(unix)]` / `#[cfg(windows)]` |
| TCP 备选 | 无 | WebSocket over TCP + token 认证 |
| 安全模型 | UDS/pipe 的文件系统权限 | UDS `0o600`，WebSocket 需认证 |

## 设计方案

### 方案对比

| 方案 | 说明 | 优点 | 缺点 |
|------|------|------|------|
| **A. 平台检测 + 双路径**（参考 Claude Code/Codex） | Unix 保持 UDS，Windows 用 Named Pipe | 各平台用原生最佳方案 | 需维护两套 bind/connect 代码 |
| **B. TCP localhost 统一** | 全平台统一用 `127.0.0.1:<port>` | 一套代码，简单 | 有端口文件管理开销 |
| **C. TCP localhost + UDS 双路径**（参考 Codex 的 `AppServerTransport`） | 默认 UDS，Windows 回退 TCP | 灵活，可扩展 | 过度设计（EMRG 不需要远程连接） |

### 选型：方案 A — 平台检测 + 双路径

与 Claude Code/Codex 思路一致：各平台用各自原生的 IPC 机制。

Python 与 Node.js/Rust 的差异：
- **Node.js** (`net` 模块) 自动根据路径格式分发 UDS / Named Pipe，调用方只需换路径
- **Rust** (`#[cfg]`) 编译期条件编译，不同平台编译进不同的代码
- **Python** (`asyncio`) UDS 和 Named Pipe 是不同的函数，需要 `if os.name == "nt"` 在运行时选择

三者本质相同（平台检测 → 原生 IPC），仅语言抽象层的厚度不同。

### 核心思路

在 `emrg/connect.py`（新文件）中封装平台差异：

```python
# emrg/connect.py

import asyncio
import os
from pathlib import Path
from emrg.config import config_dir

# ── 连接标识符 ──────────────────────────────
# Unix:  ~/.emrg/emrgd.sock
# Win32: \\.\pipe\emrgd
CONNECT_ID = "emrgd"


def get_server_path() -> str:
    """返回 server 绑定的地址，平台自适应。"""
    if os.name == "nt":
        return rf"\\.\pipe\{CONNECT_ID}"
    return str(config_dir() / f"{CONNECT_ID}.sock")


async def start_server(handler) -> asyncio.AbstractServer:
    """启动 server，平台自适应。"""
    if os.name == "nt":
        # Windows: named pipe
        server = await asyncio.start_server(
            handler,
            path=rf"\\.\pipe\{CONNECT_ID}",
        )
    else:
        # Unix: domain socket
        sock_path = config_dir() / f"{CONNECT_ID}.sock"
        if sock_path.exists():
            sock_path.unlink()
        server = await asyncio.start_unix_server(handler, path=str(sock_path))
    return server


async def connect_to_server() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """连接到 emrgd server，平台自适应。"""
    if os.name == "nt":
        return await asyncio.open_connection(rf"\\.\pipe\{CONNECT_ID}")
    sock_path = config_dir() / f"{CONNECT_ID}.sock"
    return await asyncio.open_unix_connection(str(sock_path))


def cleanup_server() -> None:
    """server 关闭时清理资源。"""
    if os.name == "nt":
        return  # named pipe 无需清理
    sock_path = config_dir() / f"{CONNECT_ID}.sock"
    if sock_path.exists():
        sock_path.unlink()
```

### 设计要点

**`os.name == "nt"` 而非 `sys.platform == "win32"`**：
- `os.name == "nt"` 在 CPython 上等价于 Windows，是 Python 官方推荐的平台检测方式
- Claude Code 用 `platform() === 'win32'`，Node.js 等价物
- Codex 用 `#[cfg(windows)]`，Rust 编译期检测

**Unix 端继续保持现有行为**：
- socket 文件在 `~/.emrg/emrgd.sock`（不变）
- 过期文件清理逻辑不变
- `0o600` 权限不变

**Windows 端 Named Pipe**：
- `\\.\pipe\emrgd`，不需要文件系统
- 无过期 socket 文件问题
- 安全性依赖 pipe namespace（仅本地可访问）

**不需要端口文件**：两种平台都不需要额外存储端口号——Unix 用文件路径，Windows 用固定的 pipe 名。

### 各文件改动

#### `emrg/connect.py`（新增）

封装 `get_server_path`、`start_server`、`connect_to_server`、`cleanup_server` 四个函数，内部用 `os.name` 分发。

#### `emrg/protocol.py`

```diff
- SOCKET_NAME = "emrgd.sock"
```

`SOCKET_NAME` 移入 `connect.py`，不再作为公开常量导出。

#### `emrg/server/daemon.py`

```diff
# __init__
- runtime_dir = config_dir()
- runtime_dir.mkdir(parents=True, exist_ok=True)
- self.socket_path = runtime_dir / SOCKET_NAME
- if self.socket_path.exists():
-     self.socket_path.unlink()

# serve()
- server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
+ server = await start_server(self._handle_client)

# finally
- if self.socket_path.exists():
-     self.socket_path.unlink()
+ cleanup_server()
```

BackgroundThread：

```diff
- reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
+ reader, writer = await connect_to_server()
```

#### `emrg/client/app.py`

```diff
# _try_connect()
- sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
- sock.settimeout(2)
- sock.connect(str(sock_path))
+ # 使用 connect.py 的统一接口

# _check_and_restart_if_stale()
- reader, writer = await asyncio.open_unix_connection(str(sock))
+ reader, writer = await connect_to_server()

# connect_to_server()
- return await asyncio.open_unix_connection(str(sock))
+ return await connect_to_server()
```

### 不变的部分

`StreamReader` / `StreamWriter` API 与底层传输无关：

- `reader.readline()` — 读行
- `writer.write()` / `writer.drain()` — 写数据
- `_send()` 的 `ConnectionResetError` / `BrokenPipeError` / `OSError` 异常处理
- `_handle_client` 的连接处理逻辑
- 整个 tool loop、memory reflection、compact 等业务逻辑

### 安全

| 平台 | 机制 |
|------|------|
| Unix | socket 文件权限 `0o600`，仅 owner 可连接 |
| Windows | Named pipe 仅在本地 pipe namespace 可见，无网络暴露 |

### 启动/重启流程

**Unix**（与现在完全相同）：
```
server 启动 → unlink 过期 sock 文件 → bind → 写 0o600 权限
client 连接 → open_unix_connection(sock_path)
server 崩溃 → sock 文件残留 → 下次启动 unlink 清理
```

**Windows**：
```
server 启动 → 创建 named pipe \\.\pipe\emrgd
client 连接 → open_connection(\\.\pipe\emrgd)
server 崩溃 → pipe 自动销毁（内核管理）
```

### 修改文件清单

| 文件 | 变更 |
|------|------|
| `emrg/connect.py` | **新增**。封装 `start_server`、`connect_to_server`、`cleanup_server` 等平台自适应函数 |
| `emrg/protocol.py` | 移除 `SOCKET_NAME` 常量 |
| `emrg/server/daemon.py` | 移除直接 socket 操作；改用 `connect.py` 函数 |
| `emrg/client/app.py` | 移除直接 socket 操作；改用 `connect.py` 函数 |
| `docs/design/evolution-refactor.md` | BackgroundThread 连接代码改用 `connect_to_server()` |

### 复杂度评估

| 方面 | 评估 |
|------|------|
| 代码改动量 | ~60 行新增（connect.py）+ ~30 行删除 + ~15 行修改 |
| 新依赖 | 无 |
| 破坏性变更 | macOS 用户无感知；Windows 首次可用 |
| 测试影响 | 现有测试不受影响（Unix 路径逻辑不变） |
| Windows 验证 | 需要 Windows 环境测试 named pipe 连接 |

### 不做的事

- ❌ 不引入 TCP localhost 端口管理（增加不必要的复杂度）
- ❌ 不加认证层（与当前安全模型一致）
- ❌ 不改协议格式（JSON 行不变）
- ❌ 不支持远程连接（始终本地 IPC）
