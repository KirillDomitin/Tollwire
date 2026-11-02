import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type Schemas } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { exactTime } from "../../lib/time";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { failed, Loading, Problem } from "./states";
import { botKeys, useBotStatus, useMyTelegram, useNotifyEvents, useOutbox } from "./useBot";

const issueCode = () => botUnwrap(api.POST("/api/v1/bot-admin/me/telegram/code", { cache: "no-store" }));
type LinkCode = Awaited<ReturnType<typeof issueCode>>;

/** Personal preferences are writable with bot:notify, including for a viewer. */
export function NotifyPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  if (!can("bot:notify")) return <Notice>{t("common.errors.forbidden")}</Notice>;
  return <Notifications />;
}

function Notifications() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [linking, setLinking] = useState(false);
  const me = useMyTelegram(true, linking);
  const catalog = useNotifyEvents();
  const status = useBotStatus({ refreshMs: 30_000 });
  const broken = failed(me, catalog, status);
  const events = useMutation({
    mutationFn: (values: Record<string, boolean>) =>
      botUnwrap(api.PUT("/api/v1/bot-admin/me/telegram/events", { body: { events: values } })),
    onSuccess: (updated) => {
      queryClient.setQueryData<Schemas["AdminTelegramRead"]>(botKeys.telegram, (current) =>
        current ? { ...current, ...updated } : current,
      );
      return queryClient.invalidateQueries({ queryKey: botKeys.telegram });
    },
  });
  const test = useMutation({
    mutationFn: () => botUnwrap(api.POST("/api/v1/bot-admin/me/telegram/test")),
  });

  async function unlink() {
    await botUnwrap(api.DELETE("/api/v1/bot-admin/me/telegram"));
    test.reset();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: botKeys.telegram }),
      queryClient.invalidateQueries({ queryKey: botKeys.adminLinks }),
      queryClient.invalidateQueries({ queryKey: botKeys.status }),
    ]);
  }

  const selected = Object.fromEntries(
    (catalog.data ?? []).map((event) => [event.key, me.data?.events[event.key] ?? event.default]),
  );
  const groups = [...new Set((catalog.data ?? []).map((event) => event.group))];
  const linked = me.data?.linked === true;
  return (
    <section className={styles.section} aria-labelledby="notify-title">
      <h2 id="notify-title" className={styles.sectionTitle}>
        {t("bot.notify.title")}
      </h2>
      {broken && (
        <Problem
          error={broken.error}
          onRetry={() => {
            void me.refetch();
            void catalog.refetch();
            void status.refetch();
          }}
        />
      )}
      {!broken && (me.isPending || catalog.isPending || status.isPending) && <Loading />}
      {me.data && (
        <>
          <div className={styles.section}>
            <p>
              {linked
                ? me.data.telegram_username
                  ? `@${me.data.telegram_username}`
                  : t("bot.notify.linked")
                : t("bot.notify.notLinked")}
            </p>
            {me.data.linked_at && (
              <time dateTime={me.data.linked_at}>
                {t("bot.notify.linkedAt", { time: exactTime(me.data.linked_at) })}
              </time>
            )}
            {status.data?.notify_capable === false && <Notice>{t("bot.notify.updateBot")}</Notice>}
            {status.data?.last_seen_local && <Notice>{t("bot.notify.localBot")}</Notice>}
            {me.data.blocked_at && <Notice>{t("bot.notify.blocked")}</Notice>}
            <div className={styles.row}>
              {(!linked || me.data.blocked_at) && <LinkTelegram onPolling={setLinking} />}
              {linked && (
                <>
                  <Button busy={test.isPending} disabled={!!me.data.blocked_at} onClick={() => test.mutate()}>
                    {t("bot.notify.test")}
                  </Button>
                  <ConfirmDialog
                    trigger={<Button variant="ghost">{t("bot.notify.unlink")}</Button>}
                    title={t("bot.notify.unlinkTitle")}
                    consequences={t("bot.notify.unlinkConsequences")}
                    actionLabel={t("bot.notify.unlink")}
                    busyLabel={t("bot.notify.unlinkBusy")}
                    onConfirm={unlink}
                  />
                </>
              )}
            </div>
            {test.isError && <Notice>{errorMessage(test.error)}</Notice>}
            {test.data && <Delivery id={test.data.outbox_id} />}
          </div>
          {events.isError && <Notice>{errorMessage(events.error)}</Notice>}
          {groups.map((group) => (
            <fieldset key={group} className={styles.choice} disabled={events.isPending}>
              <legend>{t(`bot.notify.groups.${group}` as "bot.notify.groups.servers")}</legend>
              {(catalog.data ?? [])
                .filter((event) => event.group === group)
                .map((event) => (
                  <label key={event.key}>
                    <input
                      type="checkbox"
                      checked={selected[event.key] ?? false}
                      onChange={(change) =>
                        events.mutate({ ...selected, [event.key]: change.target.checked })
                      }
                    />
                    {t(`bot.notify.events.${event.key}` as "bot.notify.events.server_down")}
                  </label>
                ))}
            </fieldset>
          ))}
          <AdminRecipients />
        </>
      )}
    </section>
  );
}

function LinkTelegram({ onPolling }: { onPolling: (value: boolean) => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const create = useMutation({ mutationFn: issueCode, gcTime: 0 });
  const { reset } = create;

  const close = useCallback(() => {
    setOpen(false);
    onPolling(false);
    reset();
  }, [onPolling, reset]);
  // Linking also unmounts the button; invalidate the recipient list and stop its poll in that case.
  useEffect(
    () => () => {
      onPolling(false);
      void queryClient.invalidateQueries({ queryKey: botKeys.adminLinks });
      void queryClient.invalidateQueries({ queryKey: botKeys.status });
    },
    [onPolling, queryClient],
  );

  return (
    <>
      <Button
        busy={create.isPending}
        onClick={() => {
          create.mutate(undefined, {
            onSuccess: () => {
              setOpen(true);
              onPolling(true);
            },
          });
        }}
      >
        {t("bot.notify.link")}
      </Button>
      {create.isError && <Notice>{errorMessage(create.error)}</Notice>}
      {open && create.data && <BindingDialog code={create.data} onClose={close} onPolling={onPolling} />}
    </>
  );
}

function BindingDialog({
  code,
  onClose,
  onPolling,
}: {
  code: LinkCode;
  onClose: () => void;
  onPolling: (value: boolean) => void;
}) {
  const { t } = useTranslation();
  const me = useMyTelegram();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (me.data?.linked && !me.data.blocked_at) onClose();
  }, [me.data, onClose]);
  const seconds = Math.max(0, Math.ceil((new Date(code.expires_at).getTime() - now) / 1000));
  useEffect(() => {
    if (seconds === 0) onPolling(false);
  }, [seconds, onPolling]);
  const remaining = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  return (
    <Dialog
      open
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
      title={t("bot.notify.linkTitle")}
      description={t("bot.notify.linkHint")}
    >
      <div className={styles.section}>
        {seconds > 0 ? (
          <>
            {code.link && (
              <a className={styles.link} href={code.link} target="_blank" rel="noopener noreferrer">
                {t("bot.notify.openTelegram")}
              </a>
            )}
            <p className={styles.mono}>{code.code}</p>
            <p role="timer">{t("bot.notify.expires", { remaining })}</p>
          </>
        ) : (
          <Notice>{t("bot.notify.expired")}</Notice>
        )}
      </div>
      <DialogActions>
        <Button variant="ghost" onClick={onClose}>
          {t("bot.notify.close")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}

function Delivery({ id }: { id: string }) {
  const { t } = useTranslation();
  const outbox = useOutbox(id);
  if (outbox.isError) return <Problem error={outbox.error} onRetry={() => void outbox.refetch()} />;
  const state = outbox.data?.status;
  if (!state || ["pending", "claimed"].includes(state)) return <p role="status">{t("bot.notify.queued")}</p>;
  return state === "sent" ? (
    <p role="status" className={styles.good}>
      {t("bot.notify.sent")}
    </p>
  ) : (
    <Notice>{t("bot.notify.deliveryFailed")}</Notice>
  );
}

function AdminRecipients() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const [open, setOpen] = useState(false);
  const links = useQuery({
    queryKey: botKeys.adminLinks,
    queryFn: () => botUnwrap(api.GET("/api/v1/bot-admin/admin-links")),
    enabled: can("bot:read") && open,
  });
  if (!can("bot:read")) return null;
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{t("bot.notify.recipients")}</summary>
      {open && (
        <>
          {links.isPending && <Loading />}
          {links.isError && <Problem error={links.error} onRetry={() => void links.refetch()} />}
          {links.data?.length === 0 && <p className={styles.quiet}>{t("bot.notify.noRecipients")}</p>}
          <ul className={styles.list}>
            {links.data?.map((link, index) => (
              <li key={index} className={styles.item}>
                <span className={styles.itemName}>{link.admin_email}</span>
                {link.telegram_username && <span>@{link.telegram_username}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </details>
  );
}
