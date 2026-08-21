**StepBar** — the thin stepper atop the wizard flow. The current step is brand-colored, completed steps show a gray-green check, upcoming steps are gray.

```jsx
<StepBar steps={["知情同意", "受试者档案", "健康问询", "设备预检", "开始检测"]} current={2} />
```

Do not show global navigation alongside the wizard — the step bar is the only wayfinding during a scan flow.
