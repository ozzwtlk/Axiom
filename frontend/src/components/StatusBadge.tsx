import type { Status } from "../api/types";

const LABEL: Record<Status, string> = {
  pending: "pending",
  running: "processing",
  done: "completed",
  partial: "partial",
  failed: "failed",
};
const ICON: Record<Status, string> = { pending: "○", running: "◔", done: "✓", partial: "◑", failed: "✕" };

export function StatusBadge({ status }: { status: Status }) {
  return (
    <span className={`badge badge-${status}`} title={`Status: ${status}`}>
      <span aria-hidden="true">{ICON[status]}</span> {LABEL[status]}
    </span>
  );
}

export const isOpenable = (s: Status) => s === "done" || s === "partial";
