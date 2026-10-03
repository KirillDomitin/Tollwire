import { useMutation } from "@tanstack/react-query";
import QRCode from "qrcode";
import { useEffect, useRef, useState } from "react";

import { api, unwrap } from "../../api/client";
import { Button } from "../../components/Button";
import { Dialog, DialogActions } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
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
  const [copied, setCopied] = useState(false);
  const reveal = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/credentials/{credential_id}/config", {
          params: { path: { credential_id: credentialId } },
        }),
        {
          409: "Этот конфиг уже отозван. Выдайте новый.",
        },
      ),
    gcTime: 0,
  });
  const config = reveal.data?.config ?? null;

  function close(next: boolean) {
    if (!next) {
      reveal.reset();
      setCopied(false);
    }
    onOpenChange(next);
  }

  async function copy() {
    if (!config) return;
    await navigator.clipboard.writeText(config);
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
      title="Конфиг для подключения"
      description="В конфиге — личный ключ пользователя. Передавайте его только ему самому. Показ записывается в журнал."
    >
      {!config && (
        <>
          {reveal.isError && <Notice>{reveal.error.message}</Notice>}
          <DialogActions>
            <Button variant="quiet" onClick={() => close(false)}>
              Отмена
            </Button>
            <Button busy={reveal.isPending} busyLabel="Получаем конфиг…" onClick={() => reveal.mutate()}>
              Показать конфиг
            </Button>
          </DialogActions>
        </>
      )}
      {config && (
        <>
          <QrCode text={config} />
          <p className={styles.hint}>
            Отсканируйте в приложении AmneziaVPN или AmneziaWG на телефоне, либо передайте файл .conf.
          </p>
          <DialogActions>
            <Button variant="quiet" onClick={() => void copy()}>
              {copied ? "Скопировано" : "Скопировать"}
            </Button>
            <Button variant="quiet" onClick={download}>
              Скачать .conf
            </Button>
            <Button onClick={() => close(false)}>Готово</Button>
          </DialogActions>
        </>
      )}
    </Dialog>
  );
}

/** QR рисуется прямо в браузере — текст конфига никуда не отправляется. */
function QrCode({ text }: { text: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!canvas.current) return;
    // Тёмное на белом и с полями — иначе камеры телефонов читают код хуже (и в тёмной теме тоже).
    QRCode.toCanvas(canvas.current, text, { errorCorrectionLevel: "M", margin: 2, width: 280 }).catch(() =>
      setFailed(true),
    );
  }, [text]);
  if (failed) return <Notice>QR-код не получился — скачайте файл .conf.</Notice>;
  return <canvas ref={canvas} className={styles.qr} role="img" aria-label="QR-код конфига" />;
}
