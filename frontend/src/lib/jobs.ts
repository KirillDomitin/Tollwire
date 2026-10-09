import type { Job } from "../api/client";

export const JOB_LABELS: Record<Job["type"], string> = {
  install_server: "Установка Amnezia",
  uninstall_server: "Удаление Amnezia",
  install_protocol: "Установка AmneziaWG",
  install_ssh_key: "Установка ключа панели",
  remove_protocol: "Снятие протокола",
  sync_server: "Сверка с базой",
  health_check: "Проверка состояния",
  create_user: "Выдача клиента",
  revoke_access: "Отзыв клиента",
  delete_user: "Удаление пользователя",
};

export const JOB_STATUS_LABELS: Record<Job["status"], string> = {
  pending: "В очереди",
  running: "Выполняется",
  success: "Готово",
  failed: "Ошибка",
  cancelled: "Отменена",
};

export function isUnfinished(job: Pick<Job, "status">): boolean {
  return job.status === "pending" || job.status === "running";
}
