"""Guards for the web frontend Download MP3 / Cancel Downloading contract.

Two defects made Download MP3 look dead and left the server busy:

1. Both download handlers started with ``if (activeAbortControllers.size > 0)``
   and *cancelled the in-flight request instead of downloading*. Sentence
   previews keep that set non-empty, so clicking Download while a preview was
   running only aborted preview work and never sent a download request.
2. The "Cancel Downloading" label was reset back to "Download MP3" by
   ``cancelGeneration()`` inside ``callTTS``, and cancelling only aborted the
   browser fetch. The server kept generating the aborted request for minutes,
   holding both concurrency slots and turning later clicks into 503s.

The server already exposes a real cancel path: ``request_id`` in the POST body
plus ``DELETE /tts-request/<id>``.  These tests pin the frontend half of that
contract so a future refactor cannot silently restore either failure mode.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script.js"


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    """Return the source of a top-level ``function name(...) { ... }`` block."""
    src = _source()
    match = re.search(rf"function {re.escape(name)}\([\s\S]*?\) \{{", src)
    assert match, f"function {name} not found in script.js"
    start = match.end() - 1  # the opening brace of the body
    depth = 0
    for index in range(start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[start : index + 1]
    raise AssertionError(f"unbalanced braces in function {name}")


def test_download_requests_carry_request_id():
    """The POST body must include ``request_id`` for the server cancel token."""
    call_tts = _function_source("callTTS")
    assert re.search(r"request_id\s*:", call_tts), (
        "callTTS must send request_id in the JSON body so a download can be "
        "cancelled server-side; otherwise aborted requests keep generating"
    )


def test_cancel_issues_delete_to_the_request_endpoint():
    """Cancel must call the server's DELETE /tts-request/<id> route."""
    fn = _function_source("cancelServerRequest")
    assert "/tts-request/" in fn, "cancelServerRequest must target /tts-request/"
    assert re.search(r'method:\s*"DELETE"', fn), (
        "cancelServerRequest must issue an HTTP DELETE"
    )


def test_cancel_retries_while_the_request_may_still_be_queued():
    """A queued POST has no live token yet, so the DELETE must be retried."""
    fn = _function_source("cancelServerRequest")
    assert re.search(r"for\s*\(|while\s*\(", fn), (
        "cancelServerRequest must retry: the server registers the request_id "
        "only once the queued request reaches the handler"
    )


def test_download_shows_cancel_downloading_label():
    assert "Cancel Downloading" in _source(), (
        "the download button must switch to the Cancel Downloading label while "
        "a download is in flight"
    )


def test_both_download_buttons_use_the_shared_download_flow():
    """Text-tab and SSML-tab buttons must share one cancel-aware handler."""
    src = _source()
    ssml_handler = re.search(
        r'downloadSsmlBtn\.addEventListener\("click",[\s\S]*?\n\}\);', src
    )
    assert ssml_handler, "downloadSsmlBtn click handler not found"
    assert "startDownload(" in ssml_handler.group(0), (
        "the SSML tab download button must route through startDownload()"
    )
    assert "startDownload(" in _function_source("handleTextGenerate"), (
        "the Text tab download path must route through startDownload()"
    )


def test_download_no_longer_cancels_instead_of_downloading():
    """The old guard aborted in-flight work and returned without downloading."""
    assert "activeAbortControllers.size > 0" not in _source(), (
        "download clicks must start a download; the removed guard cancelled "
        "any in-flight preview request and returned without downloading"
    )


def test_cancel_generation_leaves_download_buttons_alone():
    """cancelGeneration stops previews; the download flow owns its own label."""
    fn = _function_source("cancelGeneration")
    assert "downloadTextBtn" not in fn and "downloadSsmlBtn" not in fn, (
        "cancelGeneration must not reset download button labels: it runs inside "
        "callTTS(cancelExisting: true) and erased the Cancel Downloading state"
    )


def test_cancelled_download_never_saves_a_late_blob():
    """A response that lands after Cancel must not still save the file."""
    fn = _function_source("startDownload")
    assert re.search(r"if\s*\(activeDownload === state\)\s*downloadBlob\(", fn), (
        "startDownload must re-check ownership before saving: a blob that "
        "resolves after the user cancelled must not trigger a download"
    )
