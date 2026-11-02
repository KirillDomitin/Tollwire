import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap, type Schemas } from "../api/client";

type LatestMetrics = Schemas["LatestMetrics"];

/** Замер старше 5 минут на момент получения не показываем: устаревшая цифра хуже прочерка. */
const STALE_MS = 5 * 60_000;

export function freshMetrics(items: LatestMetrics[], receivedAt: number): Map<string, LatestMetrics> {
  return new Map(
    items
      .filter((item) => receivedAt - new Date(item.ts).getTime() <= STALE_MS)
      .map((item) => [item.server_id, item]),
  );
}

/**
 * Последние замеры мониторинга по серверам: для списка (клиенты, нагрузка) и плиток карточки сервера.
 * Замеры раз в минуту — и подтягиваем так же. Запрос общий (один ключ): список и карточка не дублируют его.
 */
export function useLatestMetrics(): Map<string, LatestMetrics> {
  const metrics = useQuery({
    queryKey: ["metrics", "latest"],
    queryFn: () => unwrap(api.GET("/api/v1/monitoring/latest")),
    refetchInterval: 60_000,
  });
  const { data, dataUpdatedAt } = metrics;
  return useMemo(() => freshMetrics(data ?? [], dataUpdatedAt), [data, dataUpdatedAt]);
}
