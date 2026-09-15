import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // 相对路径：Electron 用 file:// 加载 dist/index.html，默认的 "/" 会让资源指向
  // 文件系统根目录，窗口一片空白且不报错。
  base: "./",
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/testing/setup.js",
  },
});
