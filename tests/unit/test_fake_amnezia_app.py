"""Страховка самого фейка приложения AmneziaVPN: он отвечает как настоящий Docker и не прощает незнакомых команд."""

import pytest

from infrastructure.vpn.awg_config import generate_secret_keys, list_peers
from tests.fakes.fake_ssh import APP_CLIENTS_TABLE, FakeServer

CAT_CONFIG_V2 = "docker exec amnezia-awg2 cat /opt/amnezia/awg/awg0.conf"
PS_STATE = "docker ps -a --filter 'name=^/?{name}$' --format '{{{{.State}}}}'"


def test_added_app_has_config_clients_table_and_foreign_mount_inside_the_container() -> None:
    key = generate_secret_keys().public_key
    fake = FakeServer()
    app = fake.add_amnezia_app("amnezia-awg2", peers=[key], clients_table={key: "Phone"}, port=4321)
    config = fake.run(CAT_CONFIG_V2).stdout
    assert [(p.public_key, p.address) for p in list_peers(config)] == [(key, "10.8.1.2")]
    assert "ListenPort = 4321" in config and app.private_key in config
    assert "Phone" in fake.run(f"docker exec amnezia-awg2 cat {APP_CLIENTS_TABLE}").stdout
    assert fake.mounts["amnezia-awg2"] == ["/var/lib/docker/volumes/amnezia-awg2/_data"]  # не папка панели


def test_v1_uses_wg_and_wg0_v2_uses_awg_and_awg0() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg", peers=[generate_secret_keys().public_key])
    fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key])
    assert fake.run("docker exec amnezia-awg wg show wg0 latest-handshakes").exit_status == 0
    assert fake.run("docker exec amnezia-awg2 awg show awg0 latest-handshakes").exit_status == 0
    assert fake.run("docker exec amnezia-awg awg show wg0 latest-handshakes").exit_status != 0  # в v1 нет awg
    assert fake.run("docker exec amnezia-awg2 wg show awg0 latest-handshakes").exit_status != 0
    assert fake.run("docker exec amnezia-awg2 awg show wg0 latest-handshakes").exit_status != 0  # чужой интерфейс


def test_docker_exec_in_a_stopped_app_container_fails_like_docker() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2", running=False)
    result = fake.run(CAT_CONFIG_V2)
    assert result.exit_status != 0 and "not running" in result.stderr


def test_missing_file_inside_the_container_fails_like_cat() -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app("amnezia-awg2")
    del app.files[APP_CLIENTS_TABLE]
    assert fake.run(f"docker exec amnezia-awg2 cat {APP_CLIENTS_TABLE}").exit_status == 1


def test_interface_down_fails_show_but_not_the_config_read() -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app("amnezia-awg2")
    app.interface_up = False
    assert fake.run("docker exec amnezia-awg2 awg show awg0 latest-handshakes").exit_status != 0
    assert fake.run(CAT_CONFIG_V2).exit_status == 0


def test_ps_filters_by_exact_name_and_reports_image_state_status() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2", image="amneziavpn/amnezia-awg2:4.8")
    assert fake.run(PS_STATE.format(name="amnezia-awg")).stdout == ""  # точное имя: awg2 не совпадает
    assert fake.run(PS_STATE.format(name="amnezia-awg2")).stdout == "running\n"
    image = fake.run("docker ps -a --filter 'name=^/?amnezia-awg2$' --format '{{.Image}}'").stdout
    assert image == "amneziavpn/amnezia-awg2:4.8\n"
    combined = fake.run("docker ps -a --filter 'name=^/?amnezia-awg2$' --format '{{.State}}|{{.Image}}'").stdout
    assert combined == "running|amneziavpn/amnezia-awg2:4.8\n"


def test_inspect_mounts_and_running_work_for_any_container_name() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2")
    mounts = "docker inspect --type container -f '{{range .Mounts}}{{.Source}} {{end}}' amnezia-awg2"
    assert fake.run(mounts).stdout.split() == ["/var/lib/docker/volumes/amnezia-awg2/_data"]
    assert fake.run("docker inspect -f '{{.State.Running}}' amnezia-awg2").stdout == "true\n"
    assert fake.run("docker inspect -f '{{.State.Running}}' nothing").exit_status != 0


def test_changing_commands_allow_only_reading_inside_the_container() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2", peers=[generate_secret_keys().public_key])
    fake.run(CAT_CONFIG_V2)
    fake.run("docker exec amnezia-awg2 awg show awg0 latest-handshakes")
    fake.run(PS_STATE.format(name="amnezia-awg2"))
    assert fake.changing_commands() == []
    assert len(fake.mutating_commands()) == 2  # прежняя проверка по-прежнему считает любой docker exec изменением


def test_unknown_docker_exec_is_an_error_and_counts_as_a_change() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2")
    with pytest.raises(AssertionError):
        fake.run("docker exec amnezia-awg2 sh -c 'echo hi > /tmp/x'")
    with pytest.raises(AssertionError):
        fake.run("docker exec amnezia-awg2 awg set awg0 peer X remove")
    assert len(fake.changing_commands()) == 2


def test_reading_outside_the_app_folder_is_not_considered_safe() -> None:
    fake = FakeServer()
    fake.commands[:] = [
        "docker exec amnezia-awg2 cat /etc/shadow",
        "docker exec amnezia-awg2 cat /opt/amnezia/awg/../../etc/shadow",
        "docker exec amnezia-awg2 cat /opt/amnezia/awg/awg0.conf /etc/shadow",
    ]
    assert len(fake.changing_commands()) == 3


def test_reads_inside_containers_are_logged() -> None:
    fake = FakeServer()
    fake.add_amnezia_app("amnezia-awg2")
    fake.run(CAT_CONFIG_V2)
    assert fake.container_reads == [("amnezia-awg2", "/opt/amnezia/awg/awg0.conf")]


def test_app_secrets_list_has_server_private_key_and_every_psk() -> None:
    fake = FakeServer()
    app = fake.add_amnezia_app(
        "amnezia-awg2", peers=[generate_secret_keys().public_key, generate_secret_keys().public_key]
    )
    assert len(app.secrets) == 3 and app.private_key in app.secrets
    assert all(secret in fake.run(CAT_CONFIG_V2).stdout for secret in app.secrets)


def test_panel_own_container_has_no_app_files() -> None:
    fake = FakeServer(containers={"amnezia-awg": "running"})
    result = fake.run("docker exec amnezia-awg cat /opt/amnezia/awg/wg0.conf")
    assert result.exit_status == 1 and result.stdout == ""
