# 同步来源与边界

本目录是 **Claude Design 里 Steady Health Design System 项目的镜像**，作为
`apps/terminal/renderer/` 的构建输入。它是派生物，不是设计源。

| | |
|---|---|
| 源项目 | `40065384-1a4c-4f8f-93eb-4d067444d67f`（`PROJECT_TYPE_DESIGN_SYSTEM`） |
| 拉取时间 | 2026-08-21（`components/gait/LinkStatus.*` 三份于 2026-09-10 单独重拉，RAY-274 交付 4） |
| 拉取方式 | DesignSync `get_file`，逐个文件 |
| 对应 Issue | RAY-249 |

## 改动方向

**设计阶段的权威源是 Claude Design 项目本身**，组件在那里被渲染、评审、修改。

- 要改组件 → 在 Claude Design 改，或本地改后用 DesignSync 推回去，然后重新拉取本目录
- **不要只改这里** —— 那会让镜像与源分叉，而分叉不会有任何东西提醒你

判断是否过期：比对源项目的 `updatedAt` 与上表的拉取时间。

## 有意不镜像的东西

| 未镜像 | 原因 |
|---|---|
| `_ds_bundle.js`、`_ds_manifest.json` | Claude Design 的**编译产物**。仓库要的是源码，由前端自己的构建链编译。带上它们等于制造两份真相 |
| `*.card.html` | 设计系统面板的预览卡，从 unpkg 加载 React 并引用编译后的 bundle。它们是设计面的东西，不是构建输入 |
| `guidelines/*.card.html` | 同上。规范内容已在 `DESIGN-GUIDE.md` 里 |
| `ui_kits/feetforceplate/` | 另一个产品的界面 |
| `assets/`、`uploads/` | 品牌资源与上传件，按需单独引入 |

## 有意分叉（与源不同，且**不推回**）

上面的「不要只改这里」有一个例外：下列改动只存在于镜像，源项目保持原样。**从源重拉时必须保留本地版本**，
否则会被静默抹掉。

| 文件 | 本地版本 | 源项目版本 | 为什么不推回 |
|---|---|---|---|
| `tokens/fonts.css` | `@import` 随包的 `@fontsource-variable/noto-sans-sc` 与 `inter`（钉 5.3.0，OFL-1.1） | `@import` Google Fonts CSS API | 打包后的 Electron 应用里，那条渲染阻塞的外网请求让首屏等了 1.1–56.6 s，离线机构可能一直白屏（RAY-493 `preview-rc2-fixes`）。而源项目是设计面板、没有 npm，推过去面板反而加载不到字体 |
| `tokens/typography.css` | 字体栈前置 `"Noto Sans SC Variable"` / `"Inter Variable"`，原名保留在后 | 只有原名 | 随包字体的 family 名带 ` Variable` 后缀；这一行是上一条的配套 |
| `package.json` 的 `dependencies` | 两个 `@fontsource-variable` 包 | 无（源项目不管理 npm 依赖） | 同上 |

守卫：`apps/terminal/renderer/test/noExternalAssets.test.js` —— 镜像或渲染端出现 http(s) 的样式 / 字体引用即失败，
所以重拉时若把 `fonts.css` 覆盖回源版本，构建会变红而不是静默退化。

决定出处：RAY-541 R1（2026-09-28 用户裁定）。

## 为什么 package.json 里没有 react 依赖

组件都 `import React from "react"`，按常理该声明 `peerDependencies: { react: ">=18" }`。
**本轮有意不声明**，原因是时机：

pnpm 的 `auto-install-peers` 默认开启，写上这条会立刻把 React 装进锁文件——实测解析成了
`19.2.8`。也就是说，一个**还没有任何人 import 的镜像包**，替整个工作区定死了 React 版本。
而前端应用尚未存在，React 版本应当由 RAY-219 建 `apps/terminal/` 时连同 Vite、构建链一起决定。

`>=18` 还是开放区间，今天解析到 19 只是"当下最新"，不是判断。

**RAY-219 落地时**：连同 app 的 React 选型一起把 `peerDependencies` 补回来，让锁文件里的
React 版本出自一次明确的决定，而不是一次自动解析的副作用。

## 文件说明

- `styles.css` —— **唯一入口**，`@import` 全部 token。消费方只 link 这一个文件
- `tokens/` —— 六个 token 文件
- `components/{forms,feedback,flow,data}/` —— 两个产品共用的原语
- `components/gait/` —— **双足踝 IMU 专属**，六个组件
- `index.js` —— 桶导出
- `DESIGN-GUIDE.md` —— 设计指南（源项目的 `readme.md`）
- `SKILL.md` —— Agent Skill 入口

每个组件的 `.prompt.md` 写了**这个组件为什么存在**，不只是怎么用。改组件前先读它。
