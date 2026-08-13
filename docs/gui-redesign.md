# GUI Redesign — 工作区视图（Workspace Views）

> v0.2（rant 2026-08-13T18:55:09，宿主否决 v0.1「侧边栏内展开窄面板」方案）
> v0.1（rant 2026-08-13T14:10:14）：侧边栏 5 入口 + 侧边栏内展开面板（#743-#753，已被 v0.2 取代）

## 术语表（宿主确认，2026-08-13）

| 术语 | 英文 | 含义 | DOM |
|------|------|------|-----|
| 应用框架 | App Shell | GUI 整体（左侧边栏 + 右侧工作区） | `#app` |
| 侧边栏 | Sidebar | 左侧整栏 = 顶部导航 + 中部会话列表 + 底部状态区 | `#sidebar` |
| 导航 | Nav | 5 个视图切换入口（💬会话/📁项目/⏱任务/📣Rant/⚙设置） | `.side-nav-item[data-view]` |
| 会话列表 | Session List | 侧边栏中部的打开会话项（也是视图切换入口） | `#conv-list` / `#open-sessions` |
| 工作区 | Workspace | 右侧主区域，承载视图 | `#workspace` |
| 工作区视图 | Workspace View | 工作区内顶级可切换单元（互斥显示，状态保留） | `.workspace-view` |
| 会话视图 | Session View | 一个会话的工作区视图（聊天区 + 成果面板） | `.session-view` |
| 面板 / 区 / Tab | Panel / Area / Tab | 视图内部组件（成果面板、设置 Tab 等），不再是工作区顶级概念 | `.panel-tabs` 等 |

## 设计原则

1. **导航点击 → 工作区整块切换视图**，与会话切换完全同机制：DOM 显隐（`.active` 互斥类），视图状态（滚动位置 / 草稿 / 表单）保留。
2. 面板视图激活时：`#composer-wrap` / `#empty-state` / `#result-panel` / `#result-resizer` 隐藏；回会话视图时恢复。
3. 点当前激活导航项 → 关闭回会话视图（toggle）。点 💬 → 始终回会话视图。
4. 术语统一：DOM 用 `workspace-view` / `session-view`，无 `.side-panel` / `#chat-view` id 残留。

## 实现记录

- v0.2（2026-08-13）：`#chat-view` → `#workspace`；4 个面板（projects/tasks/rants/settings）从 `#sidebar` 移入 `#workspace`，`.side-panel` → `.workspace-view`；删除 `#panel-sessions`；`data-panel` → `data-view`；`switchPanel` → `switchView`；`state.activePanel` → `state.activeView`。
