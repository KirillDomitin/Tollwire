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
          {shareQr && shareQr.length > 0 ? (
            <QrSeries codes={shareQr} />
          ) : (
            <QrCode text={shareLink ?? config} />
          )}
          <p className={styles.hint}>
            {shareLink
              ? "В AmneziaVPN: «+» → «QR-код» и держите камеру на экране — приложение соберёт коды само. Или вставьте скопированный ключ vpn://. Файл .conf — для приложения AmneziaWG и роутеров."
              : "Отсканируйте в приложении AmneziaVPN или AmneziaWG на телефоне, либо передайте файл .conf."}
          </p>
          <DialogActions>
            <Button variant="quiet" onClick={() => void copy()}>
              {copied ? "Скопировано" : shareLink ? "Скопировать ключ vpn://" : "Скопировать"}
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

const SERIES_STEP_MS = 1000;

/**
 * Серия QR, как на экране экспорта в AmneziaVPN: коды сменяются сами, сканер приложения собирает их в любом
 * порядке. При «уменьшить движение» в системе коды не листаются сами — только кнопкой.
 */
function QrSeries({ codes }: { codes: string[] }) {
  const [index, setIndex] = useState(0);
  const reducedMotion =
    typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const current = index % codes.length;

  useEffect(() => {
    if (codes.length < 2 || reducedMotion) return;
    const timer = window.setInterval(() => setIndex((value) => value + 1), SERIES_STEP_MS);
    return () => window.clearInterval(timer);
  }, [codes.length, reducedMotion]);

  return (
    <>
      <QrCode text={codes[current] ?? ""} />
      {codes.length > 1 && (
        <div className={styles.qrSeries}>
          <span className={styles.hint}>
            Код {current + 1} из {codes.length}
          </span>
          <Button variant="quiet" onClick={() => setIndex((value) => value + 1)}>
            Следующий код
          </Button>
        </div>
      )}
    </>
  );
}

/** QR рисуется прямо в браузере — текст конфига никуда не отправляется. */
function QrCode({ text }: { text: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!canvas.current) return;
    // Тёмное на белом и с полями — иначе камеры телефонов читают код хуже (и в тёмной теме тоже).
    // Коррекция ошибок L: строка vpn:// длинная (~1,5 тыс. символов), а с экрана код не «повреждается» —
    // меньше избыточности → крупнее модули → камера читает легче. Ширина — во всё окно (~130 модулей по 3+ px).
    QRCode.toCanvas(canvas.current, text, { errorCorrectionLevel: "L", margin: 2, width: 440 }).catch(() =>
      setFailed(true),
    );
  }, [text]);
  if (failed) return <Notice>QR-код не получился — скачайте файл .conf.</Notice>;
  return <canvas ref={canvas} className={styles.qr} role="img" aria-label="QR-код конфига" />;
}
