# gait-IMU

IMU-based gait analysis. Sensor data acquisition and gait metrics from wearable
IMU modules (WT9011DCL-BT50 / BS-BT91).

## Development

All project commands go through the `./dev` entrypoint:

```bash
./dev setup   # install locked Python and Node dependencies
./dev test    # run the test suite
./dev lint    # static checks
./dev build   # build distributable artifacts
```

On Windows use `pwsh -File dev.ps1 <setup|test|lint|build>`.

The entrypoint resolves Node itself from `.node-version` when `fnm` is
available, mirroring what `uv run --locked` does for `.python-version`, so a
differently-versioned Node earlier on `PATH` does not change what runs. Without
`fnm` it falls back to `PATH`, where the governance preflight checks the version
instead. `./dev node <cmd>` runs a command in whichever Node environment the
entrypoint resolved — use it to see which one that is.

Python environments use uv's `centralized-project-envs` preview feature. Each
worktree/interpreter has its own mutable environment in the local uv cache;
dependencies may reuse cached wheels and filesystem links/clones, but this is
not one shared environment and does not eliminate every worktree's disk cost.
The entrypoints discard `UV_PROJECT_ENVIRONMENT` and preserve other preview
features. Python stays pinned by `.python-version`; dependencies stay locked.

After updating an existing checkout, run the governed `setup` action to migrate
its environment; do not manually delete `.venv`. `.venv` is normally a directory
link, but uv can use a plain path file or cache-only fallback on platforms that
cannot create links. After setup, use `./dev python-path` (Windows:
`pwsh -File dev.ps1 python-path`) to select the interpreter in your IDE/debugger.
`python-info` is a read-only version probe used by governance preflight; neither
probe installs dependencies or repairs an environment.

Do not run cache cleanup during debugging or active worktree use: `uv cache
prune` removes **all centralized project environments**, which setup must then
recreate. Above 20 GB cache usage or below 15% free disk, review active sessions
first, schedule a maintenance window, then use non-force uv cleanup and run
governed setup again. These thresholds are review triggers, not automatic deletion
rules. Do not hand-delete cache entries or use `--force`. Node/pnpm behavior is
unchanged.

If the preview feature is unsupported, stop and report it rather than silently
sharing a mutable environment. A reviewed entrypoint change can fall back to an
independent local `.venv` per worktree with the same locked dependencies and cache.

Project documents, PRD, and delivery evidence live in the shared cloud library
and are reached through `.project-context/`; see `.ai-project/` for the
governance manifest.
