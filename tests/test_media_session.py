"""Media Session source invariants and executable worker/MAIN/relay schedules.

The behavioral cases evaluate the real extension sources with independently
controlled page execution, relay delivery, and script promise completion.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "extension" / "background.js"


def _source() -> str:
    return BACKGROUND.read_text(encoding="utf-8")


def test_media_session_injected_into_main_world():
    src = _source()
    assert "func: injectMediaSession" in src, "injectMediaSession not injected"
    assert re.search(r'world:\s*"MAIN",\s*func:\s*injectMediaSession', src), (
        'injectMediaSession must be injected with world: "MAIN"'
    )


def test_media_handlers_registered():
    src = _source()
    for action in ("play", "pause", "previoustrack", "nexttrack", "stop"):
        assert f'setActionHandler("{action}"' in src, f"missing handler for {action}"


def test_every_audio_executescript_runs_in_main_world():
    src = _source()
    for call in re.findall(
        r"chrome\.scripting\.executeScript\(\{.*?\}\)", src, re.DOTALL
    ):
        if "__freeTtsAudio" in call:
            assert 'world: "MAIN"' in call, (
                'executeScript touching __freeTtsAudio is missing world: "MAIN":\n'
                + call[:200]
            )


def test_old_port_mechanism_removed():
    src = _source()
    for token in ("free-tts-media-session", "mediaSessionPort", "chrome.runtime.connect"):
        assert token not in src, f"stale port mechanism: {token}"


def test_relay_is_idempotent_and_source_filtered():
    src = _source()
    assert "window.__freeTtsRelayInstalled" in src
    assert "event.source !== window" in src
    assert "__freeTtsMedia" in src
    assert 'chrome.runtime.sendMessage({ ...data, source: "media" })' in src


def test_keepalive_ping_present():
    src = _source()
    assert 'send("ping")' in src
    assert 'msg.action === "ping"' not in src, "ping must remain a bound no-op"


@pytest.mark.parametrize(
    "case",
    [
        "W11 pause_at_completion_decision",
        "W18 terminal_cleanup_overlap",
        "W19 media_direct_without_worker",
        "W20 media_pause_fences_queued_start",
        "W26 relay_and_world_invariants",
    ],
)
def test_media_session_behavior(case):
    try:
        result = subprocess.run(
            [
                "node", "--test", "--test-reporter=tap", "--test-name-pattern",
                f"^{re.escape(case)}$", "tests/extension_playback.test.cjs",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"Named media case timed out after 120 seconds: {case}\n{error}")
    output = (result.stdout + result.stderr)[-12000:]
    assert result.returncode == 0, output
    assert re.search(rf"(?m)^ok \d+ - {re.escape(case)}\r?$", result.stdout), (
        f"Named media case did not run and pass: {case}\n{output}"
    )
