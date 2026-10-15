import { useTranslation } from "react-i18next";
import { NavLink, Outlet } from "react-router";

import { useAuth } from "../auth/AuthContext";
import styles from "./AppShell.module.css";
import { BrandMark } from "./BrandMark";
import { Button } from "./Button";
import { LanguageSwitch } from "./LanguageSwitch";
import { ChangePasswordDialog } from "./ChangePasswordDialog";
import { ThemeSwitch } from "./ThemeSwitch";

/** Разделы показываются только тем, кому они доступны: недоступное не видно, а не «серое». */
const SECTIONS = [
  { to: "/servers", label: "servers", permission: "servers:read" },
  { to: "/users", label: "users", permission: "vpn_users:read" },
  { to: "/audit", label: "audit", permission: "audit:read" },
] as const;

const ROLES = ["superadmin", "operator", "viewer"] as const;

export function AppShell() {
  const { t } = useTranslation();
  const { state, can, logout } = useAuth();
  const roleName = (role: string) =>
    (ROLES as readonly string[]).includes(role) ? t(`shell.roles.${role as (typeof ROLES)[number]}`) : role;
  if (state.status !== "signed-in") return null;
  const { admin } = state;

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <BrandMark />
        <nav aria-label={t("shell.nav.label")} className={styles.nav}>
          {SECTIONS.filter((section) => can(section.permission)).map((section) => (
            <NavLink
              key={section.to}
              to={section.to}
              className={({ isActive }) => `${styles.link} ${isActive ? styles.active : ""}`}
            >
              {t(`shell.nav.${section.label}`)}
            </NavLink>
          ))}
        </nav>
        <div className={styles.account}>
          <p className={styles.email}>{admin.email}</p>
          <p className={styles.role}>{roleName(admin.role)}</p>
          <ThemeSwitch />
          <LanguageSwitch />
          <ChangePasswordDialog />
          <Button variant="quiet" onClick={() => void logout()}>
            {t("shell.nav.signOut")}
          </Button>
        </div>
      </aside>
      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}
