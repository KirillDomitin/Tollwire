import QRCode from "qrcode";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "./Button";
import { Notice } from "./Notice";
import styles from "./QrSeries.module.css";

const SERIES_STEP_MS = 1000;

/**
 * Серия QR, как на экране экспорта в AmneziaVPN: коды сменяются сами, сканер приложения собирает их в любом
 * порядке. Движение можно остановить кнопкой «Пауза»; при «уменьшить движение» в системе коды не листаются сами —
 * только кнопкой. Номер кода озвучивается экранным диктором.
 */
export function QrSeries({ codes }: { codes: string[] }) {
  const { t } = useTranslation();
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  const reducedMotion =
    typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const current = index % codes.length;
  const several = codes.length > 1;

  useEffect(() => {
    if (!several || reducedMotion || paused) return;
    const timer = window.setInterval(() => setIndex((value) => value + 1), SERIES_STEP_MS);
    return () => window.clearInterval(timer);
  }, [several, reducedMotion, paused]);

  return (
    <>
      <QrCode text={codes[current] ?? ""} />
      {several && (
        <div className={styles.series}>
          <span className={styles.hint} aria-live="polite">
            {t("common.qr.codeOf", { current: current + 1, total: codes.length })}
          </span>
          <span className={styles.controls}>
            {!reducedMotion && (
              <Button variant="quiet" onClick={() => setPaused((value) => !value)} aria-pressed={paused}>
                {paused ? t("common.qr.resume") : t("common.qr.pause")}
              </Button>
            )}
            <Button variant="quiet" onClick={() => setIndex(current + 1)}>
              {t("common.qr.next")}
            </Button>
          </span>
        </div>
      )}
    </>
  );
}

/** QR рисуется прямо в браузере — текст конфига никуда не отправляется. */
export function QrCode({ text }: { text: string }) {
  const { t } = useTranslation();
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
  if (failed) return <Notice>{t("common.qr.failed")}</Notice>;
  return <canvas ref={canvas} className={styles.qr} role="img" aria-label={t("common.qr.label")} />;
}
