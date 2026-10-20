#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Скачивание списка российских префиксов (этап 24.4): только https, потоково, с жёстким лимитом размера.

Адрес берётся из настроек панели (`GEOIP_SOURCE_URL`), пользовательского ввода здесь нет. Стандартная библиотека, без
новых зависимостей; блокирующий urllib работает в потоке (`asyncio.to_thread`).
"""

import asyncio
import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Any
from urllib.parse import urlsplit

CHUNK = 64 * 1024
READ_DEADLINE_SECONDS = 300  # общий срок чтения тела: медленный источник не должен занимать воркер сколько угодно


class DownloadError(Exception):
    """Скачать не удалось: сеть, статус, размер, схема. Сообщение без содержимого ответа."""


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Редирект на не-https (или на другой протокол) запрещён: подменённый CDN не должен понизить канал."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, "Redirect to a non-https URL is refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: int) -> IO[bytes]:
    opener = urllib.request.build_opener(_HttpsOnlyRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "vpn-panel-geoip/1"})  # noqa: S310 - схема проверена
    response: IO[bytes] = opener.open(request, timeout=timeout)
    return response


def read_limited(
    stream: IO[bytes],
    max_bytes: int,
    deadline_seconds: float = READ_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> bytes:
    """Читает поток кусками и обрывает, как только набралось больше max_bytes (файл не помещается в память целиком)
    или вышел общий срок чтения (таймаут сокета действует на каждое чтение, а не на всю загрузку)."""
    chunks: list[bytes] = []
    total = 0
    deadline = clock() + deadline_seconds
    while True:
        if clock() > deadline:
            raise DownloadError(f"The download took longer than {deadline_seconds:g} seconds")
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise DownloadError(f"The list is larger than {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _download(url: str, max_bytes: int, timeout: int, opener: Callable[[str, int], IO[bytes]]) -> str:
    if urlsplit(url).scheme != "https":
        raise DownloadError("Only https sources are allowed")
    try:
        with opener(url, timeout) as stream:
            status = getattr(stream, "status", 200)
            if status != 200:
                raise DownloadError(f"The source answered with status {status}")
            length = getattr(getattr(stream, "headers", None), "get", lambda _name: None)("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise DownloadError(f"The list is larger than {max_bytes} bytes")
            data = read_limited(stream, max_bytes)
    except (urllib.error.URLError, http.client.HTTPException, OSError, TimeoutError) as exc:
        raise DownloadError(f"Failed to download the list: {type(exc).__name__}") from None
    return data.decode("utf-8", errors="replace")


async def download_text(
    url: str,
    max_bytes: int,
    timeout: int = 120,
    opener: Callable[[str, int], IO[bytes]] = _open,
) -> str:
    """Текст файла по https. Больше max_bytes, не-200, не-https (в том числе редирект), сетевой сбой - DownloadError."""
    return await asyncio.to_thread(_download, url, max_bytes, timeout, opener)
