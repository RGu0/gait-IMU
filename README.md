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

Project documents, PRD, and delivery evidence live in the shared cloud library
and are reached through `.project-context/`; see `.ai-project/` for the
governance manifest.
