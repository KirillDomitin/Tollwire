import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { Notice } from "../../components/Notice";
import { botUnwrap, errorMessage } from "./botErrors";
import styles from "./Bot.module.css";
import { protocolLabel } from "./protocols";
import { failed, Loading, Problem } from "./states";
import { botKeys, useBotProtocols, useBotSettings, type BotProtocol, type BotSettings } from "./useBot";

/**
 * «Настройки» (/bot/settings): что подставляется в приглашение. Порога «панель недоступна» здесь нет: он заработает с
 * уведомлениями (этап 25.5), а показывать настройку, которая ничего не меняет, не нужно.
 */
export function SettingsPage() {
  const { t } = useTranslation();
  const settings = useBotSettings();
  const protocols = useBotProtocols();
  const broken = failed(settings, protocols);

  return (
    <section className={styles.section} aria-labelledby="bot-settings-title">
      <h2 id="bot-settings-title" className={styles.sectionTitle}>
        {t("bot.settings.title")}
      </h2>
      {broken && (
        <Problem
          error={broken.error}
          onRetry={() => {
            void settings.refetch();
            void protocols.refetch();
          }}
        />
      )}
      {!broken && (settings.isPending || protocols.isPending) && <Loading />}
      {!broken && settings.data && protocols.data && (
        <SettingsForm settings={settings.data} protocols={protocols.data} />
      )}
    </section>
  );
}

function SettingsForm({ settings, protocols }: { settings: BotSettings; protocols: BotProtocol[] }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const manage = can("bot:manage");
  const queryClient = useQueryClient();
  const [protocolId, setProtocolId] = useState(settings.default_protocol_id ?? "");
  const [days, setDays] = useState(String(settings.default_days));
  const [valid, setValid] = useState(String(settings.invite_valid_days));
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: (body: {
      default_protocol_id: string | null;
      default_days: number;
      invite_valid_days: number;
    }) => botUnwrap(api.PUT("/api/v1/bot-admin/settings", { body })),
    onSuccess: () => {
      setSaved(true);
      return queryClient.invalidateQueries({ queryKey: botKeys.settings });
    },
  });

  const daysNumber = Number(days);
  const validNumber = Number(valid);
  const ready =
    Number.isInteger(daysNumber) &&
    daysNumber >= 1 &&
    daysNumber <= 3650 &&
    Number.isInteger(validNumber) &&
    validNumber >= 1 &&
    validNumber <= 365;

  function edit<T>(set: (value: T) => void) {
    return (value: T) => {
      set(value);
      setSaved(false);
    };
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready) return;
    save.mutate({
      default_protocol_id: protocolId || null,
      default_days: daysNumber,
      invite_valid_days: validNumber,
    });
  }

  return (
    <form className={styles.form} onSubmit={submit}>
      <div className={styles.field}>
        <label htmlFor="settings-protocol" className={styles.label}>
          {t("bot.settings.protocol")}
        </label>
        <select
          id="settings-protocol"
          className={styles.control}
          value={protocolId}
          disabled={!manage}
          onChange={(event) => edit(setProtocolId)(event.target.value)}
        >
          <option value="">{t("bot.settings.noProtocol")}</option>
          {protocols.map((protocol) => (
            <option key={protocol.id} value={protocol.id}>
              {protocolLabel(protocol)}
            </option>
          ))}
        </select>
      </div>
      <div className={styles.field}>
        <label htmlFor="settings-days" className={styles.label}>
          {t("bot.settings.days")}
        </label>
        <input
          id="settings-days"
          type="number"
          className={styles.control}
          value={days}
          disabled={!manage}
          min={1}
          max={3650}
          onChange={(event) => edit(setDays)(event.target.value)}
        />
        <p className={styles.hint}>{t("bot.settings.daysHint")}</p>
      </div>
      <div className={styles.field}>
        <label htmlFor="settings-valid" className={styles.label}>
          {t("bot.settings.validDays")}
        </label>
        <input
          id="settings-valid"
          type="number"
          className={styles.control}
          value={valid}
          disabled={!manage}
          min={1}
          max={365}
          onChange={(event) => edit(setValid)(event.target.value)}
        />
        <p className={styles.hint}>{t("bot.settings.validDaysHint")}</p>
      </div>
      {save.isError && <Notice>{errorMessage(save.error)}</Notice>}
      {manage && (
        <div className={styles.row}>
          <Button type="submit" busy={save.isPending} busyLabel={t("bot.settings.saving")} disabled={!ready}>
            {t("bot.settings.save")}
          </Button>
          {saved && (
            <p role="status" className={styles.good}>
              {t("bot.settings.saved")}
            </p>
          )}
        </div>
      )}
    </form>
  );
}
