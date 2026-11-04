"""Read-only metadata for the one trusted AmneziaWG update channel.

Manifest/config digests and image IDs are different identities. Select the
installed platform first and compare its config digest with Docker's image ID.
No layer is downloaded and no URL supplied by a server/registry is followed.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from core.redis import get_redis
from infrastructure.vpn.provider import ProviderImageInfo

REPOSITORY = "amneziavpn/amneziawg-go"
REGISTRY_URL = "https://registry-1.docker.io"
AUTH_URL = "https://auth.docker.io/token"
AUTH_SERVICE = "registry.docker.io"
AUTH_SCOPE = f"repository:{REPOSITORY}:pull"
MAX_METADATA_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
CACHE_TTL_SECONDS = 300
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_INDEX_TYPES = frozenset({
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
})
_MANIFEST_TYPES = frozenset({
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
})
_ACCEPT = ", ".join(sorted(_INDEX_TYPES | _MANIFEST_TYPES))


class RegistryError(ValueError):
    """A fixed reason code is safe to expose; response bodies never are."""

    def __init__(self, reason: str = "registry_unavailable") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RegistryImageInfo:
    image_id: str
    digest: str
    current_image_id: str | None = None


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RegistryError()
    return value


def _variant(value: Any, architecture: str) -> str | None:
    # Docker commonly omits the baseline arm64/v8 variant in image inspect.
    if value in (None, ""):
        return "v8" if architecture == "arm64" else None
    if not isinstance(value, str):
        raise RegistryError("platform_unknown")
    return value


def select_platform_manifest(
    index: dict[str, Any], os: str, architecture: str, variant: str | None = None
) -> str:
    """Return exactly one matching child, rejecting missing/ambiguous platforms."""
    if not os or not architecture or os == "unknown" or architecture == "unknown":
        raise RegistryError("platform_unknown")
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise RegistryError("platform_unknown")
    matches: list[str] = []
    for manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        platform = manifest.get("platform")
        if not isinstance(platform, dict):
            continue
        if platform.get("os") != os or platform.get("architecture") != architecture:
            continue
        if _variant(platform.get("variant"), architecture) != _variant(variant, architecture):
            continue
        # Windows descriptors may additionally require an OS version/features.
        # We cannot infer those from Docker image platform metadata.
        if platform.get("os.version") or platform.get("os.features") or platform.get("features"):
            raise RegistryError("platform_unknown")
        matches.append(_digest(manifest.get("digest")))
    if len(matches) != 1:
        raise RegistryError("platform_unknown")
    return matches[0]


class AmneziaImageRegistry:
    def __init__(self, client: httpx.AsyncClient | None = None, redis: Redis | None = None) -> None:
        self._client = client
        # Explicit HTTP injection keeps pure transport tests independent of Redis.
        self._redis = redis if redis is not None else (get_redis() if client is None else None)
        self._token: str | None = None

    async def latest_for(self, image: ProviderImageInfo) -> RegistryImageInfo:
        try:
            async with asyncio.timeout(45):
                if self._client is not None:
                    return await self._latest_for(self._client, image)
                async with httpx.AsyncClient(
                    timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
                ) as client:
                    return await self._latest_for(client, image)
        except RegistryError:
            raise
        except (httpx.HTTPError, RedisError, TimeoutError, ValueError, TypeError, KeyError):
            raise RegistryError() from None

    async def _latest_for(self, client: httpx.AsyncClient, image: ProviderImageInfo) -> RegistryImageInfo:
        _digest(image.image_id)
        latest, latest_digest = await self._metadata(client, "manifests/latest")
        media_type = latest.get("mediaType")
        if media_type in _INDEX_TYPES or (media_type is None and "manifests" in latest):
            child = select_platform_manifest(latest, image.os, image.architecture, image.variant)
            manifest, resolved_digest = await self._metadata(client, f"manifests/{child}")
            if resolved_digest != child:
                raise RegistryError()
            target_digest = child
        else:
            manifest, target_digest = latest, latest_digest
            config_digest = self._config_digest(manifest)
            config, _ = await self._metadata(client, f"blobs/{config_digest}")
            if (
                config.get("os") != image.os
                or config.get("architecture") != image.architecture
                or _variant(config.get("variant"), image.architecture) != _variant(image.variant, image.architecture)
            ):
                raise RegistryError("platform_unknown")
        current_image_id = None
        if image.manifest_digest is not None:
            installed_digest = _digest(image.manifest_digest)
            if installed_digest == target_digest:
                installed = manifest
            else:
                installed, resolved_digest = await self._metadata(client, f"manifests/{installed_digest}")
                if resolved_digest != installed_digest:
                    raise RegistryError()
            current_image_id = self._config_digest(installed)
        return RegistryImageInfo(self._config_digest(manifest), f"{REPOSITORY}@{target_digest}", current_image_id)

    @staticmethod
    def _config_digest(manifest: dict[str, Any]) -> str:
        if manifest.get("mediaType") not in _MANIFEST_TYPES:
            raise RegistryError()
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise RegistryError()
        return _digest(config.get("digest"))

    async def _metadata(self, client: httpx.AsyncClient, path: str) -> tuple[dict[str, Any], str]:
        if self._redis is None:
            return await self._fetch(client, path)
        cache_key = f"protocol-updates:registry:v1:{path}"
        cached = await self._read_cache(cache_key)
        if cached is not None:
            return cached
        # Coalesce cache misses across workers; ttl exceeds the bounded HTTP work.
        async with self._redis.lock(cache_key + ":lock", timeout=90, blocking_timeout=30):
            cached = await self._read_cache(cache_key)
            if cached is not None:
                return cached
            payload, digest = await self._fetch(client, path)
            await self._redis.set(cache_key, json.dumps({"payload": payload, "digest": digest}), ex=CACHE_TTL_SECONDS)
            return payload, digest

    async def _read_cache(self, key: str) -> tuple[dict[str, Any], str] | None:
        assert self._redis is not None
        raw = await self._redis.get(key)
        if raw is None:
            return None
        if not isinstance(raw, (str, bytes)) or len(raw) > MAX_METADATA_BYTES * 2:
            return None
        try:
            cached = json.loads(raw)
            if not isinstance(cached, dict) or not isinstance(cached.get("payload"), dict):
                return None
            return cached["payload"], _digest(cached.get("digest"))
        except (ValueError, TypeError):
            return None

    async def _fetch(self, client: httpx.AsyncClient, path: str) -> tuple[dict[str, Any], str]:
        # All path components originate in constants or strict sha256 validators.
        url = f"{REGISTRY_URL}/v2/{REPOSITORY}/{path}"
        headers = {"Accept": _ACCEPT, "Accept-Encoding": "identity"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        status, response_headers, body = await self._request(client, url, headers)
        if status == 401:
            self._token = await self._authenticate(client, response_headers.get("www-authenticate", ""))
            headers["Authorization"] = f"Bearer {self._token}"
            status, response_headers, body = await self._request(client, url, headers)
        self._check_status(status)
        payload = self._json(body)
        content_digest = response_headers.get("docker-content-digest")
        digest = _digest(content_digest) if content_digest is not None else "sha256:" + hashlib.sha256(body).hexdigest()
        # Do not accept a registry response for a different immutable object.
        reference = path.split("/", 1)[1]
        if reference != "latest" and digest != reference:
            raise RegistryError()
        return payload, digest

    async def _authenticate(self, client: httpx.AsyncClient, challenge: str) -> str:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise RegistryError()
        fields = re.findall(r'(\w+)="([^"\r\n]*)"', params)
        values = dict(fields)
        if (
            len(fields) != len(values)
            or values.get("realm") != AUTH_URL
            or values.get("service") != AUTH_SERVICE
            or values.get("scope", AUTH_SCOPE) != AUTH_SCOPE
        ):
            raise RegistryError()
        status, _, body = await self._request(
            client, AUTH_URL, {"Accept": "application/json", "Accept-Encoding": "identity"},
            params={"service": AUTH_SERVICE, "scope": AUTH_SCOPE},
        )
        self._check_status(status)
        payload = self._json(body)
        token = payload.get("token", payload.get("access_token"))
        if not isinstance(token, str) or not token or len(token) > 16384 or "\r" in token or "\n" in token:
            raise RegistryError()
        return token

    @staticmethod
    async def _request(
        client: httpx.AsyncClient, url: str, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> tuple[int, httpx.Headers, bytes]:
        async with client.stream(
            "GET", url, headers=headers, params=params, follow_redirects=False, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_METADATA_BYTES):
                raise RegistryError()
            # Compressed metadata is unnecessary; reject to bound both raw/decoded data.
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise RegistryError()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_METADATA_BYTES:
                    raise RegistryError()
                body.extend(chunk)
            return response.status_code, response.headers, bytes(body)

    @staticmethod
    def _check_status(status: int) -> None:
        if status == 429:
            raise RegistryError("rate_limited")
        if status != 200:
            raise RegistryError()

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise RegistryError() from None
        if not isinstance(result, dict):
            raise RegistryError()
        return result
