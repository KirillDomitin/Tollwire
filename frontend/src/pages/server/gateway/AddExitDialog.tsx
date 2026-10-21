import { useQueries, useQuery } from "@tanstack/react-query";
import { useId, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap } from "../../../api/client";
import { Button } from "../../../components/Button";
import { Dialog, DialogActions } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import { TextField } from "../../../components/TextField";
import { keys } from "../useServer";
import { MAX_CLIENTS_LIMIT, WEIGHT_MAX, WEIGHT_MIN, parseWhole } from "./gateway";
import styles from "./Gateway.module.css";
import { useAddExit } from "./useGatewayActions";

interface Problems {
  server?: string;
  weight?: string;
  limit?: string;
}

/**
 * Подключить сервер выходом шлюза. Предлагаются серверы панели с обычным протоколом AmneziaWG (шлюз — клиент такого
 * сервера), кроме самого шлюза и тех, что уже подключены.
 */
export function AddExitDialog({
  serverId,
  connected,
  open,
  onOpenChange,
}: {
  serverId: string;
  /** Серверы, уже подключённые выходами этого шлюза. */
  connected: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const selectId = useId();
  const [chosen, setChosen] = useState("");
  const [problems, setProblems] = useState<Problems>({});
  const add = useAddExit(serverId);

  const servers = useQuery({
    queryKey: ["servers"],
    queryFn: () => unwrap(api.GET("/api/v1/servers")),
    enabled: open,
  });
  const others = (servers.data ?? []).filter(
    (server) => server.id !== serverId && !connected.includes(server.id),
  );
  const protocols = useQueries({
    queries: others.map((server) => ({
      queryKey: keys.protocols(server.id),
      queryFn: () =>
        unwrap(
          api.GET("/api/v1/servers/{server_id}/protocols", { params: { path: { server_id: server.id } } }),
        ),
      enabled: open,
    })),
  });
  // Выходом годится только сервер с обычным AmneziaWG: шлюз и протоколы приложения AmneziaVPN не подходят.
  const candidates = others.filter((_, index) =>
    protocols[index]?.data?.some((protocol) => protocol.type === "amnezia_wg"),
  );
  const checking = servers.isPending || protocols.some((query) => query.isPending);
  const selected = candidates.some((server) => server.id === chosen) ? chosen : (candidates[0]?.id ?? "");

  function close(next: boolean) {
    if (!next) {
      add.reset();
      setProblems({});
      setChosen("");
    }
    onOpenChange(next);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const weight = parseWhole(String(form.get("weight")), WEIGHT_MIN, WEIGHT_MAX);
    const limitText = String(form.get("limit")).trim();
    const limit = limitText === "" ? null : parseWhole(limitText, 1, MAX_CLIENTS_LIMIT);
    const found: Problems = {};
    if (!selected) found.server = t("gateway.exits.add.problems.server");
    if (weight === null)
      found.weight = t("gateway.exits.add.problems.weight", { min: WEIGHT_MIN, max: WEIGHT_MAX });
    if (limitText !== "" && limit === null)
      found.limit = t("gateway.exits.add.problems.limit", { max: MAX_CLIENTS_LIMIT });
    setProblems(found);
    if (Object.keys(found).length > 0 || weight === null) return;
    add.mutate({ exit_server_id: selected, weight, max_clients: limit }, { onSuccess: () => close(false) });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("gateway.exits.add.title")}
      description={t("gateway.exits.add.description")}
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        <div className={styles.field}>
          <label htmlFor={selectId} className={styles.fieldLabel}>
            {t("gateway.exits.add.server")}
          </label>
          <select
            id={selectId}
            className={styles.select}
            value={selected}
            disabled={candidates.length === 0}
            aria-invalid={problems.server ? true : undefined}
            onChange={(event) => setChosen(event.target.value)}
          >
            {candidates.map((server) => (
              <option key={server.id} value={server.id}>
                {server.name}
              </option>
            ))}
          </select>
          {checking && <p className={styles.hint}>{t("gateway.exits.add.checking")}</p>}
          {!checking && candidates.length === 0 && (
            <p className={styles.hint}>{t("gateway.exits.add.noCandidates")}</p>
          )}
          {problems.server && <p className={styles.fieldError}>{problems.server}</p>}
        </div>
        <TextField
          label={t("gateway.exits.add.weight", { min: WEIGHT_MIN, max: WEIGHT_MAX })}
          name="weight"
          inputMode="numeric"
          defaultValue="1"
          autoComplete="off"
          error={problems.weight}
        />
        <TextField
          label={t("gateway.exits.add.limit")}
          name="limit"
          inputMode="numeric"
          placeholder={t("gateway.exits.add.limitPlaceholder")}
          autoComplete="off"
          error={problems.limit}
        />
        <p className={styles.hint}>{t("gateway.exits.add.hint")}</p>
        {add.isError && <Notice>{add.error.message}</Notice>}
        <DialogActions>
          <Button variant="secondary" onClick={() => close(false)}>
            {t("common.cancel")}
          </Button>
          <Button
            type="submit"
            busy={add.isPending}
            busyLabel={t("servers.card.queueBusy")}
            disabled={checking}
          >
            {t("gateway.exits.add.submit")}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
