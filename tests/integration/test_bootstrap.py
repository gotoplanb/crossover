"""`crossover bootstrap` — the postdeploy hook behind the one-click button.

The failure this guards against is specific and invisible: a stranger presses
Deploy, the app boots, and the login page cannot be passed. Nothing else
exercises this path, because it only ever runs once, on somebody else's machine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from auth import authenticate
from config.settings import get_settings
from models.user import User
from scripts.cli import _bootstrap

OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "a-generated-deploy-secret"  # pragma: allowlist secret


@pytest.fixture
def deploy_env(monkeypatch, db_conn):
    """The config a button deploy supplies.

    Also binds the CLI's own sessions to the test connection. `bootstrap` opens
    `SessionLocal()` directly — correct for a one-off command, but in tests that
    commits to the developer's database outside the per-test rollback, so runs
    would leave users behind and depend on each other's ordering.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    monkeypatch.setattr(
        "db.session.SessionLocal",
        lambda: AsyncSession(
            bind=db_conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        ),
    )
    monkeypatch.setenv("CROSSOVER_PUBLIC_URL", "https://example.herokuapp.com")
    monkeypatch.setenv("CROSSOVER_OWNER_EMAIL", OWNER_EMAIL)
    monkeypatch.setenv("CROSSOVER_OWNER_HANDLE", "owner")
    monkeypatch.setenv("CROSSOVER_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.setenv("CROSSOVER_INVITE_CODE", "let-them-in")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_a_button_deploy_produces_an_admin_who_can_sign_in(
    session, deploy_env, capsys
) -> None:
    """The whole point. An admin who cannot sign in reads as a broken app
    rather than an unconfigured one."""
    assert await _bootstrap() == 0

    user = await session.scalar(select(User).where(User.email == OWNER_EMAIL))
    assert user is not None
    assert user.is_admin is True
    assert user.password_hash.startswith("$argon2id$")
    assert await authenticate(session, "owner", OWNER_PASSWORD) is not None
    assert "Setup warnings" not in capsys.readouterr().out


async def test_the_password_survives_a_second_run(session, deploy_env, monkeypatch) -> None:
    """Postdeploy hooks get re-run. Silently resetting the owner's credential
    would lock out somebody who had already changed it."""
    await _bootstrap()
    user = await session.scalar(select(User).where(User.email == OWNER_EMAIL))
    original = user.password_hash

    monkeypatch.setenv("CROSSOVER_OWNER_PASSWORD", "a-completely-different-secret")
    await _bootstrap()
    await session.refresh(user)
    assert user.password_hash == original
    assert await authenticate(session, "owner", OWNER_PASSWORD) is not None


async def test_no_password_is_reported_rather_than_left_to_be_discovered(
    session, deploy_env, monkeypatch, capsys
) -> None:
    monkeypatch.delenv("CROSSOVER_OWNER_PASSWORD")
    get_settings.cache_clear()
    await _bootstrap()

    out = capsys.readouterr().out
    assert "has no password" in out
    assert "set-password" in out, "the warning should say how to fix it"


async def test_a_too_short_password_is_refused_not_silently_accepted(
    session, deploy_env, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("CROSSOVER_OWNER_PASSWORD", "short")
    get_settings.cache_clear()
    await _bootstrap()

    user = await session.scalar(select(User).where(User.email == OWNER_EMAIL))
    assert not user.password_hash
    assert "shorter than" in capsys.readouterr().out


async def test_a_closed_registration_is_pointed_out(
    session, deploy_env, monkeypatch, capsys
) -> None:
    """Not an error — closed is the safe default — but a deployer should learn
    it from the deploy log rather than from a 404 later."""
    monkeypatch.delenv("CROSSOVER_INVITE_CODE")
    get_settings.cache_clear()
    await _bootstrap()
    assert "CROSSOVER_INVITE_CODE is not set" in capsys.readouterr().out


async def test_a_localhost_public_url_is_caught(session, deploy_env, monkeypatch, capsys) -> None:
    """It breaks OAuth and the MCP transport, but only when a connector tries
    to attach — long after the deploy looked successful."""
    monkeypatch.setenv("CROSSOVER_PUBLIC_URL", "http://localhost:8020")
    get_settings.cache_clear()
    assert await _bootstrap() == 0, "a misconfiguration must not roll back the release"
    assert "not reachable from anywhere" in capsys.readouterr().out
