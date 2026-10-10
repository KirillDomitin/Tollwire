import { useTranslation } from "react-i18next";
import { Link } from "react-router";

export function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <section>
      <h1>{t("shell.notFound.title")}</h1>
      <p>
        {t("shell.notFound.hint")} <Link to="/servers">{t("shell.notFound.link")}</Link>
      </p>
    </section>
  );
}
