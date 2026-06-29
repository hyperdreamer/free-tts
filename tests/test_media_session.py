"""Guards for the Media Session (system media key) integration.

The media-key handlers and the Audio element they control must run in the page's
MAIN world: action handlers set in the extension's isolated world are never
wired to the hardware media keys, and a MAIN-world handler cannot reach an Audio
element created in the isolated world. Since MAIN world cannot call chrome.*,
prev/next/stop are bridged out via window.postMessage to an isolated-world relay
that calls chrome.runtime.sendMessage. These tests lock in those invariants so a
future refactor can't silently drop a `world: "MAIN"` and break media keys again.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKGROUND = ROOT / "extension" / "background.js"


def _source() -> str:
    return BACKGROUND.read_text(encoding="utf-8")


def test_media_session_injected_into_main_world():
    src = _source()
    assert "func: injectMediaSession" in src, "injectMediaSession not injected"
    # The injectMediaSession executeScript call must specify world: "MAIN".
    match = re.search(r"world:\s*\"MAIN\",\s*func:\s*injectMediaSession", src)
    assert match, 'injectMediaSession must be injected with world: "MAIN"'


def test_media_handlers_registered():
    src = _source()
    for action in ("play", "pause", "previoustrack", "nexttrack", "stop"):
        assert f'setActionHandler("{action}"' in src, f"missing handler for {action}"


def test_every_audio_executescript_runs_in_main_world():
    """Every executeScript block that touches window.__freeTtsAudio must run in
    the MAIN world, or the media-session handlers (also MAIN world) can't see it."""
    src = _source()
    # Find each executeScript({...}) call and check the ones referencing the audio.
    for call in re.findall(r"chrome\.scripting\.executeScript\(\{.*?\}\)", src, re.DOTALL):
        if "__freeTtsAudio" in call:
            assert 'world: "MAIN"' in call, (
                "executeScript touching __freeTtsAudio is missing world: \"MAIN\":\n"
                + call[:200]
            )


def test_prev_next_stop_bridged_via_postmessage():
    src = _source()
    # MAIN-world handler posts a message...
    assert "__freeTtsMedia" in src, "postMessage bridge marker missing"
    assert 'send("prevSentence")' in src
    assert 'send("nextSentence")' in src
    assert 'send("stopPlayback")' in src
    # ...and the isolated-world relay forwards it to the worker.
    assert "injectMediaRelay" in src, "isolated-world relay missing"
    assert "chrome.runtime.sendMessage({ action: data.action })" in src


def test_old_port_mechanism_removed():
    """The chrome.runtime.connect port could never work from MAIN world; ensure
    the dead isolated-world port machinery is gone."""
    src = _source()
    assert "free-tts-media-session" not in src, "stale port name still present"
    assert "mediaSessionPort" not in src, "stale port variable still present"
    assert "chrome.runtime.connect" not in src, "stale port connect still present"


def test_relay_is_idempotent():
    """Repeated setup must not stack duplicate message listeners."""
    src = _source()
    assert "window.__freeTtsRelayInstalled" in src, "relay idempotency guard missing"


def test_keepalive_ping_present():
    src = _source()
    assert 'send("ping")' in src, "25s keepalive ping missing"
    assert "msg.action === \"ping\"" not in src, "ping must be a no-op runtime message, not a port message"


def test_play_pause_sync_worker_state():
    """Media-key play/pause control the Audio element in-page for latency, but
    must ALSO notify the worker so sentencePipeline.isPaused, the context menu,
    and the floating control bar stay in sync."""
    src = _source()
    # MAIN-world handlers bridge a sync message out alongside the direct control.
    assert 'send("mediaResume")' in src, "play handler must notify worker"
    assert 'send("mediaPause")' in src, "pause handler must notify worker"
    # The worker routes those to a state-sync path.
    assert 'msg.action === "mediaPause"' in src, "mediaPause not routed"
    assert 'msg.action === "mediaResume"' in src, "mediaResume not routed"
    # Sync must touch the pipeline flag, context menu, and control bar.
    assert "function syncPausedState" in src, "syncPausedState helper missing"
    match = re.search(r"function syncPausedState\(.*?\n}\n", src, re.DOTALL)
    assert match, "syncPausedState body not found"
    body = match.group(0)
    assert "isPaused" in body
    assert "updateStopMenu()" in body
    assert "updateControlBar(" in body
    # The sync path must NOT re-issue audio control (the page already did it).
    assert "__freeTtsAudio" not in body, "syncPausedState must not touch the Audio element"
