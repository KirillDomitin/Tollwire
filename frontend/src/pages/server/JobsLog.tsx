import { useTranslation } from "react-i18next";

import type { Job } from "../../api/client";
import { isUnfinished, jobLabel, jobStatusLabel } from "../../lib/jobs";
import { exactTime, timeAgo } from "../../lib/time";
import styles from "./ServerPage.module.css";

interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

const CHECK_NAMES = ["ssh", "docker", "internet", "docker_daemon"] as const;

/** Журнал задач сервера: что, когда, чем кончилось. Отчёт health раскрывается по щелчку. */
export function JobsLog({ jobs }: { jobs: Job[] }) {
  const { t } = useTranslation();
  const checkName = (name: string) =>
    (CHECK_NAMES as readonly string[]).includes(name)
      ? t(`servers.jobsLog.checks.${name as (typeof CHECK_NAMES)[number]}`)
      : name;
  if (jobs.length === 0) {
    return <p className={styles.hint}>{t("servers.jobsLog.empty")}</p>;
  }
  return (
    <ol className={styles.jobs}>
      {jobs.map((job) => {
        const checks = (job.result?.checks as Check[] | undefined) ?? [];
        return (
          <li key={job.id} className={styles.job} data-status={job.status}>
            <div className={styles.jobHead}>
              <span className={styles.jobTitle}>{jobLabel(job.type)}</span>
              <span className={styles.jobStatus} data-status={job.status}>
                {jobStatusLabel(job.status)}
                {job.status === "running" && job.progress > 0 && ` · ${job.progress}%`}
              </span>
              <time className={styles.jobTime} dateTime={job.created_at} title={exactTime(job.created_at)}>
                {timeAgo(job.created_at)}
              </time>
            </div>
            {job.error && <p className={styles.jobError}>{job.error}</p>}
            {checks.length > 0 && !isUnfinished(job) && (
              <details className={styles.report}>
                <summary>{t("servers.jobsLog.report")}</summary>
                <ul>
                  {checks.map((check) => (
                    <li key={check.name} data-ok={check.ok}>
                      <strong>{checkName(check.name)}</strong> —{" "}
                      {check.ok ? t("servers.jobsLog.ok") : t("servers.jobsLog.problem")}
                      {check.detail && `: ${check.detail}`}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </li>
        );
      })}
    </ol>
  );
}
