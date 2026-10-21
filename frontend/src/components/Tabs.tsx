import { useRef, type KeyboardEvent, type ReactNode } from "react";

import styles from "./Tabs.module.css";

export interface TabItem {
  id: string;
  label: string;
}

const tabId = (prefix: string, id: string) => `${prefix}-tab-${id}`;
const panelId = (prefix: string, id: string) => `${prefix}-panel-${id}`;

/** Вкладки с подчёркиванием: role=tablist, стрелки/Home/End переключают, Tab уходит в панель (roving tabindex). */
export function Tabs({
  tabs,
  value,
  onChange,
  label,
  idPrefix,
}: {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Название набора вкладок для экранного диктора. */
  label: string;
  /** Общий префикс id: связывает вкладки с панелями (`TabPanel` с тем же значением). */
  idPrefix: string;
}) {
  const list = useRef<HTMLDivElement>(null);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = tabs.findIndex((tab) => tab.id === value);
    const step: Record<string, number> = {
      ArrowRight: (index + 1) % tabs.length,
      ArrowLeft: (index - 1 + tabs.length) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    };
    const next = step[event.key];
    if (next === undefined) return;
    event.preventDefault();
    const target = tabs[next];
    if (!target) return;
    onChange(target.id);
    list.current?.querySelector<HTMLElement>(`#${CSS.escape(tabId(idPrefix, target.id))}`)?.focus();
  }

  return (
    <div className={styles.list} role="tablist" aria-label={label} ref={list} onKeyDown={onKeyDown}>
      {tabs.map((tab) => {
        const selected = tab.id === value;
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={tabId(idPrefix, tab.id)}
            aria-selected={selected}
            aria-controls={panelId(idPrefix, tab.id)}
            tabIndex={selected ? 0 : -1}
            className={styles.tab}
            onClick={() => onChange(tab.id)}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

/** Панель вкладки: в разметке только активная — остальные не рендерятся и не опрашивают данные. */
export function TabPanel({
  idPrefix,
  id,
  value,
  children,
}: {
  idPrefix: string;
  id: string;
  value: string;
  children: ReactNode;
}) {
  if (id !== value) return null;
  return (
    <div
      role="tabpanel"
      id={panelId(idPrefix, id)}
      aria-labelledby={tabId(idPrefix, id)}
      className={styles.panel}
    >
      {children}
    </div>
  );
}
