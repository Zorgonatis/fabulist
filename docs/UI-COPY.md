# UI copy — the register

Issue #58: user-facing hints, labels and descriptions had drifted into
internal documentation. A player was being shown decision ids (`D26`, `W9.17`,
`ADR-…`, `issue #55`), engine nouns they never chose to learn (`provenance
contract`, `dispatcher`, `fan-out`, `preset binding`, `topology`, `manifest`,
`context ledger`, `cache contract`, `slot`), and hints that explained how a
thing is *built* instead of what it *does*.

This file is the register for anything the UI renders: labels, hints, drawer
and panel headings, placeholders, empty states, notices, tooltips, aria-labels.
It applies to Svelte markup **and** to served copy the UI prints verbatim
(`SETTING_VALUES` / `FEATURE_FLAGS` names in `api/settings.py`, `SLOT_LABELS`
and `SLOT_GROUPS` in `connections/taxonomy.py`, the `description` /
`runs.description` fields in `flow/manifest.py`, and the mock fixtures that
mirror them).

## The rules

1. **Say what it does for the player.** "Shows the engine's own notes under the
   story" beats "The provenance contract: how engine context blocks are read".
2. **No internal references.** Decision ids, wave/section ids, issue numbers and
   spec section marks belong in code comments and docstrings — never on screen.
3. **No engine nouns.** If a word only makes sense after reading
   `docs/decisions/`, it does not belong in a label. Translate: topology / turn
   order; manifest / turn flow; slot / role or agent; directive / rule; tier /
   group; inert / ignored; canonical / built-in (step); ledger / window use;
   projection / earlier turns; envelope / file; provenance / where it came from.
4. **Keep the product's own nouns.** Narrator, NPC lanes, Image Prompter,
   persona, world, book, memory, session, turn, cast, panel. Plain is not the
   same as dumb: do not rename the things the game is made of.
5. **Shorter wins ties.** A hint that needs a second sentence usually needs a
   rewrite, not a second sentence.
6. **Never rename a key to fix its label.** Slot keys, setting ids, event kinds
   and wire field names are contracts; only the display string changes.
7. **Copy is not behavior.** A sweep may change words; it may not change what a
   control does, mount, or send.

## What this sweep changed (issue #58)

Grouped by surface; every string below is display copy only.

| Surface | Before | After |
| --- | --- | --- |
| Settings / rules — `Engine context` | "The provenance contract: how engine context blocks are read, once." | "The engine's own notes for this turn, and how the narrator reads them." |
| Settings / rules — other served descriptions | "Owner-binding limits…"; "NPCs run high autonomy on persona…"; "Treats injected state facts…"; "Drafts novel components interwoven with prose…"; "Declares panels inline at their anchor…"; "Sorts each message OOC or incomplete…" | "Hard limits: never a predictable AI, never answerable for the player."; "Characters follow their own persona and only what they could know."; "Treats the tracked facts as ground truth for the scene."; "Adds visual panels between paragraphs; Custom allows raw HTML/CSS."; "Puts each panel where it belongs in the prose, never top or tail."; "Sorts each message as in-character or OOC; [OOC] steps out of role." |
| Rules list | "Ordered narrator directive rows. This list is render order; the preset's own list replaces the factory catalog wholesale." | "The rules the narrator is given, in the order it reads them. Edit a row to change what the narrator is told." |
| Rules list | "+ add directive", "No directive rows.", "Edit/Delete directive", "Directive name/tier/priority/content", "Budget-trim order (lower trims first)", "(never rendered into the prompt)" | "+ add rule", "No rules yet.", "Edit/Delete rule", "Rule name/group/trim order/text", "Trim order when the prompt runs long (lower goes first)", "Shown here in settings, never sent to the narrator" |
| Anti Injection hint | "Tells the narrator that the engine's tagged context blocks are the engine's words…" | "Reminds the narrator that the engine's notes are the engine's words, not the player's. Off: that line is left out of the prompt entirely." |
| Advanced (prompt text) | "Advanced — token text"; "History projection"; "Rewrites applied when projecting prompt history, plus the depth and DSL-trim knobs."; "Strip openui DSL from history" | "Advanced — text the engine fills in"; "Earlier turns"; "Clean-ups applied to earlier turns before they are sent, plus how far back to send."; "Strip panel markup from earlier turns" |
| Advanced (prompt text) | "These surfaces drive the live default pipeline (D26)."; "Old directive override text this preset still carries — fold it into the Rules rows."; tooltip naming `prompts/factory-defaults.json` | "These fields change the default prompt."; "Leftover rule text this preset still carries — move it into the Rules rows."; "Save your edits, then write them into the shipped defaults" |
| Prompt drawer | "No preset selected — the engine is using the byte-faithful default pipeline."; "…editing the persisted default pipeline directly (D26)…"; "Modules seeded from the imported card for THIS chat (D29)." | "No preset selected — the engine is using the built-in default prompt."; "…editing the default prompt directly…"; "Modules copied from the imported card for this chat only." |
| Top bar | "Edit the prompt pipeline — presets, modules, variables"; "Runtime directives — enable and order rules" | "The prompt — its steps, rules and variables"; "Rules — turn rules on, off and reorder them" |
| Connections | "The ${slot} key is inert: this role resolves through the effective binding above…"; label "{slot} key inert →" | "The ${slot} entry is ignored: this agent goes through the connection above…"; "{slot} ignored →" |
| Connections | "All lanes ride this one binding (the fan-out runs a single caller)…" | "Every NPC lane shares this one connection, so pointing it at a cheaper or stronger model moves all of them at once." |
| Connections | "ONE row per resolved role… (its own key, a legacy alias, the task, the type default, or the item slot that serves the type)…" | "One row per agent, showing the connection it actually uses and why (its own setting, an older name for the same role, its task, the default for its kind, or the model served by another role)…" |
| Connections | "No slot view from the engine — restart it so the agent taxonomy loads."; "— unresolved —" | "The engine did not send its agent list — restart it to load the agents."; "— not set —" |
| Connections | "Embedding slot…", "Decision slot…", "No slot resolves to this connection…", "context ledger", "budget warnings" | "This connection runs the memory embedder…"; "This connection answers yes/no checks…"; "No agent uses this connection yet…"; "…cannot warn you before the chat runs long, or show you what it sent" |
| Connections | Served label "Npc lanes" (slot label + group) | "NPC lanes" |
| Turn flow | "Turn topology"; "topology = execution order; the engine runs this"; "No flow graphs in the manifest." | "Turn order"; "the steps the engine runs, in order"; "No flows to show." |
| Turn flow | "lanes off — the narrator runs one forward pass"; "pool {n}"; "no lane data yet — the next turn fans out" | "lanes off — the narrator writes this scene alone"; "{n} at once"; "no lanes yet — they appear on the next turn" |
| Graph home | "Edit the executable topology"; "Store the topology"; "Topology unavailable"; "Re-add the canonical "x" node"; the manifest/topology explainer | "Edit the order the engine runs"; "Save the order"; "The flow is unavailable"; "Put the built-in "x" step back"; "You are editing the order the engine runs…" |
| Graph node badges | "Active preset binding"; "Composition — expanded in W9.8" | "Set by the active preset"; "Composition — opens as its own graph" |
| Graph regions | "Stable prefix (cache contract)"; "cache contract: must not change turn-to-turn"; "no region declared in the manifest" | "Fixed start (the same every turn)"; "sent identically every turn"; "not part of any named block" |
| Served flow manifest (every node) | "W9.31: first the seed phase…"; "W9.17: the fan-out's per-member node — ONE model pass per `Send`…"; "Emitted stage name is 'dsl'."; "byte-identical"; "d1-3b batched advisory calls…" | "First checks the world's records… then plans the scene…"; "One lane. Lanes are written down in cast order…"; "A rule-based check — no model call."; "With no cast there are no lanes and the turn runs as usual."; "Asks the yes/no checks — the anti-slop list and the movement question — 30 at a time." |
| Inspector | "Context (per task · measured prompt tokens)"; "No context ledger yet."; "copy turn provenance (JSON)"; "openui DSL ({n} chars)"; "Agent captures (D17)" | "Window use (per task · prompt tokens measured by the model)"; "Nothing measured yet."; "copy this turn's raw record (JSON)"; "panel markup ({n} chars)"; "What each agent saw and said" |
| Inspector | Column tips: "usable prompt budget…", "last measured prompt tokens (usage rows)", "owner-declared Context Size" | "room left for the prompt…", "prompt tokens the model reported last turn", "the context size you declared" |
| State sidebar | "Move the player (a registry move is a deterministic presence change)"; "…flag every disagreement with the registry" | "Move the player to the location you type"; "…flag anything that disagrees with this panel" |
| Cast pane | "Colours are a registry write (user override, latest wins)…" | "Colours you pick here win over the engine's…" |
| Recap / transcript | "seq 12–40"; "absorbed x · seq n"; "Session compacted — … folded into a summary" | "turns 12–40"; "includes x · turn n"; "Older turns condensed — … rolled up into a summary" |
| Memory pane | "Queries this turn"; "Recalled block"; "Result rows"; "Nothing was surfaced for the last turn (or it predates result-row capture)." | "What it searched for"; "What the narrator was given"; "What came back"; "Nothing came back for the last turn — or this chat is older than the record." |
| Proposals | "applied to the asset catalog · undo re-appends what the accept replaced"; "A legacy row the server did not hydrate — the prior value is not on the wire"; "accepting mints this actor persona card"; "Provenance" section | "accepted into your assets · undo puts back what the accept replaced"; "An older proposal that did not record the previous value"; "accepting creates this actor persona card"; "Where it came from" |
| Studio panes | "The palette's library. A topology node instantiated from a template executes the behaviour…"; "The plan's schema beyond its core values…"; "Nothing here is protected (D67)…"; "Built-in placeholder (not a preset port)"; "the BTS tracker" | "The library behind the graph editor's palette…"; "Extra details the plan can carry beyond its core values…"; "Every row here can be turned off or removed."; "A built-in example, not one of your own assistants"; "the world tracker" |
| Profile pane | "The roster rides the envelope and replaces it on import."; "…the envelope omits the assistant key…" | "Your assistants travel with the profile and replace the ones on the other side."; "…assistants stay out of the file…" |
| Appearance | "Light is the strict preset (every divider holds 3:1)." | "Light is the highest-contrast choice — every divider stays clearly visible." |

## Deliberately kept

- **`Anti Injection`** — the feature's own name (`ANTI_INJECTION_FLAG`), pinned
  by `tests/test_anti_injection_toggle.py`. The hint under it carries the
  meaning instead.
- **`Image generation (ComfyUI)`**, **`Image Prompter endpoint (ComfyUI URL)`**,
  **`Extra params (extra_body)`** — connection/endpoint fields whose technical
  noun is the thing being configured.
- **Node / edge** in the flow editor — the objects the editor draws and the
  player manipulates.
- **`Narrator`, `NPC lanes`, `Image Prompter`, `persona`, `world`, `book`,
  `memory`, `session`, `turn`, `cast`, `panel`** — the product's real nouns.
- **The `/components` gallery copy** — a developer surface, not a player one.
- **API field names, event kinds, slot keys, setting ids** — contracts, never
  copy.

## Known follow-up

The persisted default preset stores directive rows (and their descriptions)
from the artifact it was seeded with, so an existing install keeps the old
served descriptions in the rules list until the preset is reset
(`POST /api/prompts/default/reset`) or a directive row is re-materialized. The
code catalog and the committed artifact now carry the new copy; nothing in the
prompt itself changed.
