**DataTable** — the screening-records table. 56px rows, a 14/500 secondary header, 16px body, zebra sunken rows, brand-alpha hover. The status column uses `StatusPill`; IDs are shown masked (`**2781`, `临时034`); the action column is a ghost text button.

```jsx
<DataTable
  columns={[
    { key: "id", header: "编号" },
    { key: "time", header: "时间", numeric: true },
    { key: "status", header: "状态", render: DataTable.status },
  ]}
  rows={[
    { id: "**2781", time: "14:32", status: { tone: "success", label: "已完成" } },
    { id: "临时034", time: "14:10", status: { tone: "info", icon: "spinner", spin: true, label: "生成中" } },
  ]}
  onRowAction={(row) => open(row)}
  actionLabel="查看"
/>
```

`DataTable.status` renders a `{tone,label,icon,spin}` cell value as a `StatusPill`. Set `numeric` on time/metric columns for tabular-nums alignment.
