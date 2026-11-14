import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { lazy, Suspense, useId, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError, unwrap, type Job, type Schemas, type VPNCredential } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { Button } from "../../components/Button";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { Notice } from "../../components/Notice";
import { StatusPill } from "../../components/StatusPill";
import { TextField } from "../../components/TextField";
import { ClientMetrics } from "./ClientMetrics";
import { GatewayExitNote } from "./GatewayExitNote";
import styles from "./UserPage.module.css";
import { userKeys } from "./useUser";

const ConfigDialog = lazy(() =>
  import("./ConfigDialog").then((module) => ({ default: module.ConfigDialog })),
);
export const gatewayDevicesKey = (userId: string) => ["vpn-user", userId, "gateway-devices"] as const;

export function useGatewayDevices(userId: string) {
  const { can } = useAuth();
  return useQuery({
    queryKey: gatewayDevicesKey(userId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/vpn-users/{user_id}/gateway-devices", {
          params: { path: { user_id: userId } },
        }),
      ),
    enabled: can("access:read"),
    // The lifecycle is persisted: polling continues after navigation or a page reload.
    refetchInterval: (query) =>
      query.state.data?.devices.some((device) => device.state !== "active" && device.state !== "revoked")
        ? 2_000
        : 30_000,
  });
}

type DeviceList = Schemas["GatewayDevicesRead"];

export function GatewayDevices({
  userId,
  userName,
  data,
  onJob,
}: {
  userId: string;
  userName: string;
  data: DeviceList;
  onJob: (grantId: string, job: Job) => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [selectedGrant, setSelectedGrant] = useState("");
  const requestId = useRef<string | null>(null);
  const selectId = useId();
  const grantId = data.gateways.length === 1 ? data.gateways[0]!.grant_id : selectedGrant;
  const selectedAvailable = data.gateways.some((gateway) => gateway.grant_id === grantId);
  const full = data.occupied >= data.limit;
  const issue = useMutation({
    mutationFn: async (targetGrantId: string) => {
      requestId.current ??= crypto.randomUUID();
      return unwrap(
        api.POST("/api/v1/vpn-users/{user_id}/gateway-devices", {
          params: { path: { user_id: userId } },
          body: { grant_id: targetGrantId, device_name: name.trim(), request_id: requestId.current },
        }),
      );
    },
    onSuccess: (job, targetGrantId) => {
      onJob(targetGrantId, job);
      requestId.current = null;
      setName("");
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: gatewayDevicesKey(userId) }),
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (selectedAvailable && !full && name.trim()) issue.mutate(grantId);
  }
  const error =
    issue.error instanceof ApiError && issue.error.reason === "device_limit"
      ? t("users.devices.limitReached")
      : issue.error?.message;

  return (
    <section className={styles.devices} aria-label={t("users.devices.title")}>
      <div className={styles.grantHead}>
        <h2 className={styles.sectionTitle}>{t("users.devices.title")}</h2>
        <span role="status">{t("users.devices.occupied", { count: data.occupied, limit: data.limit })}</span>
      </div>
      <p className={styles.hint}>{t("users.devices.explanation", { limit: data.limit })}</p>
      {data.devices.length === 0 && <p className={styles.hint}>{t("users.devices.empty")}</p>}
      <ul className={styles.grants}>
        {data.devices.map((device) => (
          <DeviceRow
            key={device.id}
            device={device}
            gateways={data.gateways}
            userId={userId}
            userName={userName}
            onJob={onJob}
          />
        ))}
      </ul>
      {can("access:manage") && data.gateways.length > 0 && (
        <form className={styles.form} onSubmit={submit}>
          {data.gateways.length > 1 && (
            <div className={styles.gatewayChoice}>
              <label htmlFor={selectId}>{t("users.devices.gateway")}</label>
              <select
                id={selectId}
                value={selectedAvailable ? grantId : ""}
                disabled={issue.isPending}
                onChange={(event) => {
                  setSelectedGrant(event.target.value);
                  requestId.current = null;
                }}
              >
                <option value="">{t("users.devices.chooseGateway")}</option>
                {data.gateways.map((gateway) => (
                  <option key={gateway.grant_id} value={gateway.grant_id}>
                    {gateway.server_name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <TextField
            label={t("users.devices.name")}
            value={name}
            maxLength={100}
            required
            disabled={full || issue.isPending}
            onChange={(event) => {
              setName(event.target.value);
              requestId.current = null;
            }}
          />
          {error && <Notice>{error}</Notice>}
          {full && <p className={styles.hint}>{t("users.devices.limitReached")}</p>}
          <div className={styles.row}>
            <Button
              type="submit"
              disabled={full || !selectedAvailable || !name.trim()}
              busy={issue.isPending}
              busyLabel={t("users.grant.queueBusy")}
            >
              {t("users.devices.add")}
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}

function DeviceRow({
  device,
  gateways,
  userId,
  userName,
  onJob,
}: {
  device: VPNCredential;
  gateways: DeviceList["gateways"];
  userId: string;
  userName: string;
  onJob: (grantId: string, job: Job) => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const queryClient = useQueryClient();
  const [configOpen, setConfigOpen] = useState(false);
  const gateway = gateways.find((item) => item.grant_id === device.access_grant_id);
  const ready = device.state === "active" && device.revoked_at === null && gateway !== undefined;
  const closed = device.state === "revoked" || device.revoked_at !== null;
  const tone = ready ? "online" : closed ? "neutral" : "process";
  const name =
    device.device_name || device.external_name || t("users.devices.unnamed", { id: device.id.slice(0, 8) });
  const fileName = `${userName}-${name}`.toLowerCase().replace(/[^a-z0-9а-яё_-]+/gi, "-");
  return (
    <li className={styles.grant} data-tone={ready ? "ok" : closed ? "idle" : "process"}>
      <div className={styles.grantHead}>
        <span className={styles.deviceName}>{name}</span>
        <StatusPill tone={tone}>
          {gateway === undefined && !closed
            ? t("users.grant.removingClient")
            : t(`users.devices.states.${device.state}`)}
        </StatusPill>
      </div>
      {gateway && <p className={styles.hint}>{gateway.server_name}</p>}
      {ready && gateway && can("servers:read") && (
        <>
          <ClientMetrics
            serverId={gateway.server_id}
            protocolId={gateway.protocol_id}
            credentialId={device.id}
          />
          <GatewayExitNote serverId={gateway.server_id} credentialId={device.id} />
        </>
      )}
      <div className={styles.row}>
        {ready && device.has_config && can("config:reveal") && (
          <Button size="sm" onClick={() => setConfigOpen(true)}>
            {t("users.grant.showConfig")}
          </Button>
        )}
        {!closed && can("access:manage") && (
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="secondary">
                {t("users.devices.revoke")}
              </Button>
            }
            title={t("users.devices.revokeTitle", { name })}
            consequences={t("users.devices.revokeConsequences")}
            actionLabel={t("users.devices.revoke")}
            busyLabel={t("users.grant.revokeConfig.busy")}
            onConfirm={async () => {
              const result = await unwrap(
                api.POST("/api/v1/credentials/{credential_id}/revoke", {
                  params: { path: { credential_id: device.id } },
                }),
              );
              if ("type" in result) onJob(device.access_grant_id, result);
              await queryClient.invalidateQueries({ queryKey: gatewayDevicesKey(userId) });
              await queryClient.invalidateQueries({ queryKey: userKeys.credentials(device.access_grant_id) });
            }}
          />
        )}
      </div>
      {ready && device.has_config && configOpen && (
        <Suspense fallback={null}>
          <ConfigDialog
            credentialId={device.id}
            fileName={fileName}
            open={configOpen}
            onOpenChange={setConfigOpen}
          />
        </Suspense>
      )}
    </li>
  );
}
