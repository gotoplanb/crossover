"""The MCP surface: the six tools, and the OAuth gate in front of them.

The tools open their own DB sessions (they run outside FastAPI's dependency
system), so these tests rebind `SessionLocal` to the test transaction rather
than going through the app's overrides.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import mcp_server
from marvel.links import NOT_ON_MU, assert_tappable

#: SPEC §6: "Keep it small. Six tools."
EXPECTED_TOOLS = {
    "list_events",
    "get_event_guide",
    "whats_next",
    "bookmark_issue",
    "sequence_bookmarks",
    "add_to_shelf",
}


@pytest.fixture
def as_user(monkeypatch, db_conn, user):
    """Run tool bodies as `user`, against the test transaction."""
    sessionmaker = async_sessionmaker(
        bind=db_conn,
        expire_on_commit=False,
        class_=AsyncSession,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(mcp_server, "SessionLocal", sessionmaker)
    token = mcp_server._principal.set({"user_id": user.id, "email": user.email})
    yield user
    mcp_server._principal.reset(token)


async def test_the_tool_surface_is_exactly_six_tools() -> None:
    """Kept small on purpose. A seventh tool should be a deliberate decision,
    not something that accumulated."""
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_every_tool_documents_itself_for_a_reader_not_a_developer() -> None:
    """The descriptions are what Claude reads to decide when to call something
    mid-chapter, so they have to describe the moment, not the endpoint."""
    for tool in await mcp_server.mcp.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 80, f"{tool.name}'s description is too thin"


async def test_a_tool_called_without_a_principal_refuses() -> None:
    """Reading lists are per-person; there is no anonymous mode."""
    with pytest.raises(ToolError, match="not authenticated"):
        mcp_server._user_id()


async def test_list_events(as_user, loaded_event) -> None:
    result = await mcp_server.list_events()
    assert result["events"][0]["slug"] == "king-in-black"
    assert "best-effort" in result["coverage_note"]


async def test_get_event_guide(as_user, loaded_event) -> None:
    result = await mcp_server.get_event_guide("king-in-black")
    assert len(result["reading_order"]) == 40


async def test_get_event_guide_points_at_list_events_when_wrong(as_user) -> None:
    """Must be a ToolError, not a bare ValueError: mcp strips the message off
    anything else, and "Error executing tool get_event_guide" tells the caller
    nothing it can act on."""
    with pytest.raises(ToolError, match="list_events"):
        await mcp_server.get_event_guide("secret-wars")


async def test_whats_next_takes_a_spoken_reference(as_user, loaded_event) -> None:
    result = await mcp_server.whats_next("king in black 3")
    assert result["next_core_issue"]["issue"] == "King in Black #4"
    assert result["expands_on_what_you_just_read"]


async def test_an_ambiguous_reference_hands_the_question_back(as_user, loaded_event) -> None:
    """Ambiguity is a normal outcome of loose spoken input, not an error.

    It comes back as a *successful* payload carrying the options as data, so
    the question can be asked in one sentence. Raising instead would surface as
    "Error executing tool whats_next" with the options stripped — which is how
    this was originally written, and it made the ask impossible.
    """
    result = await mcp_server.whats_next("the Namor one")
    assert "need_to_ask" in result
    refs = {o["issue_ref"] for o in result["options"]}
    assert refs == {"King in Black: Namor #1", "King in Black: Namor #2",
                    "King in Black: Namor #3", "King in Black: Namor #4",
                    "King in Black: Namor #5"}
    # The options must be usable verbatim as the next issue_ref.
    followup = await mcp_server.whats_next(result["options"][0]["issue_ref"])
    assert "need_to_ask" not in followup


async def test_bookmark_asks_rather_than_saving_the_wrong_book(as_user, loaded_event) -> None:
    result = await mcp_server.bookmark_issue("the Namor one")
    assert "need_to_ask" in result
    assert "bookmark_id" not in result
    assert await mcp_server.sequence_bookmarks() == {
        **await mcp_server.sequence_bookmarks(),
        "count": 0,
    }


async def test_an_unknown_reference_explains_the_coverage_model(as_user, loaded_event) -> None:
    with pytest.raises(ToolError, match="best-effort by design"):
        await mcp_server.whats_next("Fantastic Four #52")


async def test_bookmark_issue_is_a_one_sentence_call(as_user, loaded_event) -> None:
    result = await mcp_server.bookmark_issue("the marauders one")
    assert result["saved"] == "King in Black: Marauders #1"
    assert result["bookmark_id"]
    assert result["note"] == "On your rack. Keep reading."


async def test_bookmark_issue_attaches_provenance_automatically(as_user, loaded_event) -> None:
    result = await mcp_server.bookmark_issue("planet of the symbiotes 1")
    assert result["why_you_saved_it"] == "Expanded in King in Black #3"


async def test_bookmark_then_sequence(as_user, loaded_event) -> None:
    await mcp_server.bookmark_issue("Web of Venom: Empyre's End #1")
    await mcp_server.bookmark_issue("venom 34")
    result = await mcp_server.sequence_bookmarks()
    assert result["count"] == 2
    assert [g["group"] for g in result["groups"]] == [
        "Before it starts", "After the dust settles",
    ]


async def test_sequence_bookmarks_accepts_chronological(as_user, loaded_event) -> None:
    await mcp_server.bookmark_issue("venom 34")
    result = await mcp_server.sequence_bookmarks(ordering="chronological")
    assert result["ordering"] == "chronological"


async def test_add_to_shelf_phase_one_then_two(as_user, loaded_event) -> None:
    proposed = await mcp_server.add_to_shelf(["King in Black: Namor #1"], source="typed")
    assert proposed["phase"] == "propose"
    entry = proposed["results"][0]
    assert "Nothing is confirmed yet" in proposed["next_step"]

    committed = await mcp_server.add_to_shelf(
        [], candidate_id=entry["candidate_id"], confirm_key="king-in-black-namor-1",
    )
    assert committed["phase"] == "confirm"
    assert committed["saved"] == "King in Black: Namor #1"


async def test_add_to_shelf_rejects_a_bad_source(as_user, loaded_event) -> None:
    with pytest.raises(ToolError, match='"photo" or "typed"'):
        await mcp_server.add_to_shelf(["x"], source="scanned")


async def test_confirming_without_the_candidate_id_is_refused(as_user, loaded_event) -> None:
    """The candidate_id is what ties a choice back to the options actually
    offered — Gate B at the commit boundary."""
    with pytest.raises(ToolError, match="candidate_id"):
        await mcp_server.add_to_shelf([], confirm_key="king-in-black-1")


async def test_confirming_an_unoffered_key_reaches_the_caller(as_user, loaded_event) -> None:
    """Gate B's refusal is only useful if the caller is told what happened."""
    proposed = await mcp_server.add_to_shelf(["King in Black: Namor #1"], source="typed")
    with pytest.raises(ToolError, match="not one of the options offered"):
        await mcp_server.add_to_shelf(
            [],
            candidate_id=proposed["results"][0]["candidate_id"],
            confirm_key="king-in-black-5",
        )


async def test_every_link_a_tool_emits_is_tappable_markdown(as_user, loaded_event) -> None:
    """Gate A over real tool output, not hand-picked strings."""
    await mcp_server.bookmark_issue("King in Black: Namor #1")
    payloads = [
        await mcp_server.get_event_guide("king-in-black"),
        await mcp_server.whats_next("King in Black #3"),
        await mcp_server.sequence_bookmarks(),
    ]

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "link" and isinstance(value, str):
                    assert_tappable(value)
                    assert value == NOT_ON_MU or value.startswith("[")
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for payload in payloads:
        walk(payload)


# --- the OAuth gate in front of the transport ---


async def test_an_unauthenticated_mcp_call_gets_401_and_a_discovery_pointer(client) -> None:
    """The WWW-Authenticate challenge is how Claude's connector finds the
    authorization server to start the OAuth dance."""
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer resource_metadata=")
    assert "/.well-known/oauth-protected-resource" in challenge


async def test_a_bogus_bearer_token_is_also_401(client) -> None:
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer xo_at_not_a_real_token"},  # pragma: allowlist secret
    )
    assert response.status_code == 401
