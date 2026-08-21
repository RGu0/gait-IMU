**StatusPill** — pill-shaped status marker; a dot/icon plus **text**, always both. Status is icon + text + color simultaneously — never color alone.

```jsx
<StatusPill tone="success" icon="dot">设备已就绪</StatusPill>
<StatusPill tone="info" icon="spinner" spin>生成中</StatusPill>
<StatusPill tone="warning" icon="warning">网络待恢复</StatusPill>
<StatusPill tone="danger" icon="x">未完成</StatusPill>
```

Tones: `success` / `warning` / `danger` / `info` / `neutral`. Never drop the text label. Use `icon="spinner" spin` for processing/info states.
