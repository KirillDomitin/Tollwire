import { useEffect, useState, type ComponentType } from "react";
import { useTranslation } from "react-i18next";
import { NavLink, Outlet, useLocation } from "react-router";

import { useAuth } from "../auth/AuthContext";
import styles from "./AppShell.module.css";
import { BrandMark } from "./BrandMark";
import { CloseIcon, LogIcon, MenuIcon, ServerIcon, UsersIcon, type IconProps } from "./icons";
import { UserMenu } from "./UserMenu";

/** Разделы показываются только тем, кому они доступны: недоступное не видно, а не «серое». */
const SECTIONS: {
  to: string;
  label: "servers" | "users" | "audit";
  permission: string;
  Icon: ComponentType<IconProps>;
}[] = [
  { to: "/servers", label: "servers", permission: "servers:read", Icon: ServerIcon },
  { to: "/users", label: "users", permission: "vpn_users:read", Icon: UsersIcon },
  { to: "/audit", label: "audit", permission: "audit:read", Icon: LogIcon },
];

/**
 * Каркас: слева сайдбар (логотип, разделы, блок пользователя), справа содержимое.
 * Сайдбар закреплён на высоту экрана и не прокручивается вместе со страницей — прокручивается только контент.
 * Ниже 768px сайдбар становится верхней панелью с кнопкой «Меню»: разделы и блок пользователя выезжают списком.
 */
export function AppShell() {
  const { t } = useTranslation();
  const { state, can } = useAuth();
  const location = useLocation();
  // Выезжающий список помнит, на какой странице открыт: перешли на другую — он закрыт сам.
  const [openAt, setOpenAt] = useState<string | null>(null);
  const open = openAt === location.pathname;
  const setOpen = (value: boolean) => setOpenAt(value ? location.pathname : null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenAt(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (state.status !== "signed-in") return null;

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar} data-open={open}>
        <div className={styles.top}>
          <BrandMark />
          <button
            type="button"
            className={styles.menuButton}
            aria-expanded={open}
            aria-controls="shell-drawer"
            aria-label={open ? t("shell.menu.close") : t("shell.menu.open")}
            onClick={() => setOpen(!open)}
          >
            {open ? <CloseIcon size={22} /> : <MenuIcon size={22} />}
          </button>
        </div>
        <div className={styles.drawer} id="shell-drawer">
          <nav aria-label={t("shell.nav.label")} className={styles.nav}>
            {SECTIONS.filter((section) => can(section.permission)).map(({ to, label, Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => `${styles.link} ${isActive ? styles.active : ""}`}
              >
                <Icon size={19} />
                {t(`shell.nav.${label}`)}
              </NavLink>
            ))}
          </nav>
          <div className={styles.account}>
            <UserMenu />
          </div>
        </div>
      </aside>
      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
