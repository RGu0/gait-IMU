import { defineConfig } from "vitest/config";

// Mirrors packages/design-system: node environment, assertions run against
// renderToStaticMarkup output. No JSX option is set — Vitest 4 transforms with
// oxc, whose default handles a .jsx file that already imports React.
export default defineConfig({
  test: {
    environment: "node",
    globals: true,
    include: ["test/**/*.test.jsx"],
  },
});
