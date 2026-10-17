"""Общее для тестов поиска Amnezia (этап 23.2): постановка задачи DISCOVER_VPN и проверка отчёта без секретов.

Новые имена (JobType.DISCOVER_VPN, AuditAction.SERVER_DISCOVER_*) берутся здесь, внутри функций: пока кода нет, падают
тесты, а не сбор тестов."""

import json
from datetime import datetime, timezone
from typing import Any

from infrastructure.workers.worker import JobWorker
from modules.audit.actor import SYSTEM_ACTOR, AuditActor
from modules.audit.models.audit_log import AuditAction
from modules.jobs.models.job import Job, JobType
from modules.jobs.service import JobService
from modules.servers.models.server import VPNServer
from tests.fakes.fake_ssh import AmneziaApp, FakeServer
from tests.fixtures import World
from tests.integration.conftest import drain

NOW = int(datetime.now(timezone.utc).timestamp())
OLD_HANDSHAKE = 1_700_000_000

PROTOCOL_KEYS = {"type", "container", "state", "image", "port", "clients"}
CLIENT_KEYS = {"public_key", "address", "name", "latest_handshake", "supported"}


def discover_job_type() -> JobType:
    return JobType("discover_vpn")  # JobType.DISCOVER_VPN


def discover_action(suffix: str) -> AuditAction:
    return AuditAction(f"server_discover_{suffix}")  # SERVER_DISCOVER_STARTED / COMPLETED / FAILED


async def run_discovery(world: World, worker: JobWorker, server: VPNServer, actor: AuditActor = SYSTEM_ACTOR) -> Job:
    """Поставить DISCOVER_VPN, выполнить воркером и вернуть задачу с итогом."""
    job = await JobService(world.session, actor).create(discover_job_type(), server_id=server.id)
    await drain(worker)
    await world.session.refresh(job)
    return job


def secrets_of(*apps: AmneziaApp) -> list[str]:
    return [secret for app in apps for secret in app.secrets]


def assert_no_secrets(payload: object, *apps: AmneziaApp) -> None:
    text = json.dumps(payload, default=str)
    for secret in secrets_of(*apps):
        assert secret not in text
    assert "PrivateKey" not in text and "PresharedKey" not in text


def by_type(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["type"]: item for item in result["protocols"]}


def add_app(fake: FakeServer, container: str = "amnezia-awg2", **kwargs: Any) -> AmneziaApp:
    return fake.add_amnezia_app(container, **kwargs)
