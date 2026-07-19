# Ignored Audit Findings

These findings were reviewed during the Claude → Hermes → Codex audit loop and intentionally left unchanged because fixing them requires a product/security decision or could regress existing behavior.

## F-001 — Extension LAN server URLs are rejected by `normalizeServerUrl`

**Audit finding:** `extension/background.js` and `extension/popup.js` only accept `localhost`, `127.0.0.1`, and `::1`, while the options page can save a LAN URL. Loading that URL later silently falls back to localhost.

**Why deferred:** Supporting arbitrary/LAN server hosts correctly requires changing the extension's `host_permissions` and URL-validation policy, not merely removing the allowlist. The current restriction is an intentional security boundary. Removing it without a deliberate permission and UX design could expose requests to unintended origins or create misleading configuration behavior. The backend already supports LAN CORS when explicitly configured, but extension LAN support needs a separate product decision and browser-permission review.

**Evidence needed before changing:** Define supported remote-host scope (specific private networks vs arbitrary HTTPS hosts), update `manifest.json` permissions, decide whether HTTP LAN endpoints are acceptable, add options-page validation/error feedback, and add end-to-end extension tests for the approved scope.

## F-007 — Shared frontend/extension helper implementations can drift

**Audit finding:** `splitSentences`, XML escaping, and SSML-building logic exist in both the web frontend and extension code; the implementations can diverge. Existing tests compare the relevant sentence-splitting behavior, while pitch support intentionally differs between the web UI and extension.

**Why deferred:** Extracting a shared module would change the extension/web loading structure and could regress Chrome MV3 execution contexts or the intentionally different pitch contract. The current tests provide a useful guard for the shared sentence-splitting behavior. This is maintainability risk, not a demonstrated runtime defect.

**Evidence needed before changing:** A concrete shared-module design compatible with file-loaded web pages, Flask-served assets, and MV3 service-worker execution, plus parity tests for every helper and explicit tests for intentional pitch differences.

## F-008 — Empty voice cache rejects all voices

**Audit finding:** If `edge_tts.list_voices()` returns an empty list without raising, the cache is marked ready and `_is_known_voice()` rejects every voice.

**Why deferred:** Accepting all voices when a successful cache refresh returns an empty list would restore a previously removed `or not _voice_cache` bypass and weaken voice validation. The empty-list response is an unverified upstream/API anomaly; treating it as a hard failure or providing a fallback cache requires a separate operational policy. No change was made to preserve the current validation behavior.

**Evidence needed before changing:** Reproduce an empty successful `list_voices()` response, establish whether it is transient or valid, and choose an explicit policy (retain the previous known-good cache, mark cache unavailable and allow fallback validation, or fail startup) with tests covering startup and refresh behavior.
