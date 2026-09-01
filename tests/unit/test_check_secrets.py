"""The secret scanner that gates commits.

A scanner that silently stops matching is worse than none — it converts "we
check for this" into false confidence. So both halves are tested: that each
credential shape this project actually handles is caught, and that the ordinary
placeholders in templates and fixtures are not, because a scanner people learn
to ignore is also useless.
"""

from __future__ import annotations

import pytest

from scripts.check_secrets import ALLOW_MARKER, scan_text


def _findings(text: str) -> list[str]:
    return scan_text(text, "probe.py")


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (
            # pragma: allowlist secret
            'MARVEL_PRIVATE_KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"',
            "marvel-key-assignment",
        ),
        (
            "MARVEL_PUBLIC_KEY=0123456789abcdef0123456789abcdef",  # pragma: allowlist secret
            "marvel-key-assignment",
        ),
        # pragma: allowlist secret
        ("CROSSOVER_ADMIN_KEY=s3cret-admin-value", "admin-key-assignment"),
        (
            'secret = "xos_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"',  # pragma: allowlist secret
            "oauth-secret",
        ),
        (
            'token = "xo_at_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"',  # pragma: allowlist secret
            "oauth-secret",
        ),
        (
            't = "sqp_0123456789abcdef0123456789abcdef01234567"',  # pragma: allowlist secret
            "sonar-token",
        ),
        (
            'DSN = "postgresql://real:hunter2@db.example.com/x"',  # pragma: allowlist secret
            "dsn-with-password",
        ),
        # pragma: allowlist secret
        ('R = "redis://user:pw@cache.example.com:6379/0"', "dsn-with-password"),
        ('K = "AKIAIOSFODNN7EXAMPLE"', "aws-access-key"),  # pragma: allowlist secret
        ("-----BEGIN RSA PRIVATE KEY-----", "private-key-block"),  # pragma: allowlist secret
        (
            # pragma: allowlist secret
            'headers = {"Authorization": "Bearer abcdefghijklmnopqrstuvwxyz0123"}',
            "generic-bearer",
        ),
    ],
)
def test_each_credential_shape_is_caught(line: str, rule: str) -> None:
    findings = _findings(line)
    assert findings, f"not caught: {line}"
    assert rule in findings[0]


@pytest.mark.parametrize(
    "line",
    [
        # Empty assignments — what .env.example contains.
        "MARVEL_PUBLIC_KEY=",
        "MARVEL_PRIVATE_KEY=",
        "CROSSOVER_ADMIN_KEY=",
        # Obvious placeholders.
        "CROSSOVER_ADMIN_KEY=change-me",
        'K = "your-key-here"',
        'K = "<your-token>"',
        'K = "${MARVEL_PRIVATE_KEY}"',
        'K = "REDACTED"',
        # A DSN with no password is not a credential.
        'DSN = "postgresql+asyncpg://crossover@localhost:5433/crossover"',
        'DSN = "postgresql+asyncpg://localhost:5433/crossover"',
        # Prose about credentials.
        "# Set MARVEL_PRIVATE_KEY in .env, never in code.",
        '"""The admin key gates the curation views."""',
    ],
)
def test_ordinary_placeholders_are_not_flagged(line: str) -> None:
    """False positives are what get a scanner disabled, so these matter as much
    as the true positives."""
    assert _findings(line) == [], f"false positive on: {line}"


def test_the_allowlist_marker_works_on_the_line_above() -> None:
    """Needed because a long fixture plus an inline marker frequently exceeds
    the formatter's line length, and no marker should require choosing between
    two linters."""
    real = 'K = "AKIAIOSFODNN7EXAMPLE"'  # pragma: allowlist secret
    assert _findings(real)
    assert _findings(f"# {ALLOW_MARKER}\n{real}") == []


def test_the_marker_does_not_leak_two_lines_down() -> None:
    real = 'K = "AKIAIOSFODNN7EXAMPLE"'  # pragma: allowlist secret
    assert _findings(f"# {ALLOW_MARKER}\nclean line\n{real}")


def test_the_allowlist_marker_suppresses_a_line() -> None:
    # pragma: allowlist secret
    real = 'MARVEL_PRIVATE_KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"'
    assert _findings(real)
    assert _findings(f"{real}  # {ALLOW_MARKER}") == []


def test_the_line_number_is_reported() -> None:
    """A finding without a location is a scavenger hunt."""
    text = "\n".join(["clean", "clean", 'K = "AKIAIOSFODNN7EXAMPLE"'])  # pragma: allowlist secret
    assert _findings(text)[0].startswith("probe.py:3:")


def test_several_findings_in_one_file_are_all_reported() -> None:
    text = "\n".join(
        [
            'a = "AKIAIOSFODNN7EXAMPLE"',  # pragma: allowlist secret
            'b = "xos_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"',  # pragma: allowlist secret
        ]
    )
    assert len(_findings(text)) == 2


def test_this_repo_is_clean() -> None:
    """The end-to-end assertion: run the real scanner over the real tree.

    This is the check the pre-commit hook runs, so a regression that would block
    a commit shows up here first, with a readable diff of what changed.
    """
    from scripts.check_secrets import main

    assert main([]) == 0
