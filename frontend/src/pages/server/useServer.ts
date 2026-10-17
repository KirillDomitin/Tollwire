import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, ApiError, unwrap, type Credential, type Job } from "../../api/client";
import i18n from "../../i18n";
import { isUnfinished } from "../../lib/jobs";

const JOB_POLL_MS = 2_000;

export const keys = {
  server: (id: string) => ["server", id] as const,
  credential: (id: string) => ["server", id, "credential"] as const,
  protocols: (id: string) => ["server", id, "protocols"] as const,
  jobs: (id: string) => ["server", id, "jobs"] as const,
  discovery: (id: string) => ["server", id, "discovery"] as const,
};

/** Последние задачи сервера: общий источник для карточки и диалога переноса (один запрос, один опрос). */
export const fetchJobs = (serverId: string) =>
  unwrap(api.GET("/api/v1/jobs", { params: { query: { server_id: serverId, limit: 10 } } }));

/** Всё о сервере для карточки. Задачи опрашиваются, только пока есть незавершённые. */
export function useServerData(serverId: string) {
  const path = { params: { path: { server_id: serverId } } };
  const queryClient = useQueryClient();

  const server = useQuery({
    queryKey: keys.server(serverId),
    queryFn: () =>
      unwrap(api.GET("/api/v1/servers/{server_id}", path), {
        404: i18n.t("servers.card.errors.notFound"),
      }),
  });
  const credential = useQuery({
    queryKey: keys.credential(serverId),
    // 404 здесь — не ошибка, а «учётка ещё не задана».
    queryFn: async (): Promise<Credential | null> => {
      try {
        return await unwrap(api.GET("/api/v1/servers/{server_id}/credentials", path));
      } catch (exc) {
        if (exc instanceof ApiError && exc.status === 404) return null;
        throw exc;
      }
    },
  });
  const protocols = useQuery({
    queryKey: keys.protocols(serverId),
    queryFn: () => unwrap(api.GET("/api/v1/servers/{server_id}/protocols", path)),
  });
  // Отчёт последнего поиска установленной AmneziaVPN. Нужен, только если панель может зайти на сервер по SSH.
  // Сбой отчёта не мешает карточке: блок про приложение просто не показывается.
  const discovery = useQuery({
    queryKey: keys.discovery(serverId),
    queryFn: () => unwrap(api.GET("/api/v1/servers/{server_id}/discovery", path)),
    enabled: Boolean(credential.data),
  });
  const jobs = useQuery({
    queryKey: keys.jobs(serverId),
    queryFn: () => fetchJobs(serverId),
    refetchInterval: (query) => (query.state.data?.some(isUnfinished) ? JOB_POLL_MS : false),
  });

  // Задача завершилась — её итог меняет сервер (статус, health, протокол): перечитываем.
  const running = jobs.data?.filter(isUnfinished).length ?? 0;
  const previous = useRef(running);
  useEffect(() => {
    if (running < previous.current) {
      void queryClient.invalidateQueries({ queryKey: keys.server(serverId), exact: true });
      void queryClient.invalidateQueries({ queryKey: keys.protocols(serverId) });
      void queryClient.invalidateQueries({ queryKey: keys.credential(serverId) }); // ключ панели меняет учётку
      void queryClient.invalidateQueries({ queryKey: keys.discovery(serverId) }); // поиск, установка и перенос клиентов
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
    }
    previous.current = running;
  }, [running, serverId, queryClient]);

  return { server, credential, protocols, discovery, jobs };
}

export type ServerAction =
  "install" | "protocol" | "sync" | "health" | "discover" | "remove-protocol" | "ssh-key";

/** Поставить серверную задачу (202 + Job) и сразу начать следить за ней. */
export function useStartJob(serverId: string) {
  const queryClient = useQueryClient();
  const path = { params: { path: { server_id: serverId } } };
  return useMutation({
    mutationFn: (action: ServerAction): Promise<Job> =>
      action === "remove-protocol"
        ? unwrap(api.DELETE("/api/v1/servers/{server_id}/protocol", path), {
            409: i18n.t("servers.card.errors.removeProtocol"),
          })
        : unwrap(api.POST(`/api/v1/servers/{server_id}/${action}`, path), {
            409:
              action === "ssh-key"
                ? i18n.t("servers.card.errors.installKey")
                : i18n.t("servers.card.errors.needAccess"),
          }),
    onSuccess: (job) => {
      queryClient.setQueryData<Job[]>(keys.jobs(serverId), (jobs = []) => [job, ...jobs]);
    },
  });
}
