import React from "react";

export interface DataTableColumn {
  key: string;
  header: React.ReactNode;
  align?: "left" | "center" | "right";
  /** Use tabular-nums + Inter for numeric columns. */
  numeric?: boolean;
  /** Custom cell renderer; e.g. DataTable.status for a StatusPill column. */
  render?: (value: any, row: any) => React.ReactNode;
}

export interface DataTableProps {
  columns: DataTableColumn[];
  rows: Array<Record<string, any>>;
  /** Renders a trailing text-button action column when provided. */
  onRowAction?: (row: any) => void;
  actionLabel?: string;
  style?: React.CSSProperties;
}

/**
 * DataTable — screening-records table; 56px rows, StatusPill status column, masked IDs.
 * @startingPoint section="Data" subtitle="Screening records table" viewport="700x300"
 */
export function DataTable(props: DataTableProps): JSX.Element;
