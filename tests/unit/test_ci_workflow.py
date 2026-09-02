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
    assert "tests/unit" in "\n".join(step.get("run", "") for step in checks["steps"])


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


# --- the deploy job ----------------------------------------------------------
#
# Deploying is the one job that changes something outside the repository, so its
# guards are worth asserting rather than trusting to a careful reading.

DEPLOY = WORKFLOW["jobs"]["deploy"]


def test_deploy_waits_for_both_gates() -> None:
    """Not just the fast one. A deploy that skipped the database suite would be
    a deploy that skipped every migration test."""
    assert set(DEPLOY["needs"]) == {"checks", "test"}


def test_deploy_only_happens_on_a_push_to_main() -> None:
    """A pull request from a fork must never be able to reach production."""
    condition = DEPLOY["if"]
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition


def test_deploy_skips_cleanly_when_unconfigured() -> None:
    """Gated on the variable, not the secret: `secrets` cannot be referenced in
    a job-level `if`, and a repo without the credential should skip rather than
    turn red."""
    assert "vars.HEROKU_APP_NAME != ''" in DEPLOY["if"]


def test_deploys_queue_rather_than_race() -> None:
    """The workflow-level group cancels superseded runs, which is right — a
    newer commit should win. Two deploys pushing at once never is."""
    assert DEPLOY["concurrency"]["group"] == "deploy-heroku"
    assert DEPLOY["concurrency"]["cancel-in-progress"] is False


def test_deploy_checks_that_the_release_actually_succeeded() -> None:
    """The most important guard here. `git push` to Heroku succeeds even when
    the release command fails — the Procfile runs `alembic upgrade head`, and
    Heroku itself says the release "will not be available until the command
    succeeds". Without this the pipeline would go green on a failed migration,
    which is the exact failure a deploy gate exists to catch."""
    steps = "\n".join(s.get("run", "") for s in DEPLOY["steps"])
    assert "releases" in steps, "the release status is never queried"
    assert "succeeded" in steps and "failed" in steps
    assert "exit 1" in steps, "a failed release must fail the job"


def test_deploy_pushes_full_history() -> None:
    """Heroku's git receive rejects a shallow push, and checkout defaults to
    depth 1."""
    checkout = next(s for s in DEPLOY["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout["with"]["fetch-depth"] == 0


def test_deploy_does_not_force_push() -> None:
    """If Heroku's history has diverged from ours, something happened that a
    deploy should not paper over."""
    steps = "\n".join(s.get("run", "") for s in DEPLOY["steps"])
    assert "--force" not in steps and "-f " not in steps


def test_the_credential_is_never_written_into_the_workflow() -> None:
    raw = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "HEROKU_API_KEY: ${{ secrets.HEROKU_API_KEY }}" in raw
    # The only occurrences may be the secret reference and shell expansions.
    for line in raw.splitlines():
        if "HEROKU_API_KEY" in line:
            assert "secrets.HEROKU_API_KEY" in line or "$HEROKU_API_KEY" in line, line
