import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import { api, unwrap, type Grant, type Job, type VPNCredential } from "../../api/client";
import { isLive } from "../../lib/access";
import { isUnfinished } from "../../lib/jobs";

const POLL_MS = 2_000;

export const userKeys = {
  user: (id: string) => ["vpn-user", id] as const,
  subscriptions: (id: string) => ["vpn-user", id, "subscriptions"] as const,
  grants: (subscriptionId: string) => ["subscription", subscriptionId, "grants"] as const,
  credentials: (grantId: string) => ["grant", grantId, "credentials"] as const,
};

/** Живой (не отозванный) конфиг доступа, если он есть. */
export function liveCredential(credentials: VPNCredential[] | undefined): VPNCredential | null {
  return credentials?.find((credential) => credential.revoked_at === null) ?? null;
}

/** Всё о пользователе для карточки: подписки, доступы текущей подписки и их конфиги. */
export function useUserData(userId: string) {
  const path = { params: { path: { user_id: userId } } };

  const user = useQuery({
    queryKey: userKeys.user(userId),
    queryFn: () =>
      unwrap(api.GET("/api/v1/vpn-users/{user_id}", path), {
        404: "Пользователь не найден — возможно, его удалили.",
      }),
  });
  const subscriptions = useQuery({
    queryKey: userKeys.subscriptions(userId),
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users/{user_id}/subscriptions", path)),
  });
  const current = subscriptions.data?.find(isLive) ?? null;

  const grants = useQuery({
    queryKey: userKeys.grants(current?.id ?? ""),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/subscriptions/{subscription_id}/grants", {
          params: { path: { subscription_id: current!.id } },
        }),
      ),
    enabled: current !== null,
  });

  const credentials = useQueries({
    queries: (grants.data ?? []).map((grant) => ({
      queryKey: userKeys.credentials(grant.id),
      queryFn: () =>
        unwrap(
          api.GET("/api/v1/access/{access_grant_id}/credentials", {
            params: { path: { access_grant_id: grant.id } },
          }),
        ),
      // Доступ отозван, а конфиг ещё жив — значит, воркер как раз убирает клиента с сервера: следим до конца.
      refetchInterval: (query: { state: { data?: VPNCredential[] } }) =>
        !isLive(grant) && liveCredential(query.state.data) ? POLL_MS : false,
    })),
  });

  const servers = useQuery({ queryKey: ["servers"], queryFn: () => unwrap(api.GET("/api/v1/servers")) });

  return {
    user,
    subscriptions,
    current,
    grants,
    credentialsByGrant: new Map(
      (grants.data ?? []).map((grant, index) => [grant.id, credentials[index]?.data]),
    ),
    servers,
  };
}

/**
 * Задачи выдачи и отзыва конфигов: API отвечает 202 и номером задачи, итог приходит позже. Номера запоминаются
 * по доступу, задачи опрашиваются, пока не завершатся; по завершении конфиги доступа перечитываются.
 */
export function useAccessJobs() {
  const queryClient = useQueryClient();
  const [tracked, setTracked] = useState<Record<string, string>>({}); // grant id → job id

  const jobs = useQueries({
    queries: Object.entries(tracked).map(([, jobId]) => ({
      queryKey: ["job", jobId],
      queryFn: () => unwrap(api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } })),
      refetchInterval: (query: { state: { data?: Job } }) =>
        !query.state.data || isUnfinished(query.state.data) ? POLL_MS : false,
    })),
  });

  const byGrant = new Map<string, Job | undefined>(
    Object.keys(tracked).map((grantId, index) => [grantId, jobs[index]?.data]),
  );
  const finishedKey = [...byGrant.entries()]
    .filter(([, job]) => job && !isUnfinished(job))
    .map(([grantId]) => grantId)
    .join(",");

  useEffect(() => {
    for (const grantId of finishedKey.split(",").filter(Boolean)) {
      void queryClient.invalidateQueries({ queryKey: userKeys.credentials(grantId) });
      void queryClient.invalidateQueries({ queryKey: ["subscription"] }); // статус доступа (pending → active)
      void queryClient.invalidateQueries({ queryKey: ["vpn-users"] });
    }
  }, [finishedKey, queryClient]);

  const track = useCallback((grantId: string, job: Job) => {
    setTracked((current) => ({ ...current, [grantId]: job.id }));
  }, []);

  return { track, jobFor: (grant: Grant) => byGrant.get(grant.id) };
}
