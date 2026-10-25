"""W2.1 directive compiler — the serial-spine head of the preset port.

``compile(directive_config, toggles, facts, history, user_content)`` turns the
distilled directive catalog (:mod:`fabulist.prompts.parts`) plus per-turn
context into an OpenAI ``messages`` list. Design constraints (PLAN W2.1):

1. **Cache-stable layout.** The system message (framing + enabled directives) is
   byte-identical across consecutive turns for a fixed directive / toggle set.
   Directives never change per turn; history follows the system message; a
   per-turn *suffix* carries facts / plan / active-panels / final user content.
   Facts ride *inside* the final user message (provider chat templates reject
   mid-conversation system messages), matching ``narrator.assemble_messages``.
2. **Token budget manager.** ``budget`` (default :data:`DEFAULT_BUDGET`) caps the
   rendered system block. When over budget, the lowest-``priority`` trimmable
   directives drop first; the five core directives and the panel-placement rules
   are never trimmed (``Directive.trimmable``). Counting is a char/4 heuristic
   with a ``tokenizer`` seam for a real tokenizer later.
3. **DSL-stripped history projection.** :func:`stripped_history` removes ``dsl``
   segments from prior turns (port of the preset's "Strip VTK from Sent
   Context"): the narrator sees prose history, not its own stale panels. A
   keep-last window (``history.dsl_keep_last``, default 3) unstrips the DSL of
   the most recent narrative turns so the narrator can continue a live panel.
4. **Runtime toggles.** ``toggles`` is ``{directive_id: bool}``; toggling only
   drops that directive. Rendering iterates the fixed declared order, so the
   stable prefix of untoggled directives is unaffected.
5. **Active-panels seam (retired).** ``active_panels`` is accepted for call-site
   compatibility but is a documented no-op — the ``<active_panels>`` suffix
   block is no longer emitted.

The ordering is reflected in ``templates/system.j2`` (stable prefix) and
``templates/suffix.j2`` (per-turn suffix); content stays distilled like
``narrator.py`` — full preset text is W2.4+.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .library_mini import render_library_doc, resolve_library_doc
from fabulist.dsl import strip_dsl  # canonical impl (D35 seam swap); name kept for importers
from .envelope import insert_engine_envelope, render_engine_envelope
from .narrator import OOC_FLAG, render_state_block
from .parts import (
    DEFAULT_DIRECTIVES,
    ENGINE_CONTEXT_DIRECTIVE_ID,
    SYSTEM_FRAMING,
    Directive,
)
from .surfaces import (
    DEFAULT_HISTORY_DEPTH,
    DEFAULT_HISTORY_KEEP_LAST_DSL,
    DEFAULT_HISTORY_STRIP_DSL,
    normalize_surfaces,
    render_surface_text,
)

__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_HISTORY_DEPTH",
    "DEFAULT_HISTORY_KEEP_LAST_DSL",
    "DEFAULT_HISTORY_STRIP_DSL",
    "Directive",
    "compile",
    "compile_with_boundary",
    "count_tokens",    "render_system_prompt",
    "strip_dsl",
    "stripped_history",
]

#: Default context budget for the rendered system/directive block (tokens).
DEFAULT_BUDGET = 16000

#: char/4 token heuristic divisor (v1); swap via the ``tokenizer`` seam.
CHARS_PER_TOKEN = 4

TokenizerFn = Callable[[str], int]

_VALID_KINDS = ("in_character", "ooc")

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)

#: Keys a directive entry may override when merged onto the catalog.
_DIRECTIVE_FIELDS = frozenset(
    {"id", "name", "content", "priority", "tier", "core", "protected", "enabled"}
)


# ---------------------------------------------------------------------------
# token accounting
# ---------------------------------------------------------------------------


def count_tokens(text: str, tokenizer: TokenizerFn | None = None) -> int:
    """Count tokens by the char/4 heuristic, or delegate to ``tokenizer``.

    The seam lets a real tokenizer replace the heuristic without changing the
    budget manager. ``None``/empty text is zero tokens.
    """
    if not text:
        return 0
    if tokenizer is not None:
        return max(0, int(tokenizer(text)))
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# directive resolution (config + toggles), fixed declared order
# ---------------------------------------------------------------------------


def _render_anti_slop_token(directives: Sequence[Directive]) -> list[Directive]:
    """Resolve ``{{slop_phrases}}`` in the ``anti_slop`` directive (issue #50).

    Consumer B: the prevention list is the SAME canonical artifact list the
    validator and the d1 bank derive from. The token is resolved on BOTH prompt
    paths (the code catalog and the preset/artifact rows) and independent of the
    general ``variables`` map, so the opening/greeting prompt renders the list
    too. A directive without the token is returned untouched (an owner may edit
    the row freely).
    """
    from fabulist.antislop import ANTI_SLOP_TOKEN, render_prompt_list

    rendered: str | None = None
    out: list[Directive] = []
    for directive in directives:
        if directive.id == "anti_slop" and ANTI_SLOP_TOKEN in directive.content:
            if rendered is None:
                rendered = render_prompt_list()
            out.append(
                replace(
                    directive,
                    content=directive.content.replace(ANTI_SLOP_TOKEN, rendered),
                )
            )
        else:
            out.append(directive)
    return out


def _apply_overrides(base: Directive, entry: Any) -> Directive:
    """Merge one config entry onto ``base`` (unknown fields are ignored)."""
    if isinstance(entry, Directive):
        return entry
    if not isinstance(entry, Mapping) or "id" not in entry:
        raise ValueError("directive entries must be a Directive or a mapping with 'id'")
    fields = {k: v for k, v in entry.items() if k in _DIRECTIVE_FIELDS}
    return replace(base, **fields)


def _entries_from_config(config: Any) -> Sequence[Any]:
    if config is None:
        return ()
    if isinstance(config, Mapping):
        if "directives" in config:
            entries = config["directives"]
            if not isinstance(entries, (list, tuple)):
                raise TypeError("config['directives'] must be a sequence")
            return entries
        # A bare {id: override} mapping (override is a dict or a bool).
        out: list[Any] = []
        for key, value in config.items():
            if key == "budget":
                continue
            if isinstance(value, Mapping):
                out.append({"id": key, **value})
            else:
                out.append({"id": key, "enabled": bool(value)})
        return out
    if isinstance(config, (list, tuple)):
        return config
    raise TypeError("directive_config must be None, a mapping, or a sequence")


def resolve_directives(
    directive_config: Any = None,
    toggles: Mapping[str, bool] | None = None,
    surfaces: Mapping[str, object] | None = None,
    variables: Mapping[str, str] | None = None,
    *,
    base: Sequence[Directive] | None = None,
) -> list[Directive]:
    """Resolve config + toggles into an ordered directive list.

    Known entries keep their declared position; new ids append in config order.
    ``toggles`` overrides ``enabled`` per id (the W2.11 surface). ``surfaces``
    (D27) may carry a ``directives`` map of ``{directive_id: content}`` that
    replaces the code-constant directive text — rendered through
    :func:`render_surface_text` so ``{{expression}}`` tokens resolve (D27);
    absent/empty ``variables`` leaves tokens literal (byte-parity fallback).

    ``base`` (preset-carried directives): ``None`` — the default and every
    pre-existing call site — starts from :data:`DEFAULT_DIRECTIVES` (code
    catalog, byte-identical). A non-``None`` list REPLACES the catalog: the
    stored order is authoritative (delete/reorder/add are list edits), and
    each row's ``content`` renders through :func:`render_surface_text` so
    ``{{expression}}`` tokens in preset rows resolve like surface text.
    Toggles and the ``surfaces`` map still apply on top of ``base`` unchanged.
    """
    catalog = list(base) if base is not None else list(DEFAULT_DIRECTIVES)
    if base is not None:
        catalog = [
            replace(d, content=render_surface_text(d.content, variables))
            for d in catalog
        ]
    index = {d.id: i for i, d in enumerate(catalog)}

    for entry in _entries_from_config(directive_config):
        ident = entry.id if isinstance(entry, Directive) else entry.get("id")
        if not ident:
            raise ValueError("directive entries must carry an 'id'")
        if ident in index:
            catalog[index[ident]] = _apply_overrides(catalog[index[ident]], entry)
        else:
            base = Directive(id=ident, name=ident, content="", priority=0, tier=0)
            index[ident] = len(catalog)
            catalog.append(_apply_overrides(base, entry))

    if toggles:
        catalog = [
            replace(d, enabled=bool(toggles[d.id])) if d.id in toggles else d
            for d in catalog
        ]
    overrides = normalize_surfaces(surfaces).get("directives")
    if overrides:
        catalog = [
            replace(
                d,
                content=render_surface_text(overrides[d.id], variables),
            )
            if d.id in overrides
            else d
            for d in catalog
        ]
    return catalog


def _budget_from_config(
    directive_config: Any, explicit: int | None
) -> int:
    if explicit is not None:
        return explicit
    if isinstance(directive_config, Mapping) and "budget" in directive_config:
        return int(directive_config["budget"])
    return DEFAULT_BUDGET


# ---------------------------------------------------------------------------
# system-prompt rendering + budget trim
# ---------------------------------------------------------------------------


def _render_system(framing: str, directives: Sequence[Directive]) -> str:
    return _ENV.get_template("system.j2").render(framing=framing, directives=directives)


def _trim_to_budget(
    directives: Sequence[Directive],
    framing: str,
    budget: int,
    tokenizer: TokenizerFn | None,
) -> list[Directive]:
    """Drop lowest-priority trimmable directives until the system fits.

    Deterministic and independent of facts/history, so the stable prefix never
    changes because of per-turn context. Core / protected blocks are exempt.
    """
    current = list(directives)
    order = {d.id: i for i, d in enumerate(directives)}
    trimmable = sorted(
        (d for d in current if d.trimmable),
        key=lambda d: (d.priority, order[d.id]),
    )
    while trimmable and count_tokens(_render_system(framing, current), tokenizer) > budget:
        dropped = trimmable.pop(0)
        current = [d for d in current if d.id != dropped.id]
    return current


def render_system_prompt(
    directive_config: Any = None,
    toggles: Mapping[str, bool] | None = None,
    *,
    library_doc: str | None = None,
    framing: str = SYSTEM_FRAMING,
    budget: int | None = None,
    tokenizer: TokenizerFn | None = None,
    surfaces: Mapping[str, object] | None = None,
    variables: Mapping[str, str] | None = None,
    directives: Sequence[Directive] | None = None,
    anti_injection: bool = False,
) -> str:
    """Render the cache-stable system prefix (framing + enabled directives).

    Only this block is subject to the token budget; per-turn facts/history never
    influence it, which is what makes the prefix byte-stable across turns.

    D27: ``surfaces`` may override the library doc (``library_doc``; its
    ``{{expression}}`` tokens resolve against ``variables``) and directive text
    (``directives``). An explicit ``library_doc`` argument still wins over the
    surface (the preset path passes ``""`` to suppress the in-system library and
    emit the separate ``library`` module instead).

    ``directives`` (preset-carried base catalog): ``None`` — the default —
    starts from the code catalog, byte-identical to the pre-preset path; a
    non-empty list replaces it in stored order (:func:`resolve_directives`).

    ``anti_injection`` (issue #39): the ONE render gate for the ``## Engine
    context`` clause (``ENGINE_CONTEXT_DIRECTIVE_ID``). ``False`` — the shipped
    default — omits the clause wholesale from the rendered prompt: the heading,
    the body and its separator, so the diff against ``True`` is exactly the
    clause. ``True`` renders today's prefix byte-identically (the clause is
    ``protected``, hence never budget-trimmed, while on). The clause's CONTENT
    is untouched either way — it stays in the code catalog and in the factory
    artifact / preset directive row; only the render is gated, so the per-turn
    engine envelope — with its ``<preset_runtime>`` payload — still rides ONE
    universal carrier (issue #44: an ephemeral ``assistant`` message immediately
    before the final user message; the injection is NOT part of this toggle).
    """
    normalized = normalize_surfaces(surfaces)
    resolved = resolve_directives(
        directive_config,
        toggles,
        surfaces=normalized,
        variables=variables,
        base=directives,
    )
    # Issue #50: the anti-slop directive's prevention list renders from the one
    # canonical artifact list (BEFORE the anti-injection drop; unrelated rows).
    resolved = _render_anti_slop_token(resolved)
    # Issue #39: the Anti Injection gate is a DROP of the whole directive row,
    # applied to the resolved list (so it wins over a config/row ``enabled`` on
    # both prompt paths: the code catalog and the preset/artifact rows). Nothing
    # else about the list moves, which is what keeps ON byte-identical to the
    # pre-toggle prompt and makes the OFF delta exactly the clause.
    if not anti_injection:
        resolved = [d for d in resolved if d.id != ENGINE_CONTEXT_DIRECTIVE_ID]
    if library_doc is None:
        library = resolve_library_doc(normalized, variables)
    else:
        library = library_doc
    enabled = [
        replace(d, content=d.content.replace("<<library>>", library))
        for d in resolved
        if d.enabled
    ]
    effective_budget = _budget_from_config(directive_config, budget)
    enabled = _trim_to_budget(enabled, framing, effective_budget, tokenizer)
    return _render_system(framing, enabled)


# ---------------------------------------------------------------------------
# DSL-stripped history projection
# ---------------------------------------------------------------------------

# strip_dsl is imported from fabulist.dsl (single authoritative implementation,
# spec-driven component table). The former local copy — including the hardcoded
# five-name regex that let flavor statements leak into history — is retired.


def _prose_from_segments(segments: Sequence[Any], *, strip: bool = True) -> str:
    """Join segment texts in document order.

    ``strip=True`` (the default) keeps ``prose`` segments only, dropping ``dsl``
    (the pre-D27 behavior). ``strip=False`` keeps every segment -- the
    ``history.strip_dsl`` surface knob turns this off to show the narrator its
    own panels.
    """
    texts: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, Mapping):
            continue
        if strip and seg.get("kind", "prose") != "prose":
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def _project_history_item(item: Any, *, strip: bool = True) -> dict[str, Any] | None:
    """Project one prior-turn item into a ``{"role","content"}`` message.

    Accepts narrative payloads with ordered ``segments``, payloads carrying
    ``prose``/``dsl`` (dsl dropped when ``strip``), already-shaped messages
    (user/system passed through untouched; assistant content stripped when
    ``strip``), or a bare segment list. Returns ``None`` when empty. ``strip``
    is the ``history.strip_dsl`` surface knob; ``True`` is the pre-D27 default.
    """
    if item is None:
        return None
    if isinstance(item, (list, tuple)):
        content = _prose_from_segments(item, strip=strip)
        return {"role": "assistant", "content": content} if content else None
    if isinstance(item, str):
        content = strip_dsl(item) if strip else item.strip()
        return {"role": "assistant", "content": content} if content else None
    if not isinstance(item, Mapping):
        raise TypeError(f"unsupported history item: {type(item)!r}")

    # Multipart content parts (an inlined image/video narrative): pass the list
    # through VERBATIM. There is no text to DSL-strip, and str() would stringify
    # the parts into a Python repr — destroying the image content part. Mirrors
    # the regex-stage guard in wiring.history_from_log.
    if isinstance(item.get("content"), list):
        return {"role": item.get("role") or "assistant", "content": item["content"]}

    role = item.get("role")
    if role in ("user", "system"):
        content = str(item.get("content", item.get("text", ""))).strip()
        return {"role": role, "content": content} if content else None

    if item.get("segments"):
        content = _prose_from_segments(item["segments"], strip=strip)
    elif "prose" in item:
        content = str(item.get("prose", "")).strip()
        dsl = str(item.get("dsl", "")).strip()
        if not strip and dsl:
            content = f"{content}\n{dsl}".strip()
    elif "dsl" in item:
        content = "" if strip else str(item.get("dsl", "")).strip()
    elif "content" in item:
        raw_content = str(item.get("content", ""))
        content = strip_dsl(raw_content) if strip else raw_content.strip()
    elif "text" in item:
        content = str(item.get("text", "")).strip()
    else:
        content = ""
    if not content:
        return None
    return {"role": role or "assistant", "content": content}


def _is_narrative_item(item: Any) -> bool:
    """True when ``item`` projects to an assistant/narrative turn.

    Explicit ``user``/``system`` messages pass through untouched and never
    count toward the keep-last window; everything else (segment lists,
    prose/dsl payloads, assistant messages) is narrative.
    """
    if isinstance(item, Mapping):
        return item.get("role") not in ("user", "system")
    return item is not None


def stripped_history(
    items: Sequence[Any] | None,
    *,
    strip: bool = True,
    keep_last: int = 0,
) -> list[dict[str, Any]]:
    """DSL-stripped OpenAI history for prior turns.

    Port of the preset's "Strip VTK from Sent Context": prior narrative events
    contribute their ordered ``prose`` segments only — the narrator never sees
    its own stale panels. Prose order is preserved.

    ``strip=False`` (the ``history.strip_dsl`` surface knob) keeps every
    segment's DSL. When ``strip`` is True, ``keep_last`` (the
    ``history.dsl_keep_last`` knob; the compile path defaults it to 3) unstrips
    the DSL of the last N *narrative* turns; older turns strip exactly as
    before. ``keep_last=0`` reproduces the legacy strip-all projection. Explicit
    ``user``/``system`` messages pass through untouched and never count toward
    the window.
    """
    entries = list(items or [])
    window = max(0, int(keep_last))
    keep: set[int] = set()
    if strip and window > 0:
        narrative_idx = [
            i for i, entry in enumerate(entries) if _is_narrative_item(entry)
        ]
        keep = set(narrative_idx[-window:])
    out: list[dict[str, Any]] = []
    for i, item in enumerate(entries):
        item_strip = False if (not strip or i in keep) else True
        projected = _project_history_item(item, strip=item_strip)
        if projected is not None:
            # Issue #42: the reasoning a prior assistant turn PRODUCED rides the
            # history message (``history_from_log`` stamps it), so the request
            # builder can pass it back for thinking-active connections. Carried
            # through projection unconditionally: WHICH connection may send it
            # is the client seam's decision, not history assembly's. Absent on
            # turns that never stored reasoning — those stay content-only.
            if (
                isinstance(item, Mapping)
                and projected.get("role") == "assistant"
                and isinstance(item.get("reasoning_content"), str)
            ):
                projected["reasoning_content"] = item["reasoning_content"]
            out.append(projected)
    return out


# ---------------------------------------------------------------------------
# suffix + compile
# ---------------------------------------------------------------------------


def _render_suffix(parts: Sequence[str]) -> str:
    return _ENV.get_template("suffix.j2").render(parts=[p for p in parts if p])


def compile(  # noqa: A001 - the compiler is the public entry point
    directive_config: Any = None,
    toggles: Mapping[str, bool] | None = None,
    facts: str | None = None,
    history: Sequence[Any] | None = None,
    user_content: str = "",
    *,
    plan: str | None = None,
    active_panels: str | None = None,
    kind: str = "in_character",
    budget: int | None = None,
    tokenizer: TokenizerFn | None = None,
    library_doc: str | None = None,
    framing: str = SYSTEM_FRAMING,
    recalled: str | None = None,
    notice: str | None = None,
    lane: str | None = None,
    palette: str | None = None,
    surfaces: Mapping[str, object] | None = None,
    variables: Mapping[str, str] | None = None,
    directives: Sequence[Directive] | None = None,
    anti_injection: bool = False,
    boundary_out: dict | None = None,
) -> list[dict[str, Any]]:
    """Compile one turn into an OpenAI ``messages`` list (see
    :func:`compile_with_boundary` for the full parameter contract).

    ``boundary_out`` (optional dict) receives the out-of-band assembly
    boundaries (§5.1): ``boundary_out["history_start"]`` — the history-splice
    message index — and ``boundary_out["def_end"]`` — one past the FINAL user
    message (issue #26: the ``<preset_def>`` bound closes there; everything the
    caller appends after it is per-turn runtime). A caller that ignores them
    (or a test double) changes nothing about the returned messages.

    The message assembly lives in :func:`compile_with_boundary` (single
    source); this entry point returns the messages alone for every existing
    caller.
    """
    messages, history_start = compile_with_boundary(
        directive_config=directive_config,
        toggles=toggles,
        facts=facts,
        history=history,
        user_content=user_content,
        plan=plan,
        active_panels=active_panels,
        kind=kind,
        budget=budget,
        tokenizer=tokenizer,
        library_doc=library_doc,
        framing=framing,
        recalled=recalled,
        notice=notice,
        lane=lane,
        palette=palette,
        surfaces=surfaces,
        variables=variables,
        directives=directives,
        anti_injection=anti_injection,
        boundary_out=boundary_out,
    )
    if boundary_out is not None:
        boundary_out.setdefault("history_start", history_start)
        boundary_out.setdefault("def_end", len(messages))
        boundary_out.setdefault("envelope_at", None)
    return messages


def compile_with_boundary(  # noqa: A001 - sibling of the public entry point
    directive_config: Any = None,
    toggles: Mapping[str, bool] | None = None,
    facts: str | None = None,
    history: Sequence[Any] | None = None,
    user_content: str = "",
    *,
    plan: str | None = None,
    active_panels: str | None = None,
    kind: str = "in_character",
    budget: int | None = None,
    tokenizer: TokenizerFn | None = None,
    library_doc: str | None = None,
    framing: str = SYSTEM_FRAMING,
    recalled: str | None = None,
    notice: str | None = None,
    lane: str | None = None,
    palette: str | None = None,
    surfaces: Mapping[str, object] | None = None,
    variables: Mapping[str, str] | None = None,
    directives: Sequence[Directive] | None = None,
    anti_injection: bool = False,
    boundary_out: dict | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """:func:`compile` plus the fallback assembly boundaries, out-of-band.

    Layout: stable system prefix, DSL-stripped history, ONE ephemeral
    ``assistant`` engine envelope (issue #44: inserted immediately BEFORE the
    final user message), then the player's message (BARE — the provenance
    contract). ``<preset_runtime>`` is added by the wrapper seam. ``kind`` is
    ``"in_character"`` or ``"ooc"``; OOC flags the final user message and the
    envelope rides unchanged.

    The per-turn data blocks all ride the envelope, in contract order:
    ``facts`` -> ``<engine_state>``, ``notice`` -> ``<engine_notice>``,
    ``recalled`` -> ``<engine_memory>`` (D22 reference framing, body only),
    ``lane`` -> ``<engine_lane>``, ``plan`` -> ``<steering>``,
    ``palette`` -> ``<engine_palette>`` (LAST). Empty blocks are absent and a
    turn with no blocks emits NO envelope message at all. Nothing
    engine-authored rides inside the player's message, and the stable prefix
    (system + history) stays byte-identical.

    D27: ``surfaces`` carries the preset's token-surface overrides. Its
    ``history`` knobs (``depth`` limits prior turns, ``strip_dsl`` toggles the
    projection, ``dsl_keep_last`` keeps the last N narrative turns' DSL) apply
    here; ``library_doc``/``directives`` flow through
    :func:`render_system_prompt`; ``variables`` resolves ``{{expression}}``
    tokens. With no ``surfaces``, the only departure from the pre-D27 output is
    the default keep-last window (3); ``history.dsl_keep_last=0`` restores the
    legacy strip-all projection byte-for-byte.

    ``active_panels`` is a retired no-op: the kwarg is accepted so existing
    call sites keep working, but the ``<active_panels>`` suffix block is no
    longer emitted.

    ``directives`` (keyword-only) is the preset-carried base catalog passed
    straight to :func:`render_system_prompt`; ``None`` = the code catalog.

    ``anti_injection`` (keyword-only, issue #39) is the persisted Anti Injection
    product setting for THIS build, passed through to
    :func:`render_system_prompt`; the default ``False`` is the shipped default
    and omits the ``## Engine context`` clause while leaving the per-turn
    ``<preset_runtime>`` envelope's carrier untouched (the envelope INJECTION is
    not gated by this toggle, issue #44).

    ``boundary_out`` (optional dict) receives BOTH assembly boundaries (§5.1):
    ``history_start`` (the stable-prefix/history splice point) and ``def_end``
    (one past the final user message — the ``<preset_def>`` close, issue #26),
    plus ``envelope_at`` — the index of the per-turn engine envelope inserted
    immediately before that message (issue #44; ``None`` on a lean turn).
    ``def_end`` is recorded at the EMISSION POINT of the final user message,
    before the envelope is inserted, then shifted past it; it is never
    re-derived from roles downstream (the tail legitimately carries user-role
    messages).
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")

    normalized = normalize_surfaces(surfaces)
    system = render_system_prompt(
        directive_config,
        toggles,
        library_doc=library_doc,
        framing=framing,
        budget=budget,
        tokenizer=tokenizer,
        surfaces=normalized,
        variables=variables,
        directives=directives,
        anti_injection=anti_injection,
    )

    # The player's message is BARE: facts and the steering pin left it (the
    # provenance contract — engine data in user territory acquires player
    # provenance). ``active_panels`` stays a retired no-op.
    final_user = f"{OOC_FLAG} {user_content}" if kind == "ooc" else user_content

    history_knobs = normalized.get("history") or {}
    strip_dsl_knob = history_knobs.get("strip_dsl")
    keep_last_knob = history_knobs.get("dsl_keep_last")
    projected = stripped_history(
        history,
        strip=DEFAULT_HISTORY_STRIP_DSL if strip_dsl_knob is None else strip_dsl_knob,
        keep_last=(
            DEFAULT_HISTORY_KEEP_LAST_DSL
            if keep_last_knob is None
            else keep_last_knob
        ),
    )
    depth = history_knobs.get("depth")
    if depth is not None:
        # Only applied when the preset actually carries the knob; the pre-D27
        # path leaves the caller-supplied (already-limited) history untouched.
        projected = projected[-depth:] if depth else []

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    # §5.1: the history splice point, exposed out-of-band for the wrapper seam
    # (well-defined for empty history — the position history would occupy).
    history_start = len(messages)
    messages.extend(projected)
    messages.append({"role": "user", "content": final_user})
    # §5.1 (issue #26): the ``<preset_def>`` bound closes AFTER the final user
    # message. Captured HERE, at the emission point, out-of-band — never
    # re-derived from roles downstream (the dynamic tail legitimately carries
    # user-role messages, so a role scan would pick the wrong index).
    def_end = len(messages)
    # The per-turn engine envelope: every block, contract order, as ONE
    # EPHEMERAL ``assistant`` message inserted immediately BEFORE that final
    # user message (issue #44, owner ruling 2026-10-24). Never a system message
    # (a stock Jinja template ``raise_exception``s on any non-first system
    # message) and never inside the player's message (which would give engine
    # data player provenance). Empty blocks are absent and a turn with no
    # blocks emits NO envelope message at all.
    envelope = render_engine_envelope(
        state=render_state_block(facts),
        notice=notice,
        memory=recalled,
        lane=lane,
        steering=plan,
        palette=palette,
    )
    messages, def_end, envelope_at = insert_engine_envelope(
        messages, def_end, envelope
    )
    if boundary_out is not None:
        boundary_out["history_start"] = history_start
        boundary_out["def_end"] = def_end
        boundary_out["envelope_at"] = envelope_at
    return messages, history_start
