export default [
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
    },
    rules: {
      // 红线 R-2：主进程不得实现任何业务判定（自检通过与否、会话有效性、质量分级）。
      // 它只管窗口、进程、打印。这条 import 限制挡住最容易越界的那一类：
      // 主进程直接去用 gait 的分析/质量模块。
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/quality/**", "**/analysis/**", "**/protocolflow/**"],
              message: "红线 R-2：主进程不做业务判定，那些结论只有 sidecar 能给。",
            },
          ],
        },
      ],
    },
  },
];
