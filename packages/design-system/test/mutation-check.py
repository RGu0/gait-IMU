"""Mutation check: damage one component at a time, prove the suite goes red.

`render.test.jsx` passing proves nothing on its own — it has to FAIL when the
thing it guards is broken. Each mutation below imitates a realistic defect from
re-syncing the mirror (a bar dropped, a glyph lost, a token swapped), not an
artificial syntax error.

This is NOT run by CI. Run it by hand when adding or changing an assertion:

    python3 packages/design-system/test/mutation-check.py

It earned its place the first time it ran: the focus-ring invariant was written
as /outline:\\s*none/, which never matches a JSX inline style because that value
is always quoted. The assertion had been passing while guarding nothing, and only
a deliberately broken component exposed it.

When a mutation reports SKIP, its anchor no longer exists — the component changed
and the mutation needs updating, which is itself worth knowing.

Every mutation is restored in a finally block, and the tree is checked clean at
the end.
"""

import pathlib
import subprocess
import sys

DS = pathlib.Path(__file__).resolve().parent.parent
WT = DS.parent.parent

MUTATIONS = [
    (
        "BatteryPair 少画一格电量（满电显示 2 格）",
        "components/gait/BatteryPair.jsx",
        'const n = tier === "full" ? 3 : tier === "mid" ? 2 : 1;',
        'const n = tier === "full" ? 2 : tier === "mid" ? 2 : 1;',
    ),
    (
        "SideBadge 丢掉「左」字，两侧都显示「右」",
        "components/gait/SideBadge.jsx",
        'const ch = isLeft ? "左" : "右";',
        'const ch = "右";',
    ),
    (
        "LinkStatus 三档都画 3 格（只剩颜色区分）",
        "components/gait/LinkStatus.jsx",
        'fair: { color: "var(--warning-fg)", bars: 2, label: "链路波动" },',
        'fair: { color: "var(--warning-fg)", bars: 3, label: "链路波动" },',
    ),
    (
        "MetricTile 不可计算态改为留空",
        "components/gait/MetricTile.jsx",
        ">本次不适用</div>",
        "></div>",
    ),
    (
        "SideBadge 左右用同一个 token（只剩形状与文字）",
        "components/gait/SideBadge.jsx",
        'background: isLeft ? "var(--side-left)" : "var(--side-right)",',
        'background: "var(--side-left)",',
    ),
    (
        "Button 抑制焦点环",
        "components/forms/Button.jsx",
        'cursor: isDisabled ? "not-allowed" : "pointer",',
        'cursor: isDisabled ? "not-allowed" : "pointer", outline: "none",',
    ),
    (
        "RhythmStrip 改用压力热力图色标",
        "components/gait/RhythmStrip.jsx",
        'stroke="var(--viz-gait-right)"',
        'stroke="var(--viz-heat-3)"',
    ),
    (
        "tokens/effects.css 把焦点环整个关掉",
        "tokens/effects.css",
        "--focus-ring: 2px solid #2569BC; /* @kind other */",
        "--focus-ring: none; /* @kind other */",
    ),
    (
        "index.js 漏掉 MetricTile 的导出",
        None,  # index.js, handled below
        'export { MetricTile } from "./components/gait/MetricTile.jsx";',
        "",
    ),
]


def run_suite():
    # check=False on purpose: a non-zero exit IS the result we are looking for.
    p = subprocess.run(
        ["pnpm", "exec", "vitest", "run", "--reporter", "dot"],
        cwd=DS, capture_output=True, text=True, check=False,
    )
    return p.returncode, (p.stdout + p.stderr)


def main():
    results = []
    for label, rel, old, new in MUTATIONS:
        target = DS / (rel if rel else "index.js")
        original = target.read_text(encoding="utf-8")
        if old not in original:
            results.append((label, "SKIP", "锚点未找到 —— 组件已变，变异需要更新"))
            continue
        try:
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            code, out = run_suite()
            if code == 0:
                results.append((label, "MISS", "损坏后测试仍然全绿 —— 这个缺陷没人守"))
            else:
                failed = [ln.strip() for ln in out.splitlines() if "×" in ln or "FAIL" in ln]
                results.append((label, "CAUGHT", failed[0][:90] if failed else "suite 退出码非 0"))
        finally:
            target.write_text(original, encoding="utf-8")

    print()
    caught = sum(1 for _, s, _ in results if s == "CAUGHT")
    for label, status, detail in results:
        mark = {"CAUGHT": "✓ 被抓住", "MISS": "✗ 漏掉", "SKIP": "? 跳过"}[status]
        print(f"  {mark}  {label}")
        if status != "CAUGHT":
            print(f"           {detail}")
    print(f"\n  {caught}/{len(results)} 个变异被测试抓住")

    # the whole point of the finally blocks
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "packages/design-system/components",
         "packages/design-system/tokens", "packages/design-system/index.js"],
        cwd=WT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    print(f"  组件源码已复原：{'是' if not dirty else '否 —— ' + dirty}")
    return 0 if caught == len(results) and not dirty else 1


if __name__ == "__main__":
    sys.exit(main())
