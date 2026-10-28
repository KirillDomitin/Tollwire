import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router";

import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { addDays, endOfLocalDay, exactTime, isValidDay, localDay, timeAgo } from "../../lib/time";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { OneTimeField } from "./OneTimeField";
import { protocolLabel } from "./protocols";
import { Empty, failed, Loading, Problem } from "./states";
import {
  botKeys,
  useBotProtocols,
  useBotSettings,
  useInvites,
  type BotProtocol,
  type BotSettings,
  type Invite,
} from "./useBot";

const SIGNAL: Record<string, string> = { valid: "process", used: "ok", expired: "idle", revoked: "idle" };
const VALIDITY_PRESETS = [1, 7, 30] as const;
const CUSTOM = "custom";
const NOTE_MAX = 200;

/** «Приглашения» (/bot/invites): список и создание. Код и ссылка приглашения нигде, кроме окна создания, не показываются. */
export function InvitesPage() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const manage = can("bot:manage");
  const invites = useInvites();
  const queryClient = useQueryClient();

  const revoke = useMutation({
    mutationFn: (id: string) =>
      botUnwrap(
        api.POST("/api/v1/bot-admin/invites/{invite_id}/revoke", { params: { path: { invite_id: id } } }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: botKeys.invites }),
  });

  return (
    <section className={styles.section} aria-labelledby="bot-invites-title">
      <div className={styles.sectionHead}>
        <h2 id="bot-invites-title" className={styles.sectionTitle}>
          {t("bot.invites.title")}
        </h2>
        {manage && <CreateInviteDialog />}
      </div>

      {revoke.isError && <Notice>{errorMessage(revoke.error)}</Notice>}
      {invites.isError && <Problem error={invites.error} onRetry={() => void invites.refetch()} />}
      {invites.isPending && <Loading />}
      {invites.data && invites.items.length === 0 && <Empty>{t("bot.invites.empty")}</Empty>}
      {invites.items.length > 0 && (
        <ul className={styles.list}>
          {invites.items.map((invite) => (
            <InviteRow
              key={invite.id}
              invite={invite}
              onRevoke={manage ? () => revoke.mutate(invite.id) : undefined}
              revoking={revoke.isPending && revoke.variables === invite.id}
            />
          ))}
        </ul>
      )}
      {invites.hasNextPage && (
        <div>
          <Button
            variant="quiet"
            busy={invites.isFetchingNextPage}
            onClick={() => void invites.fetchNextPage()}
          >
            {t("bot.more")}
          </Button>
        </div>
      )}
    </section>
  );
}

function InviteRow({
  invite,
  onRevoke,
  revoking,
}: {
  invite: Invite;
  onRevoke?: () => void;
  revoking: boolean;
}) {
  const { t } = useTranslation();
  const known = ["valid", "used", "expired", "revoked"].includes(invite.status);
  return (
    <li className={styles.item} data-signal={SIGNAL[invite.status] ?? "idle"}>
      <div className={styles.itemHead}>
        <span className={styles.itemName}>{invite.note ?? "—"}</span>
        <span className={styles.state} data-signal={SIGNAL[invite.status] ?? "idle"}>
          {known ? t(`bot.invites.status.${invite.status as "valid"}`) : invite.status}
        </span>
      </div>
      <div className={styles.itemMeta}>
        <span>{t("bot.invites.days", { count: invite.days })}</span>
        <span>
          {t("bot.invites.created")}{" "}
          <time dateTime={invite.created_at} title={exactTime(invite.created_at)}>
            {timeAgo(invite.created_at)}
          </time>
        </span>
        {invite.status === "valid" && (
          <span>
            {t("bot.invites.expires")}{" "}
            <time dateTime={invite.expires_at} title={exactTime(invite.expires_at)}>
              {timeAgo(invite.expires_at)}
            </time>
          </span>
        )}
        {invite.status === "used" && invite.used_by_name && (
          <span>
            {t("bot.invites.usedBy")}{" "}
            {invite.used_by_user_id ? (
              <Link to={`/users/${invite.used_by_user_id}`} className={styles.link}>
                {invite.used_by_name}
              </Link>
            ) : (
              invite.used_by_name
            )}
          </span>
        )}
      </div>
      {invite.status === "valid" && onRevoke && (
        <div className={styles.row}>
          <Button variant="quiet" disabled={revoking} onClick={onRevoke}>
            {t("bot.invites.revoke")}
          </Button>
        </div>
      )}
    </li>
  );
}

/**
 * «Создать приглашение». Ответ с кодом и ссылкой живёт только в состоянии мутации (`gcTime: 0`) и стирается, когда
 * окно закрывают: после этого ссылку не достать (в списке её нет).
 */
function CreateInviteDialog() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const settings = useBotSettings();
  const protocols = useBotProtocols();
  const create = useMutation({
    mutationFn: (body: { protocol_id: string; days: number; expires_at: string; note?: string }) =>
      botUnwrap(api.POST("/api/v1/bot-admin/invites", { body })),
    gcTime: 0,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: botKeys.invites }),
  });

  function close(next: boolean) {
    if (!next) create.reset();
    setOpen(next);
  }

  const created = create.data;
  const broken = failed(settings, protocols);
  return (
    <>
      <Button onClick={() => setOpen(true)}>{t("bot.invites.create.trigger")}</Button>
      <Dialog
        open={open}
        onOpenChange={close}
        title={created ? t("bot.invites.create.doneTitle") : t("bot.invites.create.title")}
        description={created ? undefined : t("bot.invites.create.description")}
      >
        {created ? (
          <>
            <OneTimeField
              label={created.link ? t("bot.invites.create.linkLabel") : t("bot.invites.create.codeLabel")}
              value={created.link ?? created.code}
            />
            <p className={styles.hint}>
              {created.link ? t("bot.invites.create.onceLink") : t("bot.invites.create.onceCode")}
            </p>
            <DialogActions>
              <Button onClick={() => close(false)}>{t("bot.done")}</Button>
            </DialogActions>
          </>
        ) : broken ? (
          <Problem error={broken.error} />
        ) : settings.data && protocols.data ? (
          <InviteForm
            settings={settings.data}
            protocols={protocols.data}
            busy={create.isPending}
            error={create.isError ? errorMessage(create.error) : null}
            onSubmit={(body) => create.mutate(body)}
            onCancel={() => close(false)}
          />
        ) : (
          <Loading />
        )}
      </Dialog>
    </>
  );
}

function InviteForm({
  settings,
  protocols,
  busy,
  error,
  onSubmit,
  onCancel,
}: {
  settings: BotSettings;
  protocols: BotProtocol[];
  busy: boolean;
  error: string | null;
  onSubmit: (body: { protocol_id: string; days: number; expires_at: string; note?: string }) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  // Значения подставлены из настроек, но отправляются явно: что на экране, то и уходит.
  const preset = (VALIDITY_PRESETS as readonly number[]).includes(settings.invite_valid_days);
  const [protocolId, setProtocolId] = useState(settings.default_protocol_id ?? "");
  const [days, setDays] = useState(String(settings.default_days));
  const [validity, setValidity] = useState(preset ? String(settings.invite_valid_days) : CUSTOM);
  const [day, setDay] = useState(() => localDay(addDays(new Date(), settings.invite_valid_days)));
  const [note, setNote] = useState("");

  const daysNumber = Number(days);
  const custom = validity === CUSTOM;
  const valid =
    protocolId !== "" && Number.isInteger(daysNumber) && daysNumber >= 1 && (!custom || isValidDay(day));

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid) return;
    const expires = custom ? endOfLocalDay(day) : addDays(new Date(), Number(validity)).toISOString();
    onSubmit({
      protocol_id: protocolId,
      days: daysNumber,
      expires_at: expires,
      ...(note.trim() ? { note: note.trim() } : {}),
    });
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.field}>
        <label htmlFor="invite-protocol" className={styles.label}>
          {t("bot.invites.create.protocol")}
        </label>
        <select
          id="invite-protocol"
          className={styles.control}
          value={protocolId}
          onChange={(event) => setProtocolId(event.target.value)}
          required
        >
          <option value="">{t("bot.protocolPlaceholder")}</option>
          {protocols.map((protocol) => (
            <option key={protocol.id} value={protocol.id}>
              {protocolLabel(protocol)}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor="invite-days" className={styles.label}>
          {t("bot.invites.create.days")}
        </label>
        <input
          id="invite-days"
          type="number"
          className={styles.control}
          value={days}
          onChange={(event) => setDays(event.target.value)}
          min={1}
          max={3650}
          required
        />
      </div>
      <div className={styles.field}>
        <label htmlFor="invite-validity" className={styles.label}>
          {t("bot.invites.create.validity")}
        </label>
        <select
          id="invite-validity"
          className={styles.control}
          value={validity}
          onChange={(event) => setValidity(event.target.value)}
        >
          {VALIDITY_PRESETS.map((count) => (
            <option key={count} value={String(count)}>
              {t("bot.invites.create.validityDays", { count })}
            </option>
          ))}
          <option value={CUSTOM}>{t("bot.invites.create.custom")}</option>
        </select>
      </div>
      {custom && (
        <div className={styles.field}>
          <label htmlFor="invite-day" className={styles.label}>
            {t("bot.invites.create.date")}
          </label>
          <input
            id="invite-day"
            type="date"
            className={styles.control}
            value={day}
            onChange={(event) => setDay(event.target.value)}
            min={localDay(new Date())}
            required
          />
        </div>
      )}
      <div className={styles.field}>
        <label htmlFor="invite-note" className={styles.label}>
          {t("bot.invites.create.note")}
        </label>
        <input
          id="invite-note"
          className={styles.control}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          maxLength={NOTE_MAX}
          autoComplete="off"
        />
      </div>
      {error && <Notice>{error}</Notice>}
      <DialogActions>
        <Button variant="quiet" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" busy={busy} busyLabel={t("bot.invites.create.busy")} disabled={!valid}>
          {t("bot.invites.create.submit")}
        </Button>
      </DialogActions>
    </form>
  );
}
