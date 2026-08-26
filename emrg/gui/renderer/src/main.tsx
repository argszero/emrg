import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
// 样式：迁移期 shell.css 只含骨架；Batch 5 切换后恢复 vanilla css/（设计承诺
// "复用 vanilla CSS，无需改样式表"，#1024 删除后未接入 build —— 组件 className
// 与 classes 完全对齐，tokens.css 提供组件内联 var(--*) 变量）。
// 顺序：tokens(变量) → base(基础) → components → layout → animations → shell(React 覆盖)
import "../css/tokens.css";
import "../css/base.css";
import "../css/components.css";
import "../css/layout.css";
import "../css/animations.css";
import "./shell.css";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("React mount point #root missing (renderer/src/index.html)");
}
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
