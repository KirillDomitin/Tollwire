import { useMutation, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap, type Credential, type Job, type Server } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import i18n from "../../i18n";
import { isUnfinished } from "../../lib/jobs";
import { serverSignal } from "../../lib/serverSignal";
import { exactTime, timeAgo } from "../../lib/time";
import { AmneziaAppSection } from "./AmneziaAppSection";
import { CredentialsDialog } from "./CredentialsDialog";
import { isAppProtocol, type AppProtocol } from "./discovery";
import { EditServerDialog } from "./EditServerDialog";
import { HostKeyDialog } from "./HostKeyDialog";
import { JobsLog } from "./JobsLog";
import styles from "./ServerPage.module.css";
import { keys, useServerData, useStartJob } from "./useServer";

// Графики (uPlot) нужны только на карточке сервера — отдельным файлом, а не в общем бандле панели.
const MonitoringSection = lazy(() =>
  import("./MonitoringSection").then((module) => ({ default: module.MonitoringSection })),
);

function authLabel(credential: Credential): string {
  return credential.managed_key
    ? i18n.t("servers.card.auth.panelKey")
    : i18n.t(`servers.card.auth.${credential.auth_type}`);
}

interface Setup {
  key: boolean;
  credential: boolean;
  installed: boolean;
  /** Есть любой протокол в панели: свой или перенесённый из приложения AmneziaVPN. */
  protocol: boolean;
  /** Есть собственный протокол панели (AmneziaWG) — только его можно снять и только он ставится из панели. */
  ownProtocol: boolean;
}

export function ServerPage() {
  const { serverId = "" } = useParams();
  const { t } = useTranslation();
  const { can } = useAuth();
  const { server, credential, protocols, discovery, jobs } = useServerData(serverId);

  if (server.isPending) return <p className={styles.hint}>{t("servers.card.loading")}</p>;
  if (server.isError) {
    return (
      <Notice
        action={
          <Link to="/servers" className={styles.back}>
            {t("servers.card.toList")}
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
  const ownProtocol = protocols.data?.find((item) => item.type === "amnezia_wg") ?? null;
  // Строка протокола приложения появляется после переноса клиентов; для панели это тоже «протокол есть».
  const protocol = ownProtocol ?? protocols.data?.[0] ?? null;
  // Найденные поиском установки приложения AmneziaVPN (отчёт читается только при SSH-учётке).
  const found: AppProtocol[] = discovery.data?.protocols.filter(isAppProtocol) ?? [];
  const setup: Setup = {
    key: Boolean(data.ssh_host_key_fingerprint),
    credential: Boolean(credential.data),
    // offline — установлен, но не отвечает: это уже обслуживание, а не подготовка. Стоящий протокол — тоже
    // доказательство установки: статус меняет только задача установки, а протокол могли поставить и без неё.
    installed: data.status === "ready" || data.status === "offline" || protocol !== null,
    protocol: protocol !== null,
    ownProtocol: ownProtocol !== null,
  };
  const ready = setup.key && setup.credential && setup.installed && setup.protocol;
  // Найденное приложение AmneziaVPN открывает «Обслуживание» (проверка, сверка) и без протокола в панели: иначе
  // сервер, где подхватывать нечего, навсегда остался бы в чек-листе без единой рабочей кнопки.
  const maintainable =
    setup.key && setup.credential && setup.installed && (setup.protocol || found.length > 0);
  const manage = can("servers:manage");

  return (
    <article className={styles.page} aria-labelledby="server-title">
      <Link to="/servers" className={styles.back}>
        {t("servers.card.allServers")}
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
          <div className={styles.row}>
            {manage && <EditServerDialog server={data} />}
            {can("audit:read") && (
              <Link to={`/audit?entity_type=server&entity_id=${data.id}`} className={styles.back}>
                {t("servers.card.events")}
              </Link>
            )}
          </div>
        </div>
        <div className={styles.state}>
          <span className={styles.stateLabel} data-signal={signal}>
            {label}
          </span>
          <span className={styles.hint}>
            {data.last_health_at ? (
              <>
                {t("servers.card.checked")}{" "}
                <time dateTime={data.last_health_at} title={exactTime(data.last_health_at)}>
                  {timeAgo(data.last_health_at)}
                </time>
              </>
            ) : (
              t("servers.card.neverChecked")
            )}
          </span>
        </div>
      </header>

      {!ready && manage && <SetupChecklist server={data} setup={setup} jobs={jobList} found={found} />}
      {!ready && !manage && !maintainable && <p className={styles.hint}>{t("servers.card.notPrepared")}</p>}
      {maintainable && (
        <Maintenance
          server={data}
          credential={credential.data ?? null}
          port={protocol?.port ?? found[0]?.port ?? null}
          appOnly={!setup.ownProtocol}
          jobs={jobList}
          searched={discovery.data?.checked_at ?? null}
          found={found.length > 0}
        />
      )}

      {setup.credential && found.length > 0 && (
        <AmneziaAppSection
          serverId={data.id}
          protocols={found}
          checkedAt={discovery.data?.checked_at ?? null}
          manage={manage}
          importing={hasRunning(jobList, "import_clients")}
        />
      )}

      {/* Замеры снимаются только с серверов, куда панель может зайти по SSH. */}
      {setup.credential && (
        <Suspense fallback={<p className={styles.hint}>{t("servers.card.loadingCharts")}</p>}>
          <MonitoringSection serverId={data.id} />
        </Suspense>
      )}

      <section className={styles.section} aria-labelledby="jobs-title">
        <h2 id="jobs-title" className={styles.sectionTitle}>
          {t("servers.card.jobs")}
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

function Step({ state, title, children }: { state: StepState; title: string; children: ReactNode }) {
  const { t } = useTranslation();
  return (
    <li className={styles.step} data-state={state} aria-current={state === "next" ? "step" : undefined}>
      <div className={styles.stepHead}>
        <span className={styles.stepTitle}>{title}</span>
        <span className={styles.stepMark}>{t(`servers.card.stepMarks.${state}`)}</span>
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
  const { t } = useTranslation();
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
      <Button
        variant="quiet"
        busy={test.isPending}
        busyLabel={t("servers.card.connection.busy")}
        onClick={() => test.mutate()}
      >
        {t("servers.card.connection.button")}
      </Button>
      {test.isError && <Notice>{test.error.message}</Notice>}
      {test.data && (
        <p className={styles.result} data-ok={test.data.success} role="status">
          {test.data.success
            ? t("servers.card.connection.ok")
            : t("servers.card.connection.failed", { message: test.data.message.replace(/^ERROR: /, "") })}
        </p>
      )}
    </>
  );
}

/** Подготовка сервера по шагам. Порядок — тот, что требует бэкенд: пароль уходит только на закреплённый ключ. */
function SetupChecklist({
  server,
  setup,
  jobs,
  found,
}: {
  server: Server;
  setup: Setup;
  jobs: Job[];
  found: AppProtocol[];
}) {
  const { t } = useTranslation();
  const [keyOpen, setKeyOpen] = useState(false);
  const [credOpen, setCredOpen] = useState(false);
  const start = useStartJob(server.id);
  const canInstall = setup.key && setup.credential;
  const installing = server.status === "installing" || hasRunning(jobs, "install_server");
  const addingProtocol = hasRunning(jobs, "install_protocol");
  const searching = hasRunning(jobs, "discover_vpn");
  // Контейнер v1 носит имя, под которым панель ставит свой протокол: рядом с приложением он не встанет.
  const appV1 = found.find((item) => item.type === "amnezia_app_awg");
  const appV2 = found.find((item) => item.type === "amnezia_app_awg2");

  return (
    <section className={styles.section} aria-labelledby="setup-title">
      <h2 id="setup-title" className={styles.sectionTitle}>
        {t("servers.setup.title")}
      </h2>
      <ol className={styles.steps}>
        <Step state={stepState(setup.key, true)} title={t("servers.setup.pinKey.title")}>
          <p className={styles.hint}>{t("servers.setup.pinKey.hint")}</p>
          <Button onClick={() => setKeyOpen(true)}>{t("servers.setup.pinKey.button")}</Button>
        </Step>

        <Step state={stepState(setup.credential, setup.key)} title={t("servers.setup.credential.title")}>
          {setup.key ? (
            <Button onClick={() => setCredOpen(true)}>{t("servers.setup.credential.button")}</Button>
          ) : (
            <p className={styles.hint}>{t("servers.setup.credential.needKey")}</p>
          )}
        </Step>

        <Step
          state={stepState(setup.installed, canInstall, installing)}
          title={t("servers.setup.install.title")}
        >
          {installing ? (
            <p className={styles.hint}>{t("servers.setup.install.running")}</p>
          ) : canInstall ? (
            <>
              {server.status === "error" && (
                <p className={styles.hint}>{t("servers.setup.install.previousFailed")}</p>
              )}
              <div className={styles.row}>
                <Button
                  busy={start.isPending && start.variables === "install"}
                  busyLabel={t("servers.card.queueBusy")}
                  onClick={() => start.mutate("install")}
                >
                  {server.status === "error"
                    ? t("servers.setup.install.retry")
                    : t("servers.setup.install.button")}
                </Button>
                <ConnectionTest serverId={server.id} />
              </div>
            </>
          ) : (
            <p className={styles.hint}>{t("servers.setup.install.locked")}</p>
          )}
        </Step>

        <Step
          state={stepState(setup.protocol, setup.installed && canInstall, addingProtocol)}
          title={t("servers.setup.protocol.title")}
        >
          {addingProtocol ? (
            <p className={styles.hint}>{t("servers.setup.protocol.running")}</p>
          ) : setup.installed && canInstall ? (
            <>
              {appV1 ? (
                <p className={styles.hint}>
                  {t("servers.setup.protocol.appV1", { container: appV1.container })}
                </p>
              ) : (
                <>
                  {appV2 && (
                    <p className={styles.hint}>
                      {t("servers.setup.protocol.appV2", { container: appV2.container })}
                    </p>
                  )}
                  <Button
                    busy={start.isPending && start.variables === "protocol"}
                    busyLabel={t("servers.card.queueBusy")}
                    onClick={() => start.mutate("protocol")}
                  >
                    {t("servers.setup.protocol.button")}
                  </Button>
                </>
              )}
              {/* Когда приложение уже найдено, поиск повторяется кнопкой в «Обслуживании». */}
              {found.length === 0 && (
                <Button
                  variant="quiet"
                  busy={searching || (start.isPending && start.variables === "discover")}
                  busyLabel={t("servers.maintenance.discovering")}
                  onClick={() => start.mutate("discover")}
                >
                  {t("servers.maintenance.discover")}
                </Button>
              )}
            </>
          ) : (
            <p className={styles.hint}>
              {setup.installed
                ? t("servers.setup.protocol.lockedNoAccess")
                : t("servers.setup.protocol.lockedNoInstall")}
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
  appOnly,
  jobs,
  searched,
  found,
}: {
  server: Server;
  credential: Credential | null;
  port: number | null;
  /** В панели есть только протокол, перенесённый из приложения AmneziaVPN, своего AmneziaWG нет. */
  appOnly: boolean;
  jobs: Job[];
  /** Время последнего поиска приложения AmneziaVPN, если он был. */
  searched: string | null;
  /** Поиск нашёл установку приложения. */
  found: boolean;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const manage = can("servers:manage");
  const start = useStartJob(server.id);
  const [keyOpen, setKeyOpen] = useState(false);
  const [credOpen, setCredOpen] = useState(false);
  const checking = hasRunning(jobs, "health_check");
  const syncing = hasRunning(jobs, "sync_server");
  const searching = hasRunning(jobs, "discover_vpn");
  const installingKey = hasRunning(jobs, "install_ssh_key");

  return (
    <section className={styles.section} aria-labelledby="service-title">
      <h2 id="service-title" className={styles.sectionTitle}>
        {t("servers.maintenance.title")}
      </h2>
      {can("servers:check") && (
        <div className={styles.row}>
          <Button
            busy={checking || (start.isPending && start.variables === "health")}
            busyLabel={t("servers.maintenance.checking")}
            onClick={() => start.mutate("health")}
          >
            {t("servers.maintenance.checkHealth")}
          </Button>
          <Button
            variant="quiet"
            busy={syncing || (start.isPending && start.variables === "sync")}
            busyLabel={t("servers.maintenance.syncing")}
            onClick={() => start.mutate("sync")}
          >
            {t("servers.maintenance.sync")}
          </Button>
          <Button
            variant="quiet"
            busy={searching || (start.isPending && start.variables === "discover")}
            busyLabel={t("servers.maintenance.discovering")}
            onClick={() => start.mutate("discover")}
          >
            {t("servers.maintenance.discover")}
          </Button>
          {manage && <ConnectionTest serverId={server.id} />}
        </div>
      )}
      {start.isError && <Notice>{start.error.message}</Notice>}
      {searched && !found && !searching && (
        <p className={styles.hint}>{t("servers.maintenance.discoverNone", { when: timeAgo(searched) })}</p>
      )}

      <dl className={styles.facts}>
        <dt>{appOnly ? t("servers.maintenance.amneziaApp") : t("servers.maintenance.amnezia")}</dt>
        <dd>{port ? t("servers.maintenance.port", { port }) : t("servers.maintenance.portUnknown")}</dd>

        <dt>{t("servers.maintenance.hostKey")}</dt>
        <dd>
          <code className={styles.keyValue}>{server.ssh_host_key_fingerprint}</code>
          {manage && (
            <Button variant="quiet" onClick={() => setKeyOpen(true)}>
              {t("servers.maintenance.recheck")}
            </Button>
          )}
        </dd>

        <dt>{t("servers.maintenance.ssh")}</dt>
        <dd>
          {credential
            ? t("servers.maintenance.sshAs", { user: credential.username, auth: authLabel(credential) })
            : t("servers.maintenance.sshSet")}
          {credential?.key_fingerprint && (
            <code className={styles.keyValue}>{credential.key_fingerprint}</code>
          )}
          {manage && (
            <Button variant="quiet" onClick={() => setCredOpen(true)}>
              {t("servers.maintenance.replace")}
            </Button>
          )}
          {manage && credential && (
            <ConfirmDialog
              trigger={
                <Button
                  variant="quiet"
                  busy={installingKey}
                  busyLabel={t("servers.maintenance.panelKey.busy")}
                >
                  {credential.managed_key
                    ? t("servers.maintenance.panelKey.renew")
                    : t("servers.maintenance.panelKey.switch")}
                </Button>
              }
              title={
                credential.managed_key
                  ? t("servers.maintenance.panelKey.renewTitle")
                  : t("servers.maintenance.panelKey.switchTitle")
              }
              consequences={
                credential.managed_key
                  ? t("servers.maintenance.panelKey.renewConsequences")
                  : t("servers.maintenance.panelKey.switchConsequences")
              }
              actionLabel={
                credential.managed_key
                  ? t("servers.maintenance.panelKey.renewAction")
                  : t("servers.maintenance.panelKey.switchAction")
              }
              busyLabel={t("servers.card.queueBusy")}
              onConfirm={() => start.mutateAsync("ssh-key")}
            />
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
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const start = useStartJob(server.id);
  const path = { params: { path: { server_id: server.id } } };

  return (
    <section className={`${styles.section} ${styles.danger}`} aria-labelledby="danger-title">
      <h2 id="danger-title" className={styles.sectionTitle}>
        {t("servers.danger.title")}
      </h2>
      <ul className={styles.dangerList}>
        {setup.ownProtocol && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="quiet">{t("servers.danger.removeProtocol.trigger")}</Button>}
              title={t("servers.danger.removeProtocol.title")}
              consequences={t("servers.danger.removeProtocol.consequences")}
              actionLabel={t("servers.danger.removeProtocol.action")}
              busyLabel={t("servers.card.queueBusy")}
              onConfirm={() => start.mutateAsync("remove-protocol")}
            />
          </li>
        )}
        {setup.credential && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="quiet">{t("servers.danger.deleteAccess.trigger")}</Button>}
              title={t("servers.danger.deleteAccess.title")}
              consequences={t("servers.danger.deleteAccess.consequences")}
              actionLabel={t("servers.danger.deleteAccess.action")}
              busyLabel={t("servers.danger.deleteAccess.busy")}
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
              trigger={<Button variant="quiet">{t("servers.danger.resetKey.trigger")}</Button>}
              title={t("servers.danger.resetKey.title")}
              consequences={t("servers.danger.resetKey.consequences")}
              actionLabel={t("servers.danger.resetKey.action")}
              busyLabel={t("servers.danger.resetKey.busy")}
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
            trigger={<Button variant="quiet">{t("servers.danger.deleteServer.trigger")}</Button>}
            title={t("servers.danger.deleteServer.title", { name: server.name })}
            consequences={t("servers.danger.deleteServer.consequences")}
            actionLabel={t("servers.danger.deleteServer.action")}
            busyLabel={t("servers.danger.deleteServer.busy")}
            confirmText={server.name}
            onConfirm={async () => {
              await unwrap(api.DELETE("/api/v1/servers/{server_id}", path), {
                409: t("servers.card.errors.hasClients"),
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
