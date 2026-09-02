"""The CI workflow must actually enforce what the pre-push hook enforces.

Until CI existed, the only thing checking quality was a *local* git hook — so a
contributor's pull request was checked by nothing and `--no-verify` bypassed
everything. Having a workflow that merely *looks* like it checks things would be
worse than none, because it would be trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
WORKFLOW = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
HOOK = (REPO / "scripts" / "hooks" / "pre-push").read_text()


def _all_run_steps() -> str:
    commands = []
    for job in WORKFLOW["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                commands.append(step["run"])
    return "\n".join(commands)


def test_it_runs_on_pull_requests() -> None:
    """The whole point. A push-only workflow leaves contributions unchecked."""
    # PyYAML parses a bare `on:` key as the boolean True.
    triggers = WORKFLOW.get("on") or WORKFLOW.get(True)
    assert "pull_request" in triggers
    assert "push" in triggers


@pytest.mark.parametrize(
    ("gate", "needle"),
    [
        ("ruff", "ruff check"),
        ("secret scan", "check_secrets.py"),
        ("tests with coverage", "--cov"),
    ],
)
def test_each_pre_push_gate_is_also_in_ci(gate: str, needle: str) -> None:
    """Drift here means CI silently stops enforcing something the hook does, and
    only the person with hooks installed would ever notice."""
    assert needle in HOOK, f"{gate} is not in the pre-push hook"
    assert needle in _all_run_steps(), f"{gate} is in the hook but not in CI"


def test_the_coverage_floor_is_not_restated_in_ci() -> None:
    """`fail_under` lives in pyproject.toml so there is one number to change.
    A duplicate here would drift and quietly weaken the gate."""
    assert "fail_under" not in _all_run_steps()
    assert "--cov-fail-under" not in _all_run_steps()


def test_dependencies_install_from_the_lockfile() -> None:
    """--locked, so CI fails on lock drift rather than resolving something
    different from what deploys."""
    assert "uv sync --locked" in _all_run_steps()


def test_the_unit_job_needs_no_database() -> None:
    """It is the fast-fail path: a lint slip or a committed secret should not
    wait behind Postgres starting."""
    checks = WORKFLOW["jobs"]["checks"]
    assert "services" not in checks
    assert "tests/unit" in "\n".join(
        step.get("run", "") for step in checks["steps"]
    )


def test_the_full_suite_job_provides_postgres() -> None:
    services = WORKFLOW["jobs"]["test"]["services"]
    assert "postgres" in services
    assert services["postgres"]["image"].startswith("postgres:")
    # Health-checked, or the suite races the database starting up.
    assert "health-cmd" in services["postgres"]["options"]


def test_no_password_is_committed_in_the_workflow() -> None:
    """Trust auth on a throwaway CI container, same as docker-compose — so
    there is no credential in a public file."""
    postgres = WORKFLOW["jobs"]["test"]["services"]["postgres"]
    assert postgres["env"].get("POSTGRES_HOST_AUTH_METHOD") == "trust"
    assert "POSTGRES_PASSWORD" not in postgres["env"]

    from scripts.check_secrets import scan_text

    raw = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert scan_text(raw, "ci.yml") == []


def test_sonar_is_optional_and_skips_cleanly() -> None:
    """The local Watchtower instance is unreachable from a runner. A missing
    optional integration must not turn every pull request red."""
    sonar = WORKFLOW["jobs"]["sonar"]
    assert "SONAR_HOST_URL" in sonar["if"]
    assert sonar["needs"] == "test"


def test_the_workflow_declares_least_privilege() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_runs_are_superseded_not_queued() -> None:
    assert WORKFLOW["concurrency"]["cancel-in-progress"] is True


def test_dependabot_is_configured() -> None:
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    ecosystems = {u["package-ecosystem"] for u in config["updates"]}
    assert {"uv", "github-actions"} <= ecosystems
