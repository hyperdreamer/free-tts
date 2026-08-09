"""Edit the user's speechd.conf without disturbing anything we do not own.

All of our lines live between two markers. Everything outside them is treated as
the user's, with one exception: a competing uncommented ``DefaultModule`` is
prefixed so it can be restored verbatim on uninstall.
"""

from __future__ import annotations

BEGIN_MARKER = "# BEGIN free-tts managed block (do not edit)"
END_MARKER = "# END free-tts managed block"
DISABLED_PREFIX = "#free-tts-disabled "


def _strip_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN_MARKER:
            inside = True
            continue
        if inside:
            if line.strip() == END_MARKER:
                inside = False
            continue
        out.append(line)
    return out


def apply_managed_block(text: str, launcher_name: str, module_conf: str) -> str:
    """Return ``text`` with exactly one current free-tts managed block."""
    lines = _strip_block(text.splitlines())
    rewritten: list[str] = []
    for line in lines:
        if line.strip().startswith("DefaultModule "):
            rewritten.append(f"{DISABLED_PREFIX}{line}")
        else:
            rewritten.append(line)
    while rewritten and not rewritten[-1].strip():
        rewritten.pop()

    block = [
        BEGIN_MARKER,
        f'AddModule "free-tts" "{launcher_name}" "{module_conf}"',
        "DefaultModule free-tts",
        END_MARKER,
    ]
    body = rewritten + ([""] if rewritten else []) + block
    return "\n".join(body) + "\n"


def remove_managed_block(text: str) -> str:
    """Return ``text`` with our block gone and any disabled line restored."""
    lines = _strip_block(text.splitlines())
    restored = [
        line[len(DISABLED_PREFIX) :] if line.startswith(DISABLED_PREFIX) else line
        for line in lines
    ]
    while restored and not restored[-1].strip():
        restored.pop()
    if not restored:
        return ""
    return "\n".join(restored) + "\n"
