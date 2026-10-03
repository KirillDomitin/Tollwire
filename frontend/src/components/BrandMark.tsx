import styles from "./BrandMark.module.css";

/** Знак панели — три статусные полосы, тот же язык, что у строк серверов. */
export function BrandMark({ name = true }: { name?: boolean }) {
  return (
    <span className={styles.brand}>
      <span className={styles.mark} aria-hidden="true">
        <span className={styles.ok} />
        <span className={styles.warn} />
        <span className={styles.down} />
      </span>
      {name && <span className={styles.name}>VPN Control Panel</span>}
    </span>
  );
}
