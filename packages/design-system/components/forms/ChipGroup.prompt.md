**ChipGroup** — multi-select tag chips for categorical intake (medical history, tags). Selected chips get a brand-subtle fill, brand border, and a check.

```jsx
<ChipGroup
  options={["高血压", "糖尿病", "既往下肢损伤", "关节炎"]}
  value={history}
  onChange={setHistory}
/>
```

Controlled via `value` (array) + `onChange`. Options may be strings or `{value,label}`. Chips are ≥44px touch targets, pill-shaped.
