import { defineConfig } from "vitest/config";

// No JSX transform option is set here on purpose. Vitest 4 transforms with oxc,
// whose default handles these files correctly — they are .jsx and already carry
// `import React`. Setting `esbuild.jsx` instead only earns a warning that the
// option is ignored, which reads like a knob that does something.
export default defineConfig({
  test: {
    // No jsdom. Every assertion runs against renderToStaticMarkup output — these
    // components have no effects, refs or handlers worth exercising, so a DOM
    // would add startup cost and buy nothing.
    environment: "node",
    globals: true,
    include: ["test/**/*.test.jsx"],
  },
});
