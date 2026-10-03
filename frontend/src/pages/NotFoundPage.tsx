import { Link } from "react-router";

export function NotFoundPage() {
  return (
    <section>
      <h1>Такой страницы нет</h1>
      <p>
        Адрес мог устареть. <Link to="/servers">Открыть список серверов</Link>
      </p>
    </section>
  );
}
