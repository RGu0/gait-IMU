**MetricTile** — one metric and its quality annotation. v1 does not gate metrics: everything is computed, and every value carries a grade.

```jsx
<MetricTile title="步速" value="1.04" unit="m/s" sub="完整链 · 直行段中段步" />

<MetricTile title="双支撑期占比" value="27.9" unit="%" grade="low"
            note="跨足指标，同步误差约 ±18 ms，此项仅供参考。" />

<MetricTile title="疲劳衰减" grade="uncomputable"
            note="本次为 120 秒配置，此项仅在 180 秒配置下输出。" />
```

`low` only changes how the value is presented — it never withholds it. `uncomputable` renders 「本次不适用」 plus the reason.

**Never render a blank, a `0`, an `N/A` or a dash.** Each of those reads as a measurement rather than an absence, and an operator cannot tell them apart from a real value.

`note` is required for `low` and `uncomputable`: the grade alone gives the reader nothing to act on. Write it in the same plain register as the rest of the product — 「本次有效步数较少，此项仅供参考」, not 「ZUPT 置信度低」.

A metric that is inapplicable by protocol stays inapplicable in the full-chain report. Recomputation improves estimates; it does not change what the protocol produced.
