"""D43 fixed-vocabulary macros render in card / cast / World Info body text.

Bug (tracked as the RESUME open item "``{{user}}`` not
substituted in card/cast body text"): a world card's description contained
``{{user}}`` under SillyTavern conventions. The macro reached the narrator
literally because ``render_template`` never ran over the assembled card blocks,
so the model read the literal token and addressed the user as the card's
in-fiction nickname instead of the persona name.

Fix: thread the fixed-vocabulary ``variables`` map through the card/WI render
seams (:func:`session_persona_block`, :func:`roster.persona_block`,
:func:`session_world_info_block`, :func:`static_prefix_parts`). ``None``/``{}``
must be byte-identical to the pre-fix render; unknown tokens stay literal.
"""

from __future__ import annotations

from fabulist.context.facts import (
    assemble_turn_context,
    context_suffix_parts,
    session_persona_block,
    session_user_identity_block,
    session_world_info_block,
    static_prefix_parts,
)
from fabulist.context.roster import persona_block
from fabulist.log import EventLog
from fabulist.prompts.variables import PERSONAS_ANCHOR, resolve_variables

#: The card body from the live bug: the in-fiction nickname is INTENTIONAL (the
#: card's comedy) and must survive verbatim; only the ``{{user}}`` binding was
#: missing.
CARD = {
    "name": "Rex",
    "description": (
        '{{user}} is a new recruit on the ship - "Assistant (to the) Captain" '
        "- a role {{char}} insisted was necessary."
    ),
    "scenario": "Aboard the {{world}}.",
    "first_mes": "Welcome aboard, {{user}}.",
}

VARS = {
    "user": "Alice",
    "persona": "Alice",
    "char": "[cast: Rex]",
    "world": "Epoch 27",
}

WORLD_NAME = "Epoch 27"


# --- pure roster renderer ---------------------------------------------------


def test_persona_block_renders_fixed_vocabulary():
    block = persona_block([CARD], [CARD["first_mes"]], variables=VARS)
    assert block is not None
    assert block.startswith("### Character: Rex\n")
    assert 'Alice is a new recruit on the ship - "Assistant (to the) Captain"' in block
    assert "[cast: Rex] insisted was necessary." in block
    assert "Scenario: Aboard the Epoch 27." in block
    assert block.endswith(
        "Opening scene (already established): Welcome aboard, Alice."
    )
    assert "{{" not in block


def test_persona_block_unknown_token_stays_literal():
    card = {"name": "Lyra", "description": "{{nope}} greets {{user}}."}
    block = persona_block([card], [""], variables=VARS)
    # Registered vocabulary resolves; an unregistered token is left verbatim.
    assert block == "### Character: Lyra\nDescription: {{nope}} greets Alice."


def test_persona_block_none_and_empty_variables_are_byte_identical():
    greeting = "Hi {{user}}."
    plain = persona_block([CARD], [greeting])
    assert plain == persona_block([CARD], [greeting], variables=None)
    assert plain == persona_block([CARD], [greeting], variables={})
    # No variables -> every token survives verbatim (the pre-fix bytes).
    assert "{{user}} is a new recruit" in (plain or "")
    assert "{{char}} insisted" in (plain or "")
    assert "Aboard the {{world}}." in (plain or "")
    assert "Opening scene (already established): Hi {{user}}." in (plain or "")


def test_cast_block_renders_macros_per_card():
    a = {"name": "Rex", "description": "Captain of the {{world}}."}
    b = {"name": "Nova", "description": "{{user}}'s rival."}
    block = persona_block([a, b], ["{{user}}, welcome.", ""], variables=VARS)
    assert block is not None
    assert "#### Rex" in block
    assert "Description: Captain of the Epoch 27." in block
    assert "#### Nova" in block
    assert "Description: Alice's rival." in block
    # Only the first card's opening scene renders, after its fields.
    assert block.count("Opening scene (already established):") == 1
    assert "Opening scene (already established): Alice, welcome." in block


# --- real-log seams ---------------------------------------------------------


def _seed(log: EventLog, sid: str = "s") -> None:
    log.append_event(
        sid, "system", "system", {"card": CARD, "first_mes": CARD["first_mes"]}
    )
    log.append_event(
        PERSONAS_ANCHOR,
        "system",
        "user",
        {"event": "persona", "persona_id": "p1", "name": "Alice"},
    )
    log.append_event(sid, "system", "user", {"event": "chat_persona", "persona_id": "p1"})
    log.append_event(
        sid, "system", "system", {"event": "world_ref", "world_id": "w1", "name": WORLD_NAME}
    )


def test_session_persona_block_renders_macros_with_variables(log: EventLog):
    sid = "s"
    _seed(log, sid)
    variables = resolve_variables(log, sid, "I report to the bridge.")
    assert variables["user"] == "Alice"
    assert variables["world"] == WORLD_NAME
    block = session_persona_block(log, sid, variables=variables)
    assert block is not None
    assert "Alice is a new recruit" in block
    assert '"Assistant (to the) Captain"' in block
    assert "Aboard the Epoch 27." in block
    # The card's in-fiction nickname is preserved; only the macro bound.
    assert "{{user}}" not in block
    assert "{{" not in block


def test_session_persona_block_without_variables_is_byte_identical(log: EventLog):
    sid = "s"
    _seed(log, sid)
    assert session_persona_block(log, sid) == session_persona_block(
        log, sid, variables={}
    )
    # Default callers keep the literal macro (unchanged bytes).
    assert "{{user}}" in (session_persona_block(log, sid) or "")


def test_static_prefix_parts_threads_variables(log: EventLog):
    sid = "s"
    _seed(log, sid)
    variables = resolve_variables(log, sid, "hi")
    rendered = static_prefix_parts(log, sid, variables=variables)
    raw = static_prefix_parts(log, sid)
    assert rendered and raw
    assert "Alice is a new recruit" in rendered[0]
    assert "{{user}}" in raw[0]
    # D54: the played-user identity block now rides IMMEDIATELY after the
    # character block in the static prefix (persona "p1" = Alice, no
    # description in this seed -> only the Name line).
    assert rendered == [
        session_persona_block(log, sid, variables=variables),
        session_user_identity_block(log, sid, variables=variables),
    ]
    assert rendered[1] == "### User (the player)\nName: Alice"


def test_session_world_info_block_renders_entry_macros(log: EventLog):
    sid = "s"
    book = {
        "name": "Ship lore",
        "entries": [
            {
                "keys": ["ship"],
                "content": "{{user}} serves aboard the {{world}}.",
                "insertion_order": 1,
            }
        ],
    }
    log.append_event(sid, "system", "system", {"event": "world_info", "book": book})
    scan = "I look at the ship."
    block = session_world_info_block(
        log, sid, scan, variables={"user": "Alice", "world": WORLD_NAME}
    )
    assert block is not None
    assert "Alice serves aboard the Epoch 27." in block
    # Byte-identity check: no variables -> literal macro survives.
    raw = session_world_info_block(log, sid, scan)
    assert "{{user}} serves aboard the {{world}}." in (raw or "")


def test_assemble_turn_context_threads_variables_into_context_hash(log: EventLog):
    sid = "s"
    _seed(log, sid)
    variables = resolve_variables(log, sid, "hi")
    rendered = assemble_turn_context(log, sid, user_text="hi", variables=variables)
    raw = assemble_turn_context(log, sid, user_text="hi")
    assert "Alice is a new recruit" in "\n\n".join(rendered.context_parts)
    assert "{{user}} is a new recruit" in "\n\n".join(raw.context_parts)
    assert rendered.static_parts == static_prefix_parts(
        log, sid, variables=variables
    )
    # context_suffix_parts (the hash anchor) honors the same threading.
    suffix = context_suffix_parts(log, sid, user_text="hi", variables=variables)
    assert "Alice is a new recruit" in "\n\n".join(suffix)


def test_fork_inherits_card_and_renders_macros(log: EventLog):
    parent, branch = "parent", "branch"
    _seed(log, parent)
    log.append_event(parent, "user", "user", {"text": "I report."})
    fork_seq = log.get_events(parent)[-1].seq
    log.fork_session(parent, at_seq=fork_seq, new_session_id=branch)

    variables = resolve_variables(log, branch, "I report again.")
    assert variables["user"] == "Alice"
    block = session_persona_block(log, branch, variables=variables)
    assert block is not None
    assert "Alice is a new recruit" in block
    assert "[cast: Rex] insisted was necessary." in block


def test_no_persona_renders_literal_user_not_empty_substitution(log: EventLog):
    """PART 2: a chat with NO persona renders ``{{user}}`` as the word "user".

    Owner: "{{user}} should simply become user." Before, the empty-resolution
    guard kept the raw token (an empty substitution garbled the sentence,
    "…paycheck.  is a new recruit…"); now the resolver supplies "user" and the
    card/cast text renders it, while genuinely absent fields still stay literal.
    """
    sid = "nopersona"
    log.append_event(
        sid, "system", "system", {"card": CARD, "first_mes": CARD["first_mes"]}
    )
    log.append_event(
        sid, "system", "system", {"event": "world_ref", "world_id": "w1", "name": WORLD_NAME}
    )
    variables = resolve_variables(log, sid, "hello")
    assert variables["user"] == "user"
    block = session_persona_block(log, sid, variables=variables)
    assert block is not None
    assert "user is a new recruit" in block  # the literal word, not the token
    assert "{{user}}" not in block
    assert "  is a new recruit" not in block  # no empty substitution
    # A present variable still resolves in the same block (world name).
    assert "Aboard the Epoch 27." in block
