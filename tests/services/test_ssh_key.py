"""Панель ставит свой SSH-ключ на сервер: постановка задачи и сама установка (с настоящими БД и Redis, SSH — фейк).

Главное, что доказываем: ключ ставится только после проверки входом по нему; любой сбой возвращает сервер и учётку
в прежнее состояние; пароль и приватный ключ не попадают ни в журнал, ни в результат задачи, ни в логи.
"""

import asyncio
import logging
from uuid import UUID

import asyncssh
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from core.database.postgres import session_factory
from core.exceptions import (
    AppError,
    CredentialNotFound,
    JobAlreadyRunningError,
    NotFoundError,
    SSHConnectionError,
    SSHError,
    SSHHostKeyNotPinnedError,
    SSHKeyVerificationError,
)
from core.security import decrypt_secret
from infrastructure.ssh.host_keys import HostKey
from modules.audit.actor import SYSTEM_ACTOR
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import JobStatus, JobType
from modules.servers.host_key import ServerHostKeyService
from modules.servers.models.credential import AuthType, ServerCredential
from modules.servers.schemas import CredentialCreate, ServerUpdate
from modules.servers.service import CredentialService, ServerService
from modules.servers.ssh_key import ServerSshKeyService
from tests.fakes.fake_ssh import AUTHORIZED_KEYS, FakeServer, key_blob
from tests.fixtures import World

PASSWORD = "S3cret-Passw0rd-xyz"
FOREIGN = "ssh-rsa AAAAB3foreignkey admin@laptop"


def new_host_key() -> HostKey:
    return HostKey.from_ssh_key(asyncssh.generate_private_key("ssh-ed25519").convert_to_public())


async def prepare(world: World, *, pinned: bool = True) -> tuple[UUID, FakeServer]:
    """Сервер с паролем root и (по умолчанию) закреплённым ключом хоста; в authorized_keys — чужой ключ админа."""
    server, _, fake = await world.server(install=False)
    await CredentialService(world.session, SYSTEM_ACTOR).set_credential(server.id, CredentialCreate(secret=PASSWORD))
    if pinned:
        await ServerHostKeyService(world.session, SYSTEM_ACTOR).learned(server.id, new_host_key())
    fake.files[AUTHORIZED_KEYS] = FOREIGN + "\n"
    return server.id, fake


async def credential(session: AsyncSession, server_id: UUID) -> ServerCredential:
    session.expire_all()
    return await CredentialService(session, SYSTEM_ACTOR).get(server_id)


def service(session: AsyncSession) -> ServerSshKeyService:
    return ServerSshKeyService(session, SYSTEM_ACTOR)


async def panel_private_key(session: AsyncSession, server_id: UUID) -> str:
    return await CredentialService(session, SYSTEM_ACTOR).get_decrypted_secret(server_id)


def panel_blobs(fake: FakeServer) -> set[str]:
    return fake.authorized_blobs() - {"AAAAB3foreignkey"}


# --- schedule_install -------------------------------------------------------------------------------------------------


async def test_schedule_creates_pending_job_for_server(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    job = await service(session).schedule_install(server_id, None)
    assert (job.type, job.status, job.server_id) == (JobType.INSTALL_SSH_KEY, JobStatus.PENDING, server_id)


async def test_schedule_unknown_server_is_not_found(world: World, session: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await service(session).schedule_install(UUID(int=0), None)


async def test_schedule_requires_pinned_host_key(world: World, session: AsyncSession) -> None:
    """Пароль не должен уйти на сервер, чью подлинность мы не проверили: без закреплённого ключа задачи нет."""
    server_id, _ = await prepare(world, pinned=False)
    with pytest.raises(SSHHostKeyNotPinnedError):
        await service(session).schedule_install(server_id, None)
    assert await world.jobs(JobType.INSTALL_SSH_KEY) == []


async def test_schedule_refused_for_unpinned_host_does_not_save_the_new_credential(
    world: World, session: AsyncSession
) -> None:
    server_id, _ = await prepare(world, pinned=False)
    with pytest.raises(SSHHostKeyNotPinnedError):
        await service(session).schedule_install(server_id, CredentialCreate(username="deploy", secret="other-pw"))
    saved = await credential(session, server_id)
    assert saved.username == "root" and decrypt_secret(saved.secret_encrypted) == PASSWORD


async def test_schedule_without_credentials_is_refused(session: AsyncSession, world: World) -> None:
    server, _, _ = await world.server(install=False)
    await CredentialService(session, SYSTEM_ACTOR).delete(server.id)
    await ServerHostKeyService(session, SYSTEM_ACTOR).learned(server.id, new_host_key())
    with pytest.raises(CredentialNotFound):
        await service(session).schedule_install(server.id, None)
    assert await world.jobs(JobType.INSTALL_SSH_KEY) == []


async def test_schedule_with_credentials_in_request_saves_them_and_creates_job(
    session: AsyncSession, world: World
) -> None:
    server, _, _ = await world.server(install=False)
    await CredentialService(session, SYSTEM_ACTOR).delete(server.id)
    await ServerHostKeyService(session, SYSTEM_ACTOR).learned(server.id, new_host_key())
    job = await service(session).schedule_install(server.id, CredentialCreate(username="deploy", secret=PASSWORD))
    assert job.type == JobType.INSTALL_SSH_KEY  # до credential(): его expire_all() «истекает» и job
    saved = await credential(session, server.id)
    assert (saved.username, saved.auth_type) == ("deploy", AuthType.PASSWORD)
    assert decrypt_secret(saved.secret_encrypted) == PASSWORD


async def test_schedule_twice_while_unfinished_is_refused(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    first = await service(session).schedule_install(server_id, None)
    with pytest.raises(JobAlreadyRunningError):
        await service(session).schedule_install(server_id, None)
    assert len(await world.jobs(JobType.INSTALL_SSH_KEY)) == 1
    first.status = JobStatus.RUNNING  # выполняющаяся задача тоже считается незавершённой
    await session.commit()
    with pytest.raises(JobAlreadyRunningError):
        await service(session).schedule_install(server_id, None)


async def test_schedule_allowed_again_after_previous_job_finished(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    first = await service(session).schedule_install(server_id, None)
    first.status = JobStatus.FAILED
    await session.commit()
    second = await service(session).schedule_install(server_id, None)
    assert second.id != first.id


async def test_schedule_other_servers_job_does_not_block(world: World, session: AsyncSession) -> None:
    first_id, _ = await prepare(world)
    second_id, _ = await prepare(world)
    await service(session).schedule_install(first_id, None)
    await service(session).schedule_install(second_id, None)
    assert len(await world.jobs(JobType.INSTALL_SSH_KEY)) == 2


# --- install: успех ---------------------------------------------------------------------------------------------------


async def test_install_switches_credentials_to_panel_key(world: World, session: AsyncSession) -> None:
    server_id, fake = await prepare(world)
    result = await service(session).install(server_id)

    saved = await credential(session, server_id)
    assert (saved.auth_type, saved.username, saved.managed_key) == (AuthType.SSH_KEY, "root", True)
    private = await panel_private_key(session, server_id)
    public = asyncssh.import_private_key(private).convert_to_public()
    assert saved.public_key is not None and saved.public_key.split()[1] == key_blob(private)
    assert saved.key_fingerprint == public.get_fingerprint() == result["fingerprint"]
    assert result["fingerprint"].startswith("SHA256:")
    assert (result["username"], result["replaced"], result["removed_old"]) == ("root", "password", 0)
    assert key_blob(private) in fake.authorized_blobs()


async def test_install_adds_marked_line_and_keeps_admins_key(world: World, session: AsyncSession) -> None:
    server_id, fake = await prepare(world)
    await service(session).install(server_id)
    lines = fake.files[AUTHORIZED_KEYS].splitlines()
    assert lines[0] == FOREIGN  # ключ админа не тронут
    assert len(lines) == 2 and lines[1].endswith(f" vpn-panel:{server_id}")
    assert fake.dirs["/root/.ssh"] == 0o700


async def test_install_result_job_and_logs_contain_no_secrets(
    world: World, session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    server_id, _ = await prepare(world)
    with caplog.at_level(logging.DEBUG):
        result = await service(session).install(server_id)
    saved = await credential(session, server_id)
    assert PASSWORD not in caplog.text and "PRIVATE KEY" not in caplog.text
    assert PASSWORD not in str(result) and "PRIVATE KEY" not in str(result)
    assert PASSWORD not in saved.secret_encrypted  # в БД секрет зашифрован
    assert PASSWORD not in (saved.public_key or "") and PASSWORD not in (saved.key_fingerprint or "")


async def test_install_audit_events_contain_no_secrets(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    job = await service(session).schedule_install(server_id, None)
    await service(session).install(server_id)
    logs = [
        log
        for action in (AuditAction.SERVER_SSH_KEY_INSTALL_STARTED, AuditAction.SERVER_CREDENTIALS_SET)
        for log in await world.audit(action, server_id)
    ]
    dump = " ".join(str(log.meta) for log in logs)  # await внутри генератора сделал бы его асинхронным
    assert str(job.id) in dump
    assert PASSWORD not in dump and "PRIVATE KEY" not in dump


async def test_install_works_with_credentials_where_username_is_not_root(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    await CredentialService(session, SYSTEM_ACTOR).set_credential(
        server_id, CredentialCreate(username="deploy", secret=PASSWORD)
    )
    result = await service(session).install(server_id)
    assert result["username"] == "deploy"
    assert (await credential(session, server_id)).username == "deploy"


# --- install: сбои ----------------------------------------------------------------------------------------------------


async def test_install_rolls_back_when_server_does_not_accept_new_key(world: World, session: AsyncSession) -> None:
    """Вход по ключам на сервере выключен: ключ записан, но войти по нему нельзя. Строку снимаем, пароль остаётся."""
    server_id, fake = await prepare(world)
    fake.key_auth_enabled = False
    with pytest.raises(SSHKeyVerificationError):
        await service(session).install(server_id)

    saved = await credential(session, server_id)
    assert (saved.auth_type, saved.managed_key, saved.public_key, saved.key_fingerprint) == (
        AuthType.PASSWORD,
        False,
        None,
        None,
    )
    assert decrypt_secret(saved.secret_encrypted) == PASSWORD
    assert fake.files[AUTHORIZED_KEYS] == FOREIGN + "\n"  # authorized_keys ровно как был


def test_verification_error_is_an_ssh_error() -> None:
    assert issubclass(SSHKeyVerificationError, SSHError)


async def test_install_server_unreachable_leaves_credentials_unchanged(world: World, session: AsyncSession) -> None:
    server_id, fake = await prepare(world)
    fake.reachable = False
    with pytest.raises(SSHConnectionError):
        await service(session).install(server_id)
    saved = await credential(session, server_id)
    assert (saved.auth_type, saved.managed_key) == (AuthType.PASSWORD, False)
    assert decrypt_secret(saved.secret_encrypted) == PASSWORD


async def test_install_without_credentials_is_refused(world: World, session: AsyncSession) -> None:
    server, _, _ = await world.server(install=False)
    await CredentialService(session, SYSTEM_ACTOR).delete(server.id)
    with pytest.raises(CredentialNotFound):
        await service(session).install(server.id)


async def test_install_when_credentials_changed_during_job_withdraws_new_key(
    world: World, session: AsyncSession
) -> None:
    """Админ сменил учётку, пока шла проверка нового ключа: его выбор важнее, наш ключ снимаем и падаем."""
    server_id, fake = await prepare(world)
    fired: list[str] = []

    async def admin_changes_credentials(command: str) -> None:
        if fired:
            return
        fired.append(command)
        async with session_factory() as other:
            await CredentialService(other, SYSTEM_ACTOR).set_credential(
                server_id, CredentialCreate(secret="changed-by-admin")
            )

    fake.on_key_command = admin_changes_credentials
    with pytest.raises(AppError):
        await service(session).install(server_id)

    assert fired  # смена действительно случилась посреди задачи
    saved = await credential(session, server_id)
    assert (saved.auth_type, saved.managed_key) == (AuthType.PASSWORD, False)
    assert decrypt_secret(saved.secret_encrypted) == "changed-by-admin"  # выбор админа не перезаписан
    assert fake.files[AUTHORIZED_KEYS] == FOREIGN + "\n"  # наш ключ с сервера снят


# --- ротация ----------------------------------------------------------------------------------------------------------


async def test_reinstall_rotates_key_and_removes_old_line(world: World, session: AsyncSession) -> None:
    server_id, fake = await prepare(world)
    await service(session).install(server_id)
    old_private = await panel_private_key(session, server_id)
    old_fingerprint = (await credential(session, server_id)).key_fingerprint

    result = await service(session).install(server_id)

    new_private = await panel_private_key(session, server_id)
    assert new_private != old_private
    assert (result["replaced"], result["removed_old"]) == ("ssh_key", 1)
    assert result["fingerprint"] != old_fingerprint
    assert key_blob(old_private) not in fake.authorized_blobs()  # старый ключ с сервера снят
    assert panel_blobs(fake) == {key_blob(new_private)}
    assert fake.files[AUTHORIZED_KEYS].splitlines()[0] == FOREIGN
    assert (await credential(session, server_id)).managed_key is True


async def test_failed_rotation_keeps_old_key_working(world: World, session: AsyncSession) -> None:
    server_id, fake = await prepare(world)
    await service(session).install(server_id)
    old_private = await panel_private_key(session, server_id)
    fingerprint = (await credential(session, server_id)).key_fingerprint
    before = fake.files[AUTHORIZED_KEYS]

    async def refuse_new_key_only(command: str) -> None:
        raise SSHConnectionError("connection dropped during verification")

    fake.on_key_command = refuse_new_key_only
    with pytest.raises(AppError):
        await service(session).install(server_id)

    assert await panel_private_key(session, server_id) == old_private
    assert (await credential(session, server_id)).key_fingerprint == fingerprint
    assert fake.files[AUTHORIZED_KEYS] == before  # новый ключ снят, старый на месте


# --- ручная смена учётки ----------------------------------------------------------------------------------------------


async def test_manual_password_resets_managed_key_fields(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    await service(session).install(server_id)
    await CredentialService(session, SYSTEM_ACTOR).set_credential(server_id, CredentialCreate(secret="new-password"))
    saved = await credential(session, server_id)
    assert (saved.auth_type, saved.managed_key, saved.public_key, saved.key_fingerprint) == (
        AuthType.PASSWORD,
        False,
        None,
        None,
    )


async def test_manual_ssh_key_is_not_managed_but_has_fingerprint(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    mine = asyncssh.generate_private_key("ssh-ed25519")
    await CredentialService(session, SYSTEM_ACTOR).set_credential(
        server_id, CredentialCreate(auth_type=AuthType.SSH_KEY, secret=mine.export_private_key().decode())
    )
    saved = await credential(session, server_id)
    assert saved.managed_key is False
    assert saved.key_fingerprint == mine.convert_to_public().get_fingerprint()


# --- ключ остаётся на сервере: предупреждение в аудите ----------------------------------------------------------------


async def managed_server(world: World, session: AsyncSession) -> tuple[UUID, str]:
    server_id, _ = await prepare(world)
    result = await service(session).install(server_id)
    return server_id, result["fingerprint"]


async def test_server_delete_audit_warns_that_panel_key_stays_on_server(world: World, session: AsyncSession) -> None:
    server_id, fingerprint = await managed_server(world, session)
    await ServerService(session, SYSTEM_ACTOR).delete(server_id)
    (event,) = await world.audit(AuditAction.SERVER_DELETED, server_id)
    assert event.meta["panel_key_left_on_server"] == fingerprint


async def test_host_change_audit_warns_that_panel_key_stays_on_old_server(world: World, session: AsyncSession) -> None:
    server_id, fingerprint = await managed_server(world, session)
    await ServerService(session, SYSTEM_ACTOR).update(server_id, ServerUpdate(host="203.0.113.99"))
    (event,) = await world.audit(AuditAction.SERVER_UPDATED, server_id)
    assert event.meta["panel_key_left_on_server"] == fingerprint
    assert event.meta["credentials_deleted"] is True


async def test_credentials_delete_audit_warns_that_panel_key_stays_on_server(
    world: World, session: AsyncSession
) -> None:
    server_id, fingerprint = await managed_server(world, session)
    await CredentialService(session, SYSTEM_ACTOR).delete(server_id)
    (event,) = await world.audit(AuditAction.SERVER_CREDENTIALS_DELETED, server_id)
    assert event.meta["panel_key_left_on_server"] == fingerprint


async def test_no_warning_for_password_credentials(world: World, session: AsyncSession) -> None:
    server_id, _ = await prepare(world)
    await ServerService(session, SYSTEM_ACTOR).delete(server_id)
    (event,) = await world.audit(AuditAction.SERVER_DELETED, server_id)
    assert "panel_key_left_on_server" not in event.meta


# --- сбои вокруг сохранения ------------------------------------------------------------------------------------------


async def test_lost_commit_acknowledgement_keeps_saved_key(
    world: World, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit дошёл до БД, а ответ потерялся: учётка уже на новом ключе — снимать его с сервера нельзя."""
    server_id, fake = await prepare(world)
    original_save = ServerSshKeyService._save

    async def save_then_lose_ack(self: ServerSshKeyService, *args: object) -> None:
        await original_save(self, *args)  # type: ignore[arg-type]
        raise ConnectionResetError("connection lost after commit")

    monkeypatch.setattr(ServerSshKeyService, "_save", save_then_lose_ack)
    result = await service(session).install(server_id)

    private = await panel_private_key(session, server_id)
    assert result["fingerprint"] == (await credential(session, server_id)).key_fingerprint
    assert key_blob(private) in fake.authorized_blobs()  # ключ, на который перешла учётка, остался


async def test_worker_cancelled_during_verification_withdraws_key(
    world: World, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    server_id, fake = await prepare(world)

    async def cancelled(verifier: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(ServerSshKeyService, "_verify", staticmethod(cancelled))
    with pytest.raises(asyncio.CancelledError):
        await service(session).install(server_id)
    assert fake.files[AUTHORIZED_KEYS] == FOREIGN + "\n"
    assert (await credential(session, server_id)).auth_type == AuthType.PASSWORD


async def test_manual_credentials_over_panel_key_warn_in_audit(world: World, session: AsyncSession) -> None:
    server_id, fingerprint = await managed_server(world, session)
    await CredentialService(session, SYSTEM_ACTOR).set_credential(server_id, CredentialCreate(secret="new-password"))
    *_, event = await world.audit(AuditAction.SERVER_CREDENTIALS_SET, server_id)
    assert event.meta["panel_key_left_on_server"] == fingerprint
