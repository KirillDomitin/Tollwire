import { useState, type ChangeEvent, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "../../../components/Button";
import { Dialog, DialogActions } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import { UPLOAD_MAX_BYTES, ingestMessage } from "./gateway";
import styles from "./Gateway.module.css";
import { useUploadList } from "./useGatewayActions";

/**
 * Запасной путь без доступа к RIPE: файл со списком (выгрузка RIPE или CIDR по строке, до 5 МБ). Файл читается в браузере
 * и уходит текстом; проверки и правила активации — те же, что у скачанного списка.
 */
export function UploadListDialog({
  open,
  onOpenChange,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Список принят: сообщение для блока «Россия напрямую». */
  onDone: (message: string) => void;
}) {
  const { t } = useTranslation();
  const upload = useUploadList();
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  function close(next: boolean) {
    if (!next) {
      upload.reset();
      setFile(null);
      setProblem(null);
    }
    onOpenChange(next);
  }

  function pick(event: ChangeEvent<HTMLInputElement>) {
    const picked = event.target.files?.[0] ?? null;
    setFile(picked);
    setProblem(
      picked && picked.size > UPLOAD_MAX_BYTES
        ? t("gateway.ru.upload.tooBig", { size: Math.round(UPLOAD_MAX_BYTES / 1024 / 1024) })
        : picked && picked.size === 0
          ? t("gateway.ru.upload.empty")
          : null,
    );
    upload.reset();
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) {
      setProblem(t("gateway.ru.upload.noFile"));
      return;
    }
    if (problem) return;
    const text = await file.text();
    upload.mutate(text, {
      onSuccess: (result) => {
        onDone(ingestMessage(result));
        close(false);
      },
    });
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("gateway.ru.upload.title")}
      description={t("gateway.ru.upload.description", { size: Math.round(UPLOAD_MAX_BYTES / 1024 / 1024) })}
    >
      <form className={styles.form} onSubmit={(event) => void submit(event)} noValidate>
        <div className={styles.field}>
          <label htmlFor="geoip-file" className={styles.fieldLabel}>
            {t("gateway.ru.upload.file")}
          </label>
          <input
            id="geoip-file"
            type="file"
            accept=".txt,.csv,text/plain"
            onChange={pick}
            aria-invalid={problem ? true : undefined}
            aria-describedby={problem ? "geoip-file-problem" : undefined}
          />
          {file && !problem && (
            <p className={styles.hint}>
              <span className={styles.fileName}>{file.name}</span>
            </p>
          )}
          {problem && (
            <p id="geoip-file-problem" className={styles.fieldError}>
              {problem}
            </p>
          )}
        </div>
        {upload.isError && <Notice>{upload.error.message}</Notice>}
        <DialogActions>
          <Button variant="secondary" onClick={() => close(false)}>
            {t("common.cancel")}
          </Button>
          <Button type="submit" busy={upload.isPending} busyLabel={t("gateway.ru.upload.busy")}>
            {t("gateway.ru.upload.submit")}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
