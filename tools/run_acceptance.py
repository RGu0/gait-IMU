"""把 `tools/acceptance/` 下的验收脚本**一次全跑完**，汇总一张表。

## 为什么需要它

RAY-328 与 RAY-339 各自交付时，**每个 scope 只跑自己承接的那几条判据**。判据里"分层
红线"与"回归测试"那两条每次 lint/test 都验，量化判据没有 —— 于是 RAY-328 的三个验收
脚本在 main 上悄悄挂掉，**过了两个 Issue 才被发现**（RAY-343）。其中一条还是真问题：
互相关的周期先验已经从有益变成净负。

一个 scope 改的是共享路径（周期估计、时基、检测器），受影响的判据却分散在几个 Issue
里 —— 没有一条命令能问"现在还全过吗"，那个问题就不会有人问。

## 它守不住什么

它**不进** `./dev` 的本机门控，也不进 CI：

* 它要读 RAY-230 的现场采集（云端共享库），而 CI 机器上没有那份数据，把它塞进门控
  等于把一个**离线可跑**的门变成要联云的；
* 七个脚本各要跑完 24 格的完整管线，单个 1~2 分钟。

所以它靠的是**约定**："动了共享路径就跑一遍"。约定会被忘，这一点没有假装解决 ——
能做的是把约定写进 `AGENTS.md` 的交付流程，让它至少出现在每次交付的必经之路上。

## 用法

    python tools/run_acceptance.py <采集目录>...
    python tools/run_acceptance.py <采集目录>... --out <结果目录>
    python tools/run_acceptance.py <采集目录>... --only cross_foot_qc antiphase

`<采集目录>` 是 RAY-230 的趟次目录，例如
`<library>/evidence/ray-230/field-v1/acceptance/tests/T-230-03-鞋型×速度矩阵/raw/S1-sport`。

退出码 0 = 全部达标；1 = 有脚本不达标或崩溃。**崩溃与不达标分开报** —— RAY-328 那个
脚本在上游删掉一个字段之后是 `KeyError` 直接崩的，而"崩了"与"判据没过"是两件事，
混在一起报会让人以为算法出了问题。
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import pkgutil
import sys
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import acceptance

from gait.config import AlgoConfig


@dataclass(frozen=True)
class Outcome:
    """一个脚本跑完之后的结论。"""

    name: str
    failures: list[str]
    rows: int
    #: 崩溃的那一行 traceback 摘要。`None` 表示没崩 —— 与"没有失败"不是一回事。
    crashed: str | None = None

    @property
    def passed(self) -> bool:
        return self.crashed is None and not self.failures


def scripts() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.iter_modules(acceptance.__path__)
        if not info.name.startswith("_")
    )


def run(name: str, trials: list[Path], cfg: AlgoConfig, out: Path | None) -> Outcome:
    """跑一个脚本。**吞掉它的标准输出** —— 汇总表要的是结论，不是七份逐格清单。

    崩溃单独接住：脚本可能因为上游改了字段而 `KeyError`，那时它一行判据都没跑到，
    报"0 条不达标"会是彻头彻尾的谎话。
    """
    try:
        module = importlib.import_module(f"acceptance.{name}")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rows = [row for trial in trials for row in module.analyse(trial, cfg)]
            failures = module.judge(rows)
    except Exception:  # noqa: BLE001 —— 崩因千奇百怪，这里只负责如实转述
        return Outcome(name=name, failures=[], rows=0, crashed=traceback.format_exc())

    if out is not None:
        (out / f"{name}.json").write_text(
            json.dumps(
                {"config_version": cfg.version, "rows": rows, "failures": failures},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    return Outcome(name=name, failures=failures, rows=len(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trials", nargs="+", type=Path, help="RAY-230 的趟次目录")
    parser.add_argument("--out", type=Path, help="逐脚本 JSON 的落盘目录")
    parser.add_argument("--only", nargs="+", help="只跑这几个脚本")
    args = parser.parse_args()

    for trial in args.trials:
        if not (trial / "arrivals.npz").exists():
            print(f"找不到 {trial / 'arrivals.npz'} —— 采集目录给错了？", file=sys.stderr)
            return 2

    names = scripts()
    if args.only:
        unknown = sorted(set(args.only) - set(names))
        if unknown:
            print(f"没有这些脚本：{unknown}；可选：{names}", file=sys.stderr)
            return 2
        names = [name for name in names if name in args.only]
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    cfg = AlgoConfig()
    print(f"配置版本 {cfg.version}；{len(names)} 个脚本；{len(args.trials)} 个采集目录\n")
    outcomes = [run(name, args.trials, cfg, args.out) for name in names]

    width = max(len(o.name) for o in outcomes)
    for outcome in outcomes:
        if outcome.crashed:
            verdict = "崩溃"
        elif outcome.failures:
            verdict = f"{len(outcome.failures)} 条不达标"
        else:
            verdict = "达标"
        print(f"{outcome.name:{width}s}  {outcome.rows:>3d} 行  {verdict}")

    broken = [o for o in outcomes if not o.passed]
    for outcome in broken:
        print(f"\n───── {outcome.name} ─────")
        if outcome.crashed:
            print(outcome.crashed.rstrip())
        for line in outcome.failures:
            print(f"  不达标：{line}")

    print()
    if not broken:
        print(f"全部达标（{len(outcomes)}/{len(outcomes)}）")
        return 0
    print(f"{len(broken)}/{len(outcomes)} 个脚本没过")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
