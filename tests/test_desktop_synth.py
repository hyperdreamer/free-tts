"""HTTP synthesis client: SSML building, cancellation, and 503 retry."""

import dataclasses
import json

import pytest

from desktop import settings, synth


def _config(**overrides):
    return dataclasses.replace(settings.DEFAULTS, **overrides)


class _Transport:
    """Scripted transport recording every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body, timeout))
        if not self._responses:
            raise AssertionError(f"unexpected extra call: {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestBuildSsml:
    def test_contains_voice_rate_pitch_and_text(self):
        ssml = synth.build_ssml("Hello", "en-US-AvaMultilingualNeural", "+10%", "-5Hz")
        assert 'name="en-US-AvaMultilingualNeural"' in ssml
        assert 'rate="+10%"' in ssml
        assert 'pitch="-5Hz"' in ssml
        assert "Hello" in ssml
        assert ssml.startswith("<speak")

    def test_escapes_text_markup(self):
        ssml = synth.build_ssml("a < b & c > d", "v", "+0%", "+0Hz")
        assert "&lt;" in ssml and "&amp;" in ssml and "&gt;" in ssml

    def test_escapes_quotes_in_voice_name(self):
        ssml = synth.build_ssml("hi", 'v"x', "+0%", "+0Hz")
        assert '"v&quot;x"' in ssml or "&quot;" in ssml


class TestRequestId:
    def test_is_url_safe_and_bounded(self):
        import re

        for _ in range(20):
            token = synth.new_request_id()
            assert re.match(r"^[A-Za-z0-9_-]{1,64}$", token)

    def test_ids_are_unique(self):
        assert len({synth.new_request_id() for _ in range(50)}) == 50


class TestVoices:
    def test_parses_payload(self):
        payload = json.dumps({"voices": [], "default_voice": "v"}).encode()
        transport = _Transport([(200, {}, payload)])
        client = synth.SynthClient(_config(), transport=transport)
        assert client.voices() == {"voices": [], "default_voice": "v"}
        assert transport.calls[0][0] == "GET"
        assert transport.calls[0][1].endswith("/voices")

    def test_non_200_raises(self):
        transport = _Transport([(500, {}, b"{}")])
        client = synth.SynthClient(_config(), transport=transport)
        with pytest.raises(synth.SynthError):
            client.voices()

    def test_invalid_json_raises(self):
        transport = _Transport([(200, {}, b"not json")])
        client = synth.SynthClient(_config(), transport=transport)
        with pytest.raises(synth.SynthError):
            client.voices()


class TestSynthesize:
    def _client(self, responses, sleeps=None):
        transport = _Transport(responses)
        client = synth.SynthClient(
            _config(),
            transport=transport,
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        )
        return client, transport

    def test_returns_audio_bytes(self):
        client, transport = self._client([(200, {}, b"\xff\xfbmp3")])
        audio_bytes = client.synthesize("hi", "v", "+0%", "+0Hz", "req1")
        assert audio_bytes == b"\xff\xfbmp3"
        method, url, body, _timeout = transport.calls[0]
        assert method == "POST"
        assert url.endswith("/generate-and-download-tts")
        assert json.loads(body)["request_id"] == "req1"

    def test_499_raises_cancelled(self):
        client, _ = self._client([(499, {}, b'{"error":"Request cancelled."}')])
        with pytest.raises(synth.Cancelled):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_503_retries_once_then_succeeds(self):
        sleeps = []
        client, transport = self._client(
            [(503, {"Retry-After": "2"}, b"{}"), (200, {}, b"audio")], sleeps=sleeps
        )
        assert client.synthesize("hi", "v", "+0%", "+0Hz", "req1") == b"audio"
        assert len(transport.calls) == 2
        assert sleeps == [2.0]

    def test_503_retry_delay_is_capped(self):
        sleeps = []
        client, _ = self._client(
            [(503, {"Retry-After": "9999"}, b"{}"), (200, {}, b"audio")], sleeps=sleeps
        )
        client.synthesize("hi", "v", "+0%", "+0Hz", "req1")
        assert sleeps == [5.0]

    def test_503_twice_raises(self):
        client, _ = self._client(
            [(503, {"Retry-After": "1"}, b"{}"), (503, {"Retry-After": "1"}, b"{}")]
        )
        with pytest.raises(synth.SynthError, match="busy"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_503_not_retried_when_aborting(self):
        client, transport = self._client([(503, {"Retry-After": "1"}, b"{}")])

        def should_abort():
            # The user aborts once the request is in flight: a 503 must then
            # surface as Cancelled rather than trigger the retry.
            return len(transport.calls) > 0

        with pytest.raises(synth.Cancelled):
            client.synthesize(
                "hi", "v", "+0%", "+0Hz", "req1", should_abort=should_abort
            )
        assert len(transport.calls) == 1

    def test_error_body_message_surfaces(self):
        client, _ = self._client([(400, {}, b'{"error":"Unknown voice: v"}')])
        with pytest.raises(synth.SynthError, match="Unknown voice"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_transport_oserror_becomes_syntherror(self):
        client, _ = self._client([OSError("connection reset")])
        with pytest.raises(synth.SynthError, match="connection reset"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")


class TestCancel:
    def test_sends_delete(self):
        transport = _Transport([(200, {}, b'{"cancelled":true}')])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("req9")
        method, url, _body, _timeout = transport.calls[0]
        assert method == "DELETE"
        assert url.endswith("/tts-request/req9")

    def test_404_is_not_an_error(self):
        transport = _Transport([(404, {}, b'{"error":"Unknown request id."}')])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("gone")

    def test_transport_failure_is_swallowed(self):
        transport = _Transport([OSError("down")])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("req9")
