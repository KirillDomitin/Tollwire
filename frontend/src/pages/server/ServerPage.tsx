import { useMutation, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap, type Credential, type Job, type Server } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import { isUnfinished } from "../../lib/jobs";
import { serverSignal } from "../../lib/serverSignal";
import { exactTime, timeAgo } from "../../lib/time";
import { CredentialsDialog } from "./CredentialsDialog";
import { HostKeyDialog } from "./HostKeyDialog";
import { JobsLog } from "./JobsLog";
import styles from "./ServerPage.module.css";
import { keys, useServerData, useStartJob } from "./useServer";

// Графики (uPlot) нужны только на карточке сервера — отдельным файлом, а не в общем бандле панели.
const MonitoringSection = lazy(() =>
  import("./MonitoringSection").then((module) => ({ default: module.MonitoringSection })),
);

const AUTH_LABELS: Record<Credential["auth_type"], string> = {
  password: "по паролю",
  ssh_key: "по SSH-ключу",
};

interface Setup {
  key: boolean;
  credential: boolean;
  installed: boolean;
  protocol: boolean;
}

export function ServerPage() {
  const { serverId = "" } = useParams();
  const { can } = useAuth();
  const { server, credential, protocols, jobs } = useServerData(serverId);

  if (server.isPending) return <p className={styles.hint}>Загружаем сервер…</p>;
  if (server.isError) {
    return (
      <Notice
        action={
          <Link to="/servers" className={styles.back}>
            К списку серверов
          </Link>
        }
      >
        {server.error.message}
      </Notice>
    );
  }

  const data = server.data;
  const jobList = jobs.data ?? [];
  const { signal, label } = serverSignal(data);
  const protocol = protocols.data?.[0] ?? null;
  const setup: Setup = {
    key: Boolean(data.ssh_host_key_fingerprint),
    credential: Boolean(credential.data),
    // offline — установлен, но не отвечает: это уже обслуживание, а не подготовка. Стоящий протокол — тоже
    // доказательство установки: статус меняет только задача установки, а протокол могли поставить и без неё.
    installed: data.status === "ready" || data.status === "offline" || protocol !== null,
    protocol: protocol !== null,
  };
  const ready = setup.key && setup.credential && setup.installed && setup.protocol;
  const manage = can("servers:manage");

  return (
    <article className={styles.page} aria-labelledby="server-title">
      <Link to="/servers" className={styles.back}>
        Все серверы
      </Link>

      <header className={styles.header} data-signal={signal}>
        <div className={styles.identity}>
          <h1 id="server-title" className={styles.title}>
            {data.name}
          </h1>
          <p className={styles.address}>
            {data.host}:{data.ssh_port}
          </p>
          {data.description && <p className={styles.description}>{data.description}</p>}
        </div>
        <div className={styles.state}>
          <span className={styles.stateLabel} data-signal={signal}>
            {label}
          </span>
          <span className={styles.hint}>
            {data.last_health_at ? (
              <>
                проверен{" "}
                <time dateTime={data.last_health_at} title={exactTime(data.last_health_at)}>
                  {timeAgo(data.last_health_at)}
                </time>
              </>
            ) : (
              "ещё не проверялся"
            )}
          </span>
        </div>
      </header>

      {!ready && manage && <SetupChecklist server={data} setup={setup} jobs={jobList} />}
      {!ready && !manage && (
        <p className={styles.hint}>Сервер ещё не подготовлен к работе. Подготовить его может суперадмин.</p>
      )}
      {ready && (
        <Maintenance
          server={data}
          credential={credential.data ?? null}
          port={protocol?.port ?? null}
          jobs={jobList}
        />
      )}

      {/* Замеры снимаются только с серверов, куда панель может зайти по SSH. */}
      {setup.credential && (
        <Suspense fallback={<p className={styles.hint}>Загружаем графики…</p>}>
          <MonitoringSection serverId={data.id} />
        </Suspense>
      )}

      <section className={styles.section} aria-labelledby="jobs-title">
        <h2 id="jobs-title" className={styles.sectionTitle}>
          Задачи
        </h2>
        {jobs.isError ? <Notice>{jobs.error.message}</Notice> : jobs.data && <JobsLog jobs={jobList} />}
      </section>

      {manage && <DangerZone server={data} setup={setup} />}
    </article>
  );
}

function hasRunning(jobs: Job[], type: Job["type"]): boolean {
  return jobs.some((job) => job.type === type && isUnfinished(job));
}

type StepState = "done" | "next" | "running" | "locked";

const STEP_MARKS: Record<StepState, string> = {
  done: "готово",
  next: "следующий шаг",
  running: "выполняется",
  locked: "пока недоступно",
};

function Step({ state, title, children }: { state: StepState; title: string; children: ReactNode }) {
  return (
    <li className={styles.step} data-state={state} aria-current={state === "next" ? "step" : undefined}>
      <div className={styles.stepHead}>
        <span className={styles.stepTitle}>{title}</span>
        <span className={styles.stepMark}>{STEP_MARKS[state]}</span>
      </div>
      {state !== "done" && <div className={styles.stepBody}>{children}</div>}
    </li>
  );
}

function stepState(done: boolean, available: boolean, running = false): StepState {
  if (done) return "done";
  if (running) return "running";
  return available ? "next" : "locked";
}

/** Проверка входа по SSH: панель выполняет на сервере безобидную команду и показывает итог. */
function ConnectionTest({ serverId }: { serverId: string }) {
  const test = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/servers/{server_id}/test-connection", {
          params: { path: { server_id: serverId } },
        }),
      ),
  });
  return (
    <>
      <Button variant="quiet" busy={test.isPending} busyLabel="Подключаемся…" onClick={() => test.mutate()}>
        Проверить соединение
      </Button>
      {test.isError && <Notice>{test.error.message}</Notice>}
      {test.data && (
        <p className={styles.result} data-ok={test.data.success} role="status">
          {test.data.success
            ? "Панель вошла на сервер по SSH."
            : `Не удалось войти: ${test.data.message.replace(/^ERROR: /, "")}`}
        </p>
      )}
    </>
  );
}

/** Подготовка сервера по шагам. Порядок — тот, что требует бэкенд: пароль уходит только на закреплённый ключ. */
function SetupChecklist({ server, setup, jobs }: { server: Server; setup: Setup; jobs: Job[] }) {
  const [keyOpen, setKeyOpen] = useState(false);
  const [credOpen, setCredOpen] = useState(false);
  const start = useStartJob(server.id);
  const canInstall = setup.key && setup.credential;
  const installing = server.status === "installing" || hasRunning(jobs, "install_server");
  const addingProtocol = hasRunning(jobs, "install_protocol");

  return (
    <section className={styles.section} aria-labelledby="setup-title">
      <h2 id="setup-title" className={styles.sectionTitle}>
        Подготовка сервера
      </h2>
      <ol className={styles.steps}>
        <Step state={stepState(setup.key, true)} title="Закрепить ключ сервера">
          <p className={styles.hint}>
            Панель запомнит ключ, который вы сверите с консолью хостинга, и будет подключаться только к этому
            серверу.
          </p>
          <Button onClick={() => setKeyOpen(true)}>Получить и сверить ключ</Button>
        </Step>

        <Step state={stepState(setup.credential, setup.key)} title="Задать SSH-доступ">
          {setup.key ? (
            <Button onClick={() => setCredOpen(true)}>Задать доступ</Button>
          ) : (
            <p className={styles.hint}>Сначала закрепите ключ сервера — без него пароль отправлять нельзя.</p>
          )}
        </Step>

        <Step state={stepState(setup.installed, canInstall, installing)} title="Установить Docker и Amnezia">
          {installing ? (
            <p className={styles.hint}>
              Идёт установка, обычно несколько минут. Страницу можно закрыть — задача продолжится.
            </p>
          ) : canInstall ? (
            <>
              {server.status === "error" && (
                <p className={styles.hint}>Прошлая установка не удалась — причина в журнале задач ниже.</p>
              )}
              <div className={styles.row}>
                <Button
                  busy={start.isPending}
                  busyLabel="Ставим в очередь…"
                  onClick={() => start.mutate("install")}
                >
                  {server.status === "error" ? "Повторить установку" : "Установить"}
                </Button>
                <ConnectionTest serverId={server.id} />
              </div>
            </>
          ) : (
            <p className={styles.hint}>Станет доступно, когда ключ закреплён и SSH-доступ задан.</p>
          )}
        </Step>

        <Step
          state={stepState(setup.protocol, setup.installed && canInstall, addingProtocol)}
          title="Установить AmneziaWG"
        >
          {addingProtocol ? (
            <p className={styles.hint}>Устанавливаем протокол…</p>
          ) : setup.installed && canInstall ? (
            <Button
              busy={start.isPending}
              busyLabel="Ставим в очередь…"
              onClick={() => start.mutate("protocol")}
            >
              Установить протокол
            </Button>
          ) : (
            <p className={styles.hint}>
              {setup.installed
                ? "Станет доступно, когда ключ закреплён и SSH-доступ задан — протокол ставится по SSH."
                : "Станет доступно после установки Amnezia."}
            </p>
          )}
        </Step>
      </ol>
      {start.isError && <Notice>{start.error.message}</Notice>}
      <HostKeyDialog serverId={server.id} open={keyOpen} onOpenChange={setKeyOpen} />
      <CredentialsDialog serverId={server.id} open={credOpen} onOpenChange={setCredOpen} />
    </section>
  );
}

/** Подготовленный сервер: проверки и сведения о подключении. */
function Maintenance({
  server,
  credential,
  port,
  jobs,
}: {
  server: Server;
  credential: Credential | null;
  port: number | null;
  jobs: Job[];
}) {
  const { can } = useAuth();
  const manage = can("servers:manage");
  const start = useStartJob(server.id);
  const [keyOpen, setKeyOpen] = useState(false);
  const [credOpen, setCredOpen] = useState(false);
  const checking = hasRunning(jobs, "health_check");
  const syncing = hasRunning(jobs, "sync_server");

  return (
    <section className={styles.section} aria-labelledby="service-title">
      <h2 id="service-title" className={styles.sectionTitle}>
        Обслуживание
      </h2>
      {can("servers:check") && (
        <div className={styles.row}>
          <Button
            busy={checking || (start.isPending && start.variables === "health")}
            busyLabel="Проверяем…"
            onClick={() => start.mutate("health")}
          >
            Проверить состояние
          </Button>
          <Button
            variant="quiet"
            busy={syncing || (start.isPending && start.variables === "sync")}
            busyLabel="Сверяем…"
            onClick={() => start.mutate("sync")}
          >
            Сверить клиентов с базой
          </Button>
          {manage && <ConnectionTest serverId={server.id} />}
        </div>
      )}
      {start.isError && <Notice>{start.error.message}</Notice>}

      <dl className={styles.facts}>
        <dt>AmneziaWG</dt>
        <dd>{port ? `порт ${port}, UDP` : "порт неизвестен — запустите сверку"}</dd>

        <dt>Ключ сервера</dt>
        <dd>
          <code className={styles.keyValue}>{server.ssh_host_key_fingerprint}</code>
          {manage && (
            <Button variant="quiet" onClick={() => setKeyOpen(true)}>
              Сверить заново
            </Button>
          )}
        </dd>

        <dt>SSH-доступ</dt>
        <dd>
          {credential ? `${credential.username}, ${AUTH_LABELS[credential.auth_type]}` : "задан"}
          {manage && (
            <Button variant="quiet" onClick={() => setCredOpen(true)}>
              Заменить
            </Button>
          )}
        </dd>
      </dl>
      {manage && (
        <>
          <HostKeyDialog serverId={server.id} open={keyOpen} onOpenChange={setKeyOpen} />
          <CredentialsDialog serverId={server.id} open={credOpen} onOpenChange={setCredOpen} />
        </>
      )}
    </section>
  );
}

/** Разрушительные действия — отдельно, и в каждом диалоге сказано, что именно сломается. */
function DangerZone({ server, setup }: { server: Server; setup: Setup }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const start = useStartJob(server.id);
  const path = { params: { path: { server_id: server.id } } };

  return (
    <section className={`${styles.section} ${styles.danger}`} aria-labelledby="danger-title">
      <h2 id="danger-title" className={styles.sectionTitle}>
        Опасные действия
      </h2>
      <ul className={styles.dangerList}>
        {setup.protocol && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="quiet">Снять AmneziaWG</Button>}
              title="Снять AmneziaWG с сервера?"
              consequences="Контейнер протокола и его настройки будут удалены с сервера, порт закроется. Пока на сервере есть живые клиенты, снять протокол нельзя — сначала отзовите их доступы."
              actionLabel="Снять протокол"
              busyLabel="Ставим в очередь…"
              onConfirm={() => start.mutateAsync("remove-protocol")}
            />
          </li>
        )}
        {setup.credential && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="quiet">Удалить SSH-доступ</Button>}
              title="Удалить SSH-доступ к серверу?"
              consequences="Панель перестанет подключаться к серверу: проверки, сверка, выдача и отзыв клиентов остановятся, пока доступ не задан заново. Сам сервер и его клиенты продолжат работать."
              actionLabel="Удалить доступ"
              busyLabel="Удаляем…"
              onConfirm={async () => {
                await unwrap(api.DELETE("/api/v1/servers/{server_id}/credentials", path));
                await queryClient.invalidateQueries({ queryKey: keys.credential(server.id) });
              }}
            />
          </li>
        )}
        {setup.key && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="quiet">Сбросить ключ сервера</Button>}
              title="Сбросить закреплённый ключ?"
              consequences="Панель забудет ключ сервера, вход по паролю станет невозможен, пока ключ не сверят и не закрепят снова. Нужно, только если сервер переустановили и ключ сменился."
              actionLabel="Сбросить ключ"
              busyLabel="Сбрасываем…"
              onConfirm={async () => {
                await unwrap(api.DELETE("/api/v1/servers/{server_id}/host-key", path));
                await queryClient.invalidateQueries({ queryKey: keys.server(server.id), exact: true });
                await queryClient.invalidateQueries({ queryKey: ["servers"] });
              }}
            />
          </li>
        )}
        <li>
          <ConfirmDialog
            trigger={<Button variant="quiet">Удалить сервер</Button>}
            title={`Удалить сервер «${server.name}»?`}
            consequences="Сервер пропадёт из панели вместе с SSH-доступом и закреплённым ключом. На самом сервере ничего не изменится. Пока на нём есть живые клиенты, удалить нельзя."
            actionLabel="Удалить сервер"
            busyLabel="Удаляем…"
            confirmText={server.name}
            onConfirm={async () => {
              await unwrap(api.DELETE("/api/v1/servers/{server_id}", path), {
                409: "На сервере есть живые клиенты. Сначала отзовите их доступы.",
              });
              queryClient.removeQueries({ queryKey: ["server", server.id] });
              await queryClient.invalidateQueries({ queryKey: ["servers"] });
              await navigate("/servers");
            }}
          />
        </li>
      </ul>
    </section>
  );
}
