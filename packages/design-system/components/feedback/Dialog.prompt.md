**Dialog** — the only blocking surface, reserved for three must-decide moments: danger confirm (stop scan), profile-conflict confirm, network-gate explanation. Everything else is a Banner or Toast.

```jsx
<Dialog
  open={open}
  danger
  title="停止本次检测?"
  confirmLabel="停止检测"
  cancelLabel="取消"
  onConfirm={stop}
  onCancel={() => setOpen(false)}
>
  已采集的数据将不生成报告,可立即重新开始。
</Dialog>
```

Max 480px wide, ≤2 buttons, default focus on the safe (cancel) action, Esc = cancel, focus trapped, `role="alertdialog"`. `danger` renders the confirm as filled red — the one place filled red is permitted. Never stack dialogs; never show one during a scan except the stop-confirm.
