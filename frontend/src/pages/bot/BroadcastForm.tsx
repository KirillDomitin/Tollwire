import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type Schemas } from "../../api/client";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { MAX_VISIBLE, validateTelegramHtml } from "../../lib/telegramHtml";
import { botUnwrap } from "./botErrors";
import styles from "./Bot.module.css";
import { protocolLabel } from "./protocols";
import { TelegramPreview } from "./TelegramPreview";
import { botKeys, useBotProtocols, type AudienceRequest, type Recipient } from "./useBot";

type Kind = AudienceRequest["kind"];
type Target = AudienceRequest["target"];
type BroadcastCreate = Schemas["BroadcastCreate"];

const SEARCH_DELAY_MS = 250;
const PEOPLE_LIMIT = 50;
const TARGETS: Record<Kind, Target[]> = {
  news: ["all", "protocol", "selected"],
  config: ["protocol", "selected"],
};

/**
 * Новая рассылка: вид (новость / ссылки на конфиг), текст с предпросмотром и счётчиком, кому, число получателей и
 * подтверждение. Что получит каждый, решает панель: число берётся из `broadcasts/audience`, а не считается здесь.
 */
export function BroadcastForm() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const ids = { text: useId(), counter: useId(), error: useId(), preview: useId() };

  const [kind, setKind] = useState<Kind>("news");
  const [target, setTarget] = useState<Target>("all");
  const [text, setText] = useState("");
  const [protocolId, setProtocolId] = useState("");
  const [picked, setPicked] = useState<Map<string, Recipient>>(() => new Map());
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");

  const news = kind === "news";
  const check = useMemo(() => validateTelegramHtml(text), [text]);
  const htmlError = news && text !== "" && !check.ok ? check.code : undefined;
  const textReady = !news || check.ok;

  // Поиск людей идёт с небольшой задержкой: по запросу на каждую букву не нужно.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  const protocols = useBotProtocols(target === "protocol");
  const people = useQuery({
    queryKey: botKeys.recipients(query),
    queryFn: () =>
      botUnwrap(
        api.GET("/api/v1/bot-admin/recipients", {
          params: { query: { search: query, limit: PEOPLE_LIMIT } },
        }),
      ),
    enabled: target === "selected" && query !== "",
    placeholderData: keepPreviousData,
  });

  // Кому: один и тот же набор полей уходит и в подсчёт получателей, и в саму рассылку.
  const audienceBody = useMemo<AudienceRequest | null>(() => {
    if (target === "all") return { kind, target };
    if (target === "protocol") return protocolId ? { kind, target, protocol_id: protocolId } : null;
    return picked.size > 0 ? { kind, target, user_ids: [...picked.keys()] } : null;
  }, [kind, target, protocolId, picked]);

  const audience = useQuery({
    queryKey: botKeys.audience(audienceBody ?? { kind, target }),
    queryFn: () => botUnwrap(api.POST("/api/v1/bot-admin/broadcasts/audience", { body: audienceBody! })),
    enabled: audienceBody !== null,
  });
  const recipients = audience.data?.recipients;
  const canSend = textReady && recipients !== undefined && recipients > 0 && audienceBody !== null;

  // Один request_id на одно и то же содержимое: повтор после ошибки не создаст вторую рассылку.
  const request = useRef<{ key: string; id: string } | null>(null);
  const send = useMutation({
    mutationFn: (body: BroadcastCreate) => botUnwrap(api.POST("/api/v1/bot-admin/broadcasts", { body })),
  });

  async function confirm() {
    if (!audienceBody) return;
    const base = { ...audienceBody, ...(news ? { text } : {}) };
    const key = JSON.stringify(base);
    if (request.current?.key !== key) request.current = { key, id: crypto.randomUUID() };
    await send.mutateAsync({ ...base, request_id: request.current.id });
    request.current = null;
    setText("");
    setPicked(new Map());
    setSearch("");
    await queryClient.invalidateQueries({ queryKey: botKeys.broadcasts });
  }

  function chooseKind(next: Kind) {
    setKind(next);
    if (!TARGETS[next].includes(target)) setTarget(TARGETS[next][0] ?? "protocol");
  }

  function toggle(person: Recipient, on: boolean) {
    setPicked((current) => {
      const next = new Map(current);
      if (on) next.set(person.id, person);
      else next.delete(person.id);
      return next;
    });
  }

  const shown = query === "" ? [] : (people.data ?? []);
  const listed = [...shown, ...[...picked.values()].filter((p) => !shown.some((s) => s.id === p.id))];
  const describedBy = [ids.counter, htmlError ? ids.error : null].filter(Boolean).join(" ");

  return (
    <div className={styles.form}>
      <h3 className={styles.subTitle}>{t("bot.broadcasts.new")}</h3>

      <fieldset className={styles.choice}>
        <legend>{t("bot.broadcasts.kind.legend")}</legend>
        {(["news", "config"] as const).map((item) => (
          <label key={item}>
            <input
              type="radio"
              name="broadcast-kind"
              checked={kind === item}
              onChange={() => chooseKind(item)}
            />
            {t(`bot.broadcasts.kind.${item}`)}
          </label>
        ))}
      </fieldset>

      {news && (
        <>
          <div className={styles.field}>
            <label htmlFor={ids.text} className={styles.label}>
              {t("bot.broadcasts.text")}
            </label>
            <textarea
              id={ids.text}
              className={`${styles.control} ${styles.textarea}`}
              value={text}
              onChange={(event) => setText(event.target.value)}
              aria-describedby={describedBy}
              aria-invalid={htmlError ? true : undefined}
              spellCheck
            />
            <p id={ids.counter} className={styles.counter} data-over={check.visible_length > MAX_VISIBLE}>
              {t("bot.broadcasts.counter", { used: check.visible_length, max: MAX_VISIBLE })}
            </p>
            {htmlError && (
              <p id={ids.error} className={styles.fieldError}>
                {t(`bot.html.${htmlError.replace("telegram_html_", "")}` as "bot.html.tag")}
              </p>
            )}
            <p className={styles.hint}>{t("bot.broadcasts.textHint")}</p>
          </div>
          <section aria-labelledby={ids.preview} className={styles.field}>
            <h3 id={ids.preview} className={styles.label}>
              {t("bot.broadcasts.preview")}
            </h3>
            <TelegramPreview html={text} />
          </section>
        </>
      )}

      <fieldset className={styles.choice}>
        <legend>{t("bot.broadcasts.target.legend")}</legend>
        {TARGETS[kind].map((item) => (
          <label key={item}>
            <input
              type="radio"
              name="broadcast-target"
              checked={target === item}
              onChange={() => setTarget(item)}
            />
            {t(`bot.broadcasts.target.${item}`)}
          </label>
        ))}
      </fieldset>

      {target === "protocol" && (
        <div className={styles.field}>
          <label htmlFor="broadcast-protocol" className={styles.label}>
            {t("bot.broadcasts.protocol")}
          </label>
          <select
            id="broadcast-protocol"
            className={styles.control}
            value={protocolId}
            onChange={(event) => setProtocolId(event.target.value)}
          >
            <option value="">{t("bot.protocolPlaceholder")}</option>
            {(protocols.data ?? []).map((protocol) => (
              <option key={protocol.id} value={protocol.id}>
                {protocolLabel(protocol)}
              </option>
            ))}
          </select>
          {protocols.isError && <p className={styles.fieldError}>{protocols.error.message}</p>}
        </div>
      )}

      {target === "selected" && (
        <div className={styles.field}>
          <label htmlFor="broadcast-search" className={styles.label}>
            {t("bot.broadcasts.search")}
          </label>
          <input
            id="broadcast-search"
            type="search"
            className={styles.control}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            autoComplete="off"
          />
          {query === "" && picked.size === 0 && (
            <p className={styles.hint}>{t("bot.broadcasts.searchHint")}</p>
          )}
          {people.isError && <p className={styles.fieldError}>{people.error.message}</p>}
          {listed.length > 0 && (
            <ul className={styles.people}>
              {listed.map((person) => (
                <li key={person.id}>
                  <label>
                    <input
                      type="checkbox"
                      checked={picked.has(person.id)}
                      onChange={(event) => toggle(person, event.target.checked)}
                    />
                    <span>{person.username}</span>{" "}
                    {person.telegram_username && (
                      <span className={styles.hint}>@{person.telegram_username}</span>
                    )}{" "}
                    {person.bot_blocked_at && (
                      <span className={styles.blocked}>{t("bot.broadcasts.blockedBot")}</span>
                    )}
                  </label>
                </li>
              ))}
            </ul>
          )}
          {query !== "" && people.data && people.data.length === 0 && (
            <p className={styles.hint}>{t("bot.broadcasts.nobody")}</p>
          )}
        </div>
      )}

      {audience.data && (audience.data.without_telegram > 0 || audience.data.blocked > 0) && (
        <p className={styles.hint}>
          {t("bot.broadcasts.audienceNote", {
            withoutTelegram: audience.data.without_telegram,
            blocked: audience.data.blocked,
          })}
        </p>
      )}
      {audience.isError && <p className={styles.fieldError}>{audience.error.message}</p>}

      <div className={styles.row}>
        <ConfirmDialog
          trigger={
            <Button disabled={!canSend}>
              {recipients === undefined
                ? t("bot.broadcasts.send")
                : t("bot.broadcasts.sendTo", { count: recipients })}
            </Button>
          }
          title={t("bot.broadcasts.confirm.title")}
          consequences={
            <>
              {t("bot.broadcasts.confirm.who", {
                recipients: t("bot.broadcasts.recipients", { count: recipients ?? 0 }),
              })}{" "}
              {news ? t("bot.broadcasts.confirm.news") : t("bot.broadcasts.confirm.config")}
            </>
          }
          actionLabel={t("bot.broadcasts.confirm.action")}
          busyLabel={t("bot.broadcasts.confirm.busy")}
          onConfirm={confirm}
        />
      </div>
    </div>
  );
}
