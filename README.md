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

Project documents, PRD, and delivery evidence live in the shared cloud library
and are reached through `.project-context/`; see `.ai-project/` for the
governance manifest.
