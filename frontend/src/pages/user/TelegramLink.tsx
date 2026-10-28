import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type VPNUser } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { currentLanguage } from "../../i18n/format";
import i18n from "../../i18n";
import { botUnwrap, errorMessage } from "../bot/botErrors";
import { botKeys, POLL_MS } from "../bot/useBot";
import styles from "./UserPage.module.css";
import { userKeys } from "./useUser";

/** Пока сообщение в очереди или у бота, итога нет и опрос продолжается. */
const OPEN = new Set(["pending", "claimed"]);

/** «12.10»: число и месяц по местному времени. */
function dayMonth(iso: string): string {
  return new Intl.DateTimeFormat(currentLanguage(), { day: "2-digit", month: "2-digit" }).format(
    new Date(iso),
  );
}

/**
 * Telegram человека в карточке: ник, пометка «заблокировал бота» и (для bot:manage) кнопка «Отправить ссылку в
 * Telegram». Кнопка ставит сообщение в очередь бота, дальше раз в 2 с спрашивает, чем кончилось, и пишет итог.
 */
export function TelegramLink({ user }: { user: VPNUser }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const hintId = useId();
  const [outboxId, setOutboxId] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: () =>
      botUnwrap(
        api.POST("/api/v1/bot-admin/users/{user_id}/config-link", { params: { path: { user_id: user.id } } }),
      ),
    onMutate: () => setOutboxId(null),
    onSuccess: (queued) => setOutboxId(queued.outbox_id),
  });

  const outbox = useQuery({
    queryKey: botKeys.outbox(outboxId ?? ""),
    queryFn: () =>
      botUnwrap(
        api.GET("/api/v1/bot-admin/outbox/{outbox_id}", { params: { path: { outbox_id: outboxId ?? "" } } }),
      ),
    enabled: outboxId !== null,
    refetchInterval: (query) =>
      query.state.status === "error" || (query.state.data && !OPEN.has(query.state.data.status))
        ? false
        : POLL_MS,
  });

  const status = outbox.data?.status;
  useEffect(() => {
    // Человек заблокировал бота: пометка появилась в карточке - перечитываем её, кнопка станет неактивной.
    if (status === "blocked")
      void queryClient.invalidateQueries({ queryKey: userKeys.user(user.id), exact: true });
  }, [status, queryClient, user.id]);

  if (user.telegram_id == null) return null;

  const blocked = user.bot_blocked_at != null;
  const who = user.telegram_username ? `@${user.telegram_username}` : t("users.telegram.linked");
  const inProgress =
    send.isPending || (outboxId !== null && !outbox.isError && (!status || OPEN.has(status)));

  return (
    <div className={styles.telegram}>
      <p className={styles.address}>
        {blocked && user.bot_blocked_at
          ? t("users.telegram.lineBlocked", { who, date: dayMonth(user.bot_blocked_at) })
          : t("users.telegram.line", { who })}
      </p>
      {can("bot:manage") && (
        <>
          <div className={styles.row}>
            <Button
              variant="quiet"
              disabled={blocked || inProgress}
              aria-describedby={blocked ? hintId : undefined}
              onClick={() => send.mutate()}
            >
              {t("users.telegram.send")}
            </Button>
          </div>
          {blocked && (
            <p id={hintId} className={styles.hint}>
              {t("users.telegram.blockedHint")}
            </p>
          )}
          {inProgress && (
            <p role="status" className={styles.hint}>
              {t("users.telegram.sending")}
            </p>
          )}
          {send.isError && <Notice>{errorMessage(send.error)}</Notice>}
          {outbox.isError && <Notice>{errorMessage(outbox.error)}</Notice>}
          {status === "sent" && (
            <p role="status" className={styles.hint}>
              {t("users.telegram.result.sent")}
            </p>
          )}
          {status === "blocked" && <Notice>{t("users.telegram.result.blocked")}</Notice>}
          {status !== undefined && !OPEN.has(status) && !["sent", "blocked"].includes(status) && (
            <Notice>{outcomeText(status, outbox.data?.error ?? null)}</Notice>
          )}
        </>
      )}
    </div>
  );
}

/** Итог, который не «отправлено» и не «заблокировал»: пропущено по понятной причине - называем её, иначе общий текст. */
function outcomeText(status: string, error: string | null): string {
  if (status === "skipped") {
    const path = `bot.errors.reasons.${error ?? ""}`;
    return error && i18n.exists(path)
      ? i18n.t(path as "bot.errors.conflict")
      : i18n.t("users.telegram.result.skipped");
  }
  return i18n.t("users.telegram.result.failed");
}
