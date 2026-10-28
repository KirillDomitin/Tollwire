import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { exactTime, timeAgo } from "../../lib/time";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { Empty, Loading, Problem } from "./states";
import { botKeys, useRegistrations, type Registration } from "./useBot";

const STATUSES = ["payment_pending", "provisioning", "active", "failed"] as const;
const SIGNAL: Record<Registration["status"], string> = {
  payment_pending: "idle",
  provisioning: "process",
  active: "ok",
  failed: "down",
};

/** «Регистрации» (/bot/registrations): кто пришёл по приглашению и чем кончилась выдача доступа. */
export function RegistrationsPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [status, setStatus] = useState<Registration["status"] | "">("");
  const registrations = useRegistrations(status);
  const queryClient = useQueryClient();

  const retry = useMutation({
    mutationFn: (id: string) =>
      botUnwrap(
        api.POST("/api/v1/bot-admin/registrations/{registration_id}/retry", {
          params: { path: { registration_id: id } },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: botKeys.registrationsAll }),
  });

  return (
    <section className={styles.section} aria-labelledby="bot-registrations-title">
      <div className={styles.sectionHead}>
        <h2 id="bot-registrations-title" className={styles.sectionTitle}>
          {t("bot.registrations.title")}
        </h2>
        <div className={styles.row}>
          <label htmlFor="registrations-status" className={styles.label}>
            {t("bot.registrations.filter")}
          </label>
          <select
            id="registrations-status"
            className={styles.control}
            value={status}
            onChange={(event) => setStatus(event.target.value as Registration["status"] | "")}
          >
            <option value="">{t("bot.registrations.all")}</option>
            {STATUSES.map((item) => (
              <option key={item} value={item}>
                {t(`bot.registrations.status.${item}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {retry.isError && <Notice>{errorMessage(retry.error)}</Notice>}
      {registrations.isError && (
        <Problem error={registrations.error} onRetry={() => void registrations.refetch()} />
      )}
      {registrations.isPending && <Loading />}
      {registrations.data && registrations.items.length === 0 && (
        <Empty>{status ? t("bot.registrations.emptyFiltered") : t("bot.registrations.empty")}</Empty>
      )}
      {registrations.items.length > 0 && (
        <ul className={styles.list}>
          {registrations.items.map((item) => (
            <RegistrationRow
              key={item.id}
              item={item}
              onRetry={can("bot:manage") ? () => retry.mutate(item.id) : undefined}
              retrying={retry.isPending && retry.variables === item.id}
            />
          ))}
        </ul>
      )}
      {registrations.hasNextPage && (
        <div>
          <Button
            variant="quiet"
            busy={registrations.isFetchingNextPage}
            onClick={() => void registrations.fetchNextPage()}
          >
            {t("bot.more")}
          </Button>
        </div>
      )}
    </section>
  );
}

function RegistrationRow({
  item,
  onRetry,
  retrying,
}: {
  item: Registration;
  onRetry?: () => void;
  retrying: boolean;
}) {
  const { t } = useTranslation();
  return (
    <li className={styles.item} data-signal={SIGNAL[item.status]}>
      <div className={styles.itemHead}>
        {item.user_id ? (
          <Link to={`/users/${item.user_id}`} className={`${styles.itemName} ${styles.link}`}>
            {item.name}
          </Link>
        ) : (
          <span className={styles.itemName}>{item.name}</span>
        )}
        <span className={styles.state} data-signal={SIGNAL[item.status]}>
          {t(`bot.registrations.status.${item.status}`)}
        </span>
      </div>
      <div className={styles.itemMeta}>
        <span>{item.email}</span>
        {item.telegram_username && <span>@{item.telegram_username}</span>}
        <time dateTime={item.created_at} title={exactTime(item.created_at)}>
          {timeAgo(item.created_at)}
        </time>
      </div>
      {item.status === "failed" && (
        <>
          {item.error && (
            <details className={styles.details}>
              <summary>{t("bot.registrations.details")}</summary>
              <p>{item.error}</p>
            </details>
          )}
          {onRetry && (
            <div className={styles.row}>
              <Button variant="quiet" disabled={retrying} onClick={onRetry}>
                {t("bot.registrations.retry")}
              </Button>
            </div>
          )}
        </>
      )}
    </li>
  );
}
