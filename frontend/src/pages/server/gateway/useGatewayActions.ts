import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";

import { api, ApiError, unwrap, type Job, type Schemas } from "../../../api/client";
import { NeedsConfirmation } from "../../../components/ConfirmDialog";
import i18n from "../../../i18n";
import { geoipKeys, keys } from "../useServer";
import { clientsLeft, geoipErrorText } from "./gateway";

/** Задача поставлена: она сразу появляется в журнале сервера и опрашивается, пока не завершится. */
function useTrackJob(serverId: string) {
  const queryClient = useQueryClient();
  return (job: Job) => {
    queryClient.setQueryData<Job[]>(keys.jobs(serverId), (jobs = []) => [job, ...jobs]);
    void queryClient.invalidateQueries({ queryKey: keys.gateway(serverId) });
  };
}

const path = (serverId: string) => ({ path: { server_id: serverId } });

/** Добавить выход (и повторить добавление, не закончившееся: тот же POST ставит его заново). */
export function useAddExit(serverId: string) {
  const { t } = useTranslation();
  const track = useTrackJob(serverId);
  return useMutation({
    mutationFn: (body: Schemas["GatewayExitCreate"]) =>
      unwrap(api.POST("/api/v1/servers/{server_id}/gateway/exits", { params: path(serverId), body }), {
        404: t("gateway.exits.errors.notFound"),
        409: t("gateway.exits.errors.addConflict"),
        422: t("gateway.exits.errors.invalid"),
      }),
    onSuccess: track,
  });
}

/** Вес и предел клиентов: только база, без задачи. */
export function useUpdateExit(serverId: string, exitId: string) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas["GatewayExitUpdate"]) =>
      unwrap(
        api.PATCH("/api/v1/servers/{server_id}/gateway/exits/{exit_id}", {
          params: { path: { server_id: serverId, exit_id: exitId } },
          body,
        }),
        { 404: t("gateway.exits.errors.exitGone"), 422: t("gateway.exits.errors.invalid") },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.gatewayExits(serverId) }),
  });
}

/**
 * Снять выход. Если после переноса часть клиентов останется без выхода, бэкенд отвечает 409 с их числом: диалог
 * показывает число и предлагает «Снять всё равно» (повтор с `force`).
 */
export function useRemoveExit(serverId: string, exitId: string) {
  const track = useTrackJob(serverId);
  return async (force: boolean): Promise<Job> => {
    try {
      const job = await unwrap(
        api.DELETE("/api/v1/servers/{server_id}/gateway/exits/{exit_id}", {
          params: { path: { server_id: serverId, exit_id: exitId }, query: force ? { force: true } : {} },
        }),
        { 404: i18n.t("gateway.exits.errors.exitGone"), 409: i18n.t("gateway.exits.errors.removeConflict") },
      );
      track(job);
      return job;
    } catch (exc) {
      const left = exc instanceof ApiError && exc.status === 409 && !force ? clientsLeft(exc.detail) : null;
      if (left !== null) {
        throw new NeedsConfirmation(
          i18n.t("gateway.exits.remove.left", { count: left }),
          i18n.t("gateway.exits.remove.force"),
        );
      }
      throw exc;
    }
  };
}

/** Перенос клиента на выход и / или закрепление за выходом (выполняется сразу, на шлюзе). */
export function useMoveClient(serverId: string, credentialId: string) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas["GatewayAssignmentMove"]) =>
      unwrap(
        api.PATCH("/api/v1/servers/{server_id}/gateway/assignments/{credential_id}", {
          params: { path: { server_id: serverId, credential_id: credentialId } },
          body,
        }),
        {
          404: t("gateway.assignments.errors.notFound"),
          409: t("gateway.assignments.errors.conflict"),
          422: t("gateway.assignments.errors.invalid"),
          // 502: выход выбран верно, но применить карту на шлюзе по SSH не удалось - ничего не сохранено, можно повторить.
          502: t("gateway.assignments.errors.notApplied"),
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.gateway(serverId) }),
  });
}

/** «Проверить сейчас»: задача проверки выходов с переносом клиентов отказавших. */
export function useCheckNow(serverId: string) {
  const { t } = useTranslation();
  const track = useTrackJob(serverId);
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/servers/{server_id}/gateway/check", { params: path(serverId) }), {
        409: t("gateway.assignments.errors.checkConflict"),
      }),
    onSuccess: track,
  });
}

/** Применить на шлюзе список «Россия напрямую» (активный список и исключения). */
export function useApplyRu(serverId: string) {
  const { t } = useTranslation();
  const track = useTrackJob(serverId);
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/servers/{server_id}/gateway/ru/apply", { params: path(serverId) }), {
        409: t("gateway.ru.errors.applyConflict"),
      }),
    onSuccess: track,
  });
}

/** Скачать список с RIPE: задача без сервера, итог читается из самой задачи. */
export function useUpdateList() {
  const { t } = useTranslation();
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/geoip/update"), { 409: t("gateway.ru.errors.updateConflict") }),
  });
}

/** Подтвердить кандидата: он станет активным и уйдёт на шлюзы. */
export function useActivateList() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (listId: string) =>
      unwrap(api.POST("/api/v1/geoip/{list_id}/activate", { params: { path: { list_id: listId } } }), {
        404: t("gateway.ru.errors.listGone"),
        409: t("gateway.ru.errors.notCandidate"),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: geoipKeys.all }),
  });
}

/**
 * Загрузка файла списка (запасной путь без RIPE): тело - сам текст, `text/plain`. В схеме бэкенда у этого маршрута
 * тела нет (оно читается потоком), поэтому запрос уходит через тот же клиент с собственным сериализатором.
 */
export function useUploadList() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (text: string): Promise<Schemas["GeoipIngestResult"]> => {
      const call = api.POST("/api/v1/geoip/upload", {
        body: text as never,
        bodySerializer: (body: unknown) => body as string,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
      try {
        return await unwrap(call, { 413: i18n.t("gateway.ru.errors.tooLarge") });
      } catch (exc) {
        // 422: бэкенд называет причину по-английски - переводим известные фразы.
        if (exc instanceof ApiError && exc.status === 422)
          throw new ApiError(geoipErrorText(exc.detail), 422);
        throw exc;
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: geoipKeys.all }),
  });
}

export function useAddOverride() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (body: Schemas["OverrideCreate"]) => {
      try {
        return await unwrap(api.POST("/api/v1/geoip/overrides", { body }), {
          409: t("gateway.ru.errors.overrideExists"),
        });
      } catch (exc) {
        if (exc instanceof ApiError && exc.status === 422)
          throw new ApiError(geoipErrorText(exc.detail), 422);
        throw exc;
      }
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: geoipKeys.overrides }),
  });
}

export function useRemoveOverride() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (overrideId: string) =>
      unwrap(
        api.DELETE("/api/v1/geoip/overrides/{override_id}", {
          params: { path: { override_id: overrideId } },
        }),
        {
          404: t("gateway.ru.errors.overrideGone"),
        },
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: geoipKeys.overrides }),
  });
}
