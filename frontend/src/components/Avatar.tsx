import styles from "./Avatar.module.css";

/** Две буквы из имени или почты: «admin@example.com» → «AD», «Иван Петров» → «ИП». */
export function initials(source: string): string {
  const words = source
    .split(/[\s@._-]+/)
    .map((word) => [...word][0])
    .filter((letter): letter is string => Boolean(letter && /[\p{L}\p{N}]/u.test(letter)));
  const first = words[0] ?? "?";
  const second = words.length > 1 ? words[1] : ([...source.replace(/[^\p{L}\p{N}]/gu, "")][1] ?? "");
  return (first + second).toUpperCase();
}

export function Avatar({ name, size = 36 }: { name: string; size?: number }) {
  return (
    <span
      className={styles.avatar}
      style={{ width: size, height: size, fontSize: size * 0.38 }}
      aria-hidden="true"
    >
      {initials(name)}
    </span>
  );
}
