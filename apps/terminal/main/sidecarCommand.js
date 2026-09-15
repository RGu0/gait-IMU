/**
 * sidecar 用什么命令拉起来 —— 开发态与打包态是两条路。
 *
 * 分出来单独一个文件，是因为**打包态那条路属于 `packaging` scope**（PyInstaller 冻结
 * 的可执行文件放在 `process.resourcesPath` 下）。本 scope 只实现开发态，并把打包态
 * 留成一个显式的、看得见的分支 —— 而不是先写一个猜出来的路径，等打包时才发现猜错。
 */

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
 * 打包态：**本 scope 未实现**。
 *
 * 冻结产物的位置、命名与签名都由打包方案决定，而那还没做。这里抛错而不是返回一个
 * 猜测的路径 —— 一个猜错的路径会在安装到机构那天才第一次暴露，那时没人在场。
 */
export function packagedCommand() {
  throw new Error(
    "打包态的 sidecar 命令尚未实现（RAY-250 的 packaging scope）。" +
      "它要等 PyInstaller 冻结方案定下来 —— 在那之前这里不猜路径。",
  );
}

export function resolveSidecarCommand({ packaged, repoRoot }) {
  return packaged ? packagedCommand() : developmentCommand(repoRoot);
}
