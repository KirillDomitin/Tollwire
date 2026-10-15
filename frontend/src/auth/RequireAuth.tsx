import { Navigate, Outlet, useLocation } from "react-router";

import { BrandMark } from "../components/BrandMark";
import { ChangePasswordPage } from "../pages/ChangePasswordPage";
import { useAuth } from "./AuthContext";

/**
 * Пускает дальше только вошедшего админа; пока сессия восстанавливается — экран ожидания.
 * С временным паролем панель закрыта (бэкенд всё равно ответит 403): вместо неё — экран смены пароля на том же
 * адресе, поэтому после смены админ остаётся там, куда шёл.
 */
export function RequireAuth() {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status === "checking") {
    return (
      <div style={{ display: "grid", placeItems: "center", height: "100%" }} aria-busy="true">
        <BrandMark />
      </div>
    );
  }
  if (state.status === "signed-out") {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (state.admin.must_change_password) return <ChangePasswordPage />;
  return <Outlet />;
}
