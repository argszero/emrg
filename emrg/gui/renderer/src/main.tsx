import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
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
