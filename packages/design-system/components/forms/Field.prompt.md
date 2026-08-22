**Field** — Steady's text input. The label is always visible above the input; never use a placeholder as the label. 48px tall, validate on blur/submit, error text says how to fix.

```jsx
<Field label="机构档案号" value={v} onChange={e => setV(e.target.value)} placeholder="例如 2024-0731" />
<Field label="身高" unit="cm" value={h} onChange={onH} />
<Field label="备注" optional value={n} onChange={onN} />
<Field label="机构档案号" value={v} onChange={onV} error="未找到该档案号,请确认后重新输入。" />
```

Props: `unit` places a muted unit (cm/kg) inside on the right; `optional` appends 「(选填)」; `error` renders red text + red border (`role="alert"`); `hint` renders neutral helper text. Distinct concepts 「未选择」/「明确无」/「不提供」stay separate options — don't merge them.
