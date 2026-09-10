"""把 `./dev lint` 的 Python 侧**在一个进程里**跑完：ruff + 四条项目红线。

## 为什么需要它

`./dev lint` 此前是五条独立的 `uv run --locked`。`--locked` 让 uv 在**每一次**调用
上重新解析依赖，而依赖里的 techflex-cloud-foundation（RAY-443 按 release + wheel +
SHA-256 钉住）是 GitHub release 资产直链，其 HTTP 缓存项每次都判过期 —— 于是每次
调用都要向 github.com 走一轮 revalidate（302 → release-assets → 304）。

实测（RAY-467）：五次合计 **18.87 s**，而这五项的真实工作量合计 **0.12 s**。
`./dev lint` 总时长 20.45 s 里 **92% 在等网络**。冷热缓存一致，`--offline` 与
`--no-sync` 都是 0.0x s —— 被等的不是建 venv、装包或起解释器。

合并成一次调用，那一轮 revalidate 就只付一次。**`--locked` 一点没放弃** —— 锁文件
新鲜度仍然每次 `./dev lint` 都验，只是验一遍而不是五遍。RAY-462 的判据 ③ 依赖的
正是这条，它继续成立。

## 与 `run_acceptance.py` 的关系

同一个口径，两处都遵守：**「崩溃」与「不达标」分开报**。一个检查器抛异常与它判出
违规是两件事 —— 混着报会让人以为代码有问题，而其实是检查器自己坏了。

## 与合并前的一处**行为差异**（有意为之）

合并前 `dev` 靠 `set -euo pipefail`，**停在第一个失败项**；后面几项当次不会跑。
现在五项**全部跑完，一次报齐** —— 一轮就看得到全部问题，不必修一个跑一次。

shell 层的顺序没变：本脚本非零退出时 `pnpm -r lint` 仍然不会跑。

## 用法

    python tools/run_lint_checks.py [ruff 的额外参数...]

额外参数**只透传给 ruff**（`./dev lint --fix` 就是这么到达 ruff 的），四条红线
不接受参数。退出码 0 = 全过；1 = 有检查不通过或崩溃。
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Outcome:
    """一条检查跑完之后的结论。"""

    name: str
    #: 检查器自己的退出码。0 = 通过。
    code: int = 0
    #: 崩溃的 traceback。`None` 表示没崩 —— 与「通过」不是一回事。
    crashed: str | None = field(default=None)

    @property
    def passed(self) -> bool:
        return self.crashed is None and self.code == 0


def _run_ruff(argv: list[str]) -> int:
    """ruff 是独立可执行文件，只能起子进程。

    但它起在**本进程内部**，不再经 `uv run` —— 所以不会再有一次依赖解析，
    也就没有那一轮 revalidate。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", *argv],
        check=False,
    )
    return completed.returncode


def _checkers(ruff_argv: list[str]) -> list[tuple[str, Callable[[], int]]]:
    """按合并前 `dev` 里的原顺序排列，逐条对应原来的一行。

    四条红线在这里**导入**而不是起子进程：它们各有 `main() -> int`，本来就是
    可调用的。`check_upstream_refs.main` 带 argparse，**必须显式传 `[]`** ——
    否则它会去解析 `sys.argv`，把本该只给 ruff 的参数吃掉。
    """
    import check_calibration_channel
    import check_layering
    import check_quality_single_source
    import check_upstream_refs

    return [
        ("ruff", lambda: _run_ruff(ruff_argv)),
        # 分层红线。ruff 管不了这条：它是项目自己的依赖方向约束，不是通用 lint 规则。
        ("check_layering", check_layering.main),
        ("check_quality_single_source", check_quality_single_source.main),
        ("check_calibration_channel", check_calibration_channel.main),
        # 上游引用红线。守的是对**别的仓库**的引用：那些仓库不在 CI 上，也不在本仓库
        # 任何测试的视野里，它们改了这边的文档会开始说错话而不会有任何东西变红。
        ("check_upstream_refs", lambda: check_upstream_refs.main([])),
    ]


def run(name: str, fn: Callable[[], int]) -> Outcome:
    """跑一条检查。**不吞它的输出** —— 与 `run_acceptance.py` 相反。

    那边要的是一张汇总表，逐格清单是噪音；这边的输出就是开发者要读的东西，
    吞掉等于把 lint 变成一个只说「红了」的黑盒。
    """
    print(f"\n=== {name} ===", flush=True)
    try:
        return Outcome(name=name, code=fn())
    except Exception:  # noqa: BLE001 —— 崩因千奇百怪，这里只负责如实转述
        traceback.print_exc()
        return Outcome(name=name, crashed=traceback.format_exc())


def main(argv: list[str] | None = None) -> int:
    ruff_argv = list(sys.argv[1:] if argv is None else argv)

    outcomes = [run(name, fn) for name, fn in _checkers(ruff_argv)]

    # 各检查器写的是 stdout，本函数的摘要写 stderr。两个流分别缓冲，不先冲掉
    # stdout，最后一项的输出会排到摘要后面 —— 读起来像它也在失败清单里。
    sys.stdout.flush()

    failed = [o for o in outcomes if not o.passed]
    if not failed:
        print(f"\n全部 {len(outcomes)} 项 Python 侧检查通过。")
        return 0

    print("\n=== 未通过 ===", file=sys.stderr)
    for outcome in failed:
        if outcome.crashed is not None:
            # 崩了：它一条判据都没跑到，说「不通过」会掩盖「检查器自己坏了」。
            print(f"  {outcome.name}：**崩溃**（见上面的 traceback）", file=sys.stderr)
        else:
            print(f"  {outcome.name}：不通过（退出码 {outcome.code}）", file=sys.stderr)
    print(
        f"\n{len(failed)}/{len(outcomes)} 项未通过。"
        "五项是一次跑完的，上面列出的是**全部**问题，不是第一个。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
