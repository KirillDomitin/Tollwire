import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError, unwrap, type Job } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { TextField } from "../../components/TextField";
import i18n from "../../i18n";
import { isUnfinished } from "../../lib/jobs";
import { addDays, endOfLocalDay, isValidDay, localDay, timeAgo } from "../../lib/time";
import {
  conflictReason,
  IMPORT_MAX_CLIENTS,
  importSummary,
  isImportable,
  type AppProtocol,
  type AppProtocolType,
  type DiscoveredClient,
  type DiscoveredProtocol,
  type ImportResult,
} from "./discovery";
import styles from "./ImportClientsDialog.module.css";
import { fetchJobs, keys } from "./useServer";

const JOB_POLL_MS = 2_000;
const DEFAULT_TERM_DAYS = 30;
// Потолок срока: дата вроде «20261-01-01» (поле даты браузера её допускает) не должна дойти до запроса.
const MAX_TERM_YEARS = 10;
const USERNAME_MAX = 100;
const EMAIL_MAX = 100;
// Простая проверка формы адреса: точную делает бэкенд (422), здесь важно поймать опечатку до запроса.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
// Управляющие знаки бэкенд не принимает в имени (они попадают в параметры задачи).
const CONTROL_CHARS = /[\p{Cc}\p{Cs}]/u;
const CONTROL_CHARS_ALL = /[\p{Cc}\p{Cs}]/gu;

// Длину считаем в кодовых точках, как бэкенд (pydantic): эмодзи — один знак, а не два.
const length = (value: string) => Array.from(value).length;
const truncate = (value: string, max: number) => Array.from(value).slice(0, max).join("");

/** Подпись клиента для людей: адрес, иначе имя из приложения, иначе начало ключа. */
export function clientTitle(client: DiscoveredClient): string {
  return client.address ?? client.name ?? client.public_key.slice(0, 8);
}

interface Row {
  selected: boolean;
  username: string;
  email: string;
}

/** Имя по умолчанию — имя из приложения (без управляющих знаков, до 100 символов) или «Client N». */
function defaultUsername(client: DiscoveredClient, index: number): string {
  const name = truncate((client.name ?? "").replace(CONTROL_CHARS_ALL, "").trim(), USERNAME_MAX);
  return name || i18n.t("servers.import.defaultName", { number: index + 1 });
}

/** Строка клиента, которого в состоянии ещё нет (он появился в отчёте уже после открытия диалога): не выбран. */
function freshRow(client: DiscoveredClient, index: number): Row {
  return { selected: false, username: defaultUsername(client, index), email: "" };
}

function rowOf(rows: Record<string, Row>, client: DiscoveredClient, index: number): Row {
  return rows[client.public_key] ?? freshRow(client, index);
}

/** Сразу выбраны новые клиенты, но не больше, чем принимает один запрос. */
function initialRows(protocol: DiscoveredProtocol): Record<string, Row> {
  let picked = 0;
  return Object.fromEntries(
    protocol.clients.map((client, index) => {
      const select = client.status === "new" && picked < IMPORT_MAX_CLIENTS;
      if (select) picked += 1;
      return [client.public_key, { ...freshRow(client, index), selected: select }];
    }),
  );
}

interface Problems {
  /** Общая проблема формы (никого не выбрали, слишком много). */
  form: string | null;
  date: string | null;
  fields: Record<string, { username?: string; email?: string }>;
}

/** Число полей с ошибками — для сводки над кнопкой отправки. */
function fieldErrorCount(problems: Problems): number {
  return (
    (problems.date ? 1 : 0) +
    Object.values(problems.fields).reduce(
      (sum, issues) => sum + (issues.username ? 1 : 0) + (issues.email ? 1 : 0),
      0,
    )
  );
}

function validate(
  protocol: DiscoveredProtocol,
  rows: Record<string, Row>,
  day: string,
  minDay: string,
  maxDay: string,
): Problems | null {
  const problems: Problems = { form: null, date: null, fields: {} };
  const chosen = protocol.clients
    .map((client, index) => ({ client, row: rowOf(rows, client, index) }))
    .filter(({ client, row }) => isImportable(client) && row.selected);
  if (chosen.length === 0) problems.form = i18n.t("servers.import.problems.none");
  else if (chosen.length > IMPORT_MAX_CLIENTS) {
    problems.form = i18n.t("servers.import.problems.tooMany", { max: IMPORT_MAX_CLIENTS });
  }
  if (!isValidDay(day) || day < minDay || day > maxDay)
    problems.date = i18n.t("servers.import.problems.date");

  const seenEmails = new Set<string>();
  for (const { client, row } of chosen) {
    const username = row.username.trim();
    const email = row.email.trim();
    const issues: { username?: string; email?: string } = {};
    if (!username || length(username) > USERNAME_MAX || CONTROL_CHARS.test(username)) {
      issues.username = i18n.t("servers.import.problems.username");
    }
    if (email) {
      if (email.length > EMAIL_MAX || !EMAIL_PATTERN.test(email)) {
        issues.email = i18n.t("servers.import.problems.email");
      } else if (seenEmails.has(email.toLowerCase())) {
        issues.email = i18n.t("servers.import.problems.emailDuplicate");
      }
      seenEmails.add(email.toLowerCase());
    }
    if (issues.username || issues.email) problems.fields[client.public_key] = issues;
  }
  const clean = !problems.form && !problems.date && Object.keys(problems.fields).length === 0;
  return clean ? null : problems;
}

/** 422 бэкенда с указанием, в каких полях ошибка: подсветить нужные строки, а не только общий текст. */
class ImportRejected extends ApiError {
  constructor(
    message: string,
    readonly fields: Record<string, { username?: boolean; email?: boolean }>,
    readonly date: boolean,
  ) {
    super(message, 422);
  }
}

/** Разбор `detail[].loc` ответа 422: `["body", "clients", N, "email"]` — поле email клиента номер N из запроса. */
function rejection(error: unknown, sent: { public_key: string }[], message: string): ImportRejected {
  const fields: ImportRejected["fields"] = {};
  let date = false;
  const detail = (error as { detail?: unknown } | null | undefined)?.detail;
  for (const item of Array.isArray(detail) ? detail : []) {
    const loc: unknown = (item as { loc?: unknown } | null)?.loc;
    if (!Array.isArray(loc)) continue;
    if (loc.includes("valid_until")) date = true;
    const at = loc.indexOf("clients");
    const index = at >= 0 ? loc[at + 1] : undefined;
    const field = at >= 0 ? loc[at + 2] : undefined;
    const key = typeof index === "number" ? sent[index]?.public_key : undefined;
    if (key && (field === "username" || field === "email")) fields[key] = { ...fields[key], [field]: true };
  }
  return new ImportRejected(message, fields, date);
}

function problemsFromRejection(error: ImportRejected): Problems | null {
  const fields: Problems["fields"] = {};
  for (const [key, flags] of Object.entries(error.fields)) {
    fields[key] = {
      ...(flags.username ? { username: i18n.t("servers.import.problems.username") } : {}),
      ...(flags.email ? { email: i18n.t("servers.import.problems.email") } : {}),
    };
  }
  const date = error.date ? i18n.t("servers.import.problems.date") : null;
  return date || Object.keys(fields).length > 0 ? { form: null, date, fields } : null;
}

/**
 * Перенос клиентов установленной AmneziaVPN в панель. Панель создаёт пользователя, доступ и запись о клиенте,
 * но не меняет сервер и не получает приватных ключей, поэтому конфиг из панели показать нельзя. Срок доступа
 * задаётся один на всех выбранных. После отправки диалог следит за задачей и показывает итог.
 */
export function ImportClientsDialog({
  serverId,
  protocols,
  open,
  onOpenChange,
}: {
  serverId: string;
  protocols: AppProtocol[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={t("servers.import.title")}
      description={t("servers.import.description")}
      wide
    >
      {/* Содержимое монтируется только при открытом диалоге: выбор и введённые имена не переживают закрытие. */}
      <ImportFlow serverId={serverId} protocols={protocols} onClose={() => onOpenChange(false)} />
    </Dialog>
  );
}

function ImportFlow({
  serverId,
  protocols,
  onClose,
}: {
  serverId: string;
  protocols: AppProtocol[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [chosenType, setChosenType] = useState<AppProtocolType | null>(null);
  const [started, setStarted] = useState<Job | null>(null);
  const protocol = protocols.find((item) => item.type === chosenType) ?? protocols[0];

  if (!protocol) return <Notice>{t("servers.import.errors.conflict")}</Notice>;
  if (started)
    return <ImportProgress serverId={serverId} started={started} protocol={protocol} onClose={onClose} />;

  return (
    <>
      {protocols.length > 1 && (
        <fieldset className={styles.choice}>
          <legend className={styles.label}>{t("servers.import.protocol")}</legend>
          {protocols.map((item) => (
            <label key={item.type}>
              <input
                type="radio"
                name="protocol"
                checked={item.type === protocol.type}
                onChange={() => setChosenType(item.type)}
              />{" "}
              {t(`servers.discovery.protocols.${item.type}`)}
            </label>
          ))}
        </fieldset>
      )}
      {/* key: другая установка — другие клиенты, форма начинается заново. */}
      <ImportForm
        key={protocol.type}
        serverId={serverId}
        protocol={protocol}
        onStarted={setStarted}
        onCancel={onClose}
      />
    </>
  );
}

function ImportForm({
  serverId,
  protocol,
  onStarted,
  onCancel,
}: {
  serverId: string;
  protocol: AppProtocol;
  onStarted: (job: Job) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  // Границы и значение по умолчанию считаются один раз при открытии: «завтра» не должно дрейфовать под руками.
  const [minDay] = useState(() => localDay(addDays(new Date(), 1)));
  const [maxDay] = useState(() => {
    const limit = new Date();
    limit.setFullYear(limit.getFullYear() + MAX_TERM_YEARS);
    return localDay(limit);
  });
  const [day, setDay] = useState(() => localDay(addDays(new Date(), DEFAULT_TERM_DAYS)));
  const [rows, setRows] = useState(() => initialRows(protocol));
  const [attempted, setAttempted] = useState(false);
  // Каждая неудачная отправка двигает счётчик: по нему фокус уходит на первое поле с ошибкой.
  const [failedSubmits, setFailedSubmits] = useState(0);

  const start = useMutation({
    mutationFn: async (body: {
      protocol_type: AppProtocolType;
      valid_until: string;
      clients: ClientBody[];
    }): Promise<Job> => {
      let result;
      try {
        result = await api.POST("/api/v1/servers/{server_id}/import", {
          params: { path: { server_id: serverId } },
          body,
        });
      } catch {
        throw new ApiError(i18n.t("common.errors.network"), 0);
      }
      if (result.response.status === 422) {
        throw rejection(result.error, body.clients, t("servers.import.errors.invalid"));
      }
      return unwrap(Promise.resolve(result), {
        404: t("servers.import.errors.notFound"),
        409: t("servers.import.errors.conflict"),
      });
    },
    onSuccess: (job) => {
      // Задача сразу попадает в журнал сервера и опрашивается вместе с остальными.
      queryClient.setQueryData<Job[]>(keys.jobs(serverId), (jobs = []) => [job, ...jobs]);
      onStarted(job);
    },
  });

  // Ошибки либо от собственных проверок (после первой попытки отправки), либо из ответа 422 бэкенда.
  const rejected = start.error instanceof ImportRejected ? start.error : null;
  const problems =
    (attempted ? validate(protocol, rows, day, minDay, maxDay) : null) ??
    (rejected ? problemsFromRejection(rejected) : null);
  const errorCount = problems ? fieldErrorCount(problems) : 0;

  useEffect(() => {
    if (failedSubmits === 0 && !rejected) return;
    formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [failedSubmits, rejected]);

  const selectedCount = protocol.clients.filter(
    (client, index) => isImportable(client) && rowOf(rows, client, index).selected,
  ).length;
  const overLimit =
    selectedCount > IMPORT_MAX_CLIENTS || protocol.clients.filter(isImportable).length > IMPORT_MAX_CLIENTS;

  function changed() {
    // Правка снимает прежнюю ошибку сервера: подсветка относилась к старым значениям.
    if (start.isError) start.reset();
  }

  function update(client: DiscoveredClient, index: number, patch: Partial<Row>) {
    changed();
    setRows((current) => ({
      ...current,
      [client.public_key]: { ...rowOf(current, client, index), ...patch },
    }));
  }

  function selectNew() {
    changed();
    setRows((current) => {
      const next = { ...current };
      let picked = protocol.clients.filter(
        (client, index) => isImportable(client) && rowOf(current, client, index).selected,
      ).length;
      protocol.clients.forEach((client, index) => {
        const row = rowOf(current, client, index);
        if (client.status === "new" && !row.selected && picked < IMPORT_MAX_CLIENTS) {
          next[client.public_key] = { ...row, selected: true };
          picked += 1;
        }
      });
      return next;
    });
  }

  function clearSelection() {
    changed();
    setRows((current) =>
      Object.fromEntries(Object.entries(current).map(([key, row]) => [key, { ...row, selected: false }])),
    );
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAttempted(true);
    if (validate(protocol, rows, day, minDay, maxDay)) {
      setFailedSubmits((count) => count + 1);
      return;
    }
    const clients = protocol.clients.flatMap((client, index): ClientBody[] => {
      const row = rowOf(rows, client, index);
      if (!isImportable(client) || !row.selected) return [];
      const email = row.email.trim();
      // Пустой email не отправляем вовсе: у клиента приложения его может не быть.
      return [
        email
          ? { public_key: client.public_key, username: row.username.trim(), email }
          : { public_key: client.public_key, username: row.username.trim() },
      ];
    });
    start.mutate({ protocol_type: protocol.type, valid_until: endOfLocalDay(day), clients });
  }

  // noValidate: сообщения свои, на языке интерфейса, а не браузерные на языке системы.
  return (
    <form ref={formRef} className={styles.form} onSubmit={submit} noValidate>
      <TextField
        label={t("servers.import.validUntil")}
        type="date"
        name="valid_until"
        min={minDay}
        max={maxDay}
        value={day}
        onChange={(event) => {
          changed();
          setDay(event.target.value);
        }}
        error={problems?.date}
        required
      />
      <p className={styles.hint}>{t("servers.import.validUntilHint")}</p>

      {/* Постоянное пояснение, а не сообщение об ошибке: role="alert" оставлен для настоящих ошибок. */}
      <p className={styles.note}>{t("servers.import.warning")}</p>

      <div className={styles.toolbar}>
        <Button variant="quiet" onClick={selectNew}>
          {t("servers.import.selectNew")}
        </Button>
        <Button variant="quiet" onClick={clearSelection}>
          {t("servers.import.clearSelection")}
        </Button>
        <span className={styles.hint} role="status">
          {t("servers.import.selected", { count: selectedCount })}
        </span>
      </div>
      {overLimit && <p className={styles.hint}>{t("servers.import.limit", { max: IMPORT_MAX_CLIENTS })}</p>}

      <ul className={styles.clients} aria-label={t("servers.import.clients")}>
        {protocol.clients.map((client, index) => (
          <ClientRow
            key={client.public_key}
            client={client}
            row={rowOf(rows, client, index)}
            problems={problems?.fields[client.public_key]}
            onChange={(patch) => update(client, index, patch)}
          />
        ))}
      </ul>

      {errorCount > 0 && <Notice>{t("servers.import.problems.summary", { count: errorCount })}</Notice>}
      {problems?.form && <Notice>{problems.form}</Notice>}
      {start.isError && <Notice>{start.error.message}</Notice>}
      <DialogActions>
        <Button variant="quiet" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" busy={start.isPending} busyLabel={t("servers.import.busy")}>
          {t("servers.import.submit")}
        </Button>
      </DialogActions>
    </form>
  );
}

type ClientBody = { public_key: string; username: string; email?: string };

function ClientRow({
  client,
  row,
  problems,
  onChange,
}: {
  client: DiscoveredClient;
  row: Row;
  problems: { username?: string; email?: string } | undefined;
  onChange: (patch: Partial<Row>) => void;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const importable = isImportable(client);
  const status = client.status;
  const known =
    status === "new" || status === "in_panel" || status === "revoked_in_panel" || status === "unsupported";
  const why = status === "in_panel" || status === "unsupported" ? t(`servers.import.why.${status}`) : null;

  return (
    <li className={styles.client} data-status={status} data-selected={row.selected && importable}>
      <div className={styles.clientHead}>
        <label className={styles.pick}>
          <input
            type="checkbox"
            checked={importable && row.selected}
            disabled={!importable}
            aria-label={t("servers.import.pick", { address: clientTitle(client) })}
            onChange={(event) => onChange({ selected: event.target.checked })}
          />
          <span id={titleId} className={styles.address}>
            {client.address ?? t("servers.import.noAddress")}
          </span>
        </label>
        <span className={styles.badge} data-status={status}>
          {known ? t(`servers.import.status.${status}`) : status}
        </span>
      </div>
      <p className={styles.meta}>
        {t("servers.import.appName", { name: client.name || t("servers.import.noName") })}
        {" · "}
        {client.latest_handshake
          ? t("servers.import.handshake", { when: timeAgo(client.latest_handshake) })
          : t("servers.import.neverConnected")}
      </p>
      {why && <p className={styles.hint}>{why}</p>}
      {importable && row.selected && (
        // Группа названа адресом клиента: у тридцати строк одинаковые подписи полей, и по ним не отличить, чьи они.
        <div className={styles.fields} role="group" aria-labelledby={titleId}>
          <TextField
            label={t("servers.import.username")}
            name={`username-${client.public_key}`}
            value={row.username}
            autoComplete="off"
            onChange={(event) => onChange({ username: event.target.value })}
            error={problems?.username}
          />
          <TextField
            label={t("servers.import.email")}
            name={`email-${client.public_key}`}
            type="email"
            value={row.email}
            autoComplete="off"
            onChange={(event) => onChange({ email: event.target.value })}
            error={problems?.email}
          />
        </div>
      )}
    </li>
  );
}

/** Ход задачи переноса и её итог. Закрыть окно можно в любой момент — задача продолжится, итог будет в журнале. */
function ImportProgress({
  serverId,
  started,
  protocol,
  onClose,
}: {
  serverId: string;
  started: Job;
  protocol: AppProtocol;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  // Тот же запрос, что у журнала задач на карточке (один ключ — один опрос), в нём берётся только наша задача.
  const poll = useQuery({
    queryKey: keys.jobs(serverId),
    queryFn: () => fetchJobs(serverId),
    select: (jobs) => jobs.find((item) => item.id === started.id) ?? started,
    refetchInterval: (query) => (query.state.data?.some(isUnfinished) ? JOB_POLL_MS : false),
  });
  const job = poll.data ?? started;
  const finished = !isUnfinished(job);

  // Перенос закончился: отчёт поиска (статусы клиентов), протоколы, пользователи и журнал задач устарели.
  useEffect(() => {
    if (!finished) return;
    void queryClient.invalidateQueries({ queryKey: keys.discovery(serverId) });
    void queryClient.invalidateQueries({ queryKey: keys.protocols(serverId) });
    void queryClient.invalidateQueries({ queryKey: keys.jobs(serverId) });
    void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
  }, [finished, serverId, queryClient]);

  const result = (job.result ?? null) as ImportResult | null;
  const summary = importSummary(result);
  const titles = new Map(protocol.clients.map((client) => [client.public_key, clientTitle(client)]));

  return (
    <div className={styles.form}>
      {!finished && (
        <p className={styles.progress} role="status">
          {t("servers.import.progress")}
        </p>
      )}
      {poll.isError && !finished && <Notice>{poll.error.message}</Notice>}
      {job.status === "failed" && (
        <Notice>
          {job.error ? t("servers.import.failed", { error: job.error }) : t("servers.import.failedNoText")}
        </Notice>
      )}
      {job.status === "success" && (
        <div role="status">
          <h3 className={styles.resultTitle}>{t("servers.import.resultTitle")}</h3>
          {summary.length > 0 ? (
            <ul className={styles.result}>
              {summary.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          ) : (
            <p>{t("servers.import.result.nothing")}</p>
          )}
          {(result?.conflict?.length ?? 0) > 0 && (
            <ul className={styles.conflicts}>
              {result?.conflict?.map((item) => (
                <li key={item.public_key}>
                  <strong>{titles.get(item.public_key) ?? item.public_key.slice(0, 8)}</strong>:{" "}
                  {conflictReason(item.reason)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <DialogActions>
        <Button onClick={onClose}>{finished ? t("servers.import.done") : t("servers.import.close")}</Button>
      </DialogActions>
    </div>
  );
}
