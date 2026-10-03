"""AppImage 产物名 —— 名字里不得含 `linux`，且每处命名它的地方必须跟着走。

来源（rant 2026-09-30T11:42:41 → issue #1781，Part 1/2）：AppImage 官方目录的
自动检查按名字点名 `EMRG-0.3.5-linux-x86_64.AppImage`：

    WARNING: AppImage name 'EMRG-0.3.5-linux-x86_64.AppImage': should not contain
    'Linux', since all AppImages are for Linux (e.g. 'App-1.0-x86_64.AppImage',
    not 'App-1.0-linux-x86_64.AppImage')

那个中缀是我们自己加的（`packaging/make-installer.sh` 的收集行），所以修法是改名 +
让所有写这个名字的地方跟上。而**文档漂移就是这条缺陷的另一半**：README 的下载表当天
已经在描述一个不含 `linux` 的 AppImage 名字，而 release 里躺着含 `linux` 的那个 ——
也就是说「产物名」和「文档里的名字」此前没有任何东西保证一致。

因此本文件里没有一处把名字**重述**一遍再断言它：

* 产物名由 `packaging/make-installer.sh` 的收集行**执行**得出（架构用同名 shell 函数
  注入，见 `_run_line` —— PATH 前缀那种替身在 Windows 上会被 `uname.exe` 盖掉），
  断言的是 `artifacts/` 里真的出现了哪个文件名；
* release workflow 的 `artifact:` glob 必须与这个执行结果 `fnmatch` 一致；
* 两份 README 的 Linux 行必须写着这个形态，且不得残留旧形态。

于是：脚本改名而 workflow/README 不动、或把 `linux` 加回脚本，都会红；而「删掉断言
让它恒真」在这里表现为找不到收集行时的显式失败，不会静默通过。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.tool_preflight import starts

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "packaging" / "make-installer.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-release.yml"
READMES = (REPO_ROOT / "README.md", REPO_ROOT / "README.cn.md")

VERSION = "9.9.9"


# ── 取「真的会执行的那一行」 ─────────────────────────────────────────────────


def _linux_branch(source: str) -> str:
    """`packaging/make-installer.sh` 的 `linux)` 分支原文（到它的 `;;` 为止）。

    取不到就失败 —— 一个找不到被测行的守卫会恒真，而这正是它要防的那类缺陷。
    """
    m = re.search(r"^\s*linux\)\n(?P<body>.*?)^\s*;;\s*$", source, re.S | re.M)
    assert m, (
        f"{INSTALLER.name} 里找不到 `linux)` 分支 —— 本文件的断言对象变了，需重写"
    )
    return m.group("body")


def _line_of(branch: str, needle: str) -> str:
    """分支里第一行含 `needle` 的**可执行**行（去掉缩进，丢掉注释行）。"""
    for line in branch.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if needle in stripped:
            return stripped
    raise AssertionError(
        f"{INSTALLER.name} 的 linux 分支里没有含 {needle!r} 的行 —— 断言对象变了"
    )


# ── 执行它 ───────────────────────────────────────────────────────────────────


def _run_line(tmp_path: Path, line: str, machine: str, **env: str) -> tuple[list[str], str]:
    """在临时目录里执行一行脚本，回它在 `$DIST/artifacts/` 里造出的文件名，和它的输出。

    执行而不是解析：`cp` 的目标串由 shell 自己展开（`$(uname -m)` 也在内），所以断言
    的是 `make-installer.sh` **真的**会写出哪个名字。

    架构用**同名 shell 函数**注入，不用 PATH 前缀 —— 后者在 Windows 上无声失效：那里的
    可执行文件按扩展名解析，一个没有扩展名的 `uname` 脚本永远盖不住 `uname.exe`，于是
    `uname -m` 回的是宿主自己的架构（实测：GitHub 的 `test-windows` 腿红在
    `aarch64` 用例上，拿到的却是宿主 x64 的 `x86_64`，run 36683843964）。函数在 bash 里
    优先于命令查找，与平台无关。

    函数是否真的生效由**探针**回答（同一段脚本里 `echo "$(uname -m)"`），而不是假定：
    一个没生效的替身会让本文件静默地量宿主，而不是量被测的那一行。
    """
    shell = shutil.which("bash")
    if not starts(shell):
        pytest.skip("no POSIX shell that starts is available to execute the installer line")
    dist = tmp_path / "dist"
    (dist / "artifacts").mkdir(parents=True)
    script = f'uname() {{ echo "{machine}"; }}\necho "PROBE:$(uname -m)"\n{line}\n'
    proc = subprocess.run(
        [shell, "-c", script], cwd=str(tmp_path), env={**os.environ, **env},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, f"{script!r} 执行失败：{proc.stderr}"
    assert f"PROBE:{machine}" in proc.stdout, (
        f"架构替身没有生效：探针没看到 {machine}，实际输出 {proc.stdout!r} —— "
        f"此时这一行量的是宿主架构，不是被测的那一行"
    )
    return sorted(p.name for p in (dist / "artifacts").iterdir()), proc.stdout


def _appimage_name(tmp_path: Path, machine: str = "x86_64") -> str:
    """收集 AppImage 的那一行跑出来的产物名。"""
    source = INSTALLER.read_text(encoding="utf-8")
    line = _line_of(_linux_branch(source), 'cp "$APPIMAGE"')
    source_image = tmp_path / "emrg-gui-dist" / f"EMRG-{VERSION}-{machine}.AppImage"
    source_image.parent.mkdir(parents=True, exist_ok=True)
    source_image.write_bytes(b"not really an appimage")
    produced, _stdout = _run_line(
        tmp_path, line, machine, APPIMAGE=str(source_image), DIST=str(tmp_path / "dist"),
        VERSION=VERSION,
    )
    assert len(produced) == 1, f"收集行写出的文件不是恰好一个：{produced}"
    return produced[0]


# ── 1. 产物名 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_collected_appimage_carries_no_linux_segment(tmp_path, machine):
    """官方规则的原文是「不该含 'Linux'」，判据按 `-` 分段看，避免误伤别的词。"""
    name = _appimage_name(tmp_path, machine)

    assert name == f"EMRG-{VERSION}-{machine}.AppImage", name
    assert "linux" not in [seg.lower() for seg in name.split("-")], (
        f"{name} 里含 `linux` 段 —— AppImage 官方目录的检查就是按名字点名这一条"
        f"（rant 2026-09-30T11:42:41）"
    )


# ── 2. workflow 的 glob ──────────────────────────────────────────────────────


def _artifact_globs() -> list[str]:
    globs = re.findall(r"^\s*artifact:\s*(\S+)\s*$", WORKFLOW.read_text("utf-8"), re.M)
    assert globs, f"{WORKFLOW.name} 的 matrix 里没有 artifact: glob —— 断言对象变了"
    return globs


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_release_globs_match_the_name_the_installer_produces(tmp_path, machine):
    """两处名字必须一致：只改一处就是「产物名与流水线各自的真源」。

    断言的是**整条名字的形状**（只把版本号换成 `*`）而不是「glob 能匹配上」——
    后者被 `EMRG-*-x86_64.AppImage` 里那个 `*` 吞掉一切，连 `linux` 中缀都吃得下：
    实测把 `linux` 加回脚本后，`fnmatch` 形式的断言仍然通过（本条守卫的第一次写法就
    是这样，被变异臂 A1 抓到）。所以形状必须逐字相同，通配只有版本号那一处。
    """
    name = _appimage_name(tmp_path, machine)
    shape = name.replace(f"-{VERSION}-", "-*-", 1)
    globs = _artifact_globs()

    assert shape in globs, (
        f"脚本产出 {name}，形状应为 {shape!r}，但 workflow 的 artifact glob 是 {globs} —— "
        f"两处名字必须逐字一致（只有版本号可以是通配）"
    )
    assert sum(1 for g in globs if fnmatch.fnmatch(name, g)) == 1, (
        f"{name} 被多条 matrix glob 同时认领：{globs} —— 通配太宽的 glob 会让本断言失去分辨力"
    )


# ── 3. 文档 ──────────────────────────────────────────────────────────────────


def _linux_row(path: Path) -> str:
    rows = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("| Linux |")]
    assert len(rows) == 1, f"{path.name} 的 Linux 产物行不是恰好一条：{rows}"
    return rows[0]


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_readme_names_the_appimage_the_installer_produces(readme, machine):
    """下载表要写用户真的会看到的那个名字，且旧形态不得残留。

    这条缺陷的另一半就在这儿：修名前，两份 README 已经写着不含 `linux` 的 AppImage
    名字，而 release 里躺着含 `linux` 的那个 —— 二者从未被任何东西绑在一起。
    """
    row = _linux_row(readme)

    assert f"EMRG-<ver>-{machine}.AppImage" in row, (
        f"{readme.name} 的 Linux 行没有写出 AppImage 的完整名字：{row}"
    )
    assert f"EMRG-<ver>-linux-{machine}.AppImage" not in row, (
        f"{readme.name} 的 Linux 行仍写着已退役的名字：{row}"
    )


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_the_readme_still_names_the_headless_artifacts_with_their_linux_token(readme, machine):
    """`.run` 的 `linux` 中缀是**故意保留**的，不是漏改。

    官方那条规则只针对 AppImage（"all AppImages are for Linux"），其余名字里 `linux`
    是有信息量的：release 资产是一张平铺的表，它正是把无头安装器与 macOS / Windows
    产物区分开的那个词。钉住它，免得下一次「顺手统一命名」把它一起删掉。
    """
    row = _linux_row(readme)

    assert f"EMRG-<ver>-linux-{machine}.run" in row, (
        f"{readme.name} 的 Linux 行没有把 .run 的完整名字写出来（`linux` 中缀是有意保留的）：{row}"
    )
