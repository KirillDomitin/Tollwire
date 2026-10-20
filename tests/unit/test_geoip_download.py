"""Скачивание списка (этап 24.4): только https, лимит размера при чтении, статус, редирект на http, сетевые сбои."""

import http.client
import io
import urllib.error
import urllib.request
from typing import IO

import pytest

from core.config import Settings
from infrastructure.geoip import CHUNK, DownloadError, _HttpsOnlyRedirects, download_text, read_limited

URL = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest"


class Response(io.BytesIO):
    """Ответ urllib: контекстный менеджер с status и headers."""

    def __init__(self, body: bytes, *, status: int = 200, length: str | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.reads = 0
        self.headers = {} if length is None else {"Content-Length": length}

    def read(self, size: int | None = -1) -> bytes:
        self.reads += 1
        return super().read(size)


def opener_of(response: Response | Exception):  # type: ignore[no-untyped-def]
    calls: list[tuple[str, int]] = []

    def opener(url: str, timeout: int) -> IO[bytes]:
        calls.append((url, timeout))
        if isinstance(response, Exception):
            raise response
        return response

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


async def test_the_body_is_returned_as_text_and_the_timeout_is_passed_on() -> None:
    opener = opener_of(Response("ripencc|RU|ipv4|5.45.200.0|256|20240101|allocated\n".encode()))

    text = await download_text(URL, 1000, 7, opener)

    assert text.startswith("ripencc|RU|ipv4") and opener.calls == [(URL, 7)]


async def test_invalid_utf8_is_replaced_not_fatal() -> None:
    assert await download_text(URL, 1000, 5, opener_of(Response(b"abc\xff\xfe"))) == "abc��"


@pytest.mark.parametrize(
    "url", ["http://ftp.ripe.net/list", "ftp://ftp.ripe.net/list", "file:///etc/passwd", "//x/y", ""]
)
async def test_only_https_is_allowed_and_the_network_is_not_touched(url: str) -> None:
    opener = opener_of(Response(b"x"))

    with pytest.raises(DownloadError, match="https"):
        await download_text(url, 1000, 5, opener)

    assert opener.calls == []


async def test_a_body_over_the_limit_is_cut_off_while_reading() -> None:
    with pytest.raises(DownloadError, match="larger"):
        await download_text(URL, 1000, 5, opener_of(Response(b"x" * 1001)))


async def test_the_limit_is_exact() -> None:
    assert len(await download_text(URL, 1000, 5, opener_of(Response(b"x" * 1000)))) == 1000


async def test_a_declared_size_over_the_limit_is_refused_before_reading() -> None:
    response = Response(b"x", length="999999999")

    with pytest.raises(DownloadError, match="larger"):
        await download_text(URL, 1000, 5, opener_of(response))

    assert response.reads == 0  # тело не читали


async def test_a_non_200_status_is_an_error() -> None:
    with pytest.raises(DownloadError, match="status 204"):
        await download_text(URL, 1000, 5, opener_of(Response(b"", status=204)))


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("name resolution failed"),
        urllib.error.HTTPError(URL, 503, "unavailable", None, None),  # type: ignore[arg-type]
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
async def test_network_failures_become_a_download_error_without_details(error: Exception) -> None:
    with pytest.raises(DownloadError) as caught:
        await download_text(URL, 1000, 5, opener_of(error))

    assert "name resolution" not in str(caught.value) and "reset" not in str(caught.value)


def test_the_reader_stops_at_the_limit_without_reading_everything() -> None:
    stream = io.BytesIO(b"x" * (CHUNK * 20))

    with pytest.raises(DownloadError):
        read_limited(stream, CHUNK + 1)

    assert stream.tell() <= CHUNK * 2  # прочитано не больше двух кусков, а не все 20


def test_a_redirect_to_http_is_refused_and_to_https_is_followed() -> None:
    handler = _HttpsOnlyRedirects()
    request = urllib.request.Request(URL)

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://evil.example/list")  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "ftp://evil.example/list")  # type: ignore[arg-type]
    followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://mirror.example/list")  # type: ignore[arg-type]
    assert followed is not None and followed.full_url == "https://mirror.example/list"


def test_the_source_url_setting_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOIP_SOURCE_URL", "http://ftp.ripe.net/list")

    with pytest.raises(ValueError, match="https"):
        Settings()  # type: ignore[call-arg]


# --- ревью 24.4: общий срок чтения и обрыв тела --------------------------------------------------------------------


class SlowStream(io.BytesIO):
    """Источник, отдающий по куску за «30 секунд» (часы подменены): таймаут сокета на каждое чтение не срабатывает."""

    def __init__(self, clock: list[float]) -> None:
        super().__init__(b"x" * 10_000_000)
        self.clock = clock

    def read(self, size: int | None = -1) -> bytes:
        self.clock[0] += 30
        return super().read(min(size or 1, 1000))


def test_a_slow_source_is_cut_off_by_the_overall_deadline() -> None:
    clock = [0.0]

    with pytest.raises(DownloadError, match="took longer than 300 seconds"):
        read_limited(SlowStream(clock), 50_000_000, 300, lambda: clock[0])

    assert 300 <= clock[0] <= 360  # оборвали сразу после срока, а не дочитали десять мегабайт


def test_a_fast_source_is_not_affected_by_the_deadline() -> None:
    clock = [0.0]

    assert read_limited(io.BytesIO(b"x" * 5000), 10_000, 300, lambda: clock[0]) == b"x" * 5000


class TruncatedStream(io.BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        raise http.client.IncompleteRead(b"SECRET-PARTIAL-BODY", 100)


async def test_an_incomplete_body_is_a_download_error_without_the_raw_exception_text() -> None:
    class Opened(TruncatedStream):
        status = 200
        headers: dict[str, str] = {}

    with pytest.raises(DownloadError) as caught:
        await download_text(URL, 1000, 5, opener_of(Opened()))  # type: ignore[arg-type]

    assert "IncompleteRead" in str(caught.value) and "SECRET-PARTIAL-BODY" not in str(caught.value)
