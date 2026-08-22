**Banner** — full-width, non-blocking notice pinned to the top of the page. Use for persistent degraded states; never use a modal to interrupt an in-progress scan.

```jsx
<Banner tone="warning" title="网络中断" onClose={dismiss}>
  当前检测不受影响,系统会自动重连。
</Banner>
```

Tones: `warning` / `info` / `success` / `danger`. Calm, executable copy. Blocking states use `Dialog` instead.
