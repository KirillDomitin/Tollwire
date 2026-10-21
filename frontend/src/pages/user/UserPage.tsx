import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { api, unwrap, type Subscription } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Avatar } from "../../components/Avatar";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { ArrowLeftIcon, ClockIcon, ServerIcon, ShieldIcon } from "../../components/icons";
import { Notice } from "../../components/Notice";
import { StatCard } from "../../components/StatCard";
import { StatusPill } from "../../components/StatusPill";
import { TabPanel, Tabs } from "../../components/Tabs";
import i18n from "../../i18n";
import { accessPill, daysLeft, formatDay, isLive } from "../../lib/access";
import { ChangePeriodDialog } from "./ChangePeriodDialog";
import { EditUserDialog } from "./EditUserDialog";
import { GrantAccessDialog } from "./GrantAccessDialog";
import { GrantRow } from "./GrantRow";
import styles from "./UserPage.module.css";
import { liveCredential, useAccessJobs, userKeys, useUserData } from "./useUser";

function period(subscription: Subscription): string {
  return i18n.t("users.card.period", {
    from: formatDay(subscription.valid_from),
    to: formatDay(subscription.valid_until),
  });
}

export function UserPage() {
  const { t } = useTranslation();
  const { userId = "" } = useParams();
  const { can } = useAuth();
  const data = useUserData(userId);
  const jobs = useAccessJobs();
  const [grantOpen, setGrantOpen] = useState(false);
  const [periodOpen, setPeriodOpen] = useState(false);
  const [params, setParams] = useSearchParams();

  if (data.user.isPending) return <p className={styles.hint}>{t("users.card.loading")}</p>;
  if (data.user.isError) {
    return (
      <Notice
        action={
          <Link to="/users" className={styles.back}>
            {t("users.card.toList")}
          </Link>
        }
      >
        {data.user.error.message}
      </Notice>
    );
  }

  const user = data.user.data;
  const { current } = data;
  const history = (data.subscriptions.data ?? [])
    .filter((subscription) => !isLive(subscription))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const latest = current ?? history[0] ?? null;
  const pill = accessPill(latest);
  const servers = new Map((data.servers.data ?? []).map((server) => [server.id, server]));
  // Отозванные и истёкшие доступы не показываем; исключение — те, чей клиент ещё убирается с сервера.
  const grants = (data.grants.data ?? []).filter(
    (grant) => isLive(grant) || liveCredential(data.credentialsByGrant.get(grant.id)) !== null,
  );
  const manage = can("access:manage");
  const left = daysLeft(current);

  // Вкладки: активная — в адресе (?tab=…). Без выбора: есть подписка — «Доступы» (там конфиги), иначе — «Подписка».
  const tabs: { id: UserTab; label: string }[] = [
    { id: "main", label: t("users.card.tabs.main") },
    { id: "subscription", label: t("users.card.tabs.subscription") },
    { id: "access", label: t("users.card.tabs.access") },
  ];
  const requested = params.get("tab");
  const defaultTab: UserTab = current ? "access" : "subscription";
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

  return (
    <article className={styles.page} aria-labelledby="user-title">
      <div className={styles.top}>
        <Link to="/users" className={styles.back}>
          <ArrowLeftIcon size={16} />
          {t("users.card.backToList")}
        </Link>
        <div className={styles.titleRow}>
          <div className={styles.titleGroup}>
            <h1 id="user-title" className={styles.title}>
              {t("users.card.title", { name: user.username })}
            </h1>
            <StatusPill tone={pill.tone}>{pill.label}</StatusPill>
          </div>
          <div className={styles.actions}>{can("vpn_users:manage") && <EditUserDialog user={user} />}</div>
        </div>
      </div>

      <section className={styles.info} aria-label={user.username}>
        <Avatar name={user.username} size={64} />
        <div className={styles.identity}>
          <p className={styles.infoName}>{user.username}</p>
          <p className={styles.address}>{user.email ?? "—"}</p>
          {user.description && <p className={styles.description}>{user.description}</p>}
        </div>
      </section>

      <div className={styles.tiles} role="group" aria-label={t("users.card.tiles.label")}>
        <StatCard
          icon={<ClockIcon size={22} />}
          label={t("users.card.tiles.created")}
          value={formatDay(user.created_at)}
        />
        <StatCard
          icon={<ShieldIcon size={22} />}
          tone="green"
          label={t("users.card.tiles.until")}
          value={current ? formatDay(current.valid_until) : t("users.access.pill.none")}
        />
        <StatCard
          icon={<ServerIcon size={22} />}
          tone="violet"
          label={t("users.card.tiles.servers")}
          value={data.grants.data ? grants.length : "…"}
        />
      </div>

      <div className={styles.tabs}>
        <Tabs
          idPrefix="user"
          label={t("users.card.tabs.label")}
          tabs={tabs}
          value={tab}
          onChange={selectTab}
        />

        <TabPanel idPrefix="user" id="main" value={tab}>
          <section className={styles.section} aria-labelledby="details-title">
            <h2 id="details-title" className={styles.sectionTitle}>
              {t("users.card.details.title")}
            </h2>
            <dl className={styles.facts}>
              <dt>{t("users.card.details.created")}</dt>
              <dd>{formatDay(user.created_at)}</dd>
              <dt>{t("users.card.details.status")}</dt>
              <dd>
                {user.status === "disabled"
                  ? t("users.card.details.disabled")
                  : t("users.card.details.active")}
              </dd>
              <dt>{t("users.card.details.note")}</dt>
              <dd>{user.description || t("users.card.details.noNote")}</dd>
            </dl>
          </section>
          {can("vpn_users:manage") && <DeleteUser userId={userId} username={user.username} />}
        </TabPanel>

        <TabPanel idPrefix="user" id="subscription" value={tab}>
          <section className={styles.section} aria-labelledby="subscription-title">
            <h2 id="subscription-title" className={styles.sectionTitle}>
              {t("users.card.subscription")}
            </h2>
            {data.subscriptions.isError && <Notice>{data.subscriptions.error.message}</Notice>}

            {data.subscriptions.data && !current && (
              <div className={styles.empty}>
                <p className={styles.hint}>
                  {history[0]
                    ? t(
                        history[0].status === "expired"
                          ? "users.card.noAccessExpired"
                          : "users.card.noAccessRevoked",
                        {
                          date: formatDay(history[0].revoked_at ?? history[0].valid_until),
                        },
                      )
                    : t("users.card.noAccessYet")}
                </p>
                {manage && <Button onClick={() => setGrantOpen(true)}>{t("users.card.grantAccess")}</Button>}
              </div>
            )}

            {current && (
              <>
                <dl className={styles.facts}>
                  <dt>{t("users.card.subscriptionPeriod")}</dt>
                  <dd>{period(current)}</dd>
                  <dt>{t("users.card.subscriptionLeft")}</dt>
                  <dd>{left ?? "—"}</dd>
                </dl>
                {manage && (
                  <div className={styles.row}>
                    <Button variant="secondary" onClick={() => setPeriodOpen(true)}>
                      {t("users.card.changePeriod")}
                    </Button>
                    <RevokeAccess subscription={current} userId={userId} />
                  </div>
                )}
                <ChangePeriodDialog subscription={current} open={periodOpen} onOpenChange={setPeriodOpen} />
              </>
            )}
          </section>

          {history.length > 0 && (
            <section className={styles.section} aria-labelledby="history-title">
              <h2 id="history-title" className={styles.sectionTitle}>
                {t("users.card.history")}
              </h2>
              <ul className={styles.history}>
                {history.map((subscription) => (
                  <li key={subscription.id}>
                    <span>{period(subscription)}</span>
                    <span className={styles.hint}>
                      {subscription.status === "expired"
                        ? t("users.card.historyExpired")
                        : t("users.card.historyRevoked", {
                            date: formatDay(subscription.revoked_at ?? subscription.valid_until),
                          })}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </TabPanel>

        <TabPanel idPrefix="user" id="access" value={tab}>
          <section className={styles.section} aria-labelledby="access-title">
            <h2 id="access-title" className={styles.sectionTitle}>
              {t("users.card.access")}
            </h2>
            {!current && data.subscriptions.data && (
              <p className={styles.hint}>{t("users.card.noAccessYet")}</p>
            )}
            {current && (
              <>
                {manage && (
                  <div className={styles.row}>
                    <Button onClick={() => setGrantOpen(true)}>{t("users.card.addServers")}</Button>
                  </div>
                )}
                {data.grants.isError && <Notice>{data.grants.error.message}</Notice>}
                {data.grants.data && grants.length === 0 && (
                  <p className={styles.hint}>{t("users.card.noServers")}</p>
                )}
                {grants.length > 0 && (
                  <ul className={styles.grants}>
                    {grants.map((grant) => (
                      <GrantRow
                        key={grant.id}
                        grant={grant}
                        server={servers.get(grant.server_id)}
                        credentials={data.credentialsByGrant.get(grant.id)}
                        job={jobs.jobFor(grant)}
                        userName={user.username}
                        onJob={jobs.track}
                      />
                    ))}
                  </ul>
                )}
              </>
            )}
          </section>
        </TabPanel>
      </div>

      {manage && (
        <GrantAccessDialog
          userId={userId}
          current={current}
          grants={grants}
          servers={data.servers.data ?? []}
          onIssued={jobs.track}
          open={grantOpen}
          onOpenChange={setGrantOpen}
        />
      )}
    </article>
  );
}

type UserTab = "main" | "subscription" | "access";

function RevokeAccess({ subscription, userId }: { subscription: Subscription; userId: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return (
    <ConfirmDialog
      trigger={<Button variant="secondary">{t("users.card.revoke.trigger")}</Button>}
      title={t("users.card.revoke.title")}
      consequences={t("users.card.revoke.consequences")}
      actionLabel={t("users.card.revoke.action")}
      busyLabel={t("users.card.revoke.busy")}
      onConfirm={async () => {
        await unwrap(
          api.POST("/api/v1/subscriptions/{subscription_id}/revoke", {
            params: { path: { subscription_id: subscription.id } },
          }),
        );
        await queryClient.invalidateQueries({ queryKey: userKeys.subscriptions(userId) });
        await queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
      }}
    />
  );
}

function DeleteUser({ userId, username }: { userId: string; username: string }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return (
    <section className={`${styles.section} ${styles.danger}`} aria-labelledby="danger-title">
      <h2 id="danger-title" className={styles.sectionTitle}>
        {t("users.card.delete.title")}
      </h2>
      <div>
        <ConfirmDialog
          trigger={<Button variant="secondary">{t("users.card.delete.trigger")}</Button>}
          title={t("users.card.delete.confirmTitle", { name: username })}
          consequences={t("users.card.delete.consequences")}
          actionLabel={t("users.card.delete.action")}
          busyLabel={t("users.card.delete.busy")}
          confirmText={username}
          onConfirm={async () => {
            await unwrap(
              api.DELETE("/api/v1/vpn-users/{user_id}", { params: { path: { user_id: userId } } }),
              {
                409: t("users.card.delete.conflict"),
              },
            );
            queryClient.removeQueries({ queryKey: userKeys.user(userId) });
            await queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
            await navigate("/users");
          }}
        />
      </div>
    </section>
  );
}
