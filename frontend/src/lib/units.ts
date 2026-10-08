/** Единицы мониторинга: объёмы — в байтах (КБ, МБ, ГБ), скорости сети — в битах (Мбит/с), как у провайдеров. */

const number = (value: number, digits: number) =>
  new Intl.NumberFormat("ru", { maximumFractionDigits: digits }).format(value);

/** 586 000 000 → «559 МБ». Двоичные кратные (1024), как показывает и сама ОС. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${number(value, value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
}

/** Скорость сети из байт/с: 125 000 Б/с → «1 Мбит/с». Десятичные кратные — так считают каналы связи. */
export function formatBitRate(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return "—";
  const bits = bytesPerSecond * 8;
  if (bits >= 1e9) return `${number(bits / 1e9, 2)} Гбит/с`;
  if (bits >= 1e6) return `${number(bits / 1e6, bits < 1e7 ? 2 : 1)} Мбит/с`;
  if (bits >= 1e3) return `${number(bits / 1e3, 0)} Кбит/с`;
  return `${number(bits, 0)} бит/с`;
}

/** Скорость диска из байт/с: «4 КБ/с». */
export function formatByteRate(bytesPerSecond: number | null | undefined): string {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return "—";
  return `${formatBytes(bytesPerSecond)}/с`;
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${number(value, value < 10 ? 1 : 0)} %`;
}

/** Доля «занято из всего» в процентах; нет данных — null. */
export function share(used: number | null | undefined, total: number | null | undefined): number | null {
  if (used === null || used === undefined || !total) return null;
  return (used / total) * 100;
}
