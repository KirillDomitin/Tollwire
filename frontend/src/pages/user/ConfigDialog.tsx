import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { QrCode, QrSeries } from "../../components/QrSeries";
import styles from "./UserPage.module.css";

/**
 * Конфиг клиента показывается только по явному запросу: каждый показ попадает в журнал аудита. Текст живёт лишь
 * в этом диалоге — не в кэше запросов и не в адресе страницы — и стирается при закрытии.
 */
export function ConfigDialog({
  credentialId,
  fileName,
  open,
  onOpenChange,
}: {
  credentialId: string;
  /** Имя файла без расширения, например «alice-us2». */
  fileName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const reveal = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/credentials/{credential_id}/config", {
          params: { path: { credential_id: credentialId } },
        }),
        {
          409: t("users.config.errors.revoked"),
        },
      ),
    gcTime: 0,
  });
  const config = reveal.data?.config ?? null;
  // Строка vpn:// для AmneziaVPN (с именем сервера); нет — у протокола своего формата нет, главный — сам конфиг.
  const shareLink = reveal.data?.share_link ?? null;
  // Тот же конфиг серией QR — в формате сканера AmneziaVPN: одиночный код со всей строкой слишком плотный.
  const shareQr = reveal.data?.share_qr ?? null;

  function close(next: boolean) {
    if (!next) {
      reveal.reset();
      setCopied(false);
    }
    onOpenChange(next);
  }

  async function copy() {
    const text = shareLink ?? config;
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopied(true);
  }

  function download() {
    if (!config) return;
    const url = URL.createObjectURL(new Blob([config], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `${fileName}.conf`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={t("users.config.title")}
      description={t("users.config.description")}
    >
      {!config && (
        <>
          {reveal.isError && <Notice>{reveal.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => close(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              busy={reveal.isPending}
              busyLabel={t("users.config.getting")}
              onClick={() => reveal.mutate()}
            >
              {t("users.config.show")}
            </Button>
          </DialogActions>
        </>
      )}
      {config && (
        <>
          {shareQr && shareQr.length > 0 ? (
            <QrSeries codes={shareQr} />
          ) : (
            <QrCode text={shareLink ?? config} />
          )}
          <p className={styles.hint}>
            {shareLink ? t("users.config.hintLink") : t("users.config.hintPlain")}
          </p>
          <DialogActions>
            <Button variant="quiet" onClick={() => void copy()}>
              {copied
                ? t("users.config.copied")
                : shareLink
                  ? t("users.config.copyLink")
                  : t("users.config.copy")}
            </Button>
            <Button variant="quiet" onClick={download}>
              {t("users.config.download")}
            </Button>
            <Button onClick={() => close(false)}>{t("users.config.done")}</Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}
