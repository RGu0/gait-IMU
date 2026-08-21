**Toast** — bottom-right, transient feedback for a completed success/info action, filling the gap between banners (persistent) and dialogs (blocking).

```jsx
<Toast tone="success" onClose={hide}>PDF 已导出</Toast>
<Toast tone="info" action={<Button variant="ghost" size="sm">查看</Button>}>档案已保存</Toast>
```

Rules: `success`/`info` only — **errors never use Toast** (errors stay readable: inline, banner, or dialog). 4s auto-dismiss, one on screen at a time, `aria-live="polite"`, never steals focus. During an active scan, queue and show after it ends.
