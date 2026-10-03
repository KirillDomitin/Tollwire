import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { api, ApiError, unwrap, type Credential, type Job } from "../../api/client";
import { isUnfinished } from "../../lib/jobs";

const JOB_POLL_MS = 2_000;

export const keys = {
  server: (id: string) => ["server", id] as const,
  credential: (id: string) => ["server", id, "credential"] as const,
  protocols: (id: string) => ["server", id, "protocols"] as const,
  jobs: (id: string) => ["server", id, "jobs"] as const,
};

/** Всё о сервере для карточки. Задачи опрашиваются, только пока есть незавершённые. */
export function useServerData(serverId: string) {
  const path = { params: { path: { server_id: serverId } } };
  const queryClient = useQueryClient();

  const server = useQuery({
    queryKey: keys.server(serverId),
    queryFn: () =>
      unwrap(api.GET("/api/v1/servers/{server_id}", path), {
        404: "Сервер не найден — возможно, его удалили.",
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
  const jobs = useQuery({
    queryKey: keys.jobs(serverId),
    queryFn: () => unwrap(api.GET("/api/v1/jobs", { params: { query: { server_id: serverId, limit: 10 } } })),
    refetchInterval: (query) => (query.state.data?.some(isUnfinished) ? JOB_POLL_MS : false),
  });

  // Задача завершилась — её итог меняет сервер (статус, health, протокол): перечитываем.
  const running = jobs.data?.filter(isUnfinished).length ?? 0;
  const previous = useRef(running);
  useEffect(() => {
    if (running < previous.current) {
      void queryClient.invalidateQueries({ queryKey: keys.server(serverId), exact: true });
      void queryClient.invalidateQueries({ queryKey: keys.protocols(serverId) });
      void queryClient.invalidateQueries({ queryKey: ["servers"] });
    }
    previous.current = running;
  }, [running, serverId, queryClient]);

  return { server, credential, protocols, jobs };
}

export type ServerAction = "install" | "protocol" | "sync" | "health" | "remove-protocol";

/** Поставить серверную задачу (202 + Job) и сразу начать следить за ней. */
export function useStartJob(serverId: string) {
  const queryClient = useQueryClient();
  const path = { params: { path: { server_id: serverId } } };
  return useMutation({
    mutationFn: (action: ServerAction): Promise<Job> =>
      action === "remove-protocol"
        ? unwrap(api.DELETE("/api/v1/servers/{server_id}/protocol", path), {
            409: "Снять протокол нельзя: на сервере есть живые клиенты (сначала отзовите доступы) или не задан SSH-доступ.",
          })
        : unwrap(api.POST(`/api/v1/servers/{server_id}/${action}`, path), {
            409: "Сначала задайте SSH-доступ к серверу.",
          }),
    onSuccess: (job) => {
      queryClient.setQueryData<Job[]>(keys.jobs(serverId), (jobs = []) => [job, ...jobs]);
    },
  });
}
