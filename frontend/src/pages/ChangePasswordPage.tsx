import { useTranslation } from "react-i18next";

import { MIN_PASSWORD_LENGTH, useAuth } from "../auth/AuthContext";
import { BrandMark } from "../components/BrandMark";
import { Button } from "../components/Button";
import { ChangePasswordForm } from "../components/ChangePasswordForm";
import styles from "./ChangePasswordPage.module.css";

/** Экран временного пароля: пока админ не задал свой, ничего другого в панели ему не показывается. */
export function ChangePasswordPage() {
  const { t } = useTranslation();
  const { logout } = useAuth();
  return (
    <div className={styles.page}>
      <section className={styles.card} aria-labelledby="change-password-title">
        <BrandMark />
        <h1 id="change-password-title" className={styles.title}>
          {t("auth.changePassword.forced.title")}
        </h1>
        <p className={styles.lead}>{t("auth.changePassword.forced.lead", { min: MIN_PASSWORD_LENGTH })}</p>
        <ChangePasswordForm
          currentLabel={t("auth.changePassword.currentTemporary")}
          secondary={
            <Button variant="secondary" onClick={() => void logout()}>
              {t("shell.nav.signOut")}
            </Button>
          }
        />
      </section>
    </div>
  );
}
