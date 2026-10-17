"""Страховка фейка в части ЗАПИСИ в контейнер приложения AmneziaVPN (этап 23.3): `docker cp`, `rm -f` и применение
конфига ведут себя как настоящие, а всё незнакомое — AssertionError (чтобы тесты провайдера не получили тихое «ок»)."""

import pytest

from infrastructure.vpn.awg_config import generate_secret_keys
from tests.fakes.fake_ssh import (
    APP_CLIENTS_TABLE,
    STAGE_DIR,
    AmneziaApp,
    FakeServer,
    FakeSSHClient,
    apply_command,
    stage_conf,
    stage_table,
)

C = "amnezia-awg2"
APPLY = apply_command(C, "awg", "awg0", "/opt/amnezia/awg/awg0.conf")


def fake_with_app() -> tuple[FakeServer, AmneziaApp, str]:
    key = generate_secret_keys().public_key
    fake = FakeServer()
    return fake, fake.add_amnezia_app(C, peers=[key]), key


async def put_stage(fake: FakeServer, path: str, text: str) -> None:
    async with FakeSSHClient(fake) as ssh:
        await ssh.write_file(path, text)


async def test_sftp_write_marks_file_private_and_logs_it() -> None:
    fake, _, _ = fake_with_app()
    await put_stage(fake, stage_conf(C), "text")
    assert fake.files[stage_conf(C)] == "text" and fake.file_modes[stage_conf(C)] == 0o600
    assert fake.writes == [stage_conf(C)] and fake.stage_files() == [stage_conf(C)]


async def test_docker_cp_copies_stage_file_into_container_and_remembers_mode() -> None:
    fake, app, _ = fake_with_app()
    await put_stage(fake, stage_conf(C), "[Interface]\nnew\n")
    result = fake.run(f"docker cp {stage_conf(C)} {C}:{app.config_path}")
    assert result.exit_status == 0 and app.files[app.config_path] == "[Interface]\nnew\n"
    (copy,) = fake.copies
    assert (copy.src, copy.container, copy.dst, copy.mode) == (stage_conf(C), C, app.config_path, 0o600)


async def test_docker_cp_of_clients_table_needs_its_own_stage_file() -> None:
    fake, _, _ = fake_with_app()
    await put_stage(fake, stage_table(C), "[]")
    assert fake.run(f"docker cp {stage_table(C)} {C}:{APP_CLIENTS_TABLE}").exit_status == 0
    with pytest.raises(AssertionError):  # конфиг нельзя положить из файла таблицы и наоборот
        fake.run(f"docker cp {stage_table(C)} {C}:/opt/amnezia/awg/awg0.conf")


@pytest.mark.parametrize(
    "dst",
    [
        "/opt/amnezia/awg/wireguard_server_private_key.key",
        "/opt/amnezia/awg/wg0.conf",  # конфиг v1 в контейнере v2
        "/etc/passwd",
        "/opt/amnezia/awg/../../etc/passwd",
    ],
)
async def test_docker_cp_into_foreign_path_is_an_error(dst: str) -> None:
    fake, _, _ = fake_with_app()
    await put_stage(fake, stage_conf(C), "x")
    with pytest.raises(AssertionError):
        fake.run(f"docker cp {stage_conf(C)} {C}:{dst}")


@pytest.mark.parametrize(
    "src", ["/etc/shadow", "/tmp/amnezia-awg2.conf", f"{STAGE_DIR}/other.conf", f"{STAGE_DIR}/../x"]
)
def test_docker_cp_from_outside_the_stage_file_is_an_error(src: str) -> None:
    fake, app, _ = fake_with_app()
    with pytest.raises(AssertionError):
        fake.run(f"docker cp {src} {C}:{app.config_path}")


def test_docker_cp_into_a_container_that_is_not_an_app_is_an_error() -> None:
    fake = FakeServer(containers={"amnezia-awg": "running"})  # контейнер самой панели
    with pytest.raises(AssertionError):
        fake.run(f"docker cp {stage_conf('amnezia-awg')} amnezia-awg:/opt/amnezia/awg/wg0.conf")


def test_docker_cp_reverse_direction_is_not_supported() -> None:
    fake, app, _ = fake_with_app()
    with pytest.raises(AssertionError):
        fake.run(f"docker cp {C}:{app.config_path} /tmp/leak")


async def test_docker_cp_missing_stage_file_and_stopped_container_fail_like_docker() -> None:
    fake, app, _ = fake_with_app()
    assert fake.run(f"docker cp {stage_conf(C)} {C}:{app.config_path}").exit_status != 0  # файла на хосте нет
    await put_stage(fake, stage_conf(C), "x")
    fake.containers[C] = "exited"
    result = fake.run(f"docker cp {stage_conf(C)} {C}:{app.config_path}")
    assert result.exit_status != 0 and "not running" in result.stderr


async def test_docker_cp_failure_mode_leaves_the_container_file_alone() -> None:
    fake, app, _ = fake_with_app()
    before = app.files[app.config_path]
    await put_stage(fake, stage_conf(C), "x")
    app.docker_cp_fails.add(app.config_path)
    result = fake.run(f"docker cp {stage_conf(C)} {C}:{app.config_path}")
    assert result.exit_status != 0 and app.files[app.config_path] == before and fake.copies == []
    assert "x" not in result.stderr.replace("copy", "").replace("Error", "")  # в ошибке нет содержимого файла


async def test_rm_f_removes_stage_file_and_tolerates_missing_one() -> None:
    fake, _, _ = fake_with_app()
    await put_stage(fake, stage_conf(C), "x")
    assert fake.run(f"rm -f {stage_conf(C)}").exit_status == 0 and fake.stage_files() == []
    assert fake.run(f"rm -f {stage_conf(C)}").exit_status == 0  # как настоящий rm -f


@pytest.mark.parametrize(
    "path", ["/opt/vpn-admin/awg/awg0.conf", "/etc/passwd", f"{STAGE_DIR}/../awg/awg0.conf", STAGE_DIR]
)
def test_rm_f_outside_the_stage_folder_is_an_error(path: str) -> None:
    with pytest.raises(AssertionError):
        FakeServer().run(f"rm -f {path}")


def test_syncconf_makes_the_config_peers_live_and_keeps_handshakes() -> None:
    fake, app, key = fake_with_app()
    app.peers[0].handshake = 1_700_000_000
    new = generate_secret_keys()
    app.files[app.config_path] += (
        f"\n[Peer]\nPublicKey = {new.public_key}\nPresharedKey = {new.psk}\nAllowedIPs = 10.8.1.9/32\n"
    )
    assert fake.run(APPLY).exit_status == 0 and app.applies == 1
    live = fake.run(f"docker exec {C} awg show awg0 latest-handshakes").stdout
    assert live == f"{key}\t1700000000\n{new.public_key}\t0\n"


def test_syncconf_of_a_removed_peer_drops_it_from_the_live_state() -> None:
    fake, app, key = fake_with_app()
    app.files[app.config_path] = app.render_config().split("\n[Peer]")[0] + "\n"
    assert fake.run(APPLY).exit_status == 0
    assert fake.run(f"docker exec {C} awg show awg0 latest-handshakes").stdout == "" and key not in app.secrets


@pytest.mark.parametrize(
    "mutation",
    [
        "garbage line",
        "[Other]",
        "[Peer]\nPublicKey = short",
        "[Peer]\nAllowedIPs = 10.8.1.2/32",
        "[Peer]\nPublicKey = {key}",
    ],
    ids=["garbage", "section", "bad-key", "no-key", "duplicate"],
)
def test_syncconf_rejects_a_broken_config_without_touching_live_state(mutation: str) -> None:
    fake, app, key = fake_with_app()
    before = list(app.peers)
    app.files[app.config_path] += "\n" + mutation.format(key=key) + "\n"
    result = fake.run(APPLY)
    assert result.exit_status != 0 and app.peers == before and app.applies == 0
    assert app.private_key not in result.stderr and key not in result.stderr


def test_syncconf_failure_modes_wrong_tool_interface_and_forced() -> None:
    fake, app, _ = fake_with_app()
    assert fake.run(APPLY.replace("awg syncconf", "wg syncconf").replace("awg-quick", "wg-quick")).exit_status == 126
    assert fake.run(APPLY.replace("awg0 <", "wg0 <")).exit_status != 0
    app.apply_fails = True
    result = fake.run(APPLY)
    assert result.exit_status == 1 and app.private_key not in result.stderr and app.applies == 0


def test_syncconf_with_foreign_config_path_is_an_error() -> None:
    fake, _, _ = fake_with_app()
    with pytest.raises(AssertionError):
        fake.run(apply_command(C, "awg", "awg0", "/etc/passwd"))
    with pytest.raises(AssertionError):
        fake.run(f"docker exec {C} bash -c 'echo hi'")


def test_on_read_hook_sees_the_number_of_the_read_and_can_change_the_file() -> None:
    fake, app, _ = fake_with_app()
    seen: list[tuple[str, int]] = []

    def change(path: str, number: int) -> None:
        seen.append((path, number))
        if number == 2:
            app.files[path] = "changed"

    app.on_read = change
    cat = f"docker exec {C} cat {app.config_path}"
    assert fake.run(cat).stdout != "changed" and fake.run(cat).stdout == "changed"
    assert seen == [(app.config_path, 1), (app.config_path, 2)] and app.read_counts == {app.config_path: 2}


async def test_unexpected_changing_commands_lets_only_the_write_protocol_through() -> None:
    fake, app, _ = fake_with_app()
    await put_stage(fake, stage_conf(C), "[Interface]\n")
    fake.run(f"docker cp {stage_conf(C)} {C}:{app.config_path}")
    fake.run(f"rm -f {stage_conf(C)}")
    fake.run(APPLY)
    fake.run(f"docker exec {C} cat {app.config_path}")
    assert fake.unexpected_changing_commands() == []
    assert len(fake.changing_commands()) == 3
    assert len(fake.mutating_commands()) == 4  # строгий список по-прежнему считает любой docker exec изменением
    fake.commands.append(f"docker exec {C} sh -c 'echo > /opt/amnezia/awg/awg0.conf'")
    fake.commands.append("rm -f /etc/passwd")
    fake.commands.append(f"docker cp /etc/shadow {C}:/opt/amnezia/awg/awg0.conf")
    assert len(fake.unexpected_changing_commands()) == 3


async def test_failed_sftp_write_leaves_no_file() -> None:
    fake, _, _ = fake_with_app()
    fake.write_fails.add(stage_conf(C))
    with pytest.raises(Exception, match="File write"):
        await put_stage(fake, stage_conf(C), "secret")
    assert fake.stage_files() == [] and fake.writes == []
