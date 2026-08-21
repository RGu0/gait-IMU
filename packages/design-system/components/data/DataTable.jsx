import React from "react";
import { StatusPill } from "../feedback/StatusPill.jsx";

/**
 * DataTable — screening records. 56px rows, 14/500 secondary header,
 * 16px body, zebra sunken rows, brand-alpha hover. Status column uses
 * StatusPill; IDs shown masked. Action column is a ghost text button.
 */
export function DataTable({ columns, rows, onRowAction, actionLabel = "查看", style, ...rest }) {
  const [hover, setHover] = React.useState(-1);
  return (
    <div style={{ border: "1px solid var(--border-default)", borderRadius: "var(--radius-card)", overflow: "hidden", background: "var(--bg-surface)", ...style }} {...rest}>
      <table style={{ width: "100%", borderCollapse: "collapse", font: "var(--text-body)" }}>
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} style={{ textAlign: c.align || "left", font: "var(--text-secondary-size)", fontWeight: 500, color: "var(--text-secondary)", padding: "var(--space-3) var(--space-4)", background: "var(--bg-surface)", borderBottom: "1px solid var(--border-default)" }}>
                {c.header}
              </th>
            ))}
            {onRowAction && <th style={{ borderBottom: "1px solid var(--border-default)", background: "var(--bg-surface)" }} />}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={row.id ?? ri}
              onMouseEnter={() => setHover(ri)} onMouseLeave={() => setHover(-1)}
              style={{ background: hover === ri ? "var(--brand-alpha-1)" : ri % 2 ? "var(--bg-sunken)" : "var(--bg-surface)", transition: "background var(--motion-fast)" }}>
              {columns.map((c) => (
                <td key={c.key} style={{ height: "var(--size-table-row)", padding: "0 var(--space-4)", textAlign: c.align || "left", color: "var(--text-primary)", borderBottom: ri < rows.length - 1 ? "1px solid var(--border-default)" : "none", fontVariantNumeric: c.numeric ? "tabular-nums" : undefined, fontFamily: c.numeric ? "var(--font-num)" : undefined }}>
                  {c.render ? c.render(row[c.key], row) : row[c.key]}
                </td>
              ))}
              {onRowAction && (
                <td style={{ padding: "0 var(--space-4)", textAlign: "right", borderBottom: ri < rows.length - 1 ? "1px solid var(--border-default)" : "none" }}>
                  <button type="button" onClick={() => onRowAction(row)} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--brand-primary)", font: "var(--text-body)", padding: "var(--space-1) var(--space-2)" }}>
                    {actionLabel}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Convenience: render a status cell as a StatusPill from a {tone,label,icon} value. */
DataTable.status = (v) => <StatusPill tone={v.tone} icon={v.icon || "dot"} spin={v.spin}>{v.label}</StatusPill>;
