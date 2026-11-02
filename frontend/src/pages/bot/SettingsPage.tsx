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

/** Invitation defaults and the panel outage notification threshold. */
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
  const [draft, setDraft] = useState<Partial<Record<"protocolId" | "days" | "valid" | "threshold", string>>>({});
  const protocolId = draft.protocolId ?? settings.default_protocol_id ?? "";
  const days = draft.days ?? String(settings.default_days);
  const valid = draft.valid ?? String(settings.invite_valid_days);
  const threshold = draft.threshold ?? String(settings.panel_down_after_seconds / 60);
  const [saved, setSaved] = useState(false);
  const [invalid, setInvalid] = useState(false);

  const save = useMutation({
    mutationFn: (body: {
      default_protocol_id: string | null;
      default_days: number;
      invite_valid_days: number;
      panel_down_after_seconds: number;
    }) => botUnwrap(api.PUT("/api/v1/bot-admin/settings", { body })),
    onSuccess: (updated) => {
      queryClient.setQueryData(botKeys.settings, updated);
      setDraft({});
      setSaved(true);
      return queryClient.invalidateQueries({ queryKey: botKeys.settings });
    },
  });

  const daysNumber = Number(days);
  const validNumber = Number(valid);
  const thresholdNumber = Number(threshold);
  const seconds = thresholdNumber * 60;
  const thresholdSeconds = draft.threshold === undefined ? settings.panel_down_after_seconds : Math.round(seconds);
  // Decimal minute inputs may acquire a floating-point rounding error when multiplied by 60.
  const wholeSeconds = Math.abs(seconds - thresholdSeconds) <= Number.EPSILON * Math.max(1, Math.abs(seconds)) * 4;
  const ready =
    Number.isInteger(daysNumber) &&
    daysNumber >= 1 &&
    daysNumber <= 3650 &&
    Number.isInteger(validNumber) &&
    validNumber >= 1 &&
    validNumber <= 365 &&
    Number.isFinite(thresholdNumber) &&
    wholeSeconds &&
    thresholdNumber >= 1 &&
    thresholdNumber <= 1440;

  function edit(field: keyof typeof draft) {
    return (value: string) => {
      setDraft((current) => ({ ...current, [field]: value }));
      setSaved(false);
      setInvalid(false);
    };
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready) {
      setInvalid(true);
      return;
    }
    save.mutate({
      default_protocol_id: protocolId || null,
      default_days: daysNumber,
      invite_valid_days: validNumber,
      panel_down_after_seconds: thresholdSeconds,
    });
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <div className={styles.field}>
        <label htmlFor="settings-protocol" className={styles.label}>
          {t("bot.settings.protocol")}
        </label>
        <select
          id="settings-protocol"
          className={styles.control}
          value={protocolId}
          disabled={!manage || save.isPending}
          onChange={(event) => edit("protocolId")(event.target.value)}
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
          disabled={!manage || save.isPending}
          min={1}
          max={3650}
          onChange={(event) => edit("days")(event.target.value)}
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
          disabled={!manage || save.isPending}
          min={1}
          max={365}
          onChange={(event) => edit("valid")(event.target.value)}
        />
        <p className={styles.hint}>{t("bot.settings.validDaysHint")}</p>
      </div>
      <div className={styles.field}>
        <label htmlFor="settings-threshold" className={styles.label}>
          {t("bot.settings.panelDown")}
        </label>
        <input
          id="settings-threshold"
          type="number"
          className={styles.control}
          value={threshold}
          disabled={!manage || save.isPending}
          min={1}
          max={1440}
          step="any"
          aria-describedby="settings-threshold-hint"
          aria-invalid={invalid || undefined}
          onChange={(event) => edit("threshold")(event.target.value)}
        />
        <p id="settings-threshold-hint" className={styles.hint}>
          {t("bot.settings.panelDownHint")}
        </p>
      </div>
      {invalid && <Notice>{t("bot.settings.invalid")}</Notice>}
      {save.isError && <Notice>{errorMessage(save.error)}</Notice>}
      {manage && (
        <div className={styles.row}>
          <Button type="submit" busy={save.isPending} busyLabel={t("bot.settings.saving")}>
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
