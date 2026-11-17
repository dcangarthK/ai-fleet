import type { ReactNode } from "react";
import { cx } from "./util";

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Plain-text label for the stacked-card layout below 640px. Defaults to `header` when it is a string. */
  label?: string;
  cell: (row: T) => ReactNode;
  numeric?: boolean;
  /** Ellipsis long values (ids, branches, paths). Put the full text in the cell's `title` yourself or use <Truncate>. */
  truncate?: boolean;
  /** Hide this column in the stacked layout. */
  hideStacked?: boolean;
}

/**
 * Responsive table. Wide: a real <table> that scrolls inside its own container. Below 640px: stacked cards,
 * each cell prefixed by its column label (data-label). The page body never scrolls horizontally.
 */
export function Table<T>({ columns, rows, rowKey, onRowClick, selectedKey, caption, className }: {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string;
  onRowClick?: (row: T) => void;
  selectedKey?: string | null;
  caption?: string;
  className?: string;
}) {
  return (
    <div className="tbl-wrap">
      <table className={cx("tbl", className)}>
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c.key} scope="col" className={cx(c.numeric && "n")}>{c.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row);
            return (
              <tr
                key={key}
                className={cx(onRowClick && "click", selectedKey === key && "selected")}
                tabIndex={onRowClick ? 0 : undefined}
                aria-selected={onRowClick ? selectedKey === key : undefined}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                onKeyDown={onRowClick ? (e) => {
                  if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
                    e.preventDefault();
                    onRowClick(row);
                  }
                } : undefined}
              >
                {columns.map((c) => (
                  <td
                    key={c.key}
                    data-label={c.label ?? (typeof c.header === "string" ? c.header : "")}
                    className={cx(c.numeric && "n", c.truncate && "cell-trunc", c.hideStacked && "hide-sm")}
                  >
                    {c.truncate ? <span className="trunc" style={{ maxWidth: "100%" }}>{c.cell(row)}</span> : c.cell(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
