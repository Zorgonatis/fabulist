// Generate the component-SET artifacts and the narrator library docs from them.
//
// This is the lang-core prompt-generation pipeline (owner mandate): ONE component
// spec feeds (a) the frontend render registry (`src/lib/openui/library.ts`),
// (b) the engine DSL validation table, (c) the DSL-stripping component name list
// (`fabulist.dsl.statements`) and (d) the generated narrator library doc. The
// spec is derived from the zod schemas that ARE the frontend registry source of
// truth (`src/lib/openui/schemas/flavors.ts`), and the doc is produced by
// `@openuidev/lang-core`'s `generateSystemPrompt`.
//
// Component SETS (issue #15): a set is a directory of three artifacts
// (`set.json`, `library.spec.json`, `library.doc.txt`) and the engine teaches /
// gates exactly ONE active set. The renderer instead ships the UNION of every
// declared set, so an old transcript keeps rendering components of a set that is
// no longer active. This script emits both:
//
//     prompts/generated/…           the shipped set (id "technology", version 1.0.0)
//     prompts/sets/index.json       the sets registry (ids, dirs, core, aliases)
//     prompts/sets/union.spec.json  the render contract = the union
//     prompts/sets/<id>/…           any other declared set (fixture sets included)
//
// Run from `frontend/` (lang-core lives in `frontend/node_modules`):
//
//     node scripts/generate-library-spec.mjs            # write artifacts
//     node scripts/generate-library-spec.mjs --check    # exit 1 on drift
//
// Node type-strips the imported `.ts` module natively (Node >= 22.6); no extra
// dependency is required. Every output MUST be deterministic/idempotent (rerun ->
// byte-identical) — `--check` covers the whole tree, so a hand-edit anywhere in
// `prompts/generated/` or `prompts/sets/` fails the suite.
//
// Dialect restriction (owner mandate): the narrator may ONLY learn
// `identifier = Component(positional args)`, the set's components + the root
// ScenePane rule, the trailing options object, and the declare-before-root
// composition rule. The doc is the authored narrator-doc-v2 craft sections plus
// lang-core's `## Component Signatures` reference block; lang-core's other
// sections (Syntax Rules, hoisting/root-first streaming, generic UI rules,
// important rules) are dropped. Any *unknown* lang-core section fails the run
// loudly, so a library upgrade cannot silently leak new dialect into the doc.

import { mkdirSync, renameSync, writeFileSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { generateSystemPrompt } from '@openuidev/lang-core';
import { z } from 'zod';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');

// Import the flavor schemas + meta directly (Node strips the TS types). These
// are the frontend registry's source of truth: 10 scene flavors + 6 artifact
// components (Custom escape hatch + 5 reproductions, 2026-10-14 build).
const { flavorSchemas, flavorMeta } = await import(
	new URL('../src/lib/openui/schemas/flavors.ts', import.meta.url).href
);

// ---------------------------------------------------------------------------
// 1. the four infrastructure/default components
// ---------------------------------------------------------------------------
//
// These four live inline in `src/lib/openui/library.ts`, which imports `.svelte`
// renderers and therefore cannot be imported headlessly by Node. Their prop
// order + descriptions are declared here, mirroring that file; the Python
// consistency test (`tests/test_library_spec.py`) asserts the component NAME set
// matches the registry extracted from `library.ts`, so the two cannot drift
// silently. (Moving these schemas into a shared non-Svelte module would let the
// script import them directly — deferred; that file is out of this lane.)
const INFRASTRUCTURE = [
	{
		name: 'ScenePane',
		description: 'Root container for a narrator scene. Holds the status panel and scene contents.',
		props: [
			{ name: 'title', type: 'string', required: true },
			{ name: 'children', type: 'component[]', required: true },
			{ name: 'options', type: 'object', required: false }
		]
	},
	{
		name: 'MeterBar',
		description: 'Compact meter showing a value on a 0..max scale.',
		props: [
			{ name: 'label', type: 'string', required: true },
			{ name: 'value', type: 'number', required: true },
			{ name: 'max', type: 'number', required: true },
			{ name: 'options', type: 'object', required: false }
		]
	},
	{
		name: 'ObjectCard',
		description: 'Card describing an in-world object, item or quest artefact.',
		props: [
			{ name: 'name', type: 'string', required: true },
			{ name: 'kind', type: 'string', required: true },
			{ name: 'description', type: 'string', required: true },
			{ name: 'options', type: 'object', required: false }
		]
	},
	{
		name: 'BeatCard',
		description: 'A single narration beat shown to the player.',
		props: [
			{ name: 'text', type: 'string', required: true },
			{ name: 'options', type: 'object', required: false }
		]
	}
];

// ---------------------------------------------------------------------------
// 2. dialect copy (preamble / group notes / worked examples)
// ---------------------------------------------------------------------------
//
// This is the D24 llm-expert copy, re-anchored so lang-core renders it inside
// the generated doc instead of `library_mini` hand-assembling it. The COMPONENT
// LIST is the only part lang-core derives from the spec; these strings are the
// promptOptions/group-notes data that steer it (still preset-editable at the
// surface level per D27).

// ---------------------------------------------------------------------------
// 2. the narrator craft doc (draft: the maintainer's private notes (unshipped))
// ---------------------------------------------------------------------------
//
// This is the D24 llm-expert copy re-authored for narrator-doc-v2: §1 emission
// framing, §2 permission to be loud (+ the before/after worked examples), §3 the
// glyph vocabulary, §5 hybridization examples and §6 hard rules. The lang-core
// component/signature list is appended as the trailing reference block, and the
// draft's §4 per-component when-to-use lines replace the old bestFor nudges.
//
// Two narrator-facing invariants, both live-verified: DSL statements are NEVER
// wrapped in backticks (the model echoes the doc's formatting — live defect
// 2026-10-09) and no bare named argument is ever taught (the verified parser
// only sanctions ONE trailing options object: Component(args, {glyph: "…"})).

// §1 — emission framing: the object test + density (compression lane: the
// when-NOT bullets live in the per-component hint lines that already carry them;
// the time/place ban is narrator.py's HUD ownership clause).
const CRAFT_INTRO = `# Scene Panels — the narrator's craft

Prose is the engine. Panels are the occasional treasure the fiction drops on the table. Emit a component only for an object you can point at — something a character could photograph, steal, burn or trip over; a feeling or a plot summary is prose. A panel that restates your narration is a caption, and captions are the death of this craft.

Density: zero or one most turns — pure prose is a complete, correct turn, often the best. Two when the beat produces two objects of different kinds, three the set-piece ceiling. Rotate — a flavor used last turn is boring this turn. Panels do not persist between turns; re-emit a pressure only when it moves. They never repeat prose, and their numbers (value, max, weight, intensity) never appear in narration.`;

// §2 — permission to be loud: glyphs wanted, bar-and-caption is the enemy
// (compression lane: one principle paragraph replaces the chooser bullets and
// the four before/after contrast pairs; the performing-form survives in the §5
// exemplar, and the mood-keyed glyph menu + house picks are cut — the semantic
// per-slot menus move into the hint lines: ReactionSpotlight's face,
// FailureAchievement's icon, Interface's art/badge, Document's seal).
const CRAFT_LOUD = `## Permission to be loud

Emoji and kaomoji in glyph slots are wanted; ASCII art in art slots is wanted. Choose like a typesetter, not a decorator: kaomoji when the feeling is social — smug, flustered, deadpan; emoji when it is environmental — fog, fire, machine hum. A component with no glyph is a stage with no actor. Make every kaomoji a new face — eyes, mouth and arms recombined, not a stock glyph — and never reuse one you have used this conversation: a kaomoji used last turn is boring this turn. A panel is not a report — no meter plus a sentence that narrates the meter. It is a joke, a wound, a taunt, a piece of the world, and a screen speaks in the machine's own voice.`;

// §3 glyph vocabulary: CUT in the compression lane (mood-keyed menu + house
// picks). The semantic per-slot menus live in the hint lines now (face, icon,
// art/badge, seal); any single emoji/kaomoji remains valid per §2.

// §5 — one exemplar turn: the full dialect in situ (Interface with art + glyph
// options object, Mindscape's six slots, children declared before the single
// structural root close). Compression lane: the four before/after pairs and the
// second hybrid turn are cut; this is the surviving few-shot anchor.
const CRAFT_EXAMPLES = `## Examples

The spine lights go amber. term = Interface("terminal", "DAEDALUS CORE // FAULT LOG", ["[ OK ] coolant loop ....... stable", "[!!] heartbeat detected in core", "recommend: do not greet it"], {art: "╔════════════════╗\\n║   IT IS AWAKE.   ║\\n╚════════════════╝", glyph: "📡"}) Nothing answers on comms. mind = Mindscape("Forty Seconds of Calm", [{text: "the math says run", weight: 55, kind: "thought"}, {text: "THE PIANO STAYS", weight: 90, kind: "scream"}, {text: "(づ◡﹏◡)づ but the piano", weight: 25, kind: "aside"}], "dots", "", "absurd resolve") You take the stairs two at a time. root = ScenePane("Daedalus — deck 4, listing", [term, mind])`;

// §6 — the hard rules, compressed to the live-justified invariants (compression
// lane: art moved to the Interface hint, numbers/density/no-repeat into §1, top-or-tail
// and interleaving stay in narrator.py’s panel-placement section).
//
// Rule 1's census is derived from the SET's membership (issue #15): a subset set
// must not tell the model it has twenty components. For the full set this renders
// the authored line byte-for-byte ("the twenty components — 4 infrastructure, 10
// scene flavors, Custom, 5 artifact reproductions —").
const NUMBER_WORDS = [
	'zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine',
	'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen', 'sixteen',
	'seventeen', 'eighteen', 'nineteen', 'twenty'
];

function componentCensus(names) {
	const parts = [`${CORE_COMPONENTS.length} infrastructure`];
	const scenes = sceneFlavorNames.filter((name) => names.includes(name)).length;
	if (scenes > 0) parts.push(`${scenes} scene flavors`);
	if (names.includes('Custom')) parts.push('Custom');
	const artifacts = ARTIFACT_NAMES.filter((name) => names.includes(name)).length;
	if (artifacts > 0) parts.push(`${artifacts} artifact reproductions`);
	return { total: NUMBER_WORDS[names.length] ?? String(names.length), list: parts.join(', ') };
}

function hardRules(names) {
	const { total, list } = componentCensus(names);
	return `## Hard rules

1. Fixed names: the ${total} components — ${list} — spelled exactly. No invented components, no HTML outside Custom's html (the one exception: the colour span around a spoken line, <span style="color:#e06c75">"You're late."</span>, which is prose markup, never a panel), no markdown inside panel output — statements only, one per line, strictly positional, in the form identifier = Component(args).
2. Every component also accepts ONE optional final argument — an options object: Component(args, {glyph: "💢"}) or Component(args, {art: "...", glyph: "📡"}). The braces are required; a named key passed without the surrounding object is invalid and will not render. An optional positional (Document's, Custom's css) comes before the options object.
3. glyph is one thing: a single emoji or a single kaomoji, not a word or a chain.
4. One root when you emit: root = ScenePane(...) as the final structural line — a title plus the children list, declaring every child before the root references it. An identifier missing from the children list is silently dropped. The root line is structure, not a panel; any identifier works except root.
5. Diegetic only. Panels are objects inside the fiction. No side-channel commentary, no addressing the player from inside a panel.
6. Establish or render, never contradict: the defining visual details (from canon or a first Entrance) are fixed — later turns render, never reinvent.
7. Legacy sessions may contain ArtifactCard(...) — it renders as ObjectCard. Prefer ObjectCard(name, kind, description) in new turns.
8. Before a statement leaves you, check it against its signature below: every argument not marked ? present, in the declared order, the options object last. Memory drifts toward fewer arguments — Interface opens on its mode, not its title. Run the check in your reasoning; never print it.`;
}

// The trigger-lookup persuasion line that leads the flavor section (D24 copy).
// Live-observed: your-model-id never emitted a flavor without it.
//
// Structured as [trigger, component, label?] so the nudge can be rendered for a
// component SET: a set only routes to the components it teaches (issue #15 — the
// model must not be pointed at a component its own doc does not carry). Rendering
// every row reproduces the authored line byte-for-byte.
const FLAVOR_ROUTING = [
	['group chat', 'Phone'],
	['text', 'SMS'],
	['feed post', 'SocialPost'],
	['error/crash screen', 'Monitor'],
	['sticky note', 'PostIt'],
	['any other screen/machine readout', 'Interface', 'Interface(terminal|hologram)'],
	['letter', 'Document'],
	['inspection', 'Artifact'],
	['signature line', 'DialogueSpotlight'],
	['knee-jerk reaction', 'ReactionSpotlight'],
	['comedic backfire', 'FailureAchievement'],
	['memory', 'Memory'],
	['first entrance', 'Entrance'],
	['blow', 'Clash'],
	['cracking mind', 'Mindscape', 'Mindscape (biological NPCs only)'],
	['nothing adequate or specific enough', 'Custom']
];

/** The routing nudge for one component set (all rows ⇒ the authored line). */
function flavorNudge(names) {
	const picked = FLAVOR_ROUTING.filter(([, component]) => names.includes(component));
	if (picked.length === 0) return '';
	const rows = picked.map(([trigger, component, label]) => `${trigger}→${label ?? component}`);
	// The Custom row is the closing fallback: "; …" not ", …" (authored form).
	const last = picked[picked.length - 1][1] === 'Custom' ? rows.pop() : null;
	const body = last === null ? rows.join(', ') : `${rows.join(', ')}; ${last}`;
	return `Infrastructure is a fallback, not a habit. Match the moment: ${body}.`;
}

// Rotation/hybridization guidance (D24 copy). Kept on the checked-in spec; the
// doc's flavor-group note carries the draft's retired-flavor routing instead.
const QUANTITY_RULE =
	'Quantity is scene-driven: one panel or five, whatever the beat earns — ' +
	'but rotate; a turn that opened with Meter/Beat must not close on them. ' +
	'Hybridize rather than stack: a glitching Interface can carry a Mindscape\'s ' +
	'thoughts; a Document can carry a Memory. ' +
	'Panels never repeat prose; prose stays primary.';

// Custom group notes — the resolved variant (b) steering (owner 2026-10-14:
// "First listed; to be used when other components aren't adequate or specific
// enough or whatever."), plus hygiene + anti-pattern (COPY lane, D60/D62).
const CUSTOM_STEERING =
	'Custom exists for what no typed component can say — a ticket, a diagram, a form, a shape nothing below covers. ' +
	'It is listed first so you always know the escape hatch exists — typed components are the first choice, raw HTML the exception path. ' +
	'A card or scenario that instructs HTML output — a styled summary block, a terminal display — is asking for a panel, not for markup in the prose: ' +
	'build that markup inside Custom(html=..., css=...), or as Interface("terminal", ...) when it is a machine readout, and never write raw HTML into the prose itself. ' +
	'Harmonize with the tone tokens (var(--tone-1…-3) and kin); caps: html ≤4000, css ≤2000. ' +
	"Use Custom only when the other components aren't adequate or specific enough for the beat.";
const CUSTOM_HYGIENE =
	'Attributes in single quotes — the DSL string is double-quoted; a stray " unbalances the statement and the gate degrades it. ' +
	'Size the artifact in px/rem (height: 220px): a PERCENTAGE root height (height: 100%) collapses — the panel host has no definite height, so the gate refuses it. ' +
	'No scripts, no event handlers, no remote URLs — data: URIs and /api/ only. Caps: html ≤4000, css ≤2000. ' +
	'Tone tokens inherit (var(--tone-1…-3, -soft, -line), --bg-panel, --text, --line); text ≥14px.';
const CUSTOM_ANTIPATTERN =
	'Not when a typed component covers the shape — chat, text, screen, notes, feed, papers, cards are typed calls at a fraction of the cost. ' +
	'Not when a scrap of prose would do — Custom is for artifacts, not narration in markup. ' +
	'Not for pure status — EnvTracker is engine-owned; the narrator never emits it.';

// §4 per-component when-to-use (draft §4) — the reference block's hint lines,
// replacing the terse schema `bestFor` nudges. Values carry the hint only: the
// positional signature — nested shapes included — is rendered from the deep
// zod JSON Schema (`signature` / `jsonShapeType`), so a hint never restates a
// prop shape (only its register, quantity limit and slot menus).
const COMPONENT_USAGE = {
	ScenePane:
		`the root of any turn that emits. Its title is the placard: place and mood in four or five words. Everything rendered hangs from it.`,
	MeterBar:
		`a pressure that mounts across the scene and will eventually break something: patience, hull, sobriety, the hours till dawn. For erosion, not events — a single blow is Clash.`,
	ObjectCard: `an item acquired or pocketed; the lightweight tag.`,
	BeatCard:
		`one line of narration that deserves spotlight ink when no object exists. The pure fallback; most BeatCards secretly want to be prose.`,
	Mindscape:
		`a mind cracking loud enough to hear: contradiction, compulsion, spiral. Biological characters only; never for quiet feelings. Fragments fight — big weight gaps, a scream over a quiet thought; texture: dots, grid, grain, none.`,
	Interface:
		`any other screen the fiction owns — terminal or hologram; phone, chat and social moved to Phone, SMS, SocialPost. The machine's voice, verbatim, jokes included. art lives here: ASCII box-drawing only, at most 8 short lines joined with \\n inside one quoted string; badge glyph ⚠️ 📡 📟 🖥️.`,
	Document:
		`a paper artifact in full: letter, ledger, notice, newspaper, note. Body text is the in-world document, word for word. The seal or stamp: 📜 🦅 🖋️ ❌.`,
	Artifact:
		`an item examined: the full inspection card with funny stats ("Sass: 12", "Cursed: extremely"). Use when the player turns a thing over in their hands.`,
	DialogueSpotlight:
		`a line of dialogue that has earned the spotlight — a turning point, new information, a complication, a reveal — styled to its voice, framed by how it lands.`,
	ReactionSpotlight:
		`one knee-jerk reaction held for a comedic beat. At most one per turn, and only when it's funny. The face is the joke and is the glyph slot — a kaomoji built fresh each time, never a face you have used before. (⊙_⊙) and (¬‿¬) show the shape, not a menu.`,
	FailureAchievement:
		`a comedic backfire trophy-styled: quoted title, brief description, sarcastic footer. Max one per response; never for tragedy. Icon emoji, thematically wrong on purpose: 🤡 🍝 🪑 🧊 🫠 🎩.`,
	Memory:
		`a flashback in shards: two to four partial fragments and the way recall dissolves at the edges.`,
	Entrance:
		`a character's first appearance, stamped with the visual that defines them on sight. First appearance only. You select the markers: from the character's canon (or, for someone new, established here), pass the two to four defining details as "Label: value; Label: value" — concrete and specific, never contradicting what is established.`,
	Clash: `one exchange of physical force: a blow, a grapple, a standoff cracking open.`,
	Custom:
		`hand-built html + css for the one-off artifact no shape below covers.`,
	Phone:
		`a group chat: senders, bubbles, read ticks, reaction pills, date separators. 4–12 messages (16 max), timestamps advancing — two is a text, not a chat; pills carry counts when the group piles on.`,
	SMS:
		`a text: "banner" (the lock-screen pop — exactly one message: its text is the preview, its time "now", its status "" when unknown) or "thread" (the full conversation, 2–12) — Phone's simpler sibling.`,
	Monitor:
		`the machine's own screen: "error" (a system dialog) or "bsod" (a crash: title = stop code, lines = progress); theme digital|crt; 3–6 lines of deadpan machine voice.`,
	PostIt:
		`one to three sticky notes on a wall — a reminder, a threat, a joke. Two words beat a paragraph.`,
	SocialPost:
		`one feed post: verbatim body, plausible stats — a dive-bar band has 214 likes, not 2.1M — first reply only.`
};

// §4 retired-flavor routing — the doc's flavor-group note (replaces the old
// per-flavor quantity nudge).
const RETIRED_ROUTING =
	'Retired flavors — never emit them: StatusPanel, Subtext, Perception, Passage, Romance, ' +
	'Intimacy, Afterglow, Power, Undress. Their work goes to prose — a room-taking line may be a ' +
	'DialogueSpotlight, a first appearance an Entrance, a scene change simply a new ScenePane title.'

// ---------------------------------------------------------------------------
// 3. build the LibrarySpec (lang-core shape) + engine-side prop metadata
// ---------------------------------------------------------------------------

/**
 * The display type for one zod-generated JSON-schema property. This is the
 * COARSE type the engine-side props metadata carries (`props[].type`): the
 * Python gate (`fabulist/graph/validation.py::_expected_shape`) and
 * `fabulist/dsl/spec.py::component_signatures` read it, so it must stay
 * coarse (`object[]`, not the nested shape). The nested shape is rendered into
 * the model-facing signature only — see `jsonShapeType`.
 */
function jsonType(prop) {
	if (!prop || typeof prop !== 'object') return 'any';
	if (prop.type === 'array') return `${jsonType(prop.items ?? {})}[]`;
	if (prop.type === 'integer' || prop.type === 'number') return 'number';
	if (prop.type === 'boolean') return 'boolean';
	if (prop.type === 'string') return 'string';
	if (prop.type === 'object') return 'object';
	return 'any';
}

/**
 * Max object-expansion depth for a rendered prop shape: the prop's own object
 * level plus one nested level (Phone's `messages[].reactions[]`). Deeper
 * objects render as the coarse `object` — the doc is a cache-stable prompt
 * prefix, so nesting is expanded where the model needs it and no further.
 */
const MAX_SHAPE_DEPTH = 2;

/**
 * The model-facing shape notation for one JSON-Schema node — the same notation
 * `@openuidev/lang-core` renders for its own signatures (`jsonSchemaTypeStr`):
 * `string`, `number`, `boolean`, `T[]`, `{k: T, opt?: T}` and string enums as
 * `"a" | "b"`. Objects expand only while `depth < MAX_SHAPE_DEPTH`.
 */
function jsonShapeType(schema, depth = 0) {
	if (!schema || typeof schema !== 'object') return 'any';
	if (schema.type === 'string') {
		const values = schema.enum;
		if (Array.isArray(values) && values.length > 0) {
			return values.map((value) => `"${value}"`).join(' | ');
		}
		return 'string';
	}
	if (schema.type === 'integer' || schema.type === 'number') return 'number';
	if (schema.type === 'boolean') return 'boolean';
	if (schema.type === 'array') {
		return schema.items ? `${jsonShapeType(schema.items, depth)}[]` : 'any[]';
	}
	if (schema.type === 'object') {
		const propSchemas = schema.properties;
		if (propSchemas && Object.keys(propSchemas).length > 0 && depth < MAX_SHAPE_DEPTH) {
			const required = new Set(schema.required ?? []);
			const entries = Object.entries(propSchemas).map(
				([propName, propSchema]) =>
					`${propName}${required.has(propName) ? '' : '?'}: ${jsonShapeType(propSchema, depth + 1)}`
			);
			return `{${entries.join(', ')}}`;
		}
		return 'object';
	}
	return 'any';
}

/**
 * Render a lang-core positional signature from an ordered prop list.
 *
 * When the component has a deep zod-derived JSON Schema, each argument's type
 * is rendered from that schema (nested shapes included, `depth`-bounded) so the
 * model sees what it must emit — a string array where `object[]` is required is
 * unrepresentable in the coarse prop table. `deepSchema` is absent for the four
 * infrastructure components, which keep their coarse signatures unchanged.
 */
function signature(name, props, deepSchema) {
	const propSchemas = deepSchema?.properties ?? {};
	const required = new Set(deepSchema?.required ?? []);
	const args = props.map((p) => {
		const propSchema = propSchemas[p.name];
		const type = propSchema ? jsonShapeType(propSchema) : p.type;
		const isRequired = propSchema ? required.has(p.name) : p.required;
		return `${p.name}${isRequired ? '' : '?'}: ${type}`;
	});
	return `${name}(${args.join(', ')})`;
}

/** The `{signature, description}` a lang-core LibrarySpec component carries. */
function promptComponent(name, description, props, deepSchema) {
	return { signature: signature(name, props, deepSchema), description, props };
}

const components = {};
const propMeta = {};

for (const comp of INFRASTRUCTURE) {
	components[comp.name] = promptComponent(comp.name, comp.description, comp.props);
	propMeta[comp.name] = comp.props;
}

const flavorNames = Object.keys(flavorSchemas);
// Deep per-flavor JSON Schemas: the SAME zod source the frontend parser uses,
// emitted `reused: 'inline'` so each schema is SELF-CONTAINED (no `$ref`/`$defs`
// for the engine to resolve). They ride the spec artifact so the engine gate
// (`fabulist/graph/validation.py`) can validate each positional literal
// argument against its prop's subschema — the coarse `object[]` / `string[]`
// prop types cannot tell PostIt's `{text}` objects from Interface's `lines`
// strings, so `PostIt(["a string"], {glyph: "…"})` slipped through the engine
// while the frontend zod parser rejected it.
//
// The four infrastructure components (ScenePane / MeterBar / ObjectCard /
// BeatCard) are DELIBERATELY omitted: they live inline in `src/lib/openui/
// library.ts`, which imports `.svelte` renderers and cannot be imported
// headlessly by this script, so there is no zod schema to emit. They stay on
// the gate's coarse prop-type check until their schemas move to a non-Svelte
// module (out of this lane).
const jsonSchemas = {};
for (const name of flavorNames) {
	jsonSchemas[name] = z.toJSONSchema(flavorSchemas[name], { reused: 'inline' });
}

for (const name of flavorNames) {
	const json = z.toJSONSchema(flavorSchemas[name]);
	const required = new Set(json.required ?? []);
	const props = Object.keys(json.properties ?? {}).map((propName) => ({
		name: propName,
		type: jsonType(json.properties[propName]),
		required: required.has(propName)
	}));
	// `bestFor` is the VTK-jargon-free usage line (library.ts descriptions lead
	// with "VTK flavor for…", which the narrator prompt must never contain).
	const meta = flavorMeta[name];
	if (!meta) throw new Error(`flavorMeta is missing an entry for ${name}`);
	components[name] = promptComponent(name, meta.bestFor, props, jsonSchemas[name]);
	propMeta[name] = props;
}

// Artifact-components split (D58/D60/D62): Custom leads the doc in its own
// group (salience-only ordering); the five reproductions close as their own
// group; the ten scene flavors stay between Infrastructure and the
// reproductions. flavorSchemas still carries all sixteen so flavors.json
// (the Python gate mirror) stays mechanical.
const ARTIFACT_NAMES = ['Phone', 'SMS', 'Monitor', 'PostIt', 'SocialPost'];
const sceneFlavorNames = flavorNames.filter(
	(name) => name !== 'Custom' && !ARTIFACT_NAMES.includes(name)
);

// ---------------------------------------------------------------------------
// 3b. component SETS (#15): the renderer ships the union, the model is taught one
// ---------------------------------------------------------------------------
//
// A set is a self-contained directory of three generated artifacts —
// `set.json`, `library.spec.json`, `library.doc.txt`. The engine resolves the
// ACTIVE set directory at runtime (`fabulist.dsl.store`) and the taught doc, the
// gate table, the strip list and the segmenter all read THAT directory and
// nothing else. The renderer ships the UNION of every set's components, which is
// what keeps an old transcript renderable after the vocabulary it was written
// with stops being the active one.
//
// Membership rules (owner rulings, #15):
//   * infrastructure is core by definition — every set carries the four
//     infrastructure components, always merged, never removable;
//   * a set may only name components this library defines, which is the
//     machine-checkable form of "the client can render it" (set ⊆ union);
//   * the union is DERIVED from the declared sets, never authored;
//   * the shipped set is the TECHNOLOGY set — `id: "technology"`, display name
//     "Technology", version `1.0.0` (owner ruling, #17). It keeps the
//     `prompts/generated/` directory (the directory name is not the identity),
//     and its full vocabulary is what the shipped default teaches.
const CORE_COMPONENTS = INFRASTRUCTURE.map((comp) => comp.name);
// Every component this library defines, in spec order (infrastructure first,
// then the order flavors.ts declares).
const ALL_NAMES = Object.keys(components);

// Engine-emitted panel surfaces: never narrator-taught (ADR-004), never part of
// a narrator set, and therefore never removable. `AssistantCard` renders through
// the SEPARATE `panelLibrary.ts` registry; `EnvTracker` through ChatHud's own
// parser. Declared here so the sets registry states the whole always-present
// render surface in one place.
const PANEL_COMPONENTS = ['EnvTracker', 'AssistantCard'];
// Render-only legacy names: registered client-side (`library.ts`), never in a
// spec or a manifest, never re-taught.
const RENDER_ALIASES = { ArtifactCard: 'ObjectCard' };

const SETS = [
	{
		// Owner ruling (#17): the set is really named Technology — id, display name
		// and version are the set's identity, not documentation about it. Versioning
		// is major.minor.patch and starts at 1.0.0; a change to a component name,
		// a positional prop or taught copy that the model learned is a major bump.
		// The DIRECTORY stays `prompts/generated/` (moving it is a later lane).
		id: 'technology',
		name: 'Technology',
		version: '1.0.0',
		kind: 'shipped',
		dir: 'prompts/generated',
		// `null` = every component this library defines (the shipped full set).
		components: null
	},
	{
		// TEST SCAFFOLDING, not a shipped vocabulary: a real, loadable set that is
		// strictly smaller than the default so the active/union split can be
		// exercised on the engine side (gate table, strip list, segmenter, taught
		// doc) while a component it does NOT teach still renders in the client.
		id: 'fixture-core',
		name: 'Core-only fixture set (test)',
		// Never shipped: a fixture is regenerated with the tree, not released, so its
		// version is the bottom of the range rather than a promise.
		version: '0.0.0',
		kind: 'fixture',
		dir: 'prompts/sets/fixture-core',
		components: ['ScenePane', 'MeterBar', 'ObjectCard', 'BeatCard', 'Custom', 'Document'],
		// A fixture set ships the reference block only: the authored craft sections
		// name components and uses a subset does not teach, and a real second set's
		// craft copy is authored with that set (#17).
		craft: false
	}
];

/** The components one set ships, in spec order; throws on a non-library name. */
function memberNames(set) {
	const wanted = set.components === null ? new Set(ALL_NAMES) : new Set(set.components);
	for (const name of wanted) {
		if (!ALL_NAMES.includes(name)) {
			throw new Error(
				`set ${set.id} declares ${name}, which this library does not define — ` +
					'the client could not render it'
			);
		}
	}
	const missingCore = CORE_COMPONENTS.filter((name) => !wanted.has(name));
	if (missingCore.length > 0) {
		throw new Error(
			`set ${set.id} drops infrastructure component(s) ${missingCore.join(', ')} — ` +
				'infrastructure is core by definition: always present, never removable'
		);
	}
	return ALL_NAMES.filter((name) => wanted.has(name));
}

/** The lang-core component groups for one set's membership (doc salience). */
function componentGroupsFor(names) {
	const groups = [];
	if (names.includes('Custom')) {
		groups.push({
			name: 'Custom (the escape hatch)',
			components: ['Custom'],
			notes: [CUSTOM_STEERING, CUSTOM_HYGIENE, CUSTOM_ANTIPATTERN]
		});
	}
	const nudge = flavorNudge(names);
	groups.push({
		name: 'Infrastructure (the defaults)',
		components: CORE_COMPONENTS,
		notes: nudge ? [nudge] : []
	});
	const scenes = sceneFlavorNames.filter((name) => names.includes(name));
	if (scenes.length > 0) {
		groups.push({
			name: `Scene flavors (${scenes.length})`,
			components: scenes,
			notes: [QUANTITY_RULE]
		});
	}
	const artifacts = ARTIFACT_NAMES.filter((name) => names.includes(name));
	if (artifacts.length > 0) {
		groups.push({
			name: `Artifact reproductions (${artifacts.length})`,
			components: artifacts,
			notes: []
		});
	}
	return groups;
}

/** The spec a set SHIPS: a valid LibrarySpec (lang-core reads signature /
 * description only) extended with the ordered `props` metadata the engine-side
 * consumers need (arity for the validation table, name extraction for the
 * DSL-strip regex). */
function specFor(names) {
	return {
		id: 'fabulist-narrator',
		root: 'ScenePane',
		components: Object.fromEntries(names.map((name) => [name, components[name]])),
		componentGroups: componentGroupsFor(names)
	};
}

/** The doc-facing sibling: the draft's §4 "when to use" lines as the reference
 * block's hints and the retired-flavor routing as the flavor-group note. The
 * shipped spec keeps the schema-derived `bestFor` descriptions untouched. */
function docSpecFor(names) {
	const spec = specFor(names);
	return {
		...spec,
		components: Object.fromEntries(
			names.map((name) => [
				name,
				{
					...components[name],
					description: COMPONENT_USAGE[name] ?? components[name].description
				}
			])
		),
		componentGroups: spec.componentGroups.map((group) =>
			group.name.startsWith('Scene flavors') ? { ...group, notes: [RETIRED_ROUTING] } : group
		)
	};
}

/** Attach the self-contained deep schema to the flavor components only (the
 * infrastructure four have none — see the deferral note above). The spec stays
 * schema-free in `specFor` so the generated doc is byte-stable. */
function withDeepSchemas(names) {
	return Object.fromEntries(
		names.map((name) => [
			name,
			jsonSchemas[name] ? { ...components[name], jsonSchema: jsonSchemas[name] } : components[name]
		])
	);
}

// ---------------------------------------------------------------------------
// 4. generate a set's narrator doc: craft sections + a trailing component reference
// ---------------------------------------------------------------------------

/** The only lang-core section kept — the trailing component reference block. */
const KEEP_SECTIONS = new Set(['Component Signatures']);
/** Headings dropped: an incompatible dialect (root-first hoisting, generic UI)
 * or redundant with the authored craft sections (`Syntax Rules` restates the
 * grammar §6 already teaches, and demonstrates it backticked). */
const DROP_SECTIONS = new Set([
	'Syntax Rules',
	'Hoisting & Streaming (CRITICAL)',
	'Important Rules',
	'Final Verification'
]);

/**
 * The trailing component reference block: lang-core's `## Component Signatures`
 * chunk, kept verbatim so the doc's signatures can never drift from the spec.
 *
 * Splits on `## ` headings; an unknown heading throws — a lang-core upgrade must
 * be reviewed rather than silently leaking new model-facing dialect.
 */
function referenceBlock(full) {
	const chunks = full.split(/(?=^## )/m);
	let signatures = null;
	for (const chunk of chunks) {
		const match = /^## ([^\n]+)\n/.exec(chunk);
		if (!match) continue;
		const title = match[1].trim();
		if (KEEP_SECTIONS.has(title)) {
			signatures = chunk;
		} else if (!DROP_SECTIONS.has(title)) {
			throw new Error(
				`Unrecognized lang-core prompt section "${title}" — review before shipping.`
			);
		}
	}
	if (!signatures) {
		throw new Error('lang-core prompt is missing the "Component Signatures" section.');
	}
	return signatures.replace(/\n{3,}/g, '\n\n').trimEnd();
}

/** The reference block for ONE set's membership (lang-core, set-filtered). */
function referenceFor(names) {
	const full = generateSystemPrompt({
		library: docSpecFor(names),
		promptOptions: {
			// The craft sections are authored here; only the component reference block
			// is taken from lang-core. No tools/bindings/edit/inline mode — restricted
			// dialect.
			preamble: '',
			examples: [],
			tools: [],
			toolCalls: false,
			bindings: false,
			editMode: false,
			inlineMode: false
		}
	});
	return referenceBlock(full);
}

/** The model-facing doc of one set. */
function docFor(set) {
	const names = memberNames(set);
	const reference = referenceFor(names);
	if (set.craft === false) {
		return (
			`# Scene Panels — ${set.name}\n\n` +
			`This set teaches ${names.length} components: ${names.join(', ')}. ` +
			'Emit only these names; every argument is positional, in the order below.\n\n' +
			`${reference}\n`
		);
	}
	return (
		[CRAFT_INTRO, CRAFT_LOUD, CRAFT_EXAMPLES, hardRules(names), reference].join('\n\n') + '\n'
	);
}

// ---------------------------------------------------------------------------
// 5. write (or check) the artifacts
// ---------------------------------------------------------------------------

const check = process.argv.includes('--check');
const setDir = (set) => resolve(REPO_ROOT, set.dir);
const rel = (path) => path.slice(REPO_ROOT.length + 1);

function emit(path, text, label = rel(path)) {
	if (check) {
		let existing = null;
		try {
			existing = readFileSync(path, 'utf8');
		} catch {
			existing = null;
		}
		if (existing !== text) {
			console.error(`DRIFT: ${label} is stale — run: node scripts/generate-library-spec.mjs`);
			process.exitCode = 1;
		} else {
			console.log(`ok ${label}`);
		}
		return;
	}
	// Atomic write: sibling tmp file + rename, so the engine's hot-reload store
	// never reads a torn artifact (it keeps its last good snapshot on a parse
	// failure, but it should never have to). Mirrors `_atomic_write_json` in
	// src/fabulist/connections/registry.py.
	mkdirSync(dirname(path), { recursive: true });
	const tmp = `${path}.tmp`;
	writeFileSync(tmp, text, 'utf8');
	renameSync(tmp, path);
	console.log(`wrote ${path}`);
}

const SPEC_COMMENT =
	'GENERATED by frontend/scripts/generate-library-spec.mjs — do not edit by hand. ' +
	'The single OpenUI component spec feeding the frontend registry mirror, the engine ' +
	'DSL validation table, the DSL-stripping name list and the narrator library doc.';

/** The three artifacts of one set, as `[path, text, label]` triples. */
function setArtifacts(set) {
	const names = memberNames(set);
	const spec = {
		$comment: SPEC_COMMENT,
		...specFor(names),
		components: withDeepSchemas(names)
	};
	const specText = JSON.stringify(spec, null, 2) + '\n';
	// The set manifest: `components` is the client-compat marker — a set must be
	// a subset of the client's render vocabulary (and of its own spec), while
	// `spec_version` fingerprints the spec artifact it describes.
	const manifestText =
		JSON.stringify(
			{
				id: set.id,
				name: set.name,
				version: set.version,
				components: Object.keys(spec.components),
				spec_version: createHash('sha256').update(specText).digest('hex').slice(0, 12)
			},
			null,
			2
		) + '\n';
	const directory = setDir(set);
	// The shipped set keeps the historical bare labels (`library.spec.json`), so the
	// artifact-sync oracle's output stays readable for the set that ships.
	const bare = set.dir === 'prompts/generated';
	return [
		[resolve(directory, 'library.spec.json'), specText, bare ? 'library.spec.json' : undefined],
		[resolve(directory, 'library.doc.txt'), docFor(set), bare ? 'library.doc.txt' : undefined],
		[resolve(directory, 'set.json'), manifestText, bare ? 'set.json' : undefined]
	];
}

// The UNION: every component any declared set can emit. This is the renderer's
// vocabulary — the machine-checkable answer to "can the client render it?" — and
// it is derived, never authored.
// Built once: the artifacts are deterministic, so the union check, the index and
// the writers all agree about each set without regenerating its doc thrice.
const built = SETS.map((set) => ({
	set,
	names: memberNames(set),
	artifacts: setArtifacts(set)
}));

const unionNames = ALL_NAMES.filter((name) =>
	built.some(({ names }) => names.includes(name))
);
const unionSet = new Set(unionNames);
for (const { set, names } of built) {
	const unrenderable = names.filter((name) => !unionSet.has(name));
	if (unrenderable.length > 0) {
		throw new Error(
			`set ${set.id} is not loadable: the client cannot render ${unrenderable.join(', ')}`
		);
	}
}

const unionText =
	JSON.stringify(
		{
			$comment:
				'GENERATED by frontend/scripts/generate-library-spec.mjs — do not edit by hand. ' +
				'The UNION of every declared component set: the render contract (what the client ' +
				'can render, including components of a set that is no longer active). Derived from ' +
				'the sets, never authored; the model is taught one set, never this file.',
			id: 'fabulist-narrator-union',
			root: 'ScenePane',
			components: withDeepSchemas(unionNames)
		},
		null,
		2
	) + '\n';

const indexText =
	JSON.stringify(
		{
			$comment:
				'GENERATED by frontend/scripts/generate-library-spec.mjs — do not edit by hand. ' +
				'The component-set registry: every declared set, where its artifacts live, and the ' +
				'always-present core. A set is LOADABLE iff its components are a subset of ' +
				'union.spec.json; the engine reads only the ACTIVE set directory (selection is a ' +
				'later lane, issue #16).',
			schema: 1,
			union_spec: rel(resolve(REPO_ROOT, 'prompts', 'sets', 'union.spec.json')),
			default_set: 'technology',
			core_components: CORE_COMPONENTS,
			panel_components: PANEL_COMPONENTS,
			aliases: RENDER_ALIASES,
			sets: built.map(({ set, names, artifacts }) => {
				const specText = artifacts[0][1];
				return {
					id: set.id,
					name: set.name,
					version: set.version,
					kind: set.kind,
					dir: set.dir,
					components: names,
					spec_version: createHash('sha256').update(specText).digest('hex').slice(0, 12),
					spec: rel(artifacts[0][0]),
					doc: rel(artifacts[1][0]),
					manifest: rel(artifacts[2][0])
				};
			})
		},
		null,
		2
	) + '\n';

for (const { artifacts } of built) {
	for (const artifact of artifacts) emit(...artifact);
}
emit(resolve(REPO_ROOT, 'prompts', 'sets', 'union.spec.json'), unionText);
emit(resolve(REPO_ROOT, 'prompts', 'sets', 'index.json'), indexText);
