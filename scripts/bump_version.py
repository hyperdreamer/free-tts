#!/usr/bin/env python3
"""Bump version across all project references: VERSION, extension/manifest.json."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_current() -> str:
    """Read current version from VERSION file (single source of truth)."""
    v = ROOT / "VERSION"
    if not v.exists():
        print("ERROR: VERSION file not found", file=sys.stderr)
        sys.exit(1)
    return v.read_text().strip()


def bump_version(current: str, part: str = "patch") -> str:
    """Bump major, minor, or patch."""
    parts = current.split(".")
    if part == "major":
        parts[0] = str(int(parts[0]) + 1)
        parts[1] = parts[2] = "0"
    elif part == "minor":
        parts[1] = str(int(parts[1]) + 1)
        parts[2] = "0"
    else:  # patch
        parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def write_all(version: str) -> None:
    """Write version to VERSION and extension/manifest.json."""
    # VERSION
    (ROOT / "VERSION").write_text(version + "\n")
    # manifest.json
    mf = ROOT / "extension" / "manifest.json"
    data = json.loads(mf.read_text())
    old = data["version"]
    data["version"] = version
    mf.write_text(json.dumps(data, indent=2) + "\n")
    print(f"{old} → {version}")


if __name__ == "__main__":
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    current = read_current()
    new = bump_version(current, part)
    write_all(new)
    print(f"Bumped {current} → {new} ({part})")
