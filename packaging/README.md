# Preview 安装包（RAY-250 preview-installers）

两段：先用 PyInstaller 把 Python sidecar 冻结成 `dist/gait-sidecar/`，再由 electron-builder
把它作为 `extraResources` 放进安装包的 `<resources>/sidecar/`。主进程经
`apps/terminal/main/sidecarCommand.js::packagedCommand()` 拉起它。

CI：`.github/workflows/preview-release.yml`（macOS arm64 + Windows x64，只出构件，不发布）。

## 本机构建 sidecar

在仓库根（worktree 根）执行，先 `./dev setup`：

```bash
UV_CONFIG_FILE=/dev/null uv run --locked --with pyinstaller==6.22.3 \
  pyinstaller packaging/gait-sidecar.spec --distpath dist --workpath build/pyinstaller --noconfirm
```

- PyInstaller 用 `--with` 临时带上，**不进 `pyproject.toml` / `uv.lock`**；升版改这里与 workflow 两处。
- `UV_CONFIG_FILE=/dev/null` 只为本机 uv 镜像配置的假 lockfile 陈旧报错；CI 上不需要。
- Windows 上把可执行文件换成 `dist\gait-sidecar\gait-sidecar.exe`，命令其余一致。

## 冒烟

```bash
uv run --locked python packaging/smoke_sidecar.py dist/gait-sidecar/gait-sidecar
```

依次验：

1. `GAIT_SELFTEST=imports`：延迟加载的模块（bleak 平台后端、`gait.app.replay`、`gait.app.blesource`、
   wt901、numpy 等）确实被冻结进去；再跑 `GAIT_SELFTEST=clock`：冻结产物**自带的**解释器 ≥ 3.13，
   且 `time.monotonic()` 分辨率 × 10 ≤ 5 ms（与 `linktest` 同一判据）。真实传感器的到达时刻全靠这个时钟，
   Windows + Python 3.12 只有 15.6 ms，会把 200 Hz 的链路 / 丢包量化失真（RAY-545）；
2. 一条 `describe` 请求；
3. 不设 `GAIT_DEVICE_SOURCE` 的 `snapshot`（stub 设备源）；
4. 按安装包实际给 sidecar 的环境（`GAIT_DEVICE_SOURCE=synthetic GAIT_PREVIEW=1 GAIT_PROTOCOL_SECONDS=60`
   + 临时 `GAIT_SESSION_ROOT`）：`snapshot` 报 `source: synthetic`，`runPreflight` 的出厂标定项为 `waived`。

手动单测：

```bash
GAIT_SELFTEST=imports dist/gait-sidecar/gait-sidecar
GAIT_SELFTEST=clock dist/gait-sidecar/gait-sidecar
echo '{"kind":"request","v":"1.0","id":"1","method":"describe","params":{}}' | dist/gait-sidecar/gait-sidecar
```

## 打安装包

```bash
pnpm --filter @gait/terminal-renderer build
CSC_IDENTITY_AUTO_DISCOVERY=false pnpm --filter @gait/terminal-main exec \
  electron-builder --config electron-builder.yml --publish never
```

- 产物在 `apps/terminal/main/release/`：macOS `GaitIMU Preview-<版本>-arm64.dmg` / `-arm64-mac.zip`；
  Windows `GaitIMU Preview Setup <版本>.exe`（NSIS）。
- 本机下载 Electron 慢时可临时加 `ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/`、
  `ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/`。
  **只在本机 shell 里设，不写进仓库**；CI 不需要。
- 不装就验一次打包后的应用（macOS）：

  ```bash
  GAIT_SMOKE_QUIT_AFTER_MS=15000 \
    "apps/terminal/main/release/mac-arm64/GaitIMU Preview.app/Contents/MacOS/GaitIMU Preview"
  ```

  期望看到 `sidecar state: ready`、`renderer did-finish-load` 与 `renderer text: "预览版…"`，
  15 秒后自己退出，`gait-sidecar` 进程随之结束。

## 装机后端到端验收

`acceptance/preview_acceptance.mjs` 拉起**已安装**的应用（`--remote-debugging-port` + 一次性
`--user-data-dir`），经 CDP 在演示模式走一遍：冷启动零外网请求 → 走满 60 秒出报告（出厂标定
「已豁免」、两条注记）→ 中途停止 → 检测记录（完成 / 已停止、重开报告）→ 采集中强杀 sidecar
（「检测已中断」、恢复、记录「未正常结束」）→ 落盘 meta → 关窗退出无残留。视口钉在 1280×720，
四个页面判横向溢出。Windows 上另经 `acceptance/win_snapshot.ps1` 枚举可见顶层窗口，判**无控制台
窗口**（先弹一个真控制台做正对照，看不见就判失败，不放过）。

CI：`preview-release.yml` 的 `acceptance-windows` job 静默安装 NSIS 包后跑它（RAY-493 A5-win），
结果与截图在构件 `acceptance-Windows`。本机（macOS）：

```bash
A5_APP="/Applications/GaitIMU Preview.app/Contents/MacOS/GaitIMU Preview" A5_OUT=/tmp/a5 \
  node packaging/acceptance/preview_acceptance.mjs
```

需要 Node ≥ 22（全局 `WebSocket`）。不碰本机的预览设置与会话：userData 是临时目录。

## 给测试人员的安装说明（Preview）

Preview **不签名、不公证**，首次打开会被系统拦一次，这是预期的。

### macOS（Apple Silicon）

1. 打开 `.dmg`，把「GaitIMU Preview」拖进「应用程序」。
2. 首次打开被拦（「无法验证开发者」/「已损坏」）时，任选其一：
   - 「系统设置 → 隐私与安全性」，拉到底部，点「仍要打开」，再确认一次；
   - 或在终端执行：`xattr -dr com.apple.quarantine "/Applications/GaitIMU Preview.app"`
3. 切到「真实传感器」时系统会请求蓝牙权限，点「允许」。Preview 用 ad-hoc 签名，签名身份随每个
   构建变化，**每装一个新构建都可能再弹一次蓝牙授权**；若之前点过「不允许」，到「系统设置 →
   隐私与安全性 → 蓝牙」里打开 GaitIMU Preview。

### Windows（x64）

1. 运行 `GaitIMU Preview Setup <版本>.exe`。
2. SmartScreen 提示「Windows 已保护你的电脑」时，点「更多信息」→「仍要运行」。
3. 安装程序允许选择安装目录，默认装在当前用户下，不需要管理员权限。
4. Windows 上「真实传感器」模式是**实验功能**：Preview 只在 Windows 上验收了演示模式（RAY-493
   砍项 4），真机蓝牙未经验证。

### 使用

- **预览菜单**：「预览 → 演示模式（合成步行）」是默认，不需要传感器即可走完整流程，界面顶部标注
  「演示数据，非实测」；「预览 → 真实传感器（实验）」连接蓝牙 IMU 模块。切换会重启采集服务并重载窗口，
  选择会被记住。
- **数据目录**：「预览 → 打开数据目录」。位置：
  - macOS：`~/Library/Application Support/GaitIMU Preview/sessions/`
  - Windows：`%APPDATA%\GaitIMU Preview\sessions\`
  - 同级的 `preview-settings.json` 记着设备来源选择。

## Preview 会话数据

Preview 期间产生的会话数据是**一次性的**：契约版本变化时不做迁移，升级后旧数据可能读不出
（用户决定，RAY-493）。不要用 Preview 采集需要长期保留的数据。

### 升级说明：数据契约 1.1 → 1.2（RAY-287）

会话元数据新增「左右确认记录」与「内外翻签名」两项，数据契约版本由 **1.1 升到 1.2**。

- **升级后，旧版本采集的会话不再出现在「检测记录」里，也无法重开报告。** 新版本按版本号拒绝
  解读旧会话，不做迁移（RAY-373 判定，《05 数据格式规范》§7.1）。
- 文件**不会被删除**，仍在上面的数据目录里。需要留底的，请在升级**前**用旧版本打开报告自行
  导出；不需要的，可在升级后清空该目录。
