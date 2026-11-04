import { screen } from "@testing-library/react";

import type { Job } from "../../api/client";
import i18n from "../../i18n";
import { renderWithProviders } from "../../test/render";
import { JobsLog } from "./JobsLog";
import { ProtocolUpdateStatus } from "./ProtocolUpdates";

const DATE = "2026-11-04T00:00:00Z";

function job(status: Job["status"], result: Job["result"]): Job {
  return {
    id: "update-job", type: "update_protocol", server_id: "server-id", status,
    progress: status === "success" ? 100 : 0, created_at: DATE, started_at: DATE,
    finished_at: DATE, result, error: null, message: null,
  };
}

it.each([
  ["success", { changed: true, rollback: "not_needed" }, "Контейнер обновлён"],
  ["success", { changed: false, rollback: "not_needed" }, "Уже установлен актуальный образ"],
  ["failed", { changed: false, rollback: "succeeded" }, "Предыдущий контейнер и конфигурация восстановлены"],
  ["failed", { changed: false, rollback: "failed" }, "Откат не завершён. Требуется проверить сервер и повторить обновление."],
] as const)("shows the %s update outcome without inferring success from rollback", (status, result, expected) => {
  renderWithProviders(<JobsLog jobs={[job(status, result)]} />);
  expect(screen.getByText(expected, { exact: false })).toBeInTheDocument();
  if (status === "failed") expect(screen.queryByText("Контейнер обновлён")).not.toBeInTheDocument();
});

it("localizes the unchanged result when the interface is English", async () => {
  await i18n.changeLanguage("en");
  renderWithProviders(<JobsLog jobs={[job("success", { changed: false, rollback: "not_needed" })]} />);
  expect(screen.getByText(i18n.t("servers.protocolUpdates.result.unchanged"), { exact: false })).toBeInTheDocument();
  expect(screen.queryByText("Уже установлен актуальный образ")).not.toBeInTheDocument();
});

it("shows a failed availability check as unknown and stale rather than an available update", () => {
  renderWithProviders(<ProtocolUpdateStatus serverId="server-id" kind="amnezia_wg" jobs={[]}
    observation={{
      protocol_id: "protocol-id", protocol_type: "amnezia_wg", status: "unknown", stale: true,
      checked_at: DATE, last_attempt_at: "2026-11-04T00:05:00Z", reason: "registry_unavailable",
      current_image_id: null, target_image_id: null, target_digest: null,
    }} />, { permissions: ["servers:read"] });
  expect(screen.getByText("Не удалось проверить")).toBeInTheDocument();
  expect(screen.getByText("Сведения устарели")).toBeInTheDocument();
  expect(screen.queryByText("Доступно обновление")).not.toBeInTheDocument();
});
