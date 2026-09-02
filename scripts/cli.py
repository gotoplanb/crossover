"""Operational entry points. `python -m scripts.cli <command>`.

The commands that matter for getting this running for real are `check-api-key`
and `sync-event`, in that order — see docs/gates.md for why the API key is the
precondition everything else waits on.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from config.settings import get_settings


async def _check_api_key() -> int:
    """SPEC §0: confirm the key works and that event tagging yields digital ids.

    Prints the coverage number, because that number is the go/no-go on the whole
    linking premise.
    """
    from marvel.client import MarvelAPIError, MarvelClient, MarvelCredentialsMissing
    from marvel.records import parse_comics

    settings = get_settings()
    client = MarvelClient(settings.marvel_public_key, settings.marvel_private_key)
    if not client.configured:
        print(
            "MARVEL_PUBLIC_KEY / MARVEL_PRIVATE_KEY are not set.\n"
            "Get a free key at https://developer.marvel.com and put both in .env."
        )
        return 1
    try:
        response = await client.get("comics", limit=20, orderBy="-onsaleDate")
    except (MarvelAPIError, MarvelCredentialsMissing) as exc:
        print(f"Marvel API rejected the call: {exc}")
        return 1

    records = parse_comics(response.body)
    with_digital = [r for r in records if r.digital_id]
    print(f"OK — {response.total} comics visible; sampled {len(records)}.")
    print(f"digital ids present on {len(with_digital)}/{len(records)} sampled records.")
    print(f"attribution: {response.attribution_text}")
    if not with_digital:
        print(
            "\nWARNING: no digital ids in the sample. If that holds for a real event "
            "roster, the linking premise needs rethinking before more curation "
            "effort goes in (SPEC §0)."
        )
    return 0


async def _list_events(query: str) -> int:
    """Find an event's numeric Marvel id, to paste into the curation YAML."""
    from marvel.client import MarvelClient

    settings = get_settings()
    client = MarvelClient(settings.marvel_public_key, settings.marvel_private_key)
    response = await client.get("events", nameStartsWith=query, limit=20)
    for row in response.results:
        print(f"{row.get('id'):>8}  {row.get('title')}")
    return 0


async def _sync_event(slug: str) -> int:
    from marvel.client import MarvelClient
    from marvel.sync import sync_event

    settings = get_settings()
    client = MarvelClient(settings.marvel_public_key, settings.marvel_private_key)
    from db.session import SessionLocal

    async with SessionLocal() as session:
        report = await sync_event(session, client, slug)
    print(report.summary())
    return 0


async def _load_curation() -> int:
    """Load curation YAML, then apply any vendored catalog snapshots.

    Two steps in a fixed order, because they own different columns: the loader
    writes curation's (order, roles, edges) and the snapshot writes the
    API-derived ones (ids, covers, dates). Running curation first means a brand
    new issue exists by the time the snapshot looks for it.
    """
    from curation.loader import load_all
    from db.session import SessionLocal
    from marvel import snapshot as snapshots

    async with SessionLocal() as session:
        # Snapshots are the Gate B evidence: a curated digital_id must trace to
        # one, so the index is built before the load rather than after.
        index = snapshots.combined_record_index()
        report = await load_all(session, record_index=index or None)
        print(report.summary())

        for applied in await snapshots.apply_all(session):
            print(
                f"snapshot {applied.event_slug}: {applied.issues_matched} issues matched, "
                f"{applied.digital_ids_confirmed} digital ids, "
                f"{applied.newly_linkable} newly linkable"
            )
            if applied.issues_unmatched:
                print("  no snapshot record for: " + ", ".join(sorted(applied.issues_unmatched)))
    return 0


async def _seed(email: str, name: str, handle: str = "", is_admin: bool = False) -> int:
    """Create or update a reader. Idempotent. The allowlist is small by design.

    A reader signs in with `CROSSOVER_PASSWORD_{HANDLE}`, so the handle is not
    cosmetic — without a matching config var the reader exists but cannot log in,
    which this reports rather than leaving to be discovered at the login form.
    """
    from sqlalchemy import select

    from db.session import SessionLocal
    from models.user import User, valid_handle

    handle = (handle or email.split("@")[0]).lower()
    if not valid_handle(handle):
        print(
            f"invalid handle {handle!r}: must start with a letter and contain only "
            "lowercase letters, digits and underscores, so it can name an "
            "environment variable"
        )
        return 1

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email)
            session.add(user)
        user.handle = handle
        user.display_name = name or user.display_name or handle.title()
        user.is_admin = is_admin or user.is_admin
        await session.commit()
        await session.refresh(user)

    role = "admin" if user.is_admin else "reader"
    print(f"{role}: {user.display_name} <{user.email}> handle={user.handle}")
    if get_settings().reader_password(user.handle) is None:
        print(
            f"  ! CROSSOVER_PASSWORD_{user.handle.upper()} is not set — "
            "this reader cannot sign in until it is."
        )
    return 0


async def _bootstrap() -> int:
    """First-run setup for a fresh deploy. Idempotent.

    Run from `app.json`'s postdeploy hook, so a one-click deploy produces a
    *usable* app rather than one with an empty reader allowlist and a login page
    nobody can get past. Also surfaces the two config mistakes that are silent
    until they bite: a weak admin key, and a public URL that doesn't match where
    the app actually is (which breaks OAuth and the MCP transport, but only when
    a connector tries to attach).
    """
    settings = get_settings()
    problems: list[str] = []

    if "localhost" in settings.public_base_url or "127.0.0.1" in settings.public_base_url:
        problems.append(
            f"CROSSOVER_PUBLIC_URL is {settings.public_base_url!r}, which is not "
            "reachable from anywhere. Set it to this app's https URL or the Claude "
            "connector will not be able to attach."
        )
    elif not settings.public_base_url.startswith("https://"):
        problems.append(
            f"CROSSOVER_PUBLIC_URL is {settings.public_base_url!r} — OAuth requires https."
        )

    email = os.environ.get("CROSSOVER_OWNER_EMAIL", "").strip()
    handle = os.environ.get("CROSSOVER_OWNER_HANDLE", "").strip().lower()
    if email:
        await _seed(email, "", handle=handle, is_admin=True)
    else:
        problems.append(
            "CROSSOVER_OWNER_EMAIL is not set, so no reader exists and nobody can "
            "sign in. Create one with: heroku run python -m scripts.cli seed you@example.com"
        )

    if problems:
        print("\nSetup warnings:")
        for problem in problems:
            print(f"  ! {problem}")
        # Deliberately exit 0: these are misconfigurations to fix, not reasons to
        # fail the release and roll back a deploy that is otherwise fine.
    else:
        print("bootstrap complete — sign in at /ui/login")
    return 0


async def _revoke_sessions(handle: str) -> int:
    """Sign a reader out everywhere.

    The remedy the sessions table exists for: before it, invalidating a leaked
    cookie meant deleting the reader, which cascaded away their bookmarks.
    """
    from sqlalchemy import select

    from auth import revoke_all_for_user
    from db.session import SessionLocal
    from models.user import User

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.handle == handle))
        if user is None:
            print(f"no reader with handle {handle!r}")
            return 1
        count = await revoke_all_for_user(session, user.id)
    print(f"revoked {count} session(s) for {handle} — they will need to sign in again")
    return 0


async def _purge_sessions(keep_days: int) -> int:
    from auth import purge_expired_sessions
    from db.session import SessionLocal

    async with SessionLocal() as session:
        removed = await purge_expired_sessions(session, keep_days=keep_days)
    print(f"deleted {removed} session row(s) expired more than {keep_days} days ago")
    return 0


async def _enrich(limit: int | None) -> int:
    """Fill in thin issue rows that a bookmark already points at.

    Demand-driven: an issue earns a lookup by being on somebody's shelf. The
    mirror allows 60 requests a minute and each issue costs one, so `--limit`
    is a real limit, not a page size — run it again to continue.
    """
    from db.session import SessionLocal
    from marvel.mirror_cache import cached_mirror
    from service.enrich import DEFAULT_LIMIT, enrich_bookmarked_issues

    # Resolved here rather than as an argparse default: this module defers every
    # heavy import into the command bodies, and reading the constant at
    # parser-build time would pull SQLAlchemy in on `--help`.
    async with cached_mirror() as mirror, SessionLocal() as session:
        report = await enrich_bookmarked_issues(
            session, mirror, limit=limit if limit is not None else DEFAULT_LIMIT
        )
    print(report.summary())
    return 0


async def _purge_mirror_cache(days: int) -> int:
    """Drop cached mirror responses past their usefulness.

    Nothing depends on these rows existing — losing them costs request budget,
    not correctness — but the table has no natural ceiling, so something has to
    trim it.
    """
    from datetime import timedelta

    from db.session import SessionLocal
    from marvel.mirror_cache import purge

    async with SessionLocal() as session:
        removed = await purge(session, timedelta(days=days))
    print(f"dropped {removed} cached mirror response(s) older than {days} days")
    return 0


async def _register_connector(name: str, email: str, redirect_uri: str) -> int:
    """Register an OAuth client for a Claude custom connector.

    The secret is printed once and stored only as a SHA-256 hash.
    """
    from sqlalchemy import select

    from db.session import SessionLocal
    from models.oauth import OAuthClient
    from models.user import User
    from oauth_provider import hash_secret, new_client_id, new_client_secret

    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"no reader with email {email!r} — run `seed` first")
            return 1
        client_id = new_client_id()
        secret = new_client_secret()
        session.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=hash_secret(secret),
                name=name,
                user_id=user.id,
                redirect_uris=[redirect_uri],
            )
        )
        await session.commit()

    base = get_settings().public_base_url.rstrip("/")
    print("Connector registered. The secret is shown once:\n")
    print(f"  client_id:     {client_id}")
    print(f"  client_secret: {secret}")
    print(f"  authorize:     {base}/oauth/authorize")
    print(f"  token:         {base}/oauth/token")
    print(f"  mcp endpoint:  {base}/mcp")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crossover")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-api-key", help="verify Marvel credentials and digital-id coverage")

    p_events = sub.add_parser("list-events", help="find an event's numeric Marvel id")
    p_events.add_argument("query")

    p_sync = sub.add_parser("sync-event", help="fetch an event roster into the cache")
    p_sync.add_argument("slug")

    sub.add_parser("load-curation", help="load curation YAML into the database")
    sub.add_parser("bootstrap", help="first-run setup for a fresh deploy (idempotent)")

    p_revoke = sub.add_parser("revoke-sessions", help="sign a reader out on every device")
    p_revoke.add_argument("handle")

    p_purge = sub.add_parser("purge-sessions", help="delete long-expired session rows")
    p_purge.add_argument("--keep-days", type=int, default=90)

    p_seed = sub.add_parser("seed", help="create or update a reader")
    p_seed.add_argument("email")
    p_seed.add_argument("--name", default="")
    p_seed.add_argument(
        "--handle", default="", help="login handle; defaults to the email local part"
    )
    p_seed.add_argument(
        "--admin", action="store_true", help="grant the curation views and OAuth consent"
    )

    p_enrich = sub.add_parser(
        "enrich", help="fill in bookmarked issues missing cover art or a digital id"
    )
    p_enrich.add_argument(
        "--limit",
        type=int,
        default=None,
        help="issues to look up in one pass (default 25, one mirror request each)",
    )

    p_mirror = sub.add_parser("purge-mirror-cache", help="drop old cached mirror responses")
    p_mirror.add_argument("--older-than-days", type=int, default=90)

    p_conn = sub.add_parser("register-connector", help="register an OAuth client")
    p_conn.add_argument("name")
    p_conn.add_argument("email")
    p_conn.add_argument("redirect_uri")

    args = parser.parse_args(argv)
    match args.command:
        case "check-api-key":
            return asyncio.run(_check_api_key())
        case "list-events":
            return asyncio.run(_list_events(args.query))
        case "sync-event":
            return asyncio.run(_sync_event(args.slug))
        case "load-curation":
            return asyncio.run(_load_curation())
        case "bootstrap":
            return asyncio.run(_bootstrap())
        case "revoke-sessions":
            return asyncio.run(_revoke_sessions(args.handle))
        case "purge-sessions":
            return asyncio.run(_purge_sessions(args.keep_days))
        case "seed":
            return asyncio.run(_seed(args.email, args.name, args.handle, args.admin))
        case "enrich":
            return asyncio.run(_enrich(args.limit))
        case "purge-mirror-cache":
            return asyncio.run(_purge_mirror_cache(args.older_than_days))
        case "register-connector":
            return asyncio.run(_register_connector(args.name, args.email, args.redirect_uri))
    return 1


if __name__ == "__main__":
    sys.exit(main())
