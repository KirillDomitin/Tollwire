import { useMutation, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { api, unwrap, type Credential, type Job, type Protocol, type Server } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ArrowLeftIcon, CpuIcon, DiskIcon, MemoryIcon, ServerIcon, UsersIcon } from "../../components/icons";
import { Notice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { SIGNAL_TONE, StatusPill } from "../../components/StatusPill";
import { TabPanel, Tabs } from "../../components/Tabs";
import i18n from "../../i18n";
import { isUnfinished } from "../../lib/jobs";
import { flagCountryName, splitFlag } from "../../lib/flags";
import { serverSignal } from "../../lib/serverSignal";
import { exactTime, timeAgo } from "../../lib/time";
import { formatPercent, share } from "../../lib/units";
import { useLatestMetrics } from "../latestMetrics";
import { AmneziaAppSection } from "./AmneziaAppSection";
import { CredentialsDialog } from "./CredentialsDialog";
import { isAppProtocol, type AppProtocol } from "./discovery";
import { EditServerDialog } from "./EditServerDialog";
import { GatewaySection } from "./gateway/GatewaySection";
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
  /** Есть обычный протокол в панели: свой (AmneziaWG) или перенесённый из приложения AmneziaVPN. Шлюз не в счёт. */
  protocol: boolean;
  /** Есть собственный протокол панели (AmneziaWG) — «Снять AmneziaWG» и порт в «Обслуживании» относятся к нему. */
  ownProtocol: boolean;
  /** Есть протокол шлюза (отдельный контейнер, ставится и снимается отдельно от AmneziaWG). */
  gateway: boolean;
}

export function ServerPage() {
  const { serverId = "" } = useParams();
  const { t, i18n: i18nInstance } = useTranslation();
  const { can } = useAuth();
  const { server, credential, protocols, discovery, jobs } = useServerData(serverId);
  const latestMetrics = useLatestMetrics();
  const [params, setParams] = useSearchParams();

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
  const gatewayProtocol: Protocol | null =
    protocols.data?.find((item) => item.type === "amnezia_gateway") ?? null;
  // Строка протокола приложения появляется после переноса клиентов; для панели это тоже «протокол есть». Шлюз —
  // отдельный протокол со своим контейнером и портом: он не делает сервер «с протоколом» и не подменяет собой AmneziaWG.
  const protocol = ownProtocol ?? protocols.data?.find((item) => item.type !== "amnezia_gateway") ?? null;
  // Найденные поиском установки приложения AmneziaVPN (отчёт читается только при SSH-учётке).
  const found: AppProtocol[] = discovery.data?.protocols.filter(isAppProtocol) ?? [];
  const setup: Setup = {
    key: Boolean(data.ssh_host_key_fingerprint),
    credential: Boolean(credential.data),
    // offline — установлен, но не отвечает: это уже обслуживание, а не подготовка. Стоящий протокол — тоже
    // доказательство установки: статус меняет только задача установки, а протокол могли поставить и без неё.
    installed:
      data.status === "ready" || data.status === "offline" || protocol !== null || gatewayProtocol !== null,
    protocol: protocol !== null,
    ownProtocol: ownProtocol !== null,
    gateway: gatewayProtocol !== null,
  };
  const ready = setup.key && setup.credential && setup.installed && setup.protocol;
  // Найденное приложение AmneziaVPN открывает «Обслуживание» (проверка, сверка) и без протокола в панели: иначе
  // сервер, где подхватывать нечего, навсегда остался бы в чек-листе без единой рабочей кнопки.
  const maintainable =
    setup.key && setup.credential && setup.installed && (setup.protocol || setup.gateway || found.length > 0);
  const manage = can("servers:manage");

  // Вкладки: показываются только те, что есть у этого сервера. Активная — в адресе (?tab=…): обновление страницы и
  // ссылка не теряют её. Без явного выбора: рабочий сервер открывается на графиках, неподготовленный — на подготовке.
  const tabs: { id: ServerTab; label: string }[] = [
    ...(setup.credential ? [{ id: "charts" as const, label: t("servers.card.tabs.charts") }] : []),
    { id: "config", label: t("servers.card.tabs.config") },
    { id: "jobs", label: t("servers.card.tabs.jobs") },
    ...(manage ? [{ id: "settings" as const, label: t("servers.card.tabs.settings") }] : []),
  ];
  const requested = params.get("tab");
  const defaultTab: ServerTab = setup.credential && (ready || maintainable) ? "charts" : "config";
  const tab = tabs.find((item) => item.id === requested)?.id ?? defaultTab;
  const selectTab = (id: string) =>
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        next.set("tab", id);
        return next;
      },
      { replace: true },
    );

  const { flag, rest } = splitFlag(data.name);
  const country = flag ? flagCountryName(flag, i18nInstance.resolvedLanguage ?? "en") : null;
  const protocolNames = [
    ownProtocol ? t("servers.maintenance.amnezia") : null,
    !ownProtocol && (protocol || found.length > 0) ? t("servers.card.info.protocolApp") : null,
    gatewayProtocol ? t("servers.maintenance.gateway") : null,
  ].filter(Boolean);
  const metric = latestMetrics.get(data.id);
  const memoryShare = share(metric?.mem_used_bytes, metric?.mem_total_bytes);
  const diskShare = share(metric?.disk_used_bytes, metric?.disk_total_bytes);

  return (
    <article className={styles.page} aria-labelledby="server-title">
      <div className={styles.top}>
        <Link to="/servers" className={styles.back}>
          <ArrowLeftIcon size={16} />
          {t("servers.card.backToList")}
        </Link>
        <div className={styles.titleRow}>
          <div className={styles.titleGroup}>
            <h1 id="server-title" className={styles.title}>
              {t("servers.card.title", { name: data.name })}
            </h1>
            <StatusPill tone={SIGNAL_TONE[signal]}>{label}</StatusPill>
          </div>
          <div className={styles.actions}>
            {manage && <EditServerDialog server={data} />}
            {can("audit:read") && (
              <Link to={`/audit?entity_type=server&entity_id=${data.id}`} className={styles.linkButton}>
                {t("servers.card.events")}
              </Link>
            )}
          </div>
        </div>
      </div>

      <section className={styles.info} aria-label={data.name}>
        <div className={styles.infoMain}>
          <span className={styles.flagBox} aria-hidden="true">
            {flag ? <span className={styles.flag}>{flag}</span> : <ServerIcon size={30} />}
          </span>
          <div className={styles.identity}>
            <p className={styles.infoName}>{rest || data.name}</p>
            <p className={styles.address}>
              {data.host}:{data.ssh_port}
            </p>
            {data.description && <p className={styles.description}>{data.description}</p>}
          </div>
        </div>
        <dl className={styles.infoFacts}>
          {country && (
            <div>
              <dt>{t("servers.card.info.country")}</dt>
              <dd>{country}</dd>
            </div>
          )}
          <div>
            <dt>{t("servers.card.info.protocol")}</dt>
            <dd>
              {protocolNames.length > 0 ? protocolNames.join(", ") : t("servers.card.info.protocolNone")}
            </dd>
          </div>
          <div>
            <dt>{t("servers.card.info.check")}</dt>
            <dd>
              {data.last_health_at ? (
                <time dateTime={data.last_health_at} title={exactTime(data.last_health_at)}>
                  {timeAgo(data.last_health_at)}
                </time>
              ) : (
                t("servers.card.neverChecked")
              )}
            </dd>
          </div>
          <div>
            <dt>{t("servers.card.info.hostKey")}</dt>
            <dd>{setup.key ? t("servers.card.info.hostKeyPinned") : t("servers.card.info.hostKeyNone")}</dd>
          </div>
        </dl>
      </section>

      {metric && (
        <div className={styles.tiles} role="group" aria-label={t("servers.card.tiles.label")}>
          <StatCard
            icon={<UsersIcon size={22} />}
            tone="violet"
            label={t("servers.card.tiles.clients")}
            value={metric.clients_online ?? "—"}
            unit={metric.clients_total === null ? undefined : `/ ${metric.clients_total}`}
          />
          <StatCard
            icon={<CpuIcon size={22} />}
            tone="green"
            label={t("servers.card.tiles.cpu")}
            value={formatPercent(metric.cpu_percent)}
          />
          <StatCard
            icon={<MemoryIcon size={22} />}
            label={t("servers.card.tiles.memory")}
            value={formatPercent(memoryShare)}
          />
          <StatCard
            icon={<DiskIcon size={22} />}
            tone="violet"
            label={t("servers.card.tiles.disk")}
            value={formatPercent(diskShare)}
          />
        </div>
      )}

      <div className={styles.tabs}>
        <Tabs
          idPrefix="server"
          label={t("servers.card.tabs.label")}
          tabs={tabs}
          value={tab}
          onChange={selectTab}
        />

        <TabPanel idPrefix="server" id="charts" value={tab}>
          {/* Замеры снимаются только с серверов, куда панель может зайти по SSH. */}
          <Suspense fallback={<p className={styles.hint}>{t("servers.card.loadingCharts")}</p>}>
            <MonitoringSection serverId={data.id} />
          </Suspense>
        </TabPanel>

        <TabPanel idPrefix="server" id="config" value={tab}>
          {!ready && manage && <SetupChecklist server={data} setup={setup} jobs={jobList} found={found} />}
          {!ready && !manage && !maintainable && (
            <p className={styles.hint}>{t("servers.card.notPrepared")}</p>
          )}
          {maintainable && (
            <Maintenance
              server={data}
              credential={credential.data ?? null}
              port={protocol?.port ?? found[0]?.port ?? null}
              hasProtocol={setup.protocol || found.length > 0}
              appOnly={!setup.ownProtocol}
              gateway={gatewayProtocol}
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
          {gatewayProtocol && <GatewaySection server={data} protocol={gatewayProtocol} jobs={jobList} />}
        </TabPanel>

        <TabPanel idPrefix="server" id="jobs" value={tab}>
          <section className={styles.section} aria-labelledby="jobs-title">
            <h2 id="jobs-title" className={styles.sectionTitle}>
              {t("servers.card.jobs")}
            </h2>
            {jobs.isError ? <Notice>{jobs.error.message}</Notice> : jobs.data && <JobsLog jobs={jobList} />}
          </section>
        </TabPanel>

        <TabPanel idPrefix="server" id="settings" value={tab}>
          {manage && <DangerZone server={data} setup={setup} />}
        </TabPanel>
      </div>
    </article>
  );
}

type ServerTab = "charts" | "config" | "jobs" | "settings";

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
        variant="secondary"
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
              {/* Шлюз — отдельный контейнер (vpn-admin-gw): он не занимает имя приложения и ставится рядом с обычным протоколом. */}
              {setup.gateway ? (
                <p className={styles.hint}>{t("servers.setup.protocol.gatewayInstalled")}</p>
              ) : (
                <>
                  <p className={styles.hint}>{t("servers.setup.protocol.gatewayHint")}</p>
                  <Button
                    variant="secondary"
                    busy={start.isPending && start.variables === "gateway-install"}
                    busyLabel={t("servers.card.queueBusy")}
                    onClick={() => start.mutate("gateway-install")}
                  >
                    {t("servers.setup.protocol.gatewayButton")}
                  </Button>
                </>
              )}
              {/* Когда приложение уже найдено, поиск повторяется кнопкой в «Обслуживании». */}
              {found.length === 0 && (
                <Button
                  variant="secondary"
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
  hasProtocol,
  appOnly,
  gateway,
  jobs,
  searched,
  found,
}: {
  server: Server;
  credential: Credential | null;
  port: number | null;
  /** Есть обычный протокол (свой или приложения AmneziaVPN), чей порт показан в первой строке сведений. */
  hasProtocol: boolean;
  /** В панели есть только протокол, перенесённый из приложения AmneziaVPN, своего AmneziaWG нет. */
  appOnly: boolean;
  /** Протокол шлюза, если он установлен. */
  gateway: Protocol | null;
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
  const installingProtocol = hasRunning(jobs, "install_protocol");

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
            variant="secondary"
            busy={syncing || (start.isPending && start.variables === "sync")}
            busyLabel={t("servers.maintenance.syncing")}
            onClick={() => start.mutate("sync")}
          >
            {t("servers.maintenance.sync")}
          </Button>
          <Button
            variant="secondary"
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
        {hasProtocol && (
          <>
            <dt>{appOnly ? t("servers.maintenance.amneziaApp") : t("servers.maintenance.amnezia")}</dt>
            <dd>{port ? t("servers.maintenance.port", { port }) : t("servers.maintenance.portUnknown")}</dd>
          </>
        )}

        {(gateway || manage) && (
          <>
            <dt>{t("servers.maintenance.gateway")}</dt>
            <dd>
              {gateway ? (
                gateway.port ? (
                  t("servers.maintenance.port", { port: gateway.port })
                ) : (
                  t("servers.maintenance.portUnknown")
                )
              ) : (
                <>
                  <span className={styles.hint}>{t("servers.maintenance.gatewayNone")}</span>
                  <Button
                    variant="secondary"
                    busy={installingProtocol || (start.isPending && start.variables === "gateway-install")}
                    busyLabel={t("servers.card.queueBusy")}
                    onClick={() => start.mutate("gateway-install")}
                  >
                    {t("servers.maintenance.gatewayInstall")}
                  </Button>
                </>
              )}
            </dd>
          </>
        )}

        <dt>{t("servers.maintenance.hostKey")}</dt>
        <dd>
          <code className={styles.keyValue}>{server.ssh_host_key_fingerprint}</code>
          {manage && (
            <Button variant="secondary" onClick={() => setKeyOpen(true)}>
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
            <Button variant="secondary" onClick={() => setCredOpen(true)}>
              {t("servers.maintenance.replace")}
            </Button>
          )}
          {manage && credential && (
            <ConfirmDialog
              trigger={
                <Button
                  variant="secondary"
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
              trigger={<Button variant="secondary">{t("servers.danger.removeProtocol.trigger")}</Button>}
              title={t("servers.danger.removeProtocol.title")}
              consequences={t("servers.danger.removeProtocol.consequences")}
              actionLabel={t("servers.danger.removeProtocol.action")}
              busyLabel={t("servers.card.queueBusy")}
              onConfirm={() => start.mutateAsync("remove-protocol")}
            />
          </li>
        )}
        {setup.gateway && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="secondary">{t("servers.danger.removeGateway.trigger")}</Button>}
              title={t("servers.danger.removeGateway.title")}
              consequences={t("servers.danger.removeGateway.consequences")}
              actionLabel={t("servers.danger.removeGateway.action")}
              busyLabel={t("servers.card.queueBusy")}
              onConfirm={() => start.mutateAsync("gateway-remove")}
            />
          </li>
        )}
        {setup.credential && (
          <li>
            <ConfirmDialog
              trigger={<Button variant="secondary">{t("servers.danger.deleteAccess.trigger")}</Button>}
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
              trigger={<Button variant="secondary">{t("servers.danger.resetKey.trigger")}</Button>}
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
            trigger={<Button variant="secondary">{t("servers.danger.deleteServer.trigger")}</Button>}
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
