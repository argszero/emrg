import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Vite 构建配置（React 迁移 Batch 0，设计文档 D1：产物输出 renderer/dist/）。
 *
 * - root: "src" —— src/index.html 作为 Vite 入口模板（与旧 vanilla
 *   renderer/index.html 完全隔离，Batch 5 前互不干扰，D3 全量切验证），
 *   产物落位 dist/index.html + dist/assets/*.js
 * - 无 inline script —— CSP `script-src 'self'` 兼容；base "./" 供
 *   main.js loadFile(file://) 相对引用
 * - 测试：Vitest + jsdom + @testing-library（替代 vm.runInContext 形态的 React 组件测试）
 */
export default defineConfig({
  root: "src",
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    target: "chrome120",
  },
  test: {
    globals: true, // @testing-library/react 依赖全局 afterEach 做容器自动清理
    environment: "jsdom",
    setupFiles: ["../test/setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
  },
});
