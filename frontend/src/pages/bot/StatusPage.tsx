import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { exactTime, timeAgo } from "../../lib/time";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { OneTimeField } from "./OneTimeField";
import { Empty, failed, Loading, Problem } from "./states";
import { botKeys, useBotKeys, useBotStatus, type BotKey } from "./useBot";

/** Очередь стоит, если сообщения ждут, а бот не забирал их дольше этого. */
const QUEUE_STUCK_MS = 2 * 60_000;
const STATUS_REFRESH_MS = 30_000;
const KEY_NAME_MAX = 64;

const botName = (username: string | null) => (username ? `@${username}` : "—");

function Ago({ iso }: { iso: string | null }) {
  const { t } = useTranslation();
  if (!iso) return <>{t("bot.status.never")}</>;
  return (
    <time dateTime={iso} title={exactTime(iso)}>
      {timeAgo(iso)}
    </time>
  );
}

/** «Состояние» (/bot): жив ли бот, забирает ли очередь, и ключи, по которым он ходит в панель. */
export function StatusPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const status = useBotStatus({ refreshMs: STATUS_REFRESH_MS });
  const keys = useBotKeys();
  const queryClient = useQueryClient();
  const broken = failed(status, keys);

  const manageKeys = can("bot:keys");
  const data = status.data;
  // Время «сейчас» - момент получения ответа (в отрисовке Date.now() брать нельзя).
  const queued = data ? data.outbox.pending + data.outbox.claimed : 0;
  const claimIsOld =
    data !== undefined &&
    (data.last_claim_at === null || status.dataUpdatedAt - Date.parse(data.last_claim_at) > QUEUE_STUCK_MS);

  return (
    <section className={styles.section} aria-labelledby="bot-status-title">
      <div className={styles.sectionHead}>
        <h2 id="bot-status-title" className={styles.sectionTitle}>
          {t("bot.status.title")}
        </h2>
        {manageKeys && keys.data && !broken && <CreateKeyDialog />}
      </div>

      {broken && (
        <Problem
          error={broken.error}
          onRetry={() => void queryClient.invalidateQueries({ queryKey: ["bot"] })}
        />
      )}
      {!broken && (status.isPending || keys.isPending) && <Loading />}

      {data && !broken && (
        <>
          {data.stale && <Notice>{t("bot.status.stale")}</Notice>}
          {queued > 0 && claimIsOld && <Notice>{t("bot.status.queueStuck")}</Notice>}
          <dl className={styles.facts}>
            <dt>{t("bot.status.bot")}</dt>
            <dd>{botName(data.bot_username)}</dd>
            <dt>{t("bot.status.version")}</dt>
            <dd>{data.last_seen_version ?? "—"}</dd>
            <dt>{t("bot.status.lastSeen")}</dt>
            <dd>
              <Ago iso={data.last_seen_at} />
            </dd>
            <dt>{t("bot.status.lastClaim")}</dt>
            <dd>
              <Ago iso={data.last_claim_at} />
            </dd>
            <dt>{t("bot.status.queue")}</dt>
            <dd>
              {t("bot.status.queueCounts", { pending: data.outbox.pending, claimed: data.outbox.claimed })}
            </dd>
          </dl>
        </>
      )}

      {keys.data && !broken && (
        <div className={styles.section}>
          <h3 className={styles.subTitle}>{t("bot.keys.title")}</h3>
          {keys.data.length === 0 ? (
            <Empty>{t("bot.keys.empty")}</Empty>
          ) : (
            <ul className={styles.list}>
              {keys.data.map((key) => (
                <KeyRow key={key.id} botKey={key} canRevoke={manageKeys} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function KeyRow({ botKey, canRevoke }: { botKey: BotKey; canRevoke: boolean }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const revoked = botKey.revoked_at !== null;
  return (
    <li className={styles.item} data-signal={revoked ? "idle" : "ok"}>
      <div className={styles.itemHead}>
        <span className={styles.itemName}>{botKey.name}</span>
        {revoked ? (
          <span className={styles.state} data-signal="idle">
            {t("bot.keys.revoked")}
          </span>
        ) : (
          canRevoke && (
            <ConfirmDialog
              trigger={<Button variant="quiet">{t("bot.keys.revoke.trigger")}</Button>}
              title={t("bot.keys.revoke.title", { name: botKey.name })}
              consequences={t("bot.keys.revoke.consequences")}
              actionLabel={t("bot.keys.revoke.action")}
              busyLabel={t("bot.keys.revoke.busy")}
              onConfirm={async () => {
                await botUnwrap(
                  api.POST("/api/v1/bot-admin/keys/{key_id}/revoke", {
                    params: { path: { key_id: botKey.id } },
                  }),
                );
                await queryClient.invalidateQueries({ queryKey: botKeys.keys });
                await queryClient.invalidateQueries({ queryKey: botKeys.status });
              }}
            />
          )
        )}
      </div>
      <div className={styles.itemMeta}>
        <code className={styles.mono}>{botKey.key_prefix}</code>
        {botKey.bot_username && <span>{botName(botKey.bot_username)}</span>}
        {botKey.last_seen_version && <span>{botKey.last_seen_version}</span>}
        <span>
          {t("bot.status.lastSeen")}: <Ago iso={botKey.last_seen_at} />
        </span>
      </div>
    </li>
  );
}

/**
 * «Создать ключ». Открытый ключ приходит один раз: он живёт только в состоянии мутации (`gcTime: 0`) и стирается,
 * когда окно закрывают, - ни в кэше запросов, ни на странице после этого его нет.
 */
function CreateKeyDialog() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const create = useMutation({
    mutationFn: (name: string) => botUnwrap(api.POST("/api/v1/bot-admin/keys", { body: { name } })),
    gcTime: 0,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: botKeys.keys });
      void queryClient.invalidateQueries({ queryKey: botKeys.status });
    },
  });

  function close(next: boolean) {
    if (!next) create.reset();
    setOpen(next);
  }

  const created = create.data;
  return (
    <>
      <Button onClick={() => setOpen(true)}>{t("bot.keys.create.trigger")}</Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={created ? t("bot.keys.create.doneTitle") : t("bot.keys.create.title")}
        description={created ? t("bot.keys.create.doneDescription") : t("bot.keys.create.description")}
      >
        {created ? (
          <>
            <OneTimeField label={t("bot.keys.create.keyLabel")} value={created.key} />
            <DialogActions>
              <Button onClick={() => close(false)}>{t("bot.done")}</Button>
            </DialogActions>
          </>
        ) : (
          <CreateKeyForm
            busy={create.isPending}
            error={create.isError ? errorMessage(create.error) : null}
            onSubmit={(name) => create.mutate(name)}
            onCancel={() => close(false)}
          />
        )}
      </Dialog>
    </>
  );
}

function CreateKeyForm({
  busy,
  error,
  onSubmit,
  onCancel,
}: {
  busy: boolean;
  error: string | null;
  onSubmit: (name: string) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (name.trim()) onSubmit(name.trim());
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.field}>
        <label htmlFor="bot-key-name" className={styles.label}>
          {t("bot.keys.create.name")}
        </label>
        <input
          id="bot-key-name"
          className={styles.control}
          value={name}
          onChange={(event) => setName(event.target.value)}
          maxLength={KEY_NAME_MAX}
          autoComplete="off"
          required
        />
        <p className={styles.hint}>{t("bot.keys.create.nameHint")}</p>
      </div>
      {error && <Notice>{error}</Notice>}
      <DialogActions>
        <Button variant="quiet" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" busy={busy} busyLabel={t("bot.keys.create.busy")}>
          {t("bot.keys.create.submit")}
        </Button>
      </DialogActions>
    </form>
  );
}
