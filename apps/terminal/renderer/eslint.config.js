export default [
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      parserOptions: {
        ecmaFeatures: { jsx: true },
        ecmaVersion: "latest",
        sourceType: "module",
      },
    },
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            { name: "fs", message: "Renderer code must not access the filesystem." },
            { name: "node:fs", message: "Renderer code must not access the filesystem." },
            { name: "bleak", message: "Renderer code must not access BLE clients." },
          ],
          patterns: [
            {
              group: ["**/*device*client*", "**/*upload*client*", "**/*network*client*"],
              message: "Renderer code must consume the terminal adapter snapshot, not platform clients.",
            },
          ],
        },
      ],
    },
  },
];
