"""The version check of the CI (tools/check_version.py)."""

from __future__ import annotations

from tools.check_version import check, main

MANIFEST = '{"domain": "gmx_mail_mcp", "version": "0.1.0"}'
CHANGELOG = "# Changelog\n\n## 0.1.0 – 2026-11-26\n\n- x\n\n## 0.0.7 – 2026-11-24\n"


def test_consistent_versions_pass() -> None:
    assert check(MANIFEST, CHANGELOG, None) == []
    assert check(MANIFEST, CHANGELOG, "v0.1.0") == []


def test_mismatches_are_reported() -> None:
    assert check(MANIFEST.replace("0.1.0", "0.1.1"), CHANGELOG, None)
    assert check(MANIFEST, CHANGELOG, "v0.0.9")
    assert check(MANIFEST, "# Changelog\n", None)


def test_the_repository_is_consistent() -> None:
    assert main([]) == 0
