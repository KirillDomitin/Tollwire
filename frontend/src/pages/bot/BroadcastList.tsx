import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { parseTelegramHtml, plainText } from "../../lib/telegramHtml";
import { exactTime, timeAgo } from "../../lib/time";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { Empty, Loading, Problem } from "./states";
import { botKeys, useBroadcasts, type Broadcast } from "./useBot";

const SIGNAL: Record<Broadcast["state"], string> = { running: "process", finished: "ok", cancelled: "warn" };
const EXCERPT = 140;

/** Видимый текст новости без разметки, обрезанный: список не должен разрастаться от длинной рассылки. */
function excerpt(text: string): string {
  const plain = plainText(parseTelegramHtml(text)).replace(/\s+/g, " ").trim();
  return plain.length > EXCERPT ? `${plain.slice(0, EXCERPT)}…` : plain;
}

/** Список рассылок со счётчиками. Пока хоть одна идёт, он перечитывается раз в 2 секунды; потом опрос стихает. */
export function BroadcastList() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const broadcasts = useBroadcasts();
  const queryClient = useQueryClient();

  const cancel = useMutation({
    mutationFn: (id: string) =>
      botUnwrap(
        api.POST("/api/v1/bot-admin/broadcasts/{broadcast_id}/cancel", {
          params: { path: { broadcast_id: id } },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: botKeys.broadcasts }),
  });

  return (
    <div className={styles.section}>
      <h3 className={styles.subTitle}>{t("bot.broadcasts.list")}</h3>
      {cancel.isError && <Notice>{errorMessage(cancel.error)}</Notice>}
      {broadcasts.isError && <Problem error={broadcasts.error} onRetry={() => void broadcasts.refetch()} />}
      {broadcasts.isPending && <Loading />}
      {broadcasts.data && broadcasts.items.length === 0 && <Empty>{t("bot.broadcasts.empty")}</Empty>}
      {broadcasts.items.length > 0 && (
        <ul className={styles.list}>
          {broadcasts.items.map((item) => (
            <BroadcastRow
              key={item.id}
              item={item}
              onCancel={can("bot:manage") ? () => cancel.mutate(item.id) : undefined}
              cancelling={cancel.isPending && cancel.variables === item.id}
            />
          ))}
        </ul>
      )}
      {broadcasts.hasNextPage && (
        <div>
          <Button
            variant="quiet"
            busy={broadcasts.isFetchingNextPage}
            onClick={() => void broadcasts.fetchNextPage()}
          >
            {t("bot.more")}
          </Button>
        </div>
      )}
    </div>
  );
}

function BroadcastRow({
  item,
  onCancel,
  cancelling,
}: {
  item: Broadcast;
  onCancel?: () => void;
  cancelling: boolean;
}) {
  const { t } = useTranslation();
  const processed = item.sent + item.blocked + item.failed + item.skipped + item.cancelled;
  const counters = [
    ["sent", item.sent],
    ["blocked", item.blocked],
    ["failed", item.failed],
    ["skipped", item.skipped],
    ["cancelled", item.cancelled],
  ] as const;
  return (
    <li className={styles.item} data-signal={SIGNAL[item.state]}>
      <div className={styles.itemHead}>
        <span className={styles.itemName}>
          {item.kind === "config" ? t("bot.broadcasts.kind.config") : t("bot.broadcasts.kind.news")}
        </span>
        <span className={styles.state} data-signal={SIGNAL[item.state]}>
          {t(`bot.broadcasts.state.${item.state}`)}
        </span>
      </div>
      {item.kind === "news" && item.text && <p className={styles.excerpt}>{excerpt(item.text)}</p>}
      <div className={styles.itemMeta}>
        <span>{t(`bot.broadcasts.target.${item.target as "all"}`)}</span>
        <time dateTime={item.created_at} title={exactTime(item.created_at)}>
          {timeAgo(item.created_at)}
        </time>
      </div>
      <progress
        className={styles.progress}
        value={processed}
        max={Math.max(item.total, 1)}
        aria-label={t("bot.broadcasts.progress", { done: processed, total: item.total })}
      />
      <ul className={styles.counters}>
        {counters.map(([key, value]) => (
          <li key={key}>
            {t(`bot.broadcasts.counters.${key}`)}: {value}
          </li>
        ))}
      </ul>
      {item.state === "running" && onCancel && (
        <div className={styles.row}>
          <Button variant="quiet" disabled={cancelling} onClick={onCancel}>
            {t("bot.broadcasts.cancel")}
          </Button>
        </div>
      )}
    </li>
  );
}
