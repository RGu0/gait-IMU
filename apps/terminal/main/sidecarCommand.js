/**
 * sidecar 用什么命令拉起来 —— 开发态与打包态是两条路。
 *
 * 分出来单独一个文件，是因为两条路的事实来源不同：开发态跟着仓库与 uv 走；打包态跟着
 * PyInstaller 冻结产物走（`packaging/gait-sidecar.spec`，RAY-250 preview-installers），
 * 产物由 electron-builder 的 `extraResources` 放到 `process.resourcesPath/sidecar/` 下。
 */

import path from "node:path";

/**
 * 让 uv 忽略用户级配置时，`UV_CONFIG_FILE` 该指向的空设备。
 *
 * 用它而**不是** `UV_NO_CONFIG=1`（RAY-483）：后者会连 `.python-version` 一起停用，
 * 新 `.venv` 于是落到最新解释器上。两者对镜像的隔离效果相同，只有这一个保留 pin。
 *
 * Windows 写 `NUL` 而不用 `os.devNull`（那是 `\\.\nul`）：前者已在
 * techflex-cloud-foundation 的 windows-latest CI 上跑通，后者没有人验证过 uv 接受。
 * 导出它，是为了让测试与产品共用**同一个值**，而不是各抄一份。
 */
export const UV_CONFIG_FILE_NULL = process.platform === "win32" ? "NUL" : "/dev/null";

/** 开发态：经 uv 跑仓库里的模块。与契约测试起 sidecar 的方式完全一致。 */
export function developmentCommand(repoRoot) {
  return {
    command: "uv",
    args: ["run", "--locked", "python", "-m", "gait.app"],
    cwd: repoRoot,
    // UV_CONFIG_FILE：本机 uv 镜像配置会报一个假的 lockfile 陈旧错误（见上面常量的注释）。
    // PYTHONUTF8：sidecar 的文案是中文，Windows 默认代码页会把它变成乱码。
    env: { UV_CONFIG_FILE: UV_CONFIG_FILE_NULL, PYTHONUTF8: "1" },
  };
}

/**
 * 打包态：冻结产物 `<resourcesPath>/sidecar/gait-sidecar[.exe]`。
 *
 * 路径不是猜的：`sidecar` 目录名来自 `electron-builder.yml` 的 `extraResources.to`，
 * 可执行文件名来自 `packaging/gait-sidecar.spec` 的 `name`。改其中任何一处都要同时改这里
 * —— 测试钉住了这个形状，漂移会在 CI 里红，而不是在机构安装那天才暴露。
 *
 * - cwd 设为产物目录：冻结的 sidecar 不依赖 cwd，但一个确定存在的目录比继承
 *   Electron 的启动目录（macOS 上是 `/`）更不容易出意外。
 * - 不带 `UV_CONFIG_FILE`：打包态没有 uv。
 * - PYTHONUTF8：与开发态同理，Windows 默认代码页会把中文文案变成乱码。
 */
export function packagedCommand({ resourcesPath, platform = process.platform } = {}) {
  if (!resourcesPath) {
    throw new Error("打包态需要 resourcesPath（Electron 的 process.resourcesPath）才能定位 sidecar。");
  }
  // 按目标平台选路径语义，而不是按当前宿主：这样 Windows 的形状在 macOS 上的测试里也钉得住。
  const paths = platform === "win32" ? path.win32 : path.posix;
  const sidecarDir = paths.join(resourcesPath, "sidecar");
  return {
    command: paths.join(sidecarDir, platform === "win32" ? "gait-sidecar.exe" : "gait-sidecar"),
    args: [],
    cwd: sidecarDir,
    env: { PYTHONUTF8: "1" },
  };
}

export function resolveSidecarCommand({ packaged, repoRoot, resourcesPath, platform }) {
  return packaged ? packagedCommand({ resourcesPath, platform }) : developmentCommand(repoRoot);
}
