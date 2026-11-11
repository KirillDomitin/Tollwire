import type { ReactNode } from "react";
import { Button } from "./Button";
import { ChevronDownIcon, ChevronUpIcon } from "./icons";
import table from "./DataTable.module.css";
import type { Sort } from "../lib/tableSort";

export function SortHeader<K extends string>({
  field,
  sort,
  onSort,
  children,
}: {
  field: K;
  sort: Sort<K>;
  onSort: (field: K) => void;
  children: ReactNode;
}) {
  const direction = sort?.field === field ? sort.direction : "none";
  return (
    <th scope="col" aria-sort={direction} className={table.sortable}>
      <Button variant="ghost" size="sm" className={table.sortButton} onClick={() => onSort(field)}>
        {children}
        {direction !== "none" && (
          <span aria-hidden="true">
            {direction === "ascending" ? <ChevronUpIcon /> : <ChevronDownIcon />}
          </span>
        )}
      </Button>
    </th>
  );
}
