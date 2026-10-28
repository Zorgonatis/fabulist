"""Chrome-density pins (issue #22).

The density audit measured 506px of fixed LEFT chrome (rail 241 + dock/state 265)
and 336px of VERTICAL chrome (head 103 + HUD 56 + DSL strip 23 + empty assistant
strip 31 + composer 88 + attach row 35) on a 1280x800 viewport, with time and
location printed THREE times (HUD bar, state-pane rows, DSL scene title) and the
46rem turn cap reading as a left-anchored block instead of a measure.

The owner's ruling in the #22 thread is display-only and concrete, so this module
pins exactly that — no engine surface is involved:

1. **Left.** The docked state pane is COLLAPSED by default and both of its states
   persist under the existing key; the session rail auto-collapses below 1440px
   but an EXPLICIT choice (a stored ``'1'``/``'0'``) is never overruled.
   (Issue #98, owner-approved, refines this: the bar is the READING BLOCK's first
   column now, so it follows the rail's `stored choice > viewport` rule with the
   block's own arithmetic as the threshold — see
   ``test_state_dock_collapse_follows_the_rail_rule``.)
2. **Vertical.** The attach control is one of the composer's actions (the row
   exists only while a file is pending or an error is showing); the HUD is a band
   INSIDE the thread head rather than its own row; the DSL strip renders only
   while a turn runs; the assistant strip's disabled state collapses (UI-LANGUAGE
   §6) with its door moved into the composer; the head is a fixed two-band grid
   whose badge row is capped with a ``+N`` overflow count and whose session id is
   a tooltip.
3. **Duplicates.** Exactly one owner per fact: the HUD station owns time / date /
   location / weather / forecast, the state pane keeps TONE (the theming input),
   and the DSL strip shows the scene title only while a turn runs.
4. **Measure.** The transcript's scroll area stays full-bleed and one centred
   column of 46rem holds every block, so the reclaimed width is symmetric gutter.
   (Issue #98: the 46rem measure is the reading block's grid TRACK now, and the
   docked bar is the block's first column — one box, one gap, so the gutter cannot
   return; see ``test_transcript_is_one_centred_46rem_column``.)
5. **No token touches.** The layout components' styles do not re-declare a theme
   token: the sheet in ``app.css`` (and the presets + wallpaper layer that
   ``tests/test_theme_contrast_ladder.py`` recomputes) is untouched by this pass.

Everything here reads the real component sources — the same files the browser
loads — so a later "tidy" that re-adds a duplicate fact or a permanent row fails
loudly instead of quietly costing prose its measure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHAT = ROOT / "frontend" / "src" / "routes" / "chat"

PAGE = CHAT / "+page.svelte"
RAIL = CHAT / "SessionDrawer.svelte"
STATE = CHAT / "StateSidebar.svelte"
HUD = CHAT / "ChatHud.svelte"
DSL = CHAT / "DslHeader.svelte"
ASSISTANT = CHAT / "AssistantPanel.svelte"
COMPOSER = CHAT / "Composer.svelte"
TRANSCRIPT = CHAT / "TranscriptList.svelte"
CHROME_CSS = ROOT / "frontend" / "src" / "app.css"

LAYOUT_SOURCES = (PAGE, RAIL, STATE, HUD, DSL, ASSISTANT, COMPOSER, TRANSCRIPT)

#: The facts the HUD station is the ONE owner of (issue #22, "DUPLICATES").
HUD_FACTS = ("time", "date", "loc", "weather", "forecast")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def strip_css_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def style_block(path: Path) -> str:
    """The component's scoped <style> content, comments removed."""
    text = read(path)
    match = re.search(r"<style>(.*?)</style>", text, flags=re.DOTALL)
    assert match, f"{path.name} has no <style> block"
    return strip_css_comments(match.group(1))


def rule_bodies(css: str, selector: str) -> list[str]:
    """Bodies of every rule whose selector list contains ``selector`` verbatim."""
    bodies: list[str] = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            bodies.append(match.group(2))
    return bodies


def style_declarations(css: str, prop: str) -> list[str]:
    """Every declared value of ``prop`` across a stylesheet."""
    return [m.group(1).strip() for m in re.finditer(rf"(?<![\w-]){re.escape(prop)}\s*:\s*([^;}}]+)", css)]


# --- 1. LEFT: the two persisted collapse defaults --------------------------------


def test_state_dock_collapse_follows_the_rail_rule():
    """Issue #98 (owner-approved): the dock's collapse obeys the CHAT RAIL's rule.

    A stored ``'1'``/``'0'`` is obeyed; with no stored choice the component follows
    the viewport and keeps following it until the reader operates the control; the
    control writes the choice, so the reader always wins. Both states persist under
    the existing key.

    This supersedes issue #22's "collapsed at every width" (which existed because a
    265px LEFT column cost prose its measure): as the reading block's first column
    the bar takes its width from the block it shares with the measure, and below the
    block's own floor (~1012px of viewport) it still starts collapsed.
    """
    src = read(STATE)
    assert "fabulist.statebar.collapsed" in src, "the dock's persistence key moved"
    # Collapsed is the initial value in both worlds (SSR has no viewport).
    assert re.search(r"collapsed = \$bindable\(true\)", src), (
        "the dock must start collapsed so the first paint never reflows"
    )
    mount = re.search(r"onMount\(\(\) => \{(.*?)\n\t\}\);", src, flags=re.DOTALL)
    assert mount, "the dock's onMount restore is gone"
    body = mount.group(1)
    # The stored choice is honoured BEFORE the viewport rule is consulted...
    assert body.index("getItem(COLLAPSE_KEY)") < body.index("matchMedia"), (
        "a stored choice must outrank the viewport"
    )
    # ...which is a media query, not a one-shot innerWidth read...
    assert "matchMedia" in body, "auto-collapse must follow resizes"
    assert "if (!statebarChoice) collapsed = !event.matches" in body, (
        "a resize may only move the bar while the reader has not chosen"
    )
    # ...and both states are written back on every control, the head door included.
    assert "setItem(COLLAPSE_KEY, collapsed ? '1' : '0')" in src
    assert "statebarChoice = true" in src
    # The head's named door drives the same component-owned state.
    assert re.search(r"export function expand\(\)", src)
    assert re.search(r"export function collapse\(\)", src)


def test_rail_auto_collapses_below_1440_unless_the_reader_chose():
    """The rail follows the viewport below 1440px; an explicit choice wins."""
    src = read(RAIL)
    assert "fabulist.rail.collapsed" in src
    assert re.search(r"AUTO_COLLAPSE_MAX_PX\s*=\s*1440", src), "the owner's threshold is 1440px"
    assert "matchMedia" in src, "auto-collapse must read the viewport, not a one-shot innerWidth"
    assert "railChoice" in src, "an explicit choice must be tracked separately from auto"

    mount = re.search(r"onMount\(\(\) => \{(.*?)\n\t\}\);", src, flags=re.DOTALL)
    assert mount, "the rail's onMount is gone"
    body = mount.group(1)
    # Explicit state first, THEN the media query.
    assert body.index("saved === '1'") < body.index("matchMedia"), (
        "the stored choice must be honoured before the viewport rule is consulted"
    )
    assert "if (!railChoice) railCollapsed = narrow.matches" in body, (
        "a resize may only move the rail while the reader has not chosen"
    )
    # ...and the toggle records the choice.
    toggle = re.search(r"function toggleRail\(\): void \{(.*?)\n\t\}", src, flags=re.DOTALL)
    assert toggle and "railChoice = true" in toggle.group(1), (
        "toggling the rail must stop the viewport from overruling it"
    )


# --- 2. VERTICAL: one row per job ------------------------------------------------


def test_attach_row_is_gone_from_the_thread_column():
    """The 35px attach row is now one COMPOSER ACTION (chip inline)."""
    page = read(PAGE)
    assert 'class="attach-row"' not in page, "the permanent attach row must not come back"
    assert "Attach media" not in page or "onAttach={pickFile}" in page

    composer = read(COMPOSER)
    actions = re.search(r'<div class="actions">(.*?)</div>\s*\{#if micError\}', composer, flags=re.DOTALL)
    assert actions, "the composer's actions row moved"
    row = actions.group(1)
    assert 'class="attach"' in row, "the attach control belongs in the actions row"
    assert 'class="attach-chip"' in row, "the pending file rides the same row as a chip"
    # The chip is conditional on a pending file; the row is not permanent chrome.
    assert re.search(r"\{#if pendingName\}", row), "the chip renders only while a file is pending"
    # The page still owns the File (it prefixes the next send) and passes the name.
    assert "pendingName={pendingFile?.name ?? null}" in page
    assert "bind:this={fileInput}" in page, "the page still owns the hidden input"


def test_hud_is_a_band_in_the_thread_head_not_its_own_row():
    """EnvTracker folded into the head: no separate 56px HUD row."""
    page = read(PAGE)
    head = re.search(r"<header class=\"thread-head\">(.*?)</header>", page, flags=re.DOTALL)
    assert head, "the thread head is gone"
    assert "<ChatHud />" in head.group(1), "the HUD must render INSIDE the thread head"
    assert len(re.findall(r"<ChatHud\s*/>", page)) == 1, "there is exactly one HUD mount"

    css = style_block(PAGE)
    head_rule = rule_bodies(css, ".thread-head")
    assert head_rule, "the head rule is gone"
    body = head_rule[0]
    assert "grid-template-areas" in body and "env" in body, (
        "the head's two bands (identity/actions + env) must be declared, not left to wrapping"
    )
    assert "grid-template-columns: minmax(0, 1fr) auto" in body, (
        "the head is a fixed two-column grid so its height cannot depend on the viewport"
    )
    # The station's own card geometry is compacted only where the head hosts it.
    assert any("min-height: 0" in rule for rule in rule_bodies(css, ".thread-head :global(.env-tracker)"))


def test_hud_owns_the_five_facts_and_renders_nothing_until_it_has_them():
    hud = read(HUD)
    for fact in HUD_FACTS:
        assert f"{fact}={{" in hud, f"the HUD station must still render {fact}"
    # No statement for THIS session -> no element at all (an empty chat pays no height).
    assert re.search(r"\{#if values\}\s*\n\s*<div class=\"hud-bar\"", hud), (
        "the HUD wrapper must be gated on parsed values"
    )


def test_dsl_strip_renders_only_while_a_turn_runs():
    src = read(DSL)
    visible = re.search(r"const visible = \$derived\((.*?)\);", src)
    assert visible, "DslHeader's visibility derivation is gone"
    expr = visible.group(1)
    assert "running" in expr
    assert "sceneTitle" not in expr, "an idle scene title must not buy the strip 23px"
    assert "steps.length" not in expr, "an idle step trace must not buy the strip 23px"
    # An open drop-down still pins the host it renders in (never yanked mid-read).
    assert "open" in expr and "flowOpen" in expr


def test_assistant_strip_collapses_when_disabled_and_its_door_is_the_composer():
    src = read(ASSISTANT)
    # The disabled state renders only while the picker is open (UI-LANGUAGE §6).
    assert re.search(r"\{:else if pickerOpen\}", src), (
        "the disabled assistant strip must not occupy a row at rest"
    )
    # Focus loss still closes it, and the flag is the host's (bindable).
    assert "pickerOpen = $bindable(false)" in src
    assert re.search(r"if \(!pickerOpen \|\| saving\) return;", src)
    # The host binds it and the composer carries the door.
    assert "bind:pickerOpen={assistantPickerOpen}" in read(PAGE)
    composer = read(COMPOSER)
    assert 'class="assistant-door"' in composer
    assert "onToggleAssistant" in composer
    # The door lives OUTSIDE the panel, so "collapse on focus loss" needs a
    # document-level close that skips only the panel and the door itself.
    assert 'data-assistant-door="1"' in composer
    assert "window.addEventListener('pointerdown', onPointerDown, true)" in src
    assert "window.addEventListener('focusin', onFocusIn, true)" in src


def test_head_badge_row_is_one_line_with_an_overflow_count():
    page = read(PAGE)
    assert re.search(r"const IDENTITY_BADGE_CAP = \d+", page), "the badge cap must be explicit"
    assert "identityCast" in page and "identityOverflow" in page
    assert "data-badge-overflow" in page, "the overflow must be a visible count, not a clipped badge"

    css = style_block(PAGE)
    cluster = rule_bodies(css, ".identity-cluster")
    assert cluster, "the identity cluster rule is gone"
    assert "flex-wrap: nowrap" in cluster[0], "the badge row must never grow a second line"


def test_session_id_is_a_tooltip_not_a_header_row_item():
    page = read(PAGE)
    assert 'class="session"' not in page, "the session id must not render beside the title"
    assert "const headTitle = $derived(" in page, "the id rides the title's tooltip"
    assert "session ${$sessionId}" in page
    # ...and the dead rule went with it.
    assert not rule_bodies(style_block(PAGE), ".session"), "the .session rule is dead chrome"


# --- 3. DUPLICATES: exactly one owner per fact -----------------------------------


def test_state_pane_keeps_tone_only():
    """Time / date / location / weather / forecast have ONE owner: the HUD."""
    src = read(STATE)
    top = re.search(r"const topRows = \$derived\(\[(.*?)\]\);", src, flags=re.DOTALL)
    assert top, "the state pane's turn rows are gone"
    rows = top.group(1)
    keys = set(re.findall(r"key: '([a-z_]+)'", rows))
    assert keys == {"tone"}, f"the state pane still reprints HUD facts: {sorted(keys)}"
    # The Forecast row is the PIN's state, never the outlook text (the HUD owns
    # that value); the D45 override door survives on the pin's own row.
    assert "pinForecast" in src, "the forecast row must show pin state, not the value"
    assert "{#if forecast}" not in src, "the state pane must not print the forecast value"
    assert "beginForecast" in src, "the D45 forecast pin door must survive"
    # The no-tree fallback skips the HUD-owned leaves too.
    assert re.search(r"const HUD_OWNED = new Set\(\[([^\]]*)\]\)", src)
    owned = re.search(r"const HUD_OWNED = new Set\(\[([^\]]*)\]\)", src).group(1)  # type: ignore[union-attr]
    for fact in ("'time'", "'date'", "'loc'", "'weather'", "'forecast'"):
        assert fact in owned, f"the fallback still reprints {fact}"


def test_the_player_move_door_survives_the_row_removal():
    """The WRITE path is not a duplicate fact: `move_being` moved, it did not die.

    The retired Location top row was the state pane's only `move_being` door. The
    fact now belongs to the HUD, but dropping the door with the row would have
    removed the UI's only way to place the player — so it lives on the Player row,
    beside the line that already says where they are.
    """
    src = read(STATE)
    assert "'move_being'" in src, "the player move door must survive issue #22"
    assert re.search(r"who: playerKey", src), "the door still targets the player key"
    player = src.split("<summary>Player</summary>", 1)
    assert len(player) == 2, "the Player section is gone"
    assert "move_being" in player[1].split("</details>", 1)[0], (
        "the move door belongs in the Player row"
    )


def test_time_and_location_are_printed_once_each_across_the_chrome():
    """A fact appears in ONE component's markup, never two."""
    owners = {
        "time": HUD,
        "date": HUD,
        "loc": HUD,
        "weather": HUD,
        "forecast": HUD,
    }
    for fact, owner in owners.items():
        assert f"{fact}=" in read(owner), f"{owner.name} must render {fact}"
    # The DSL strip shows the scene TITLE only (a name, not a clock/place pair).
    dsl = read(DSL)
    for fact in ("values.time", "values.date", "values.loc", "values.weather"):
        assert fact not in dsl


# --- 4. MEASURE: the reading cap binds -------------------------------------------


def test_transcript_is_one_centred_46rem_column():
    css = style_block(TRANSCRIPT)
    block = rule_bodies(css, ".reading-block")
    assert block, "the reading block rule is gone"
    body = block[0]
    assert "minmax(0, 46rem)" in body, "the owner's measure is 46rem"
    assert "justify-content: center" in body, "centring is what makes the cap BIND as a measure"
    # Issue #98: the docked bar is the block's FIRST column and the gap is fixed,
    # so the bar's edge and the prose's edge are one composition.
    assert "column-gap: 0.75rem" in body
    docked = rule_bodies(css, ".reading-block.docked")
    assert docked, "the docked variant of the block is gone"
    assert "auto minmax(0, 46rem)" in docked[0], (
        "docked: a content-sized bar column, then the measure"
    )

    # ONE owner for the width: the rows fill their column and carry no cap.
    rows = rule_bodies(css, ".transcript > *")
    assert rows, "the reading column rule is gone"
    assert "width: 100%" in rows[0]
    assert "max-width" not in rows[0], (
        "the rows must not re-declare the reading measure (issue #98: the track owns it)"
    )
    # The user indent is padding, so centring cannot be pushed off-centre.
    user = rule_bodies(css, ".turn.user")
    assert user, "the user-turn rule is gone"
    assert "padding-left: 3rem" in user[0]
    assert "margin-left" not in user[0]


# --- 5. No token touches: the landed theme chain stays untouched ------------------


@pytest.mark.parametrize("path", LAYOUT_SOURCES, ids=lambda p: p.name)
def test_layout_styles_declare_no_theme_tokens(path: Path):
    """A layout component may CONSUME tokens, never re-declare them (issue #22)."""
    css = style_block(path)
    declared = re.findall(r"(?<![\w-])(--[a-zA-Z0-9-]+)\s*:", css)
    assert not declared, f"{path.name} re-declares theme tokens: {declared}"


def test_the_presets_and_the_wallpaper_layer_still_land():
    """The contrast floor this pass must keep green is still the whole chain."""
    css = strip_css_comments(read(CHROME_CSS))
    assert ":root[data-theme='oled']" in css
    assert ":root[data-theme='light']" in css
    assert ":root[data-theme='savilen']" in css
    assert ":root[data-wallpaper]" in css
    # ...and the page still does not paint itself a second time (#13).
    chat_rule = rule_bodies(style_block(PAGE), ".chat")
    assert chat_rule and "background" not in chat_rule[0]
