import { useInfiniteQuery, useQuery, type InfiniteData, type Query } from "@tanstack/react-query";

import { api, type Schemas } from "../../api/client";
import { botUnwrap } from "./botErrors";

export type BotStatus = Schemas["BotStatusRead"];
export type BotKey = Schemas["BotKeyRead"];
export type BotSettings = Schemas["BotSettingsRead"];
export type BotProtocol = Schemas["BotProtocolRead"];
export type Invite = Schemas["InviteRead"];
export type Registration = Schemas["RegistrationRead"];
export type Broadcast = Schemas["BroadcastRead"];
export type Recipient = Schemas["RecipientRead"];
export type AudienceRequest = Schemas["AudienceRequest"];

/** Раз в сколько опрашивают рассылки в работе и сообщение, поставленное в очередь. */
export const POLL_MS = 2_000;
const PAGE = 100;
const FIVE_MINUTES = 5 * 60_000;

export const botKeys = {
  status: ["bot", "status"] as const,
  keys: ["bot", "keys"] as const,
  settings: ["bot", "settings"] as const,
  protocols: ["bot", "protocols"] as const,
  recipients: (search: string) => ["bot", "recipients", search] as const,
  invites: ["bot", "invites"] as const,
  registrations: (status: string) => ["bot", "registrations", status] as const,
  registrationsAll: ["bot", "registrations"] as const,
  broadcasts: ["bot", "broadcasts"] as const,
  audience: (body: AudienceRequest) => ["bot", "audience", body] as const,
  outbox: (id: string) => ["bot", "outbox", id] as const,
};

/**
 * Состояние бота. Его читают меню и каркас страницы: «есть ли хоть один ключ» меняется редко, поэтому `staleTime`
 * пять минут; экран «Состояние» просит свежее сам (`refreshMs`).
 */
export function useBotStatus(options: { enabled?: boolean; refreshMs?: number } = {}) {
  return useQuery({
    queryKey: botKeys.status,
    queryFn: () => botUnwrap(api.GET("/api/v1/bot-admin/status")),
    staleTime: options.refreshMs ? 0 : FIVE_MINUTES,
    refetchInterval: options.refreshMs,
    enabled: options.enabled,
  });
}

export const useBotKeys = () =>
  useQuery({ queryKey: botKeys.keys, queryFn: () => botUnwrap(api.GET("/api/v1/bot-admin/keys")) });

export const useBotSettings = (enabled = true) =>
  useQuery({
    queryKey: botKeys.settings,
    queryFn: () => botUnwrap(api.GET("/api/v1/bot-admin/settings")),
    enabled,
  });

export const useBotProtocols = (enabled = true) =>
  useQuery({
    queryKey: botKeys.protocols,
    queryFn: () => botUnwrap(api.GET("/api/v1/bot-admin/protocols")),
    enabled,
  });

interface Cursor {
  created_at: string;
  id: string;
}

/**
 * Список с курсором (`before_created_at` + `before_id`, как у журнала аудита). `pollWhile` - опрашивать раз в 2 с,
 * пока в загруженном есть то, что ещё меняется; как только нет - опрос прекращается (`false`).
 */
export function useCursorList<T extends { id: string; created_at: string }>(options: {
  queryKey: readonly unknown[];
  load: (cursor: Cursor | null) => Promise<T[]>;
  pollWhile?: (items: T[]) => boolean;
}) {
  const query = useInfiniteQuery({
    queryKey: options.queryKey,
    initialPageParam: null as Cursor | null,
    queryFn: ({ pageParam }) => options.load(pageParam),
    getNextPageParam: (last) => {
      const tail = last.at(-1);
      return last.length === PAGE && tail ? { created_at: tail.created_at, id: tail.id } : undefined;
    },
    refetchInterval: (current: Query<T[], Error, InfiniteData<T[], Cursor | null>>) =>
      options.pollWhile?.(current.state.data?.pages.flat() ?? []) ? POLL_MS : false,
  });
  return { ...query, items: query.data?.pages.flat() ?? [] };
}

export const useInvites = () =>
  useCursorList<Invite>({
    queryKey: botKeys.invites,
    load: (cursor) =>
      botUnwrap(
        api.GET("/api/v1/bot-admin/invites", {
          params: { query: { limit: PAGE, before_created_at: cursor?.created_at, before_id: cursor?.id } },
        }),
      ),
  });

export const useRegistrations = (status: Registration["status"] | "") =>
  useCursorList<Registration>({
    queryKey: botKeys.registrations(status),
    load: (cursor) =>
      botUnwrap(
        api.GET("/api/v1/bot-admin/registrations", {
          params: {
            query: {
              limit: PAGE,
              status: status || undefined,
              before_created_at: cursor?.created_at,
              before_id: cursor?.id,
            },
          },
        }),
      ),
  });

/** Рассылка ещё идёт: её счётчики растут, список перечитывается. */
export const isRunning = (broadcast: Broadcast) => broadcast.state === "running";

export const useBroadcasts = () =>
  useCursorList<Broadcast>({
    queryKey: botKeys.broadcasts,
    load: (cursor) =>
      botUnwrap(
        api.GET("/api/v1/bot-admin/broadcasts", {
          params: { query: { limit: PAGE, before_created_at: cursor?.created_at, before_id: cursor?.id } },
        }),
      ),
    pollWhile: (items) => items.some(isRunning),
  });
