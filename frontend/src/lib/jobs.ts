import type { Job } from "../api/client";
import i18n from "../i18n";

export function jobLabel(type: Job["type"]): string {
  return i18n.t(`jobs.types.${type}`);
}

export function jobStatusLabel(status: Job["status"]): string {
  return i18n.t(`jobs.statuses.${status}`);
}

export function isUnfinished(job: Pick<Job, "status">): boolean {
  return job.status === "pending" || job.status === "running";
}
