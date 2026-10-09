import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/AuthContext";
import styles from "./AppShell.module.css";
import { BrandMark } from "./BrandMark";
import { Button } from "./Button";
import { ThemeSwitch } from "./ThemeSwitch";

/** Разделы показываются только тем, кому они доступны: недоступное не видно, а не «серое». */
const SECTIONS: { to: string; label: string; permission: string }[] = [
  { to: "/servers", label: "Серверы", permission: "servers:read" },
  { to: "/users", label: "Пользователи", permission: "vpn_users:read" },
  { to: "/audit", label: "Журнал", permission: "audit:read" },
];

const ROLE_NAMES: Record<string, string> = {
  superadmin: "Суперадмин",
  operator: "Оператор",
  viewer: "Наблюдатель",
};

export function AppShell() {
  const { state, can, logout } = useAuth();
  if (state.status !== "signed-in") return null;
  const { admin } = state;

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <BrandMark />
        <nav aria-label="Разделы" className={styles.nav}>
          {SECTIONS.filter((section) => can(section.permission)).map((section) => (
            <NavLink
              key={section.to}
              to={section.to}
              className={({ isActive }) => `${styles.link} ${isActive ? styles.active : ""}`}
            >
              {section.label}
            </NavLink>
          ))}
        </nav>
        <div className={styles.account}>
          <p className={styles.email}>{admin.email}</p>
          <p className={styles.role}>{ROLE_NAMES[admin.role] ?? admin.role}</p>
          <ThemeSwitch />
          <Button variant="quiet" onClick={() => void logout()}>
            Выйти
          </Button>
        </div>
      </aside>
      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
