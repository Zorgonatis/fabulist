"""Compiler-path session-module splice (D29, orchestrator integration).

The preset path splices via ``splice_session_modules`` on the flow (Wave B,
pinned in test_session_modules.py). This pins the OTHER half added at
integration: ``_splice_compiled_messages`` — the byte-faithful compiler path's
insertion of session-carried modules, found necessary because a bare engine
(feel-test harness, scratch logs) carries no persisted default preset, and card
homes must never silently drop there. Pure-function tests only; the live-model
proof is the orchestrator's ``/tmp/probe_homes.py`` probe (KEEPER-STYLE at the
top anchor, PROBE-PHI at post_history, capture-verified on :4444).
"""

from __future__ import annotations

from fabulist.wiring import _splice_compiled_messages

BASE = [
    {"role": "system", "content": "narrator prefix"},
    {"role": "user", "content": "seed history row"},
    {"role": "user", "content": "the actual turn"},
]

ROWS = [
    {
        "event": "session_module",
        "module_id": "m1",
        "anchor": "top",
        "content": "TOP-MODULE",
        "enabled": True,
    },
    {
        "event": "session_module",
        "module_id": "m2",
        "anchor": "post_history",
        "content": "POST-MODULE",
        "enabled": True,
    },
]


def test_top_lands_after_system_prefix_post_before_final_user():
    out = _splice_compiled_messages(BASE, ROWS)
    roles = [(m["role"], m["content"]) for m in out]
    assert roles == [
        ("system", "narrator prefix"),
        ("system", "TOP-MODULE"),
        ("user", "seed history row"),
        # Issue #68: a post_history row rides the ASSISTANT carrier — a system
        # row after history is a hard 500 on a stock Jinja chat template.
        ("assistant", "POST-MODULE"),
        ("user", "the actual turn"),
    ]


def test_disabled_rows_are_skipped():
    rows = [dict(ROWS[0], enabled=False), ROWS[1]]
    out = _splice_compiled_messages(BASE, rows)
    assert [c for _, c in [(m["role"], m["content"]) for m in out]] == [
        "narrator prefix",
        "seed history row",
        "POST-MODULE",
        "the actual turn",
    ]


def test_empty_rows_return_same_messages_unmutated():
    out = _splice_compiled_messages(BASE, [])
    assert out is BASE


def test_input_never_mutated():
    snapshot = [dict(m) for m in BASE]
    _splice_compiled_messages(BASE, ROWS)
    assert BASE == snapshot


def test_no_user_message_appends_posts_last():
    base = [{"role": "system", "content": "only system"}]
    out = _splice_compiled_messages(base, [ROWS[1]])
    assert [m["content"] for m in out] == ["only system", "POST-MODULE"]
