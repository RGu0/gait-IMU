**ChecklistItem** — one row of the device pre-check list: a 24px status icon, the item name, and a right-side hint. On failure the hint is an actionable fix, never a technical detail.

```jsx
<ChecklistItem status="pass"    label="压力垫连接" hint="已连接" />
<ChecklistItem status="running" label="传感器自检" hint="检测中…" />
<ChecklistItem status="fail"    label="设备连接" hint="请检查设备连接线" />
<ChecklistItem status="pending" label="校准" />
```

Statuses: `pending` (empty ring), `running` (blue spinner), `pass` (green check), `fail` (red cross + fix text). Never show serial ports, frame rates, or other tech metrics.
