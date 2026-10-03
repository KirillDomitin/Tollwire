import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, unwrap, type Subscription } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import { accessSignal, formatDay, isLive } from "../../lib/access";
import { ChangePeriodDialog } from "./ChangePeriodDialog";
import { GrantAccessDialog } from "./GrantAccessDialog";
import { GrantRow } from "./GrantRow";
import styles from "./UserPage.module.css";
import { useAccessJobs, userKeys, useUserData } from "./useUser";

function period(subscription: Subscription): string {
  return `${formatDay(subscription.valid_from)} — ${formatDay(subscription.valid_until)}`;
}

export function UserPage() {
  const { userId = "" } = useParams();
  const { can } = useAuth();
  const data = useUserData(userId);
  const jobs = useAccessJobs();
  const [grantOpen, setGrantOpen] = useState(false);
  const [periodOpen, setPeriodOpen] = useState(false);

  if (data.user.isPending) return <p className={styles.hint}>Загружаем пользователя…</p>;
  if (data.user.isError) {
    return (
      <Notice
        action={
          <Link to="/users" className={styles.back}>
            К списку пользователей
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
  const { signal, label } = accessSignal(current ?? history[0] ?? null);
  const servers = new Map((data.servers.data ?? []).map((server) => [server.id, server]));
  const grants = data.grants.data ?? [];
  const manage = can("access:manage");

  return (
    <article className={styles.page} aria-labelledby="user-title">
      <Link to="/users" className={styles.back}>
        Все пользователи
      </Link>

      <header className={styles.header} data-signal={signal}>
        <div className={styles.identity}>
          <h1 id="user-title" className={styles.title}>
            {user.username}
          </h1>
          <p className={styles.address}>{user.email}</p>
          {user.description && <p className={styles.description}>{user.description}</p>}
        </div>
        <div className={styles.state}>
          <span className={styles.stateLabel} data-signal={signal}>
            {label}
          </span>
          {current && <span className={styles.hint}>{period(current)}</span>}
        </div>
      </header>

      <section className={styles.section} aria-labelledby="access-title">
        <h2 id="access-title" className={styles.sectionTitle}>
          Доступ
        </h2>
        {data.subscriptions.isError && <Notice>{data.subscriptions.error.message}</Notice>}

        {data.subscriptions.data && !current && (
          <div className={styles.empty}>
            <p className={styles.hint}>
              {history[0]
                ? `Сейчас доступа нет — последний ${history[0].status === "expired" ? "истёк" : "отозван"} ${formatDay(history[0].revoked_at ?? history[0].valid_until)}.`
                : "Доступа ещё не было."}
            </p>
            {manage && <Button onClick={() => setGrantOpen(true)}>Выдать доступ</Button>}
          </div>
        )}

        {current && (
          <>
            {manage && (
              <div className={styles.row}>
                <Button onClick={() => setGrantOpen(true)}>Добавить серверы</Button>
                <Button variant="quiet" onClick={() => setPeriodOpen(true)}>
                  Изменить срок
                </Button>
                <RevokeAccess subscription={current} userId={userId} />
              </div>
            )}
            {data.grants.isError && <Notice>{data.grants.error.message}</Notice>}
            {data.grants.data && grants.length === 0 && (
              <p className={styles.hint}>Срок задан, но серверов в доступе нет — добавьте хотя бы один.</p>
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
            <ChangePeriodDialog subscription={current} open={periodOpen} onOpenChange={setPeriodOpen} />
          </>
        )}

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
      </section>

      {history.length > 0 && (
        <section className={styles.section} aria-labelledby="history-title">
          <h2 id="history-title" className={styles.sectionTitle}>
            Прошлые доступы
          </h2>
          <ul className={styles.history}>
            {history.map((subscription) => (
              <li key={subscription.id}>
                <span>{period(subscription)}</span>
                <span className={styles.hint}>
                  {subscription.status === "expired"
                    ? "истёк"
                    : `отозван ${formatDay(subscription.revoked_at ?? subscription.valid_until)}`}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {can("vpn_users:manage") && <DeleteUser userId={userId} username={user.username} />}
    </article>
  );
}

function RevokeAccess({ subscription, userId }: { subscription: Subscription; userId: string }) {
  const queryClient = useQueryClient();
  return (
    <ConfirmDialog
      trigger={<Button variant="quiet">Отозвать доступ</Button>}
      title="Отозвать весь доступ?"
      consequences="Все конфиги пользователя перестанут работать: клиенты будут удалены со всех серверов. Срок обнулится — чтобы вернуть доступ, его придётся выдать заново."
      actionLabel="Отозвать доступ"
      busyLabel="Отзываем…"
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
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  return (
    <section className={`${styles.section} ${styles.danger}`} aria-labelledby="danger-title">
      <h2 id="danger-title" className={styles.sectionTitle}>
        Опасные действия
      </h2>
      <div>
        <ConfirmDialog
          trigger={<Button variant="quiet">Удалить пользователя</Button>}
          title={`Удалить пользователя «${username}»?`}
          consequences="Пользователь пропадёт из панели вместе с историей доступов. Пока у него есть рабочие конфиги, удалить нельзя — сначала отзовите доступ."
          actionLabel="Удалить пользователя"
          busyLabel="Удаляем…"
          confirmText={username}
          onConfirm={async () => {
            await unwrap(
              api.DELETE("/api/v1/vpn-users/{user_id}", { params: { path: { user_id: userId } } }),
              {
                409: "У пользователя есть рабочие конфиги. Сначала отзовите доступ и дождитесь удаления клиентов.",
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
