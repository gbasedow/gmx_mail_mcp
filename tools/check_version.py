"""Check that the manifest version, the newest CHANGELOG entry and (on a tag build) the tag agree.

Usage: python tools/check_version.py [--tag vX.Y.Z]. Exit code 1 with a message on a mismatch. Used by the CI (validate.yml).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "gmx_mail_mcp" / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"
_HEADING = re.compile(r"^## (\d+\.\d+\.\d+)\b", re.MULTILINE)


def check(manifest_text: str, changelog_text: str, tag: str | None) -> list[str]:
    """Return the problems found (empty list = consistent)."""
    problems: list[str] = []
    version = str(json.loads(manifest_text).get("version", ""))
    headings = _HEADING.findall(changelog_text)
    if not headings:
        problems.append("CHANGELOG.md has no version heading (## X.Y.Z)")
    elif headings[0] != version:
        problems.append(f"manifest version {version} differs from the newest CHANGELOG entry {headings[0]}")
    if tag is not None and tag != f"v{version}":
        problems.append(f"tag {tag} differs from the manifest version v{version}")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="tag name of a release build, e.g. v0.1.0")
    args = parser.parse_args(argv)
    problems = check(MANIFEST.read_text(encoding="utf-8"), CHANGELOG.read_text(encoding="utf-8"), args.tag or None)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
