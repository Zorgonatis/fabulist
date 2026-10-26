"""Tests for the revision (message-edit) router (SURFACE #6).

Exercises the real FastAPI app (httpx ASGI transport) with the revisions router
mounted, against a tmp SQLite event log. The log stays the source of truth
(D9/D10): an edit appends a ``revision`` row and never mutates the target, and
the projection's latest-wins resolver renders the new text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.revisions import router
from fabulist.log import EventLog


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps sharing one tmp event-log path; closes logs after."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed(build):
    """Append a user + narrative + asset + system row; return ids + log."""
    app = build()
    log = app.state.log
    user = log.append_event("s1", "user", "user", {"text": "orig", "kind": "in_character"})
    narrative = log.append_event("s1", "narrative", "narrator", {"prose": "old prose", "dsl": "x"})
    asset = log.append_event("s1", "asset", "user", {"asset_id": "a1", "kind": "image"})
    system = log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )
    return app, log, user, narrative, asset, system


def test_edit_user_message_latest_wins(build_app):
    build, _ = build_app
    app, log, user, *_ = _seed(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "edited"},
            )
            assert resp.status_code == 200
            body = resp.json()
            # The projection now renders the edited text (latest wins).
            events = (await client.get("/api/sessions/s1/events")).json()
            return body, events

    body, events = run(scenario())

    assert body["session_id"] == "s1"
    assert body["target_id"] == user.id
    assert body["revision_id"] != user.id
    assert [m["payload"]["text"] for m in events["user_messages"]] == ["edited"]
    # Non-text keys are preserved in the replacement payload.
    assert events["user_messages"][0]["payload"]["kind"] == "in_character"
    # The original row is untouched in the raw log (append-only, D10).
    original = next(e for e in log.get_events("s1") if e.id == user.id)
    assert original.payload["text"] == "orig"
    assert original.superseded_by == body["revision_id"]


def test_edit_narrative_shows_new_prose(build_app):
    build, _ = build_app
    app, log, _user, narrative, *_ = _seed(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "new prose"},
            )
            assert resp.status_code == 200
            events = (await client.get("/api/sessions/s1/events")).json()
            return resp.json(), events

    body, events = run(scenario())

    assert body["target_id"] == narrative.id
    assert [n["payload"]["prose"] for n in events["narratives"]] == ["new prose"]
    # Other payload keys (dsl) survive the full-replacement swap.
    assert events["narratives"][0]["payload"]["dsl"] == "x"
    original = next(e for e in log.get_events("s1") if e.id == narrative.id)
    assert original.payload["prose"] == "old prose"


def test_edit_legacy_narrative_invents_no_segments(build_app):
    """A narrative payload WITHOUT segments keeps the pre-fix replacement exactly.

    The dsl rewrite rule must not drag a `segments` key into a legacy row: the
    transcript then renders the flat prose/dsl stack, and inventing a segment
    list would change what an untouched key set renders.
    """
    build, _ = build_app
    app, log, _user, narrative, *_ = _seed(build)  # seeded payload has no segments
    assert "segments" not in narrative.payload

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "revised"},
            )
            assert resp.status_code == 200
            revision_id = resp.json()["revision_id"]
            events = (await client.get("/api/sessions/s1/events")).json()
            return revision_id, events

    revision_id, events = run(scenario())

    row = next(e for e in log.get_events("s1") if e.id == revision_id)
    assert row.payload["revised"] == {"dsl": "x", "prose": "revised"}
    assert "segments" not in events["narratives"][0]["payload"]


def test_edit_segmented_narrative_rewrites_segments(build_app):
    """The transcript renders `segments`, so a revision must rewrite them too.

    Rule: the FIRST prose segment carries the new text, later prose segments are
    dropped (the user edited the whole prose as one block), and dsl segments are
    preserved verbatim and in order.
    """
    build, _ = build_app
    app = build()
    log = app.state.log
    original_segments = [
        {"kind": "prose", "text": "first paragraph."},
        {"kind": "dsl", "text": "card = ObjectCard(\"a\")"},
        {"kind": "prose", "text": "middle paragraph."},
        {"kind": "dsl", "text": "env = EnvTracker(\"b\")"},
        {"kind": "prose", "text": "last paragraph."},
    ]
    narrative = log.append_event(
        "s1",
        "narrative",
        "narrator",
        {
            "prose": "first paragraph.\n\nmiddle paragraph.\n\nlast paragraph.",
            "dsl": 'card = ObjectCard("a")\nenv = EnvTracker("b")',
            "segments": original_segments,
        },
    )

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "the whole prose, edited"},
            )
            assert resp.status_code == 200
            return resp.json(), (await client.get("/api/sessions/s1/events")).json()

    body, events = run(scenario())

    assert body["target_id"] == narrative.id
    payload = events["narratives"][0]["payload"]
    # The projection renders this list (hydrate -> coerceSegments): first prose
    # segment = the edit, every later prose segment gone, dsl intact and ordered.
    assert payload["segments"] == [
        {"kind": "prose", "text": "the whole prose, edited"},
        {"kind": "dsl", "text": 'card = ObjectCard("a")'},
        {"kind": "dsl", "text": 'env = EnvTracker("b")'},
    ]
    assert payload["prose"] == "the whole prose, edited"
    # The joined dsl string mirrors the preserved dsl segments: untouched.
    assert payload["dsl"] == 'card = ObjectCard("a")\nenv = EnvTracker("b")'
    # Append-only: the original row keeps its own segments (D10).
    current = next(e for e in log.get_events("s1") if e.id == narrative.id)
    assert current.payload["segments"] == original_segments


def test_edit_dsl_only_narrative_prepends_prose(build_app):
    """A segmented narrative with NO prose segment gets one prepended."""
    build, _ = build_app
    app = build()
    log = app.state.log
    narrative = log.append_event(
        "s1",
        "narrative",
        "narrator",
        {
            "prose": "",
            "dsl": "card = ObjectCard(\"a\")",
            "segments": [
                {"kind": "dsl", "text": 'card = ObjectCard("a")'},
                {"kind": "dsl", "text": 'env = EnvTracker("b")'},
            ],
        },
    )

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "a line of prose"},
            )
            assert resp.status_code == 200
            return (await client.get("/api/sessions/s1/events")).json()

    events = run(scenario())

    payload = events["narratives"][0]["payload"]
    assert payload["segments"] == [
        {"kind": "prose", "text": "a line of prose"},
        {"kind": "dsl", "text": 'card = ObjectCard("a")'},
        {"kind": "dsl", "text": 'env = EnvTracker("b")'},
    ]
    assert payload["prose"] == "a line of prose"
    assert payload["dsl"] == 'card = ObjectCard("a")'


def test_edit_user_message_has_no_segment_rewrite(build_app):
    """The segment rewrite is narrative-only: a user row swaps just its text."""
    build, _ = build_app
    app, log, user, *_ = _seed(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "edited user line"},
            )
            assert resp.status_code == 200
            return resp.json(), (await client.get("/api/sessions/s1/events")).json()

    body, events = run(scenario())

    row = next(e for e in log.get_events("s1") if e.id == body["revision_id"])
    assert row.payload["revised"] == {"kind": "in_character", "text": "edited user line"}
    assert events["user_messages"][0]["payload"]["text"] == "edited user line"
    for narrative in events["narratives"]:
        assert "segments" not in narrative["payload"]


@pytest.mark.parametrize("target", ["asset", "system"])
def test_editing_non_text_event_is_422(build_app, target):
    build, _ = build_app
    app, log, user, narrative, asset, system = _seed(build)
    target_id = {"asset": asset.id, "system": system.id}[target]

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": target_id, "new_text": "nope"},
            )

    resp = run(scenario())
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["type"] == target
    assert "not editable" in detail["reason"]
    # No revision row was appended.
    assert [e for e in log.get_events("s1") if e.type == "revision"] == []


def test_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/nope/revisions",
                json={"target_id": 1, "new_text": "x"},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


def test_unknown_target_id_is_404(build_app):
    build, _ = build_app
    app, log, *_ = _seed(build)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": 9999, "new_text": "x"},
            )

    resp = run(scenario())
    assert resp.status_code == 404
    assert "9999" in resp.json()["detail"]
    assert [e for e in log.get_events("s1") if e.type == "revision"] == []


def test_reason_default_and_recorded(build_app):
    build, _ = build_app
    app, log, user, narrative, *_ = _seed(build)

    async def scenario():
        async with client_for(app) as client:
            default = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "a"},
            )
            explicit = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": narrative.id, "new_text": "b", "reason": "typo fix"},
            )
            listing = (await client.get("/api/sessions/s1/revisions")).json()
            return default.json(), explicit.json(), listing

    default, explicit, listing = run(scenario())

    # The reason lands in the revision payload in the raw log.
    rows = {e.id: e for e in log.get_events("s1") if e.type == "revision"}
    assert rows[default["revision_id"]].payload["reason"] == "user edit"
    assert rows[explicit["revision_id"]].payload["reason"] == "typo fix"

    # List endpoint shape: id / supersedes / reason / ts.
    revisions = listing["revisions"]
    assert [r["supersedes"] for r in revisions] == [user.id, narrative.id]
    assert [r["reason"] for r in revisions] == ["user edit", "typo fix"]
    assert [r["id"] for r in revisions] == [
        default["revision_id"],
        explicit["revision_id"],
    ]
    for row in revisions:
        assert set(row) == {"id", "supersedes", "reason", "ts"}
        assert isinstance(row["ts"], str) and row["ts"]


def test_list_revisions_empty_and_unknown_session(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            unknown = await client.get("/api/sessions/nope/revisions")
            return unknown

    resp = run(scenario())
    assert resp.status_code == 404

    # A known-but-revision-less session still returns a well-formed empty list.
    app2 = build()
    app2.state.log.append_event("s2", "user", "user", {"text": "hi"})

    async def empty():
        async with client_for(app2) as client:
            return (await client.get("/api/sessions/s2/revisions")).json()

    body = run(empty())
    assert body == {"session_id": "s2", "revisions": []}


# -- fork views (issue #90) --------------------------------------------------
#
# A branch renders its ancestors' pre-cut events (``get_projection`` walks the
# chain), so an inherited message carries an edit control. The target is
# resolved over the CHAIN, and the revision is appended to the FORK — the edit
# is branch-local: the parent keeps its own text in its own projection.


def _forked(build):
    """Seed ``s1`` (user/narrative/asset/system) and cut ``s1b`` above them.

    Returns ``(app, log, user, narrative, cut)`` where ``cut`` is the fork's
    ``base_seq`` — the narrative seq, so the user row is INHERITED by ``s1b``.
    """
    app, log, user, narrative, _asset, _system = _seed(build)
    cut = narrative.seq
    log.fork_session("s1", at_seq=cut, new_session_id="s1b")
    return app, log, user, narrative, cut


def test_root_view_edit_still_resolves_its_own_event(build_app):
    """Regression guard for the chain read: a root edit is unchanged.

    The chain walk must not change the root case, and it must not leak a
    CHILD's rows upward: an event that exists only in a fork is still a 404
    from the root view.
    """
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)
    branch_only = log.append_event("s1b", "user", "user", {"text": "branch only"})

    async def scenario():
        async with client_for(app) as client:
            root_edit = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "edited at the root"},
            )
            # An event id that lives only in the fork is not part of the
            # root's chain.
            leaked = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": branch_only.id, "new_text": "nope"},
            )
            return root_edit, leaked

    root_edit, leaked = run(scenario())

    assert root_edit.status_code == 200
    assert root_edit.json()["session_id"] == "s1"
    # The revision landed in the ROOT, not in the fork.
    assert [e.session_id for e in log.get_events("s1") if e.type == "revision"] == [
        "s1"
    ]
    assert [e for e in log.get_events("s1b") if e.type == "revision"] == []
    # Chain direction: a child's row is invisible from the parent.
    assert leaked.status_code == 404
    assert str(branch_only.id) in leaked.json()["detail"]


def test_fork_view_edit_of_inherited_event_appends_to_the_fork(build_app):
    """The repro: a fork view edits a PARENT-owned pre-cut event.

    The revision lands in the FORK with ``supersedes`` = the parent event id;
    the parent's own row keeps its payload (append-only, D10).
    """
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": user.id, "new_text": "alt line"},
            )
            return resp, (await client.get("/api/sessions/s1b/events")).json()

    resp, fork_events = run(scenario())

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "s1b"
    assert body["target_id"] == user.id

    # The revision row lives in the FORK and supersedes the parent's event.
    fork_rows = [e for e in log.get_events("s1b") if e.type == "revision"]
    assert [(e.id, e.session_id, e.supersedes) for e in fork_rows] == [
        (body["revision_id"], "s1b", user.id)
    ]
    # Nothing was appended to the parent, and its payload is untouched.
    assert [e for e in log.get_events("s1") if e.type == "revision"] == []
    parent_row = next(e for e in log.get_events("s1") if e.id == user.id)
    assert parent_row.payload["text"] == "orig"
    # The fork's projection renders the edit (latest-wins folds the branch row
    # onto the inherited event).
    assert [m["payload"]["text"] for m in fork_events["user_messages"]] == ["alt line"]
    assert fork_events["user_messages"][0]["payload"]["kind"] == "in_character"


def test_fork_edit_leaves_the_parent_projection_unchanged(build_app):
    """Branch-local semantics: the parent view keeps the ORIGINAL text.

    ``append_revision`` points the parent row's ``superseded_by`` at the fork's
    revision, but ``_resolve_payloads`` only folds a revision that is IN the
    streamed list — and the fork's row is not in the parent's stream.
    """
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": user.id, "new_text": "alt line"},
            )
            assert resp.status_code == 200
            fork = (await client.get("/api/sessions/s1b/events")).json()
            parent = (await client.get("/api/sessions/s1/events")).json()
            return resp.json(), fork, parent

    body, fork, parent = run(scenario())

    assert [m["payload"]["text"] for m in fork["user_messages"]] == ["alt line"]
    assert [m["payload"]["text"] for m in parent["user_messages"]] == ["orig"]
    # The parent row DID get its superseded_by pointer (the append's own
    # bookkeeping) — the projection ignores it because the revision row is not
    # in the parent's stream.
    row = next(e for e in log.get_events("s1") if e.id == user.id)
    assert row.superseded_by == body["revision_id"]
    assert row.payload["text"] == "orig"


def test_fork_edit_feeds_the_branch_last_user_turn(build_app):
    """``{{last_user_turn}}`` reads the projection, so a branch prompt gets the edit.

    The variable is resolved from ``projection["user_messages"]``
    (:func:`fabulist.prompts.variables._last_user_before`), which is where the
    revision has to land for the narrator's prompt to see it.
    """
    from fabulist.prompts.variables import resolve_variables

    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": user.id, "new_text": "the branch's own line"},
            )
            assert resp.status_code == 200

    run(scenario())

    projection = log.get_projection("s1b")
    assert [m["payload"]["text"] for m in projection["user_messages"]] == [
        "the branch's own line"
    ]
    # The prompt variable the narrator reads follows the projection.
    assert resolve_variables(log, "s1b", "a new branch turn.")["last_user_turn"] == (
        "the branch's own line"
    )
    # The parent's prompt variable is untouched.
    assert resolve_variables(log, "s1", "a new root turn.")["last_user_turn"] == "orig"


def test_list_revisions_on_a_fork_includes_inherited_rows(build_app):
    """The edited marker on an inherited message: the fork lists the parent's row."""
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)

    async def scenario():
        async with client_for(app) as client:
            root = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "root edit"},
            )
            assert root.status_code == 200
            branch = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": narrative.id, "new_text": "branch prose"},
            )
            assert branch.status_code == 200
            return (
                root.json(),
                branch.json(),
                (await client.get("/api/sessions/s1b/revisions")).json(),
                (await client.get("/api/sessions/s1/revisions")).json(),
            )

    root_body, branch_body, fork_listing, root_listing = run(scenario())

    # The fork sees BOTH: the parent's revision for the inherited user row and
    # its own revision for the inherited narrative row. (The chain view is
    # seq-ordered, and a branch's own seqs restart above its cut, so the two
    # sessions' rows do not share one append clock — assert membership, not an
    # arbitrary cross-session order.)
    by_id = {r["id"]: r for r in fork_listing["revisions"]}
    assert set(by_id) == {root_body["revision_id"], branch_body["revision_id"]}
    assert by_id[root_body["revision_id"]]["supersedes"] == user.id
    assert by_id[branch_body["revision_id"]]["supersedes"] == narrative.id
    for row in fork_listing["revisions"]:
        assert set(row) == {"id", "supersedes", "reason", "ts"}
        assert isinstance(row["ts"], str) and row["ts"]

    # The parent still lists only its own.
    assert [r["id"] for r in root_listing["revisions"]] == [root_body["revision_id"]]


def test_fork_edit_of_an_id_outside_the_chain_is_404(build_app):
    """A target id that exists, but not in this fork's chain, is still a 404."""
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)
    # A sibling session's row: a real event id, outside s1b's chain.
    other = log.append_event(
        "sibling", "user", "user", {"text": "not in this chain"}
    )
    # A row of the parent ABOVE the cut is not inherited either.
    tail = log.append_event("s1", "user", "user", {"text": "above the cut"})

    async def scenario():
        async with client_for(app) as client:
            outside = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": other.id, "new_text": "x"},
            )
            above_cut = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": tail.id, "new_text": "x"},
            )
            return outside, above_cut

    outside, above_cut = run(scenario())

    assert outside.status_code == 404
    assert str(other.id) in outside.json()["detail"]
    # The chain stops at the cut: a parent row above it is not editable from
    # the branch view (it is not rendered there).
    assert above_cut.status_code == 404
    assert [e for e in log.get_events("s1b") if e.type == "revision"] == []


def test_fork_edit_of_its_own_event_still_works(build_app):
    """The chain contains the fork's OWN rows too: a branch-local edit works."""
    build, _ = build_app
    app, log, user, narrative, cut = _forked(build)
    own = log.append_event("s1b", "user", "user", {"text": "branch turn"})

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/sessions/s1b/revisions",
                json={"target_id": own.id, "new_text": "branch turn, edited"},
            )
            fork = (await client.get("/api/sessions/s1b/events")).json()
            return resp, fork

    resp, fork = run(scenario())

    assert resp.status_code == 200
    assert resp.json()["target_id"] == own.id
    texts = [m["payload"]["text"] for m in fork["user_messages"]]
    assert texts == ["orig", "branch turn, edited"]
    # Still branch-local: the root view never saw the branch's own turn.
    assert [m["payload"]["text"] for m in log.get_projection("s1")["user_messages"]] == [
        "orig"
    ]
