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

依次验：`GAIT_SELFTEST=imports`（延迟加载的 bleak 平台后端、wt901、numpy 等确实被冻结进去）、
一条 `describe` 请求、一条不设 `GAIT_DEVICE_SOURCE` 的 `snapshot` 请求。手动单测：

```bash
GAIT_SELFTEST=imports dist/gait-sidecar/gait-sidecar
echo '{"kind":"request","v":"1.0","id":"1","method":"describe","params":{}}' | dist/gait-sidecar/gait-sidecar
```

## 打安装包

```bash
pnpm --filter @gait/terminal-renderer build
cd apps/terminal/main
CSC_IDENTITY_AUTO_DISCOVERY=false pnpm exec electron-builder --config electron-builder.yml --publish never
```

产物在 `apps/terminal/main/release/`（macOS：`.dmg` + `.zip`；Windows：NSIS `.exe`）。

## 未签名安装须知（Preview）

Preview 不签名、不公证。

- **macOS Gatekeeper**：首次打开被拦时，到「系统设置 → 隐私与安全性」底部点「仍要打开」；
  或在终端执行 `xattr -dr com.apple.quarantine "/Applications/GaitIMU Preview.app"`。
- **macOS 蓝牙授权**：ad-hoc 签名的身份随每次构建变化，系统可能把新构建当成新应用，
  **每装一个新构建都可能再弹一次蓝牙授权**。这是预期的，不是故障。
- **Windows SmartScreen**：「Windows 已保护你的电脑」→ 点「更多信息」→「仍要运行」。

## Preview 会话数据

Preview 期间产生的会话数据是**一次性的**：契约版本变化时不做迁移，升级后旧数据可能读不出
（用户决定，RAY-493）。不要用 Preview 采集需要长期保留的数据。
