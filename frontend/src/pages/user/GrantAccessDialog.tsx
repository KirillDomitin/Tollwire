import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { api, unwrap, type Grant, type Job, type Server, type Subscription } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { isLive } from "../../lib/access";
import styles from "./UserPage.module.css";
import { userKeys } from "./useUser";

const PRESETS = [
  { days: 30, label: "30 дней" },
  { days: 90, label: "3 месяца" },
  { days: 365, label: "Год" },
] as const;

const DAY_MS = 24 * 60 * 60 * 1000;

/** Конец выбранного дня по местному времени — «до 3 ноября» значит «весь день 3 ноября». */
export function endOfDay(date: string): string {
  return new Date(`${date}T23:59:59`).toISOString();
}

/**
 * Период нового доступа: с этой минуты на N дней или до выбранного дня. Начало — минутой раньше: если часы
 * браузера чуть впереди сервера, выдача конфига не упрётся в «период ещё не начался».
 */
export function newPeriod(choice: number | "date", untilDate: string, now: Date = new Date()) {
  const validFrom = new Date(now.getTime() - 60_000);
  const validUntil =
    choice === "date" ? endOfDay(untilDate) : new Date(validFrom.getTime() + choice * DAY_MS).toISOString();
  return { valid_from: validFrom.toISOString(), valid_until: validUntil };
}

/**
 * Выдать доступ одним действием: срок (если доступа ещё нет) и серверы. Панель сама создаёт подписку, привязывает
 * серверы и ставит задачи выдачи конфигов — каждый шаг тот же, что в API, просто без лишних щелчков.
 */
export function GrantAccessDialog({
  userId,
  current,
  grants,
  servers,
  onIssued,
  open,
  onOpenChange,
}: {
  userId: string;
  current: Subscription | null;
  grants: Grant[];
  servers: Server[];
  onIssued: (grantId: string, job: Job) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [preset, setPreset] = useState<number | "date">(30);
  const [untilDate, setUntilDate] = useState("");
  const [selected, setSelected] = useState<string[]>([]);

  // Выдать можно только там, где стоит AmneziaWG и сервер готов; уже выданные серверы не предлагаются.
  const granted = new Set(grants.filter(isLive).map((grant) => grant.server_id));
  const candidates = servers.filter((server) => server.status === "ready" && !granted.has(server.id));
  const protocols = useQueries({
    queries: candidates.map((server) => ({
      queryKey: ["server", server.id, "protocols"],
      queryFn: () =>
        unwrap(
          api.GET("/api/v1/servers/{server_id}/protocols", { params: { path: { server_id: server.id } } }),
        ),
      enabled: open,
    })),
  });
  const available = candidates.flatMap((server, index) => {
    const protocol = protocols[index]?.data?.[0];
    return protocol ? [{ server, protocolId: protocol.id }] : [];
  });

  const grant = useMutation({
    mutationFn: async () => {
      let subscription = current;
      if (!subscription) {
        subscription = await unwrap(
          api.POST("/api/v1/vpn-users/{user_id}/subscriptions", {
            params: { path: { user_id: userId } },
            body: newPeriod(preset, untilDate),
          }),
          {
            409: "У пользователя уже есть действующий доступ — обновите страницу.",
            422: "Проверьте срок доступа.",
          },
        );
      }
      for (const { server, protocolId } of available.filter((item) => selected.includes(item.server.id))) {
        const created = await unwrap(
          api.POST("/api/v1/subscriptions/{subscription_id}/grants", {
            params: { path: { subscription_id: subscription.id } },
            body: { protocol_id: protocolId },
          }),
          { 409: `Доступ к серверу ${server.name} уже выдан.` },
        );
        const job = await unwrap(
          api.POST("/api/v1/access/{access_grant_id}/credentials", {
            params: { path: { access_grant_id: created.id } },
          }),
          { 409: `Конфиг для ${server.name} уже выдаётся или период доступа ещё не начался.` },
        );
        onIssued(created.id, job);
      }
    },
    // Даже если упал шаг посередине, уже сделанное надо показать на карточке.
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: userKeys.subscriptions(userId) });
      void queryClient.invalidateQueries({ queryKey: ["subscription"] });
      void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
    },
    onSuccess: () => close(false),
  });

  function close(next: boolean) {
    if (!next) {
      grant.reset();
      setSelected([]);
    }
    onOpenChange(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    grant.mutate();
  }

  const loading = protocols.some((query) => query.isPending);
  const ready = selected.length > 0 && (current !== null || preset !== "date" || untilDate !== "");

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={current ? "Добавить серверы" : "Выдать доступ"}
      description={
        current
          ? "Серверы добавятся к текущему доступу, срок останется прежним."
          : "Панель создаст доступ на выбранный срок и выдаст конфиги для выбранных серверов."
      }
    >
      <form className={styles.form} onSubmit={submit}>
        {!current && (
          <fieldset className={styles.choice}>
            <legend className={styles.label}>Срок</legend>
            {PRESETS.map(({ days, label }) => (
              <label key={days}>
                <input
                  type="radio"
                  name="preset"
                  checked={preset === days}
                  onChange={() => setPreset(days)}
                />
                {label}
              </label>
            ))}
            <label>
              <input
                type="radio"
                name="preset"
                checked={preset === "date"}
                onChange={() => setPreset("date")}
              />
              До даты
            </label>
            {preset === "date" && (
              <input
                type="date"
                aria-label="Последний день доступа"
                className={styles.date}
                required
                min={new Date().toISOString().slice(0, 10)}
                value={untilDate}
                onChange={(event) => setUntilDate(event.target.value)}
              />
            )}
          </fieldset>
        )}

        <fieldset className={styles.servers}>
          <legend className={styles.label}>Серверы</legend>
          {loading && <p className={styles.hint}>Проверяем серверы…</p>}
          {!loading && available.length === 0 && (
            <p className={styles.hint}>
              {granted.size > 0
                ? "Все готовые серверы уже выданы этому пользователю."
                : "Нет готовых серверов с AmneziaWG. Подготовьте сервер в разделе «Серверы»."}
            </p>
          )}
          {available.map(({ server }) => (
            <label key={server.id}>
              <input
                type="checkbox"
                checked={selected.includes(server.id)}
                onChange={(event) =>
                  setSelected((current) =>
                    event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id),
                  )
                }
              />
              {server.name} <span className={styles.address}>{server.host}</span>
            </label>
          ))}
        </fieldset>

        {grant.isError && <Notice>{grant.error.message}</Notice>}
        <DialogActions>
          <Button variant="quiet" onClick={() => close(false)}>
            Отмена
          </Button>
          <Button type="submit" disabled={!ready} busy={grant.isPending} busyLabel="Выдаём…">
            {current ? "Добавить" : "Выдать доступ"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
