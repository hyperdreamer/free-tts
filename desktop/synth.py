"""HTTP client for the free-tts synthesis API."""

from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from desktop.settings import AdapterConfig

logger = logging.getLogger("free-tts.synth")

_MAX_RETRY_DELAY = 5.0
_RETRY_ABORT_POLL_INTERVAL = 0.05
_CANCEL_TIMEOUT = 5.0
_CANCEL_HANDOFF_SECONDS = 1.0
_CANCEL_RETRY_INTERVAL = 0.05
_VOICES_TIMEOUT = 20.0

SSML_TEMPLATE = (
    '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" '
    'xml:lang="en-US"><voice name="{voice}">'
    '<prosody rate="{rate}" pitch="{pitch}">{text}</prosody></voice></speak>'
)


class SynthError(Exception):
    """Synthesis could not be completed."""


class Cancelled(SynthError):
    """Synthesis was cancelled, by us or by the backend."""


def new_request_id() -> str:
    """Opaque, URL-safe id so a request can be cancelled later."""
    return secrets.token_hex(8)


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;").replace("'", "&apos;")


def build_ssml(text: str, voice_name: str, rate: str, pitch: str) -> str:
    """Build the SSML document the backend expects for one chunk."""
    return SSML_TEMPLATE.format(
        voice=_escape_attr(voice_name),
        rate=_escape_attr(rate),
        pitch=_escape_attr(pitch),
        text=_escape_text(text),
    )


def _http_transport(
    method: str, url: str, body: bytes | None, timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Perform one HTTP call, mapping HTTP errors onto status codes."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OSError(str(exc)) from exc


class SynthClient:
    """Talks to the local backend over HTTP. Holds no playback state."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: Callable[
            [str, str, bytes | None, float], tuple[int, Mapping[str, str], bytes]
        ]
        | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport or _http_transport
        self._sleep = sleep

    def voices(self) -> object:
        """Fetch and parse the backend's voice catalog payload."""
        url = f"{self._config.backend_url}/voices"
        try:
            status, _headers, body = self._transport("GET", url, None, _VOICES_TIMEOUT)
        except OSError as exc:
            raise SynthError(f"could not load voices: {exc}") from exc
        if status != 200:
            raise SynthError(f"voice listing failed with status {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SynthError(f"voice listing was not valid JSON: {exc}") from exc

    def synthesize(
        self,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> bytes:
        """Return MP3 bytes for one chunk, retrying a single 503."""
        url = f"{self._config.backend_url}/generate-and-download-tts"
        payload = json.dumps(
            {
                "ssml": build_ssml(text, voice_name, rate, pitch),
                "request_id": request_id,
            }
        ).encode("utf-8")

        for attempt in (1, 2):
            if should_abort is not None and should_abort():
                raise Cancelled("aborted before request")
            try:
                status, headers, body = self._transport(
                    "POST", url, payload, float(self._config.request_timeout)
                )
            except OSError as exc:
                raise SynthError(str(exc)) from exc

            if status == 200:
                return body
            if status == 499:
                raise Cancelled("backend reported the request as cancelled")
            if status == 503 and attempt == 1:
                self._wait_before_retry(self._retry_delay(headers), should_abort)
                continue
            if status == 503:
                raise SynthError("backend busy after retry")
            raise SynthError(self._error_detail(status, body))
        raise SynthError("backend busy after retry")

    def _wait_before_retry(
        self,
        delay: float,
        should_abort: Callable[[], bool] | None,
    ) -> None:
        """Wait for Retry-After while keeping STOP and discarded work prompt."""
        if should_abort is None:
            self._sleep(delay)
            return

        remaining = delay
        while remaining > 0.0:
            if should_abort():
                raise Cancelled("aborted during retry wait")
            interval = min(_RETRY_ABORT_POLL_INTERVAL, remaining)
            self._sleep(interval)
            if should_abort():
                raise Cancelled("aborted during retry wait")
            remaining = max(0.0, remaining - interval)
        if should_abort():
            raise Cancelled("aborted instead of retrying")

    def cancel(
        self,
        request_id: str,
        *,
        still_wanted: Callable[[], bool] | None = None,
    ) -> bool:
        """Deliver cancellation, tolerating the pre-registration interval.

        The adapter can send DELETE before the backend has registered the POST,
        which answers 404. That is "not yet", not "unknown", so retry briefly
        while this generation still wants the request cancelled.
        """
        url = f"{self._config.backend_url}/tts-request/{request_id}"
        deadline = time.monotonic() + _CANCEL_HANDOFF_SECONDS
        while True:
            try:
                status, _headers, _body = self._transport(
                    "DELETE", url, None, _CANCEL_TIMEOUT
                )
            except OSError as exc:
                logger.debug(
                    "Cancel for %s could not be delivered: %s", request_id, exc
                )
                return False
            if status != 404:
                return True
            if still_wanted is None or not still_wanted():
                return False
            if time.monotonic() >= deadline:
                logger.debug(
                    "Cancel for %s was never registered by the backend", request_id
                )
                return False
            self._sleep(_CANCEL_RETRY_INTERVAL)

    @staticmethod
    def _retry_delay(headers: Mapping[str, str]) -> float:
        raw = headers.get("Retry-After") or headers.get("retry-after") or "1"
        try:
            delay = float(raw)
        except ValueError:
            delay = 1.0
        return max(0.0, min(_MAX_RETRY_DELAY, delay))

    @staticmethod
    def _error_detail(status: int, body: bytes) -> str:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                return f"{parsed['error']} (status {status})"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return f"synthesis failed with status {status}"
