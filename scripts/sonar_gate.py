#!/usr/bin/env python3
"""Poll SonarQube's quality gate for the latest analysis and report it.

Split out of the pre-push hook because the gate is computed *asynchronously*
after the scanner uploads: querying it immediately reads the previous run's
verdict, which is the kind of green-when-actually-red that makes a gate worse
than no gate.

Reads SONAR_TOKEN and optionally SONAR_HOST_URL from the environment or .env.
Exits 0 on OK, 1 on ERROR or timeout.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from base64 import b64encode
from pathlib import Path

PROJECT_KEY = "crossover"
DEFAULT_HOST = "http://localhost:9000"
POLL_TIMEOUT_S = 90
POLL_INTERVAL_S = 3


def _load_env() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _get(url: str, token: str) -> dict:
    request = urllib.request.Request(url)  # noqa: S310 — fixed local https/http host
    # Sonar takes the token as the Basic-auth username with an empty password.
    request.add_header(
        "Authorization", "Basic " + b64encode(f"{token}:".encode()).decode()
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.load(response)


def _report(status: dict) -> None:
    print(f"  quality gate: {status['status']}")
    for condition in status.get("conditions", []):
        mark = "✓" if condition["status"] == "OK" else "✗"
        print(
            f"    {mark} {condition['metricKey']}: "
            f"{condition.get('actualValue')} "
            f"({condition.get('comparator', '')} {condition.get('errorThreshold', '')})"
        )


def main() -> int:
    _load_env()
    token = os.environ.get("SONAR_TOKEN")
    host = os.environ.get("SONAR_HOST_URL", DEFAULT_HOST).rstrip("/")
    if not token:
        print("  SONAR_TOKEN unset — cannot read the quality gate", file=sys.stderr)
        return 1

    # Wait for the analysis queued by the scanner to finish, so the gate we read
    # belongs to this push and not the previous one.
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            queue = _get(
                f"{host}/api/ce/component?component={PROJECT_KEY}", token
            )
        except urllib.error.URLError as exc:
            # HTTPError subclasses URLError, so this covers both.
            print(f"  could not reach SonarQube: {exc}", file=sys.stderr)
            return 1
        if not queue.get("queue") and queue.get("current", {}).get("status") in {
            "SUCCESS",
            "FAILED",
            "CANCELED",
        }:
            if queue["current"]["status"] != "SUCCESS":
                print(
                    f"  analysis {queue['current']['status'].lower()} — see {host}"
                    f"/dashboard?id={PROJECT_KEY}",
                    file=sys.stderr,
                )
                return 1
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        print(
            f"  analysis did not finish within {POLL_TIMEOUT_S}s — check {host}"
            f"/dashboard?id={PROJECT_KEY}",
            file=sys.stderr,
        )
        return 1

    try:
        status = _get(
            f"{host}/api/qualitygates/project_status?projectKey={PROJECT_KEY}", token
        )["projectStatus"]
    except (urllib.error.URLError, KeyError) as exc:
        # HTTPError subclasses URLError; KeyError covers an unexpected shape.
        print(f"  could not read the quality gate: {exc}", file=sys.stderr)
        return 1

    _report(status)
    if status["status"] != "OK":
        print(f"  see {host}/dashboard?id={PROJECT_KEY}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
