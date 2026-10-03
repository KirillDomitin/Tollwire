// Типы только для того, что панель использует из qrcode. Пакет @types/qrcode не поставили: он тянет @types/node,
// а Node-типы во фронтенде не нужны.
declare module "qrcode" {
  interface QRCodeRenderOptions {
    errorCorrectionLevel?: "L" | "M" | "Q" | "H";
    margin?: number;
    width?: number;
    color?: { dark?: string; light?: string };
  }

  const QRCode: {
    toCanvas(canvas: HTMLCanvasElement, text: string, options?: QRCodeRenderOptions): Promise<void>;
  };
  export default QRCode;
}
