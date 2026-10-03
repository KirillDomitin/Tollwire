import type { Job } from "../../api/client";
import { JOB_LABELS, JOB_STATUS_LABELS, isUnfinished } from "../../lib/jobs";
import { exactTime, timeAgo } from "../../lib/time";
import styles from "./ServerPage.module.css";

interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

const CHECK_NAMES: Record<string, string> = {
  ssh: "SSH",
  docker: "Docker",
  internet: "Интернет",
  docker_daemon: "Служба Docker",
};

/** Журнал задач сервера: что, когда, чем кончилось. Отчёт health раскрывается по щелчку. */
export function JobsLog({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) {
    return <p className={styles.hint}>Задач по этому серверу ещё не было.</p>;
  }
  return (
    <ol className={styles.jobs}>
      {jobs.map((job) => {
        const checks = (job.result?.checks as Check[] | undefined) ?? [];
        return (
          <li key={job.id} className={styles.job} data-status={job.status}>
            <div className={styles.jobHead}>
              <span className={styles.jobTitle}>{JOB_LABELS[job.type]}</span>
              <span className={styles.jobStatus} data-status={job.status}>
                {JOB_STATUS_LABELS[job.status]}
                {job.status === "running" && job.progress > 0 && ` · ${job.progress}%`}
              </span>
              <time className={styles.jobTime} dateTime={job.created_at} title={exactTime(job.created_at)}>
                {timeAgo(job.created_at)}
              </time>
            </div>
            {job.error && <p className={styles.jobError}>{job.error}</p>}
            {checks.length > 0 && !isUnfinished(job) && (
              <details className={styles.report}>
                <summary>Отчёт проверки</summary>
                <ul>
                  {checks.map((check) => (
                    <li key={check.name} data-ok={check.ok}>
                      <strong>{CHECK_NAMES[check.name] ?? check.name}</strong> —{" "}
                      {check.ok ? "в порядке" : "проблема"}
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
