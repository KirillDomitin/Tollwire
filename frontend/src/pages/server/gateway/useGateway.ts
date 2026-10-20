import { useQueries, useQuery } from "@tanstack/react-query";

import { api, unwrap, type Job } from "../../../api/client";
import { isUnfinished } from "../../../lib/jobs";
import { userKeys } from "../../user/useUser";
import { geoipKeys, keys } from "../useServer";

/** Здоровье выходов меняет планировщик раз в минуту; чаще перечитывать нет смысла. Фоновая вкладка не опрашивается. */
const REFRESH_MS = 30_000;
const JOB_POLL_MS = 2_000;

export function useExits(serverId: string) {
  return useQuery({
    queryKey: keys.gatewayExits(serverId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/gateway/exits", { params: { path: { server_id: serverId } } }),
      ),
    refetchInterval: REFRESH_MS,
  });
}

export function useAssignments(serverId: string) {
  return useQuery({
    queryKey: keys.gatewayAssignments(serverId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/servers/{server_id}/gateway/assignments", {
          params: { path: { server_id: serverId } },
        }),
      ),
    refetchInterval: REFRESH_MS,
  });
}

export function useGeoip() {
  return useQuery({
    queryKey: geoipKeys.overview,
    queryFn: () => unwrap(api.GET("/api/v1/geoip")),
  });
}

export function useOverrides() {
  return useQuery({
    queryKey: geoipKeys.overrides,
    queryFn: () => unwrap(api.GET("/api/v1/geoip/overrides")),
  });
}

/** Задача, у которой нет сервера (обновление списка RIPE): в журнал сервера она не попадает, поэтому следим за ней сами. */
export function useJob(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => unwrap(api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId ?? "" } } })),
    enabled: jobId !== null,
    refetchInterval: (query: { state: { data?: Job } }) =>
      !query.state.data || isUnfinished(query.state.data) ? JOB_POLL_MS : false,
  });
}

export interface GatewayClient {
  userId: string;
  username: string;
  /** Конфиг клиента жив (не отозван): ему положен выход. */
  live: boolean;
}

/**
 * Кто есть кто среди клиентов шлюза: назначения знают только credential_id, а имя пользователя лежит на другом конце
 * цепочки «пользователь → подписка → доступы → конфиги». Обратного поиска в API нет, поэтому цепочка собирается по
 * пользователям, у которых есть доступ, теми же запросами и под теми же ключами, что и карточка пользователя (кэш общий).
 * Нужны права на чтение доступов и пользователей; без них (или пока грузится) клиенты остаются без имён.
 */
export function useGatewayClients(serverId: string, gatewayProtocolId: string, enabled: boolean) {
  const users = useQuery({
    queryKey: ["vpn-users"],
    queryFn: () => unwrap(api.GET("/api/v1/vpn-users")),
    enabled,
  });
  const withAccess = (users.data ?? []).flatMap((user) =>
    user.access ? [{ user, subscriptionId: user.access.id }] : [],
  );

  const grantQueries = useQueries({
    queries: withAccess.map(({ subscriptionId }) => ({
      queryKey: userKeys.grants(subscriptionId),
      queryFn: () =>
        unwrap(
          api.GET("/api/v1/subscriptions/{subscription_id}/grants", {
            params: { path: { subscription_id: subscriptionId } },
          }),
        ),
      enabled,
    })),
  });
  const gatewayGrants = withAccess.flatMap(({ user }, index) =>
    (grantQueries[index]?.data ?? [])
      .filter((grant) => grant.server_id === serverId && grant.protocol_id === gatewayProtocolId)
      .map((grant) => ({ user, grant })),
  );

  const credentialQueries = useQueries({
    queries: gatewayGrants.map(({ grant }) => ({
      queryKey: userKeys.credentials(grant.id),
      queryFn: () =>
        unwrap(
          api.GET("/api/v1/access/{access_grant_id}/credentials", {
            params: { path: { access_grant_id: grant.id } },
          }),
        ),
      enabled,
    })),
  });

  const clients = new Map<string, GatewayClient>();
  gatewayGrants.forEach(({ user }, index) => {
    for (const credential of credentialQueries[index]?.data ?? []) {
      clients.set(credential.id, {
        userId: user.id,
        username: user.username,
        live: credential.revoked_at === null,
      });
    }
  });

  const loading =
    enabled &&
    (users.isPending ||
      grantQueries.some((query) => query.isPending) ||
      credentialQueries.some((query) => query.isPending));
  return { clients, loading };
}
