import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthContext";
import { LANGUAGES } from "../i18n";
import { Avatar } from "./Avatar";
import { ChangePasswordDialog } from "./ChangePasswordDialog";
import { ChevronUpIcon, KeyIcon, LogoutIcon } from "./icons";
import styles from "./UserMenu.module.css";

const ROLES = ["superadmin", "operator", "viewer"] as const;

/**
 * Блок пользователя внизу сайдбара: аватар, почта, роль. По щелчку — меню: язык, «Сменить пароль», «Выйти».
 * Доступность: кнопка с aria-haspopup/aria-expanded, меню role=menu со стрелками, Esc закрывает и возвращает фокус.
 */
export function UserMenu() {
  const { t, i18n } = useTranslation();
  const { state, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const items = () => [...(menu.current?.querySelectorAll<HTMLElement>("[role^='menuitem']") ?? [])];

  useEffect(() => {
    if (!open) return;
    items()[0]?.focus();
    const onPointer = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    return () => document.removeEventListener("mousedown", onPointer);
  }, [open]);

  if (state.status !== "signed-in") return null;
  const { admin } = state;
  const role = (ROLES as readonly string[]).includes(admin.role)
    ? t(`shell.roles.${admin.role as (typeof ROLES)[number]}`)
    : admin.role;

  function close(restoreFocus: boolean) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      close(true);
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    const list = items();
    const index = list.indexOf(document.activeElement as HTMLElement);
    let next: number | null = null;
    if (event.key === "ArrowDown") next = (index + 1) % list.length;
    else if (event.key === "ArrowUp") next = (index - 1 + list.length) % list.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = list.length - 1;
    if (next === null) return;
    event.preventDefault();
    list[next]?.focus();
  }

  return (
    <div className={styles.root} ref={root} onKeyDown={open ? onKeyDown : undefined}>
      <button
        type="button"
        ref={trigger}
        className={styles.trigger}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowUp" || event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <Avatar name={admin.email} />
        <span className={styles.who}>
          <span className={styles.email} title={admin.email}>
            {admin.email}
          </span>
          <span className={styles.role}>{role}</span>
        </span>
        <ChevronUpIcon className={styles.caret} size={16} data-open={open} />
      </button>

      {open && (
        <div className={styles.menu} role="menu" id={menuId} aria-label={t("shell.account.menu")} ref={menu}>
          <div role="group" aria-label={t("shell.language.legend")} className={styles.group}>
            <span className={styles.groupLabel} aria-hidden="true">
              {t("shell.language.legend")}
            </span>
            {LANGUAGES.map((language) => (
              <button
                key={language}
                type="button"
                role="menuitemradio"
                aria-checked={i18n.resolvedLanguage === language}
                className={styles.item}
                onClick={() => {
                  setOpen(false);
                  void i18n.changeLanguage(language);
                }}
              >
                <span className={styles.itemText}>{t(`shell.language.${language}Title`)}</span>
                <span className={styles.code}>{t(`shell.language.${language}`)}</span>
              </button>
            ))}
          </div>
          <hr className={styles.separator} />
          <button
            type="button"
            role="menuitem"
            className={styles.item}
            onClick={() => {
              setOpen(false);
              setPasswordOpen(true);
            }}
          >
            <KeyIcon size={16} />
            <span className={styles.itemText}>{t("auth.changePassword.open")}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className={`${styles.item} ${styles.danger}`}
            onClick={() => {
              setOpen(false);
              void logout();
            }}
          >
            <LogoutIcon size={16} />
            <span className={styles.itemText}>{t("shell.nav.signOut")}</span>
          </button>
        </div>
      )}

      <ChangePasswordDialog open={passwordOpen} onClose={() => setPasswordOpen(false)} />
    </div>
  );
}
