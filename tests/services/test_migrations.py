"""Миграции целиком: откат до нуля и обратно до head.

Ловит ошибки, на которые проект уже наступал: забытый DROP TYPE для enum в downgrade (11.1, 13.2),
безымянный внешний ключ, который downgrade не может удалить (12.3, 14.1).
"""

from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from core.database.postgres import engine, session_factory
from modules.jobs.models.job import Job, JobStatus, JobType
from modules.servers.models.server import VPNServer
from tests.conftest import run_alembic

LEFTOVERS_SQL = text(
    """
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    UNION ALL
    SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype = 'e'
    """
)


async def test_full_downgrade_and_upgrade_cycle() -> None:
    run_alembic("downgrade", "base")
    async with engine.connect() as conn:
        leftovers = (await conn.execute(LEFTOVERS_SQL)).scalars().all()
    assert leftovers == []  # после отката не осталось ни таблиц, ни enum-типов
    run_alembic("upgrade", "head")
    # Таблицы пересозданы — соединения пула держат подготовленные запросы к старым. Сбрасываем пул.
    await engine.dispose()


# --- ключ панели: server_credentials.{managed_key, public_key, key_fingerprint} и jobtype.INSTALL_SSH_KEY ---

KEY_COLUMNS_SQL = text(
    """
    SELECT column_name, data_type, is_nullable, column_default, character_maximum_length
    FROM information_schema.columns
    WHERE table_name = 'server_credentials' AND column_name IN ('managed_key', 'public_key', 'key_fingerprint')
    """
)
JOB_TYPES_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'jobtype'"
)


# Откат миграции ключа панели вместе со всем, что накатано поверх неё (ревизия перед ней: b587ddfe2d19).
BEFORE_SSH_KEY = "b587ddfe2d19"


async def _query(sql: object) -> list[tuple[object, ...]]:
    async with engine.connect() as conn:
        return [tuple(row) for row in (await conn.execute(sql)).all()]  # type: ignore[call-overload]


async def _alembic(*args: str) -> None:
    await engine.dispose()  # соединения пула держали бы старые типы и таблицы
    run_alembic(*args)
    await engine.dispose()


async def test_ssh_key_migration_adds_columns_and_job_type() -> None:
    columns = {row[0]: row for row in await _query(KEY_COLUMNS_SQL)}
    assert set(columns) == {"managed_key", "public_key", "key_fingerprint"}
    assert columns["managed_key"][1:3] == ("boolean", "NO") and "false" in str(columns["managed_key"][3])
    assert columns["public_key"][2] == "YES" and columns["key_fingerprint"][2] == "YES"
    assert columns["key_fingerprint"][4] == 100
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


async def test_ssh_key_migration_downgrade_removes_columns_and_job_type_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_SSH_KEY)
    try:
        assert await _query(KEY_COLUMNS_SQL) == []
        assert "INSTALL_SSH_KEY" not in {row[0] for row in await _query(JOB_TYPES_SQL)}
    finally:
        await _alembic("upgrade", "head")
    assert len(await _query(KEY_COLUMNS_SQL)) == 3
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


async def test_ssh_key_migration_downgrade_deletes_jobs_of_new_type_and_keeps_others() -> None:
    """Значение enum нельзя убрать, пока есть строки с ним: откат сначала удаляет такие задачи, остальные целы."""

    async with session_factory() as session:
        session.add(Job(type=JobType.INSTALL_SSH_KEY, status=JobStatus.SUCCESS))
        session.add(Job(type=JobType.HEALTH_CHECK, status=JobStatus.SUCCESS))
        await session.commit()
    await _alembic("downgrade", BEFORE_SSH_KEY)
    try:
        assert [row[0] for row in await _query(text("SELECT type::text FROM jobs"))] == ["HEALTH_CHECK"]
    finally:
        await _alembic("upgrade", "head")
    assert "INSTALL_SSH_KEY" in {row[0] for row in await _query(JOB_TYPES_SQL)}


# --- журнал аудита: составной индекс (created_at DESC, id DESC) под курсорную пагинацию ---

# Ревизия перед миграцией индекса: откатываем именно её, а не «на одну назад» (поверх могут лежать новые).
BEFORE_AUDIT_INDEX = "0a313252c0db"
AUDIT_INDEX_SQL = text(
    "SELECT indexdef FROM pg_indexes WHERE tablename = 'audit_logs' "
    "AND indexdef ILIKE '%created_at DESC%' AND indexdef ILIKE '%id DESC%'"
)


def _is_cursor_index(definition: object) -> bool:
    normalized = " ".join(str(definition).lower().split())
    return "(created_at desc, id desc)" in normalized


async def test_audit_cursor_index_exists_after_upgrade() -> None:
    assert any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))


async def test_audit_cursor_index_downgrade_removes_it_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_AUDIT_INDEX)
    try:
        assert not any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))
    finally:
        await _alembic("upgrade", "head")
    assert any(_is_cursor_index(row[0]) for row in await _query(AUDIT_INDEX_SQL))


# --- смена пароля: admins.{must_change_password, password_changed_at} ---

PASSWORD_COLUMNS_SQL = text(
    """
    SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
    WHERE table_name = 'admins' AND column_name IN ('must_change_password', 'password_changed_at')
    """
)
BEFORE_PASSWORD_CHANGE = "7c1e4a9d2b30"


async def test_password_change_migration_adds_columns_with_safe_defaults() -> None:
    columns = {row[0]: row for row in await _query(PASSWORD_COLUMNS_SQL)}
    assert set(columns) == {"must_change_password", "password_changed_at"}
    assert columns["must_change_password"][1:3] == ("boolean", "NO") and "false" in str(
        columns["must_change_password"][3]
    )
    assert columns["password_changed_at"][2] == "YES"


async def test_password_change_migration_keeps_existing_admins_unflagged_and_downgrades() -> None:
    """Админы, жившие до миграции, пароли выбирали сами — принудительной смены у них быть не должно."""
    await _alembic("downgrade", BEFORE_PASSWORD_CHANGE)
    try:
        assert await _query(PASSWORD_COLUMNS_SQL) == []
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admins (id, email, password_hash, is_active, role, created_at, updated_at) "
                    "VALUES (gen_random_uuid(), 'old@example.com', 'x', true, 'VIEWER', now(), now())"
                )
            )
    finally:
        await _alembic("upgrade", "head")
    assert await _query(text("SELECT must_change_password, password_changed_at FROM admins")) == [(False, None)]


# --- поиск Amnezia (23.2): protocoltype.{AMNEZIA_APP_AWG, AMNEZIA_APP_AWG2} и jobtype.DISCOVER_VPN ---

# Ревизия перед миграцией поиска (прежняя голова): откатываем именно к ней, а не «на одну назад».
BEFORE_DISCOVERY = "e4f1a7b39c20"
PROTOCOL_TYPES_SQL = text(
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = 'protocoltype'"
)
NEW_PROTOCOL_TYPES = {"AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2"}


async def _labels(sql: object) -> set[object]:
    return {row[0] for row in await _query(sql)}


async def _add_server() -> UUID:
    async with session_factory() as session:
        server = VPNServer(name="migration-srv", host="203.0.113.10")
        session.add(server)
        await session.commit()
        return server.id


async def test_discovery_migration_adds_protocol_and_job_types() -> None:
    assert NEW_PROTOCOL_TYPES | {"AMNEZIA_WG"} <= await _labels(PROTOCOL_TYPES_SQL)
    assert "DISCOVER_VPN" in await _labels(JOB_TYPES_SQL)
    assert "INSTALL_SSH_KEY" in await _labels(JOB_TYPES_SQL)  # прежние значения на месте


async def test_discovery_migration_new_types_are_usable_by_the_orm() -> None:
    """Значения enum в БД совпадают с теми, что пишет код (ORM хранит ИМЕНА членов enum)."""
    from modules.protocols.models.protocol import ProtocolType, VPNProtocol

    server_id = await _add_server()
    async with session_factory() as session:
        session.add(VPNProtocol(server_id=server_id, type=ProtocolType("amnezia_app_awg2"), port=1))
        session.add(Job(type=JobType("discover_vpn"), status=JobStatus.SUCCESS, server_id=server_id))
        await session.commit()
    assert await _query(text("SELECT type::text FROM protocols")) == [("AMNEZIA_APP_AWG2",)]
    assert await _query(text("SELECT type::text FROM jobs")) == [("DISCOVER_VPN",)]


async def test_discovery_migration_downgrade_removes_new_values_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        assert await _labels(PROTOCOL_TYPES_SQL) == {"AMNEZIA_WG"}
        assert "DISCOVER_VPN" not in await _labels(JOB_TYPES_SQL)
        assert "INSTALL_SSH_KEY" in await _labels(JOB_TYPES_SQL)
    finally:
        await _alembic("upgrade", "head")
    assert NEW_PROTOCOL_TYPES <= await _labels(PROTOCOL_TYPES_SQL)
    assert "DISCOVER_VPN" in await _labels(JOB_TYPES_SQL)


async def test_discovery_migration_downgrade_deletes_rows_of_new_types_and_keeps_others() -> None:
    """Значение enum нельзя убрать, пока есть строки с ним: откат сначала удаляет протоколы и задачи новых типов
    (что панель «подключила» из найденного — теряется), остальное цело."""
    server_id = await _add_server()
    async with engine.begin() as conn:
        for protocol_type in ("AMNEZIA_WG", "AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2"):
            await conn.execute(
                text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, :type)"),
                {"sid": server_id, "type": protocol_type},
            )
        for job_type in ("DISCOVER_VPN", "HEALTH_CHECK", "INSTALL_SSH_KEY"):
            await conn.execute(
                text("INSERT INTO jobs (id, type, status) VALUES (gen_random_uuid(), :type, 'SUCCESS')"),
                {"type": job_type},
            )
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        assert await _query(text("SELECT type::text FROM protocols")) == [("AMNEZIA_WG",)]
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM jobs"))) == [
            "HEALTH_CHECK",
            "INSTALL_SSH_KEY",
        ]
        assert await _labels(PROTOCOL_TYPES_SQL) == {"AMNEZIA_WG"}
    finally:
        await _alembic("upgrade", "head")
    assert NEW_PROTOCOL_TYPES <= await _labels(PROTOCOL_TYPES_SQL)


async def test_discovery_migration_downgrade_keeps_protocols_table_defaults() -> None:
    """Пересоздание enum не должно потерять уникальность (server_id, type) и значение по умолчанию."""
    await _alembic("downgrade", BEFORE_DISCOVERY)
    try:
        server_id = await _add_server()
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, 'AMNEZIA_WG')"),
                {"sid": server_id},
            )
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, 'AMNEZIA_WG')"),
                    {"sid": server_id},
                )
        except Exception as exc:  # noqa: BLE001 — нужен именно отказ БД по уникальному ограничению
            assert "unique" in str(exc).lower()
        else:
            raise AssertionError("UniqueConstraint(server_id, type) was lost by the downgrade")
    finally:
        await _alembic("upgrade", "head")


# --- импорт клиентов из AmneziaVPN (23.4): email и конфиг могут отсутствовать, источник credential, jobs.params ---

# Ревизия перед миграцией импорта (прежняя голова): откатываем именно к ней, а не «на одну назад».
BEFORE_CLIENT_IMPORT = "d3a7c9e15b42"
IMPORT_COLUMNS_SQL = text(
    """
    SELECT table_name || '.' || column_name, data_type, is_nullable, column_default, character_maximum_length
    FROM information_schema.columns
    WHERE (table_name, column_name) IN (
        ('vpn_users', 'email'), ('vpn_credentials', 'encrypted_config'), ('vpn_credentials', 'source'),
        ('vpn_credentials', 'encrypted_psk'), ('vpn_credentials', 'external_name'), ('jobs', 'params')
    )
    """
)
NEW_IMPORT_COLUMNS = {
    "vpn_credentials.source",
    "vpn_credentials.encrypted_psk",
    "vpn_credentials.external_name",
    "jobs.params",
}
CHECK_NAME = "ck_vpn_credentials_issued_has_config"
CHECK_SQL = text(f"SELECT conname FROM pg_constraint WHERE conname = '{CHECK_NAME}'")


async def _import_columns() -> dict[str, tuple[object, ...]]:
    return {str(row[0]): row[1:] for row in await _query(IMPORT_COLUMNS_SQL)}


async def _insert_user(conn: AsyncConnection, email: str | None, username: str = "u") -> UUID:
    user_id = uuid4()
    await conn.execute(
        text("INSERT INTO vpn_users (id, username, email) VALUES (:id, :username, :email)"),
        {"id": user_id, "username": username, "email": email},
    )
    return user_id


async def _insert_grant(conn: AsyncConnection, server_id: UUID, user_id: UUID, protocol_type: str) -> UUID:
    """Протокол сервера (если его ещё нет), подписка и грант пользователя; возвращает id гранта."""
    await conn.execute(
        text(
            "INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, :type) "
            "ON CONFLICT (server_id, type) DO NOTHING"
        ),
        {"sid": server_id, "type": protocol_type},
    )
    protocol_id = (
        await conn.execute(
            text("SELECT id FROM protocols WHERE server_id = :sid AND type = :type"),
            {"sid": server_id, "type": protocol_type},
        )
    ).scalar_one()
    subscription_id, grant_id = uuid4(), uuid4()
    await conn.execute(
        text(
            "INSERT INTO subscriptions (id, user_id, status, valid_from, valid_until) "
            "VALUES (:id, :uid, 'ACTIVE', now() - interval '1 day', now() + interval '30 days')"
        ),
        {"id": subscription_id, "uid": user_id},
    )
    await conn.execute(
        text(
            "INSERT INTO access_grants (id, subscription_id, server_id, protocol_id, status) "
            "VALUES (:id, :sub, :sid, :pid, 'ACTIVE')"
        ),
        {"id": grant_id, "sub": subscription_id, "sid": server_id, "pid": protocol_id},
    )
    return grant_id


async def _insert_credential(
    conn: AsyncConnection, grant_id: UUID, key: str, *, source: str | None = None, config: str | None = "enc"
) -> None:
    params: dict[str, object] = {"id": uuid4(), "gid": grant_id, "config": config, "key": key}
    columns = "id, access_grant_id, encrypted_config, client_address, client_public_key"
    values = ":id, :gid, :config, '10.8.1.2', :key"
    if source is not None:
        columns, values, params["source"] = columns + ", source", values + ", :source", source
    await conn.execute(text(f"INSERT INTO vpn_credentials ({columns}) VALUES ({values})"), params)


async def test_client_import_migration_changes_columns_as_agreed() -> None:
    columns = await _import_columns()
    assert set(columns) == NEW_IMPORT_COLUMNS | {"vpn_users.email", "vpn_credentials.encrypted_config"}
    assert columns["vpn_users.email"][1] == "YES" and columns["vpn_credentials.encrypted_config"][1] == "YES"
    data_type, nullable, default, length = columns["vpn_credentials.source"]
    assert (data_type, nullable, length) == ("character varying", "NO", 16) and "ISSUED" in str(default)
    assert columns["vpn_credentials.encrypted_psk"][:2] == ("text", "YES")
    data_type, nullable, _, length = columns["vpn_credentials.external_name"]
    assert (data_type, nullable, length) == ("character varying", "YES", 100)
    assert columns["jobs.params"][:2] == ("jsonb", "YES")
    assert "IMPORT_CLIENTS" in await _labels(JOB_TYPES_SQL) and "DISCOVER_VPN" in await _labels(JOB_TYPES_SQL)
    assert await _query(CHECK_SQL) == [(CHECK_NAME,)]
    enum_types = {row[0] for row in await _query(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))}
    assert "credentialsource" not in enum_types  # source — VARCHAR (native_enum=False), а не тип Postgres


async def test_client_import_migration_keeps_email_unique_but_allows_several_nulls() -> None:
    async with engine.begin() as conn:
        await _insert_user(conn, None, "a")
        await _insert_user(conn, None, "b")  # несколько NULL допустимы
        await _insert_user(conn, "same@example.com", "c")
    try:
        async with engine.begin() as conn:
            await _insert_user(conn, "same@example.com", "d")
    except Exception as exc:  # noqa: BLE001 — нужен именно отказ БД по уникальности
        assert "unique" in str(exc).lower()
    else:
        raise AssertionError("vpn_users.email lost its uniqueness")


async def test_client_import_migration_check_requires_config_only_for_issued_credentials() -> None:
    server_id = await _add_server()
    async with engine.begin() as conn:
        # Живой credential у гранта один (uq_vpn_credentials_live_per_grant): каждому — свой пользователь и грант.
        grants = [
            await _insert_grant(
                conn, server_id, await _insert_user(conn, f"check{i}@example.com", f"u{i}"), "AMNEZIA_WG"
            )
            for i in range(4)
        ]
        await _insert_credential(conn, grants[0], "k-issued", source="ISSUED")
        await _insert_credential(conn, grants[1], "k-default")  # source по умолчанию — ISSUED
        await _insert_credential(conn, grants[2], "k-imported", source="IMPORTED", config=None)
    assert await _query(text("SELECT client_public_key, source FROM vpn_credentials ORDER BY client_public_key")) == [
        ("k-default", "ISSUED"),
        ("k-imported", "IMPORTED"),
        ("k-issued", "ISSUED"),
    ]
    for source in ("ISSUED", None):  # выданный (в том числе по умолчанию) credential без конфига БД не принимает
        try:
            async with engine.begin() as conn:
                await _insert_credential(conn, grants[3], "k-bad", source=source, config=None)
        except Exception as exc:  # noqa: BLE001
            assert CHECK_NAME in str(exc)
        else:
            raise AssertionError(f"ISSUED credential without a config was accepted (source={source!r})")


async def test_client_import_migration_new_values_are_usable_by_the_orm() -> None:
    """Имена членов enum в БД совпадают с теми, что пишет код: source хранится именем (IMPORTED), задача IMPORT_CLIENTS
    и действия аудита (VARCHAR — миграция для них не нужна) принимаются, email может быть NULL."""
    from core.security import encrypt_secret
    from modules.access.models.access_grant import AccessGrant
    from modules.access.models.subscription import Subscription
    from modules.access.models.vpn_credential import VPNCredential
    from modules.audit.models.audit_log import AuditAction, AuditLog
    from modules.protocols.models.protocol import ProtocolType, VPNProtocol
    from modules.vpn_users.models.user import VPNUser
    from tests.import_support import credential_source

    server_id = await _add_server()
    async with session_factory() as session:
        protocol = VPNProtocol(server_id=server_id, type=ProtocolType.AMNEZIA_APP_AWG2, port=1)
        user = VPNUser(username="orm", email=None)
        session.add_all([protocol, user])
        await session.flush()
        now = (await session.execute(text("SELECT now()"))).scalar_one()
        subscription = Subscription(user_id=user.id, valid_from=now, valid_until=now)
        session.add(subscription)
        await session.flush()
        grant = AccessGrant(subscription_id=subscription.id, server_id=server_id, protocol_id=protocol.id)
        session.add(grant)
        await session.flush()
        session.add(
            VPNCredential(
                access_grant_id=grant.id,
                encrypted_config=None,
                client_address="10.8.1.2",
                client_public_key="k",
                source=credential_source("imported"),
                encrypted_psk=encrypt_secret("psk"),
                external_name="Phone",
            )
        )
        session.add(Job(type=JobType("import_clients"), status=JobStatus.SUCCESS, server_id=server_id, params={"a": 1}))
        session.add(AuditLog(action=AuditAction("clients_import_started")))
        await session.commit()
    assert await _query(text("SELECT source, external_name FROM vpn_credentials")) == [("IMPORTED", "Phone")]
    assert await _query(text("SELECT type::text, params FROM jobs")) == [("IMPORT_CLIENTS", {"a": 1})]
    assert await _query(text("SELECT action FROM audit_logs")) == [("CLIENTS_IMPORT_STARTED",)]
    assert await _query(text("SELECT email FROM vpn_users")) == [(None,)]


async def test_client_import_migration_downgrade_restores_old_schema_then_upgrade_restores_new() -> None:
    await _alembic("downgrade", BEFORE_CLIENT_IMPORT)
    try:
        columns = await _import_columns()
        assert set(columns) == {"vpn_users.email", "vpn_credentials.encrypted_config"}  # новые колонки сняты
        assert columns["vpn_users.email"][1] == "NO" and columns["vpn_credentials.encrypted_config"][1] == "NO"
        assert "IMPORT_CLIENTS" not in await _labels(JOB_TYPES_SQL)
        assert {"DISCOVER_VPN", "INSTALL_SSH_KEY"} <= await _labels(JOB_TYPES_SQL)
        assert await _query(CHECK_SQL) == []
        default_sql = text(
            "SELECT column_default FROM information_schema.columns WHERE table_name = 'jobs' AND column_name = 'type'"
        )
        assert "HEALTH_CHECK" in str(await _query(default_sql))  # значение по умолчанию jobs.type пережило enum
    finally:
        await _alembic("upgrade", "head")
    columns = await _import_columns()
    assert set(columns) == NEW_IMPORT_COLUMNS | {"vpn_users.email", "vpn_credentials.encrypted_config"}
    assert columns["vpn_users.email"][1] == "YES"
    assert "IMPORT_CLIENTS" in await _labels(JOB_TYPES_SQL)
    assert await _query(CHECK_SQL) == [(CHECK_NAME,)]


async def test_client_import_migration_downgrade_with_imported_rows_does_not_fail_and_removes_only_them() -> None:
    """Откат не должен падать на данных нового вида: импортированные доступы (вместе с credential'ами) и задачи
    IMPORT_CLIENTS удаляются, обычные строки остаются, у пользователей без email появляется заглушка."""
    server_id = await _add_server()
    async with engine.begin() as conn:
        issued_user = await _insert_user(conn, "issued@example.com", "issued")
        imported_user = await _insert_user(conn, None, "imported")
        issued_grant = await _insert_grant(conn, server_id, issued_user, "AMNEZIA_WG")
        imported_grant = await _insert_grant(conn, server_id, imported_user, "AMNEZIA_APP_AWG2")
        await _insert_credential(conn, issued_grant, "k-issued", source="ISSUED")
        await _insert_credential(conn, imported_grant, "k-imported", source="IMPORTED", config=None)
        for job_type in ("IMPORT_CLIENTS", "HEALTH_CHECK", "DISCOVER_VPN"):
            await conn.execute(
                text("INSERT INTO jobs (id, type, status) VALUES (gen_random_uuid(), :type, 'SUCCESS')"),
                {"type": job_type},
            )
    await _alembic("downgrade", BEFORE_CLIENT_IMPORT)
    try:
        assert await _query(text("SELECT client_public_key FROM vpn_credentials")) == [("k-issued",)]
        assert await _query(text("SELECT id FROM access_grants")) == [(issued_grant,)]
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM jobs"))) == [
            "DISCOVER_VPN",
            "HEALTH_CHECK",
        ]
        emails = {row[0]: row[1] for row in await _query(text("SELECT id, email FROM vpn_users"))}
        assert emails[issued_user] == "issued@example.com"
        assert emails[imported_user] == f"{imported_user}@imported.invalid"  # заглушка вместо NULL
    finally:
        await _alembic("upgrade", "head")
    assert await _query(text("SELECT source FROM vpn_credentials")) == [("ISSUED",)]  # обычные строки уцелели


async def test_client_import_migration_downgrade_keeps_a_grant_that_also_has_an_issued_credential() -> None:
    """После отзыва импортированного клиента по тому же гранту выпустили обычный: откат убирает только
    импортированный credential, а грант с живым обычным остаётся."""
    server_id = await _add_server()
    async with engine.begin() as conn:
        user = await _insert_user(conn, "both@example.com", "both")
        grant = await _insert_grant(conn, server_id, user, "AMNEZIA_APP_AWG2")
        await _insert_credential(conn, grant, "k-old-imported", source="IMPORTED", config=None)
        await conn.execute(
            text("UPDATE vpn_credentials SET revoked_at = now() WHERE client_public_key = 'k-old-imported'")
        )
        await _insert_credential(conn, grant, "k-new-issued", source="ISSUED")
    await _alembic("downgrade", BEFORE_CLIENT_IMPORT)
    try:
        assert await _query(text("SELECT client_public_key FROM vpn_credentials")) == [("k-new-issued",)]
        assert await _query(text("SELECT id FROM access_grants")) == [(grant,)]
    finally:
        await _alembic("upgrade", "head")


# --- шлюз (24.1): protocoltype.AMNEZIA_GATEWAY ---

# Ревизия перед миграцией шлюза (прежняя голова): откатываем именно к ней.
BEFORE_GATEWAY = "f6b2d8a41c73"
PROTOCOL_TYPES_BEFORE_GATEWAY = {"AMNEZIA_WG", "AMNEZIA_APP_AWG", "AMNEZIA_APP_AWG2"}


async def test_gateway_migration_adds_the_protocol_type() -> None:
    assert await _labels(PROTOCOL_TYPES_SQL) == PROTOCOL_TYPES_BEFORE_GATEWAY | {"AMNEZIA_GATEWAY"}


async def test_gateway_migration_new_type_is_usable_by_the_orm() -> None:
    from modules.protocols.models.protocol import ProtocolType, VPNProtocol

    server_id = await _add_server()
    async with session_factory() as session:
        session.add(VPNProtocol(server_id=server_id, type=ProtocolType("amnezia_gateway"), port=1))
        await session.commit()
    assert await _query(text("SELECT type::text FROM protocols")) == [("AMNEZIA_GATEWAY",)]


async def test_gateway_migration_downgrade_removes_the_value_then_upgrade_restores() -> None:
    await _alembic("downgrade", BEFORE_GATEWAY)
    try:
        assert await _labels(PROTOCOL_TYPES_SQL) == PROTOCOL_TYPES_BEFORE_GATEWAY
    finally:
        await _alembic("upgrade", "head")
    assert "AMNEZIA_GATEWAY" in await _labels(PROTOCOL_TYPES_SQL)


async def test_gateway_migration_downgrade_deletes_gateway_rows_with_their_grants_and_keeps_others() -> None:
    """Значение enum нельзя убрать, пока есть строки с ним: откат удаляет протоколы шлюза (каскадом - их доступы и
    credential'ы); обычные и приложения остаются со своими клиентами."""
    server_id = await _add_server()
    async with engine.begin() as conn:
        # Одна живая подписка на пользователя: у каждого доступа свой пользователь.
        users = [await _insert_user(conn, f"gw{index}@example.com", f"gw{index}") for index in range(3)]
        kept_grants = [
            await _insert_grant(conn, server_id, users[0], "AMNEZIA_WG"),
            await _insert_grant(conn, server_id, users[1], "AMNEZIA_APP_AWG2"),
        ]
        gateway_grant = await _insert_grant(conn, server_id, users[2], "AMNEZIA_GATEWAY")
        await _insert_credential(conn, kept_grants[0], "k-wg")
        await _insert_credential(conn, gateway_grant, "k-gateway")
    await _alembic("downgrade", BEFORE_GATEWAY)
    try:
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM protocols"))) == [
            "AMNEZIA_APP_AWG2",
            "AMNEZIA_WG",
        ]
        assert sorted(row[0] for row in await _query(text("SELECT id FROM access_grants"))) == sorted(kept_grants)
        assert await _query(text("SELECT client_public_key FROM vpn_credentials")) == [("k-wg",)]
        assert await _labels(PROTOCOL_TYPES_SQL) == PROTOCOL_TYPES_BEFORE_GATEWAY
    finally:
        await _alembic("upgrade", "head")
    assert "AMNEZIA_GATEWAY" in await _labels(PROTOCOL_TYPES_SQL)
    assert await _query(text("SELECT count(*) FROM protocols")) == [(2,)]


async def test_gateway_migration_downgrade_keeps_the_unique_constraint_and_stays_idempotent() -> None:
    """Пересоздание enum не теряет уникальность (server_id, type); upgrade → downgrade → upgrade не падает."""
    await _alembic("downgrade", BEFORE_GATEWAY)
    try:
        server_id = await _add_server()
        insert = text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :sid, 'AMNEZIA_WG')")
        async with engine.begin() as conn:
            await conn.execute(insert, {"sid": server_id})
        try:
            async with engine.begin() as conn:
                await conn.execute(insert, {"sid": server_id})
        except Exception as exc:  # noqa: BLE001 - нужен именно отказ БД по уникальному ограничению
            assert "unique" in str(exc).lower()
        else:
            raise AssertionError("UniqueConstraint(server_id, type) was lost by the downgrade")
    finally:
        await _alembic("upgrade", "head")
    await _alembic("downgrade", BEFORE_GATEWAY)
    await _alembic("upgrade", "head")
    assert "AMNEZIA_GATEWAY" in await _labels(PROTOCOL_TYPES_SQL)


# --- выходы шлюза (24.2): таблица gateway_exits и jobtype.GATEWAY_*_EXIT ---

BEFORE_GATEWAY_EXITS = "a8c4e1f7b925"  # голова перед миграцией выходов
EXIT_JOB_TYPES = {"GATEWAY_ADD_EXIT", "GATEWAY_REMOVE_EXIT"}
EXITS_TABLE_SQL = text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'gateway_exits'")
EXITS_CONSTRAINTS_SQL = text(
    "SELECT conname, contype::text FROM pg_constraint WHERE conrelid = 'gateway_exits'::regclass ORDER BY conname"
)


_INSERT_SERVER = text("INSERT INTO vpn_servers (id, name, host) VALUES (gen_random_uuid(), :n, 'h') RETURNING id")
_INSERT_PROTOCOL = text("INSERT INTO protocols (id, server_id, type) VALUES (gen_random_uuid(), :s, :t) RETURNING id")


async def _exit_scene() -> tuple[UUID, UUID, UUID, UUID]:
    """Два сервера: на первом протокол шлюза, на втором AMNEZIA_WG. Возвращает (сервер, протокол) шлюза и выхода."""
    ids: list[UUID] = []
    async with engine.begin() as conn:
        for name, protocol_type in (("gw-srv", "AMNEZIA_GATEWAY"), ("exit-srv", "AMNEZIA_WG")):
            server_id = (await conn.execute(_INSERT_SERVER, {"n": name})).scalar_one()
            protocol_id = (await conn.execute(_INSERT_PROTOCOL, {"s": server_id, "t": protocol_type})).scalar_one()
            ids += [server_id, protocol_id]
    return ids[0], ids[1], ids[2], ids[3]


_INSERT_EXIT = text(
    "INSERT INTO gateway_exits (id, gateway_protocol_id, exit_protocol_id, slot) "
    "VALUES (gen_random_uuid(), :g, :e, :slot)"
)


async def test_gateway_exits_migration_creates_the_table_and_job_types() -> None:
    assert await _query(EXITS_TABLE_SQL) == [("gateway_exits",)]
    assert EXIT_JOB_TYPES <= await _labels(JOB_TYPES_SQL)
    names = {row[0] for row in await _query(EXITS_CONSTRAINTS_SQL)}
    assert {
        "uq_gateway_exits_slot",
        "uq_gateway_exits_exit",
        "ck_gateway_exits_slot",
        "ck_gateway_exits_weight",
        "ck_gateway_exits_max_clients",
        "gateway_exits_gateway_protocol_id_fkey",
        "gateway_exits_exit_protocol_id_fkey",
    } <= names


async def test_gateway_exits_defaults_and_the_orm_agree() -> None:
    from modules.gateway.models.gateway_exit import ExitState, GatewayExit

    _, gateway, _, exit_protocol = await _exit_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_EXIT, {"g": gateway, "e": exit_protocol, "slot": 1})
    async with session_factory() as session:
        (row,) = (await session.execute(select(GatewayExit))).scalars().all()
        assert (row.slot, row.weight, row.state, row.max_clients) == (1, 1, ExitState.PENDING, None)
        assert row.client_public_key is None and row.encrypted_config is None and row.health is None
        assert row.created_at is not None and row.updated_at is not None
    assert await _query(text("SELECT state FROM gateway_exits")) == [("PENDING",)]


async def test_gateway_exits_integrity_rules() -> None:
    _, gateway, _, exit_protocol = await _exit_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_EXIT, {"g": gateway, "e": exit_protocol, "slot": 1})

    async def refused(sql: object, params: dict[str, object]) -> str:
        try:
            async with engine.begin() as conn:
                await conn.execute(sql, params)  # type: ignore[call-overload]
        except Exception as exc:  # noqa: BLE001 - нужен именно отказ БД
            return str(exc).lower()
        raise AssertionError("the database accepted a row it must refuse")

    assert "uq_gateway_exits_exit" in await refused(_INSERT_EXIT, {"g": gateway, "e": exit_protocol, "slot": 2})
    other = text(
        "INSERT INTO protocols (id, server_id, type) SELECT gen_random_uuid(), server_id, 'AMNEZIA_APP_AWG' "
        "FROM protocols WHERE id = :e RETURNING id"
    )
    async with engine.begin() as conn:
        other_protocol = (await conn.execute(other, {"e": exit_protocol})).scalar_one()
    assert "uq_gateway_exits_slot" in await refused(_INSERT_EXIT, {"g": gateway, "e": other_protocol, "slot": 1})
    for bad_slot in (0, 201):
        assert "ck_gateway_exits_slot" in await refused(
            _INSERT_EXIT, {"g": gateway, "e": other_protocol, "slot": bad_slot}
        )
    for bad_weight in (0, 101):
        sql = text(
            "INSERT INTO gateway_exits (id, gateway_protocol_id, exit_protocol_id, slot, weight) "
            "VALUES (gen_random_uuid(), :g, :e, 5, :w)"
        )
        assert "ck_gateway_exits_weight" in await refused(sql, {"g": gateway, "e": other_protocol, "w": bad_weight})
    sql = text(
        "INSERT INTO gateway_exits (id, gateway_protocol_id, exit_protocol_id, slot, max_clients) "
        "VALUES (gen_random_uuid(), :g, :e, 5, 0)"
    )
    assert "ck_gateway_exits_max_clients" in await refused(sql, {"g": gateway, "e": other_protocol})


async def test_gateway_exits_foreign_keys_restrict_the_exit_and_cascade_from_the_gateway() -> None:
    _, gateway, _, exit_protocol = await _exit_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_EXIT, {"g": gateway, "e": exit_protocol, "slot": 1})
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM protocols WHERE id = :e"), {"e": exit_protocol})
    except Exception as exc:  # noqa: BLE001 - RESTRICT: пока выход подключён, его протокол не удалить
        assert "gateway_exits" in str(exc).lower()
    else:
        raise AssertionError("the exit protocol was deleted while a gateway uses it")
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM protocols WHERE id = :g"), {"g": gateway})
    assert await _query(text("SELECT count(*) FROM gateway_exits")) == [(0,)]


async def test_gateway_exits_migration_downgrade_drops_the_table_and_the_job_types_then_upgrade_restores() -> None:
    _, gateway, _, exit_protocol = await _exit_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_EXIT, {"g": gateway, "e": exit_protocol, "slot": 1})
        for job_type in ("GATEWAY_ADD_EXIT", "GATEWAY_REMOVE_EXIT", "HEALTH_CHECK", "IMPORT_CLIENTS"):
            await conn.execute(
                text("INSERT INTO jobs (id, type, status) VALUES (gen_random_uuid(), :type, 'SUCCESS')"),
                {"type": job_type},
            )
    await _alembic("downgrade", BEFORE_GATEWAY_EXITS)
    try:
        assert await _query(EXITS_TABLE_SQL) == []
        assert not EXIT_JOB_TYPES & await _labels(JOB_TYPES_SQL)
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM jobs"))) == [
            "HEALTH_CHECK",
            "IMPORT_CLIENTS",
        ]
        assert await _query(text("SELECT count(*) FROM protocols")) == [(2,)]  # протоколы на месте
    finally:
        await _alembic("upgrade", "head")
    assert EXIT_JOB_TYPES <= await _labels(JOB_TYPES_SQL)
    assert await _query(EXITS_TABLE_SQL) == [("gateway_exits",)]
    assert await _query(text("SELECT count(*) FROM gateway_exits")) == [(0,)]  # выходы откат не переживают


async def test_gateway_exits_migration_cycle_is_idempotent_and_keeps_the_job_default() -> None:
    await _alembic("downgrade", BEFORE_GATEWAY_EXITS)
    await _alembic("upgrade", "head")
    await _alembic("downgrade", BEFORE_GATEWAY_EXITS)
    try:
        default = await _query(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'jobs' AND column_name = 'type'"
            )
        )
        assert "HEALTH_CHECK" in str(default)  # DEFAULT пересозданного enum на месте
    finally:
        await _alembic("upgrade", "head")
    assert EXIT_JOB_TYPES <= await _labels(JOB_TYPES_SQL)


# --- назначения выходов клиентам (24.3): таблица gateway_assignments ---

BEFORE_ASSIGNMENTS = "c7e1b4d8a932"  # голова перед миграцией назначений
ASSIGNMENTS_TABLE_SQL = text(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = 'gateway_assignments'"
)


async def _assignment_scene() -> tuple[UUID, UUID, UUID]:
    """Шлюз с двумя выходами и клиентом шлюза: возвращает (credential, выход 1, выход 2)."""
    gateway_server, gateway, _, first_protocol = await _exit_scene()
    async with engine.begin() as conn:
        second_protocol = (
            await conn.execute(
                _INSERT_PROTOCOL,
                {"s": (await conn.execute(_INSERT_SERVER, {"n": "exit-two"})).scalar_one(), "t": "AMNEZIA_WG"},
            )
        ).scalar_one()
        exits = []
        for slot, exit_protocol in ((1, first_protocol), (2, second_protocol)):
            exits.append(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO gateway_exits (id, gateway_protocol_id, exit_protocol_id, slot) "
                            "VALUES (gen_random_uuid(), :g, :e, :slot) RETURNING id"
                        ),
                        {"g": gateway, "e": exit_protocol, "slot": slot},
                    )
                ).scalar_one()
            )
        user = await _insert_user(conn, "gwa@example.com", "gwa")
        grant = await _insert_grant(conn, gateway_server, user, "AMNEZIA_GATEWAY")
        credential = (
            await conn.execute(
                text(
                    "INSERT INTO vpn_credentials (id, access_grant_id, encrypted_config, client_address, "
                    "client_public_key) VALUES (gen_random_uuid(), :g, 'enc', '10.77.0.2', 'k') RETURNING id"
                ),
                {"g": grant},
            )
        ).scalar_one()
    return credential, exits[0], exits[1]


_INSERT_ASSIGNMENT = text(
    "INSERT INTO gateway_assignments (credential_id, exit_id, pinned_exit_id) VALUES (:c, :e, :p)"
)


async def test_assignments_migration_creates_the_table_with_defaults_and_the_orm_agrees() -> None:
    from modules.gateway.models.gateway_assignment import AssignReason, GatewayAssignment

    assert await _query(ASSIGNMENTS_TABLE_SQL) == [("gateway_assignments",)]
    credential, first, _ = await _assignment_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": first, "p": None})
    async with session_factory() as session:
        row = await session.get(GatewayAssignment, credential)
        assert row is not None and (row.exit_id, row.pinned_exit_id, row.reason) == (first, None, AssignReason.ISSUE)
        assert row.assigned_at is not None
    assert await _query(text("SELECT reason FROM gateway_assignments")) == [("ISSUE",)]


async def test_assignments_are_one_per_credential() -> None:
    credential, first, second = await _assignment_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": first, "p": None})
    try:
        async with engine.begin() as conn:
            await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": second, "p": None})
    except Exception as exc:  # noqa: BLE001 - нужен именно отказ БД по первичному ключу
        assert "gateway_assignments_pkey" in str(exc)
    else:
        raise AssertionError("a credential got two assignments")


async def test_assignments_follow_their_credential_and_exit_and_the_pin_is_released() -> None:
    credential, first, second = await _assignment_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": first, "p": second})
    async with engine.begin() as conn:  # закреплённый выход удалён: назначение живо, закрепление снято
        await conn.execute(text("DELETE FROM gateway_exits WHERE id = :id"), {"id": second})
    assert await _query(text("SELECT exit_id, pinned_exit_id FROM gateway_assignments")) == [(first, None)]
    async with engine.begin() as conn:  # выход удалён: назначение уходит каскадом
        await conn.execute(text("DELETE FROM gateway_exits WHERE id = :id"), {"id": first})
    assert await _query(text("SELECT count(*) FROM gateway_assignments")) == [(0,)]


async def test_assignments_go_with_a_deleted_credential() -> None:
    credential, first, _ = await _assignment_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": first, "p": None})
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM vpn_credentials WHERE id = :id"), {"id": credential})
    assert await _query(text("SELECT count(*) FROM gateway_assignments")) == [(0,)]


async def test_assignments_migration_downgrade_drops_only_the_table_then_upgrade_restores() -> None:
    credential, first, _ = await _assignment_scene()
    async with engine.begin() as conn:
        await conn.execute(_INSERT_ASSIGNMENT, {"c": credential, "e": first, "p": None})
    await _alembic("downgrade", BEFORE_ASSIGNMENTS)
    try:
        assert await _query(ASSIGNMENTS_TABLE_SQL) == []
        assert await _query(EXITS_TABLE_SQL) == [("gateway_exits",)]  # выходы и клиенты на месте
        assert await _query(text("SELECT count(*) FROM gateway_exits")) == [(2,)]
        assert await _query(text("SELECT count(*) FROM vpn_credentials")) == [(1,)]
    finally:
        await _alembic("upgrade", "head")
    assert await _query(ASSIGNMENTS_TABLE_SQL) == [("gateway_assignments",)]
    assert await _query(text("SELECT count(*) FROM gateway_assignments")) == [(0,)]


async def test_assignments_migration_adds_the_drain_counters_with_zero_defaults_and_downgrade_removes_them() -> None:
    columns_sql = text(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'gateway_exits' AND column_name IN ('drained_moved', 'drained_unassigned') "
        "ORDER BY column_name"
    )
    rows = await _query(columns_sql)
    assert [(name, nullable) for name, nullable, _ in rows] == [("drained_moved", "NO"), ("drained_unassigned", "NO")]
    assert all("0" in str(default) for _, _, default in rows)
    _, first, _ = await _assignment_scene()
    assert await _query(
        text("SELECT drained_moved, drained_unassigned FROM gateway_exits WHERE id = :id").bindparams(id=first)
    ) == [(0, 0)]
    await _alembic("downgrade", BEFORE_ASSIGNMENTS)
    try:
        assert await _query(columns_sql) == []
    finally:
        await _alembic("upgrade", "head")
    assert len(await _query(columns_sql)) == 2


# --- «Россия напрямую» (24.4): geoip_lists, geoip_overrides и jobtype.GEOIP_UPDATE / GATEWAY_APPLY_RU ---

BEFORE_GEOIP = "d2f8a6c3b514"  # голова перед миграцией geoip
GEOIP_JOB_TYPES = {"GEOIP_UPDATE", "GATEWAY_APPLY_RU"}
GEOIP_TABLES_SQL = text(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'geoip\\_%' ORDER BY tablename"
)
_INSERT_LIST = text(
    "INSERT INTO geoip_lists (id, source, version, sha256, prefixes_count, addresses_count, data, status) "
    "VALUES (gen_random_uuid(), 'ripe', '20261020', :sha, 3000, 3000000, '1.0.0.0/24', :status)"
)
_INSERT_OVERRIDE = text(
    "INSERT INTO geoip_overrides (id, network, action) VALUES (gen_random_uuid(), :network, 'DIRECT')"
)


async def _refused(sql: object, params: dict[str, object]) -> str:
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, params)  # type: ignore[call-overload]
    except Exception as exc:  # noqa: BLE001 - нужен именно отказ БД
        return str(exc).lower()
    raise AssertionError("the database accepted a row it must refuse")


async def test_geoip_migration_creates_the_tables_and_job_types() -> None:
    assert await _query(GEOIP_TABLES_SQL) == [("geoip_lists",), ("geoip_overrides",)]
    assert GEOIP_JOB_TYPES <= await _labels(JOB_TYPES_SQL)


async def test_geoip_migration_defaults_and_the_orm_agree() -> None:
    from modules.geoip.models.geoip import GeoipList, GeoipOverride, GeoipStatus, OverrideAction

    async with engine.begin() as conn:
        await conn.execute(_INSERT_LIST, {"sha": "a" * 64, "status": "ACTIVE"})
        await conn.execute(_INSERT_OVERRIDE, {"network": "5.45.200.0/24"})
    async with session_factory() as session:
        (row,) = (await session.execute(select(GeoipList))).scalars().all()
        assert (row.status, row.prefixes_count, row.addresses_count) == (GeoipStatus.ACTIVE, 3000, 3000000)
        assert row.created_at is not None and row.activated_at is None
        (override,) = (await session.execute(select(GeoipOverride))).scalars().all()
        assert (override.network, override.action, override.note) == ("5.45.200.0/24", OverrideAction.DIRECT, None)


async def test_geoip_allows_one_active_list_but_any_number_of_the_others() -> None:
    async with engine.begin() as conn:
        await conn.execute(_INSERT_LIST, {"sha": "a" * 64, "status": "ACTIVE"})
        for status in ("CANDIDATE", "CANDIDATE", "SUPERSEDED", "SUPERSEDED"):
            await conn.execute(_INSERT_LIST, {"sha": "b" * 64, "status": status})
    assert "uq_geoip_lists_one_active" in await _refused(_INSERT_LIST, {"sha": "c" * 64, "status": "ACTIVE"})


async def test_geoip_override_networks_are_unique() -> None:
    async with engine.begin() as conn:
        await conn.execute(_INSERT_OVERRIDE, {"network": "5.45.200.0/24"})
    assert "uq_geoip_overrides_network" in await _refused(_INSERT_OVERRIDE, {"network": "5.45.200.0/24"})


async def test_geoip_migration_downgrade_drops_the_tables_and_the_job_types_then_upgrade_restores() -> None:
    async with engine.begin() as conn:
        await conn.execute(_INSERT_LIST, {"sha": "a" * 64, "status": "ACTIVE"})
        for job_type in ("GEOIP_UPDATE", "GATEWAY_APPLY_RU", "HEALTH_CHECK", "GATEWAY_ADD_EXIT"):
            await conn.execute(
                text("INSERT INTO jobs (id, type, status) VALUES (gen_random_uuid(), :type, 'SUCCESS')"),
                {"type": job_type},
            )
    await _alembic("downgrade", BEFORE_GEOIP)
    try:
        assert await _query(GEOIP_TABLES_SQL) == []
        assert not GEOIP_JOB_TYPES & await _labels(JOB_TYPES_SQL)
        assert sorted(row[0] for row in await _query(text("SELECT type::text FROM jobs"))) == [
            "GATEWAY_ADD_EXIT",
            "HEALTH_CHECK",
        ]
    finally:
        await _alembic("upgrade", "head")
    assert GEOIP_JOB_TYPES <= await _labels(JOB_TYPES_SQL)
    assert await _query(GEOIP_TABLES_SQL) == [("geoip_lists",), ("geoip_overrides",)]
    assert await _query(text("SELECT count(*) FROM geoip_lists")) == [(0,)]


async def test_geoip_migration_cycle_is_idempotent_and_keeps_the_job_default() -> None:
    await _alembic("downgrade", BEFORE_GEOIP)
    await _alembic("upgrade", "head")
    await _alembic("downgrade", BEFORE_GEOIP)
    try:
        default = await _query(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name = 'jobs' AND column_name = 'type'"
            )
        )
        assert "HEALTH_CHECK" in str(default)
    finally:
        await _alembic("upgrade", "head")
