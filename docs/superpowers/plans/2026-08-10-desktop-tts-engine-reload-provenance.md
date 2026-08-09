# Desktop TTS Reload and Provenance Remediation

**Goal:** Close the two load-bearing residuals from the terminal remediation run:
`F-18` (the installer can signal an unrelated process whose PID was recycled) and
`F-19` (a legacy ownership manifest without `speechd.conf` provenance is migrated
from already-mutated state, so the promised uninstall round trip is wrong).

**Architecture:** Both defects live in `desktop/install.py`.

- Reload identity becomes correct by construction. The reload path opens a pidfd
  for the recorded PID, verifies through `/proc` that the pinned process really is
  Speech Dispatcher, and signals the pidfd rather than the number. A pidfd pins one
  specific process: if that process has exited, `pidfd_send_signal` fails with
  `ProcessLookupError` instead of reaching a recycled PID. An identity mismatch or
  a stale PID file is a normal no-op, while a malformed PID file or a refused
  signal stays an actionable `InstallError`.
- Provenance becomes honest instead of inferred. Manifests written by the current
  code already record whether `speechd.conf` existed and its original mode. A
  manifest lacking that provenance cannot be repaired, because once an older build
  created the managed file, "originally absent" and "originally empty" are
  observationally identical. Upgrade therefore refuses fail-closed with an
  actionable message, and uninstall degrades conservatively: it strips the managed
  block, never deletes the file, and preserves the file's current mode.

`desktop/install.py` was created on this unmerged branch, so no released build ever
wrote a manifest. Only a manual install from an intermediate branch commit can
produce a legacy manifest, which makes fail-closed refusal correct rather than a
compatibility break for real users.

**Tech stack:** Python standard library only, `pytest` for tests, Linux pidfd
(`os.pidfd_open`, `signal.pidfd_send_signal`), which are available on the declared
Python 3.11 floor.

## Global Constraints

- `desktop/` must import only the Python standard library. It must never import
  `server.py` or `flask`.
- Standard output carries only Speech Dispatcher protocol traffic. All logging goes
  to standard error.
- Never modify `tests/test_extension_split_sentences.py` or
  `tests/test_media_session.py`.
- Never weaken, skip, delete, or make timing-dependent any existing test. Changing
  a test call site because a function signature changed is allowed; changing an
  assertion to make a failure disappear is not.
- Preserve every earlier fix: manifest ownership and path validation, transactional
  rollback, prerequisite preflight, launcher quoting, backend restart on every
  boundary, generation-local cancellation, decoder process ownership, and backend
  URL validation.
- Keep the existing Speech Dispatcher response codes and events exactly as they
  are.
- Prove behavior with real processes, real signals, and real files rather than
  mocks wherever the standard library allows it. Never use `time.sleep` as an
  ordering mechanism.
- Run tests with `.venv/bin/python -m pytest` from the repository root. The full
  suite reports 437 passed at the time of writing; a different total is fine as
  long as nothing fails, errors, or is newly skipped.
- Do not add a runtime dependency, and do not touch the browser frontend or the
  Chrome extension.

## Task 1: Authenticate the reload target with a pidfd (F-18)

**Implementer tier:** Advanced

**Problem:** `_read_speechd_pid` proves only that the PID file is a regular
user-owned file, and `restart_speech_dispatcher` then sends `SIGHUP` to that
number. A hermetic probe placed an unrelated Python process's PID in the accepted
file and that process received the signal and exited. A stale PID file whose PID
has been recycled can therefore disrupt any same-user process, and `SIGHUP`
commonly terminates. `tests/test_desktop_install.py` currently codifies the unsafe
behavior by expecting an arbitrary Python child to be signalled.

**Files:**
- Modify: `desktop/install.py`
- Test: `tests/test_desktop_install.py`

**Interfaces:**
- Consumes: `_speechd_pid_path()` and `_read_speechd_pid()` from
  `desktop/install.py`, both unchanged.
- Produces: `SPEECHD_PROCESS_NAMES: tuple[str, ...]`, the accepted executable
  basenames.
- Produces: `_process_identity(pid: int) -> str | None`, returning the basename of
  the live process's executable, falling back to its `argv[0]` basename, and
  `None` when the process is gone or unreadable.
- Produces: `restart_speech_dispatcher(*, opener=os.pidfd_open, sender=signal.pidfd_send_signal, identity=_process_identity) -> bool`,
  returning True only when a verified Speech Dispatcher process was signalled.

### Steps

- [ ] **Step 1: Write the failing reload tests.** In
  `tests/test_desktop_install.py`, replace the whole `class TestRestart:` body with
  the version below. It keeps the existing missing-daemon, malformed-PID, and
  refused-signal expectations and adds identity coverage.

```python
class TestRestart:
    @staticmethod
    def _pid_file(runtime):
        return runtime / "speech-dispatcher" / "pid" / "speech-dispatcher.pid"

    @staticmethod
    def _daemon():
        script = (
            "import signal, sys\n"
            "def reload_config(_signum, _frame):\n"
            "    print('RELOADED', flush=True)\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGHUP, reload_config)\n"
            "print('READY', flush=True)\n"
            "signal.pause()\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        assert process.stdout.readline() == "READY\n"
        return process

    def test_missing_daemon_is_a_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert install.restart_speech_dispatcher() is False

    def test_malformed_pid_file_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        pid_file = self._pid_file(tmp_path)
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text("not-a-pid\n")

        with pytest.raises(install.InstallError, match="PID|pid"):
            install.restart_speech_dispatcher()

    def test_recycled_pid_of_another_process_is_never_signalled(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        process = self._daemon()
        try:
            pid_file = self._pid_file(tmp_path)
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text(f"{process.pid}\n")

            # Real identity: an ordinary Python child is not Speech Dispatcher.
            assert install.restart_speech_dispatcher() is False
            assert process.poll() is None
        finally:
            process.terminate()
            process.wait(timeout=3)

    def test_exited_pid_is_a_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        process = self._daemon()
        process.terminate()
        process.wait(timeout=3)
        pid_file = self._pid_file(tmp_path)
        pid_file.parent.mkdir(parents=True)
        pid_file.write_text(f"{process.pid}\n")

        assert (
            install.restart_speech_dispatcher(
                identity=lambda _pid: "speech-dispatcher"
            )
            is False
        )

    def test_unsupported_pidfd_does_not_signal(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        process = self._daemon()
        try:
            pid_file = self._pid_file(tmp_path)
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text(f"{process.pid}\n")

            def unsupported(_pid, _flags):
                raise NotImplementedError("pidfd unavailable")

            assert (
                install.restart_speech_dispatcher(
                    opener=unsupported,
                    identity=lambda _pid: "speech-dispatcher",
                )
                is False
            )
            assert process.poll() is None
        finally:
            process.terminate()
            process.wait(timeout=3)

    def test_signal_failure_is_actionable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        process = self._daemon()
        try:
            pid_file = self._pid_file(tmp_path)
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text(f"{process.pid}\n")

            def deny(_descriptor, _signal):
                raise PermissionError("signal denied")

            with pytest.raises(install.InstallError, match="reload|signal"):
                install.restart_speech_dispatcher(
                    sender=deny,
                    identity=lambda _pid: "speech-dispatcher",
                )
            assert process.poll() is None
        finally:
            process.terminate()
            process.wait(timeout=3)

    def test_sends_real_sighup_through_a_pidfd(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        process = self._daemon()
        try:
            pid_file = self._pid_file(tmp_path)
            pid_file.parent.mkdir(parents=True)
            pid_file.write_text(f"{process.pid}\n")

            reloaded = install.restart_speech_dispatcher(
                identity=lambda _pid: "speech-dispatcher"
            )
            try:
                stdout, stderr = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                pytest.fail("the verified daemon did not receive SIGHUP")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)

        assert reloaded is True
        assert process.returncode == 0, stderr
        assert stdout == "RELOADED\n"

    def test_identity_reads_the_live_executable(self):
        assert install._process_identity(os.getpid()) is not None
        assert install._process_identity(2**31 - 1) is None
```

- [ ] **Step 2: Run the reload tests and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_install.py::TestRestart -q
```

Expected: failures. `restart_speech_dispatcher` still takes `kill`, has no
identity or pidfd seams, and signals an unrelated process.

- [ ] **Step 3: Authenticate and signal through a pidfd.** In
  `desktop/install.py`, add the accepted-name constant next to the other module
  constants:

```python
SPEECHD_PROCESS_NAMES = ("speech-dispatcher",)
```

Then replace `restart_speech_dispatcher` with the identity-checked version, and add
`_process_identity` immediately above it. Keep `_speechd_pid_path` and
`_read_speechd_pid` exactly as they are.

```python
def _process_identity(pid: int) -> str | None:
    """Basename of the live process's program, or None if it cannot be read."""
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            argv0 = stream.read().split(b"\0")[0]
    except OSError:
        return None
    if not argv0:
        return None
    return os.path.basename(argv0.decode("utf-8", "replace"))


def restart_speech_dispatcher(
    *,
    opener: Callable[[int, int], int] = os.pidfd_open,
    sender: Callable[[int, int], None] = signal.pidfd_send_signal,
    identity: Callable[[int], str | None] = _process_identity,
) -> bool:
    """Reload the user's running Speech Dispatcher with ``SIGHUP``.

    The recorded PID is pinned with a pidfd before it is signalled, so a PID that
    has already been recycled cannot be reached: signalling a stale pidfd fails
    instead of hitting whatever process now owns that number. A missing PID file,
    an exited daemon, or a process that is not Speech Dispatcher is a no-op,
    because the next daemon start reads the updated configuration anyway.
    """
    pid_path = _speechd_pid_path()
    if pid_path is None:
        logger.info(
            "XDG_RUNTIME_DIR is unset; no running Speech Dispatcher to reload."
        )
        return False
    pid = _read_speechd_pid(pid_path)
    if pid is None:
        logger.info("No running Speech Dispatcher to reload at %s.", pid_path)
        return False

    try:
        descriptor = opener(pid, 0)
    except ProcessLookupError:
        logger.info("Speech Dispatcher PID %d is no longer running.", pid)
        return False
    except (AttributeError, NotImplementedError, OSError) as exc:
        logger.warning(
            "Cannot pin Speech Dispatcher PID %d for a safe reload (%s); "
            "restart Speech Dispatcher manually to load the new configuration.",
            pid,
            exc,
        )
        return False

    try:
        # The pidfd already pins one process, so this check cannot be raced into
        # signalling a different one: if the pinned process exited, the send below
        # fails rather than reaching a recycled PID.
        name = identity(pid)
        if name not in SPEECHD_PROCESS_NAMES:
            logger.warning(
                "PID %d from %s is %s, not Speech Dispatcher; refusing to signal "
                "it. Restart Speech Dispatcher manually if it is running.",
                pid,
                pid_path,
                name if name is not None else "gone",
            )
            return False
        try:
            sender(descriptor, signal.SIGHUP)
        except ProcessLookupError:
            logger.info("Speech Dispatcher PID %d exited before reload.", pid)
            return False
        except (OSError, OverflowError, ValueError) as exc:
            raise InstallError(
                f"could not signal Speech Dispatcher PID {pid} to reload: {exc}"
            ) from exc
    finally:
        os.close(descriptor)

    logger.info("Reloaded Speech Dispatcher configuration for PID %d.", pid)
    return True
```

- [ ] **Step 4: Run the reload tests, the installer file, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_install.py -q
.venv/bin/python -m pytest -q
```

Expected: both pass with no failures, no errors, and no new skips.

- [ ] **Step 5: Commit.**

```bash
git add desktop/install.py tests/test_desktop_install.py
git commit -m "fix(desktop): pin the reload target with a pidfd before signalling"
```

## Task 2: Refuse to guess legacy speechd.conf provenance (F-19)

**Implementer tier:** Advanced

**Problem:** `_speechd_provenance` falls back to the state observed during an
upgrade when the manifest has no provenance. An installation made by an older
build with an originally absent `speechd.conf` already has an installer-created
managed file by then, so it is recorded as `speechd_conf_existed=True`. A probe of
exactly that shape produced `migrated_existed=True` and, after uninstall, an empty
`speechd.conf` left behind. The original mode is likewise unrecoverable because the
older build wrote the file with the 0644 default. Inference cannot be made correct,
so it must be replaced by an explicit policy.

**Files:**
- Modify: `desktop/install.py`
- Test: `tests/test_desktop_install.py`

**Interfaces:**
- Consumes: `_validate_manifest`, `_load_manifest`, `_check_install_ownership`,
  `_snapshot_path`, and `_atomic_write` from `desktop/install.py`.
- Produces: `_has_provenance(manifest: dict[str, object]) -> bool`.
- Removes: `_speechd_provenance`. Upgrade requires recorded provenance, and
  uninstall no longer infers it.

### Steps

- [ ] **Step 1: Write the failing legacy-manifest tests.** Append this class to
  `tests/test_desktop_install.py`, immediately before `class TestUpgradeRollback:`.

```python
class TestLegacyManifestProvenance:
    """A manifest without provenance is never repaired by guessing."""

    def _legacy_install(self, source_root, paths):
        _install(source_root, paths)
        manifest_file = paths["root"] / install.MANIFEST_NAME
        payload = json.loads(manifest_file.read_text())
        del payload["speechd_conf_existed"]
        del payload["speechd_conf_mode"]
        manifest_file.write_text(json.dumps(payload, indent=2))
        return manifest_file

    def test_upgrade_refuses_a_legacy_manifest_without_mutation(
        self, source_root, paths
    ):
        self._legacy_install(source_root, paths)
        before = _snapshot(source_root.parent)

        with pytest.raises(install.InstallError, match="provenance"):
            _install(source_root, paths)

        assert _snapshot(source_root.parent) == before

    def test_legacy_uninstall_keeps_the_config_and_its_mode(
        self, source_root, paths
    ):
        self._legacy_install(source_root, paths)
        conf = paths["config_dir"] / "speechd.conf"
        conf.chmod(0o600)

        removed = install.uninstall(
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
        )

        assert conf.is_file()
        assert sc.BEGIN_MARKER not in conf.read_text()
        assert conf.stat().st_mode & 0o777 == 0o600
        assert str(conf) in removed
        assert not paths["root"].exists()

    def test_legacy_uninstall_preserves_unrelated_user_content(
        self, source_root, paths
    ):
        paths["config_dir"].mkdir(parents=True)
        conf = paths["config_dir"] / "speechd.conf"
        conf.write_text("LogLevel 3\n")
        self._legacy_install(source_root, paths)

        install.uninstall(
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
        )

        assert conf.read_text() == "LogLevel 3\n"

    def test_incomplete_provenance_is_still_rejected(self, source_root, paths):
        _install(source_root, paths)
        manifest_file = paths["root"] / install.MANIFEST_NAME
        payload = json.loads(manifest_file.read_text())
        del payload["speechd_conf_mode"]
        manifest_file.write_text(json.dumps(payload, indent=2))

        with pytest.raises(install.InstallOwnershipError, match="incomplete"):
            install.uninstall(
                root=paths["root"],
                launcher=paths["launcher"],
                config_dir=paths["config_dir"],
            )
```

- [ ] **Step 2: Run the new tests and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_install.py -k LegacyManifestProvenance -q
```

Expected: failures. Upgrade currently migrates a legacy manifest by inference and
uninstall deletes the installer-created file.

- [ ] **Step 3: Make provenance explicit.** In `desktop/install.py`, delete the
  whole `_speechd_provenance` function and add:

```python
def _has_provenance(manifest: dict[str, object]) -> bool:
    """True when the manifest records the original speechd.conf state."""
    return "speechd_conf_existed" in manifest
```

- [ ] **Step 4: Refuse a legacy upgrade.** In `install`, replace the provenance
  block that currently starts with `speechd_conf_existed, speechd_conf_mode = _speechd_provenance(`
  with:

```python
    if owned_manifest is not None and not _has_provenance(owned_manifest):
        raise InstallError(
            "this installation was made by an older build that did not record "
            "speechd.conf provenance, so its original state cannot be restored. "
            f"Run `python -m desktop.install uninstall` first (it keeps "
            f"{speechd_conf}), then install again."
        )
    snapshot = snapshots[speechd_conf]
    if owned_manifest is not None:
        speechd_conf_existed = owned_manifest["speechd_conf_existed"] is True
        recorded_mode = owned_manifest["speechd_conf_mode"]
        speechd_conf_mode = recorded_mode if isinstance(recorded_mode, int) else None
    else:
        speechd_conf_existed = snapshot.kind == "file"
        speechd_conf_mode = snapshot.mode if speechd_conf_existed else None
```

The refusal happens before `staging` is created, so no target is touched.

- [ ] **Step 5: Degrade uninstall conservatively.** In `uninstall`, replace the
  block that begins `speechd_snapshot = _snapshot_path(speechd_conf)` and ends with
  the managed-block rewrite with:

```python
    speechd_snapshot = _snapshot_path(speechd_conf)
    if _has_provenance(owned):
        speechd_conf_existed = owned["speechd_conf_existed"] is True
        recorded_mode = owned["speechd_conf_mode"]
        speechd_conf_mode = recorded_mode if isinstance(recorded_mode, int) else None
    else:
        # Provenance was never recorded, and an installer-created file is now
        # indistinguishable from a pre-existing empty one. Keep the file.
        logger.warning(
            "Ownership manifest has no speechd.conf provenance; keeping %s and "
            "removing only the managed block.",
            speechd_conf,
        )
        speechd_conf_existed = True
        speechd_conf_mode = speechd_snapshot.mode

    removed: list[str] = []
    if speechd_conf.is_file():
        current = speechd_conf.read_text(encoding="utf-8")
        cleaned = remove_managed_block(current)
        if not speechd_conf_existed and not cleaned:
            speechd_conf.unlink()
            removed.append(str(speechd_conf))
        else:
            restored_mode = (
                speechd_conf_mode
                if speechd_conf_mode is not None
                else speechd_snapshot.mode
            )
            if cleaned != current or restored_mode != speechd_snapshot.mode:
                _atomic_write(
                    speechd_conf, cleaned.encode("utf-8"), restored_mode
                )
                removed.append(str(speechd_conf))
```

- [ ] **Step 6: Run the installer tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_install.py -q
.venv/bin/python -m pytest -q
```

Expected: every existing provenance round-trip test still passes alongside the new
legacy tests, and the full suite has no failures, no errors, and no new skips.

- [ ] **Step 7: Document the policy.** In `docs/desktop-tts.md`, add one short
  paragraph to the install/uninstall section stating that uninstall restores
  `speechd.conf` exactly when the manifest recorded its original state, that an
  installation from an older build must be uninstalled before reinstalling, and
  that in that case uninstall keeps `speechd.conf` and removes only the managed
  block.

- [ ] **Step 8: Commit.**

```bash
git add desktop/install.py tests/test_desktop_install.py docs/desktop-tts.md
git commit -m "fix(desktop): require recorded speechd.conf provenance"
```
