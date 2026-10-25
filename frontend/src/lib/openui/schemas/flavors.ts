// VTK (Visual Toolkit) flavor prop schemas — W2.4a; Wave 2a cull (ten survivors);
// artifact-components build 2026-10-14 adds six: Custom (the raw-HTML escape
// hatch, D60) + five artifact reproductions (Phone, SMS, Monitor, PostIt,
// SocialPost — plan the maintainer's private notes (unshipped) v1.2).
//
// Single source of truth for the narrator component schemas
// design review, drawn from the Stabs-EDH preset's "Visual Toolkit (VTK)"
// entry (Tier 4 output-addition directives). The narrator emits
// positional-args OpenUI Lang, so the zod key order below IS the DSL argument
// order. `library.ts` turns each schema into a `defineComponent` and this same
// package is consumed by the narrator-side validation gate.
//
// `flavors.json` is a plain-JSON mirror of this file so the Python gate can
// validate without a JS runtime. It is GENERATED from these schemas by
// `frontend/scripts/gen_flavors_json.mjs` (`npm run gen:flavors`) — never edit
// it by hand; the two must stay in lockstep.
//
// Preset provenance for every flavor: "Visual Toolkit (VTK)" table, plus the
// "Technical Foundation" / "Style and Tone State Integration" subsections
// (dark theme, CSS custom properties, @keyframes motion, font-size >= 14px).

import { z } from 'zod';

// ── 0. Expressive options — the shared trailing optional argument (Wave 2b).
// Verified against @openuidev/lang-core: call syntax is positional-only, so
// expressive slots ride as ONE trailing options object — Component(args,
// {glyph: "💢"}). Bare named syntax (glyph: "💢") is NOT supported by the
// parser (excess-args → payload falls back to raw text). zod key order is
// positional order: `options` must remain the LAST prop of every schema.
export const OptionsSchema = z.object({
	glyph: z
		.string()
		.optional()
		.describe('ONE emoji or kaomoji rendered large as the component visual anchor'),
	art: z
		.string()
		.optional()
		.describe('Multi-line ASCII art (box-drawing charset, <=8 lines), monospace pre-wrap')
});

// ── 1. Mindscape — internal conflict, strong emotion, cognition, memory,
//    altered perception. Biological NPCs only (preset constraint).
export const MindscapeSchema = z.object({
	title: z.string().describe("Heading for the inner world, e.g. 'Cracks in the Facade'"),
	debris: z
		.array(
			z.object({
				text: z.string().describe('One thought fragment, verbatim'),
				weight: z
					.number()
					.describe('Prominence 0-100: drives the fragment size, indent and opacity'),
				kind: z.string().describe("Fragment register: 'thought', 'scream' or 'aside'")
			})
		)
		.describe('Three or more scattered fragments at different weights, contradictions included'),
	texture: z
		.string()
		.describe("Backdrop texture: 'dots', 'grid' or 'grain' ('none' to drop it)"),
	image: z.string().describe('Optional backdrop image URL; pass an empty string for texture only'),
	mood: z.string().describe('Dominant emotional register colouring the mindscape'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type MindscapeProps = z.infer<typeof MindscapeSchema>;

// ── 2. Interface — tech, communication, digital media: terminals, phone UI,
//    social apps, holographic displays.
export const InterfaceSchema = z.object({
	mode: z
		.string()
		.describe(
			"'terminal' or 'hologram' — a styled terminal a card asks for is Interface('terminal', ...), never HTML in prose (phone/chat/social deprecated D59: use Phone / SMS / SocialPost)"
		),
	title: z.string().describe('Window / device / channel label'),
	lines: z.array(z.string()).describe('Ordered log, message or readout lines'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type InterfaceProps = z.infer<typeof InterfaceSchema>;

// ── 3. Document — papers, letters, ledgers, lore: dark paper, forms,
//    newspapers, anchored sticky notes.
export const DocumentSchema = z.object({
	docType: z
		.string()
		.describe("Document kind: 'letter', 'ledger', 'notice', 'newspaper' or 'note'"),
	title: z.string().describe('Document heading or subject line'),
	body: z.string().describe('Body text of the document, verbatim in-world'),
	signature: z
		.string()
		.optional()
		.describe('Optional sign-off, seal or attribution'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type DocumentProps = z.infer<typeof DocumentSchema>;

// ── 4. Artifact — objects worth examining: RPG-style inspection cards with
//    stats, icons and flavour text.
export const ArtifactSchema = z.object({
	name: z.string().describe('Name of the inspected object'),
	kind: z.string().describe("Short category, e.g. 'Relic', 'Weapon', 'Curio'"),
	rarity: z.string().describe("Rarity tier, e.g. 'Common', 'Rare', 'Cursed'"),
	flavor: z.string().describe('One or two sentences of inspection flavour text'),
	stats: z.array(z.string()).describe("Stat lines as 'Label: value' strings"),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type ArtifactProps = z.infer<typeof ArtifactSchema>;

// ── 5. Dialogue Spotlight — a signature line of dialogue styled to the
//    speaker's voice; `tell` is the delivery detail framing it.
export const DialogueSpotlightSchema = z.object({
	speaker: z.string().describe('Who is speaking'),
	line: z.string().describe('The signature line, quoted in its own voice'),
	tell: z.string().describe('Delivery detail that frames the line (how it lands)'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type DialogueSpotlightProps = z.infer<typeof DialogueSpotlightSchema>;

// ── 6. Reaction Spotlight — one enlarged emoji plus one internal thought.
//    Rare: at most ~1 per turn.
export const ReactionSpotlightSchema = z.object({
	face: z.string().describe('A single emoji or glyph for the reaction'),
	thought: z.string().describe('One short internal thought behind the face'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type ReactionSpotlightProps = z.infer<typeof ReactionSpotlightSchema>;

// ── 7. Failure Achievement — comedic failures and social disasters. Trophy
//    container; max one per response; skip for tragic moments (preset rule).
export const FailureAchievementSchema = z.object({
	icon: z.string().describe('Emoji or short glyph for the achievement badge'),
	title: z.string().describe('Punchy achievement title, quoted in the render'),
	description: z.string().describe('Brief description of what went wrong'),
	footer: z.string().describe('Sarcastic one-line footer'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type FailureAchievementProps = z.infer<typeof FailureAchievementSchema>;

// ── 8. Memory — a sepia flashback; `era` is when it happened, `fragments`
//    2-4 remembered shards, `edge` how recall distorts or dissolves.
export const MemorySchema = z.object({
	era: z.string().describe('When the memory is from, e.g. "the summer of ash"'),
	fragments: z
		.array(z.string())
		.describe('2-4 remembered shards, sensory and partial'),
	edge: z.string().describe('How the recall distorts, frays or dissolves at the edges'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type MemoryProps = z.infer<typeof MemorySchema>;

// ── 9. Entrance — a first appearance intro card; `signature` is the defining
//    visual that stamps them. Its options object carries the persona marker
//    cherry-pick (D36): `eyes`/`hair` ride INSIDE the trailing options —
//    Entrance(name, role, signature, {eyes, hair, glyph}) — no positional
//    placeholders, uniform call shape with every other component.
export const EntranceOptionsSchema = OptionsSchema.extend({
	markers: z
		.string()
		.optional()
		.describe('Narrator-selected visual markers as "Label: value; Label: value" — the two to four details that define this character on sight')
});
export const EntranceSchema = z.object({
	name: z.string().describe('Who makes the entrance'),
	role: z.string().describe('Their role or standing in the scene'),
	signature: z.string().describe('The defining visual that stamps them on first sight'),
	options: EntranceOptionsSchema.optional().describe('Trailing optional arg {eyes, hair, glyph, art}; omit when unused')
});
export type EntranceProps = z.infer<typeof EntranceSchema>;

// ── 10. Clash — physical conflict, combat, confrontations: impact frames,
//    motion blur, staged stances, tension indicators, strike emphasis.
export const ClashSchema = z.object({
	attacker: z.string().describe('Who is pressing the attack'),
	defender: z.string().describe('Who is bearing the brunt of it'),
	intensity: z.number().describe('Tension / impact level on a 0..100 scale'),
	note: z.string().describe('Short caption for the exchange'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type ClashProps = z.infer<typeof ClashSchema>;

// ── 11. Custom — the raw-HTML escape hatch (D60; artifact-components §1.1).
//    FIRST-LISTED for salience; used only when no typed component is adequate
//    or specific enough (owner steering, variant b). Gate caps: html <=4000,
//    css <=2000 chars (engine-side, custom_policy.py).
export const CustomSchema = z.object({
	title: z.string().describe('Panel title; also heads the paper-scrap fallback'),
	html: z
		.string()
		.describe(
			'THE home for any rich markup the scene calls for — HTML a card or scenario instructs goes here, never into the prose; single-quoted attributes; size in px/rem (a percentage root height collapses the panel); no scripts/handlers/remote URLs'
		),
	css: z
		.string()
		.optional()
		.describe(
			'Style channel for the html above (<=2000 chars); tone tokens var(--tone-1..-3, kin) inherit'
		),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}; omit when unused')
});
export type CustomProps = z.infer<typeof CustomSchema>;

// ── 12. Phone — a group chat on a messaging app (artifact-components §1.2;
//    whatsapp-like STRUCTURE, own palette, likeness never 1:1).
export const PhoneMessageSchema = z.object({
	from: z.string().describe('Sender name as the group knows them'),
	text: z.string().describe('Message text, verbatim'),
	time: z.string().describe("Timestamp like '20:58'"),
	status: z.string().describe("Delivery state: 'sent', 'delivered', 'read' or ''"),
	reactions: z
		.array(
			z.object({
				emoji: z.string().describe('One emoji'),
				count: z.number().optional().describe('Pile-on count 1-999; omit for a bare pill')
			})
		)
		.optional()
		.describe('Reaction pills pinned to the bubble bottom edge'),
	day: z
		.string()
		.optional()
		.describe("Day label like 'Today'; renders a date separator when it changes")
});
export const PhoneOptionsSchema = OptionsSchema.extend({
	me: z.string().optional().describe('The right-aligned sender; gets delivery ticks'),
	typing: z.string().optional().describe('Name shown in the typing indicator and header subtitle'),
	clock: z.string().optional().describe('Status-bar time from the narrative clock'),
	battery: z.number().optional().describe('Battery percent 0-100, decorative (default 87)')
});
export const PhoneSchema = z.object({
	thread: z.string().describe('Contact or group name in the header'),
	messages: z
		.array(PhoneMessageSchema)
		.min(1)
		.max(16)
		.describe('The conversation, 4-12 typical, 16 max; timestamps advance'),
	options: PhoneOptionsSchema.optional().describe('Trailing optional arg {me, typing, clock, battery, glyph, art}')
});
export type PhoneProps = z.infer<typeof PhoneSchema>;

// ── 13. SMS — a text message: lock-screen banner or full thread (§1.3).
export const SMSOptionsSchema = OptionsSchema.extend({
	me: z.string().optional().describe('The right-aligned sender in thread mode')
});
export const SMSSchema = z.object({
	mode: z.string().describe('"banner" (lock-screen notification, exactly one message) or "thread" (full conversation)'),
	title: z.string().describe('Sender/app title (banner) or sender name (thread)'),
	messages: z
		.array(PhoneMessageSchema)
		.min(1)
		.max(12)
		.describe('banner: exactly one; thread: 2-12'),
	options: SMSOptionsSchema.optional().describe('Trailing optional arg {me, glyph, art}')
});
export type SMSProps = z.infer<typeof SMSSchema>;

// ── 14. Monitor — the machine's own screen: error dialog or BSOD (§1.4).
export const MonitorOptionsSchema = OptionsSchema.extend({
	theme: z.string().optional().describe("'digital' (flat, default) or 'crt' (phosphor + scanlines)")
});
export const MonitorSchema = z.object({
	mode: z.string().describe('"error" (system dialog) or "bsod" (full crash screen; title = stop code)'),
	title: z.string().describe('Dialog title or stop code'),
	lines: z.array(z.string()).max(12).describe('3-6 typical; dialog body or crash progress lines'),
	options: MonitorOptionsSchema.optional().describe('Trailing optional arg {theme, glyph, art}')
});
export type MonitorProps = z.infer<typeof MonitorSchema>;

// ── 15. PostIt — one to three sticky notes on a wall (§1.5; italic pen).
export const PostItSchema = z.object({
	notes: z
		.array(z.object({ text: z.string().describe('Note text — two words beat a paragraph') }))
		.min(1)
		.max(3)
		.describe('1-3 notes; the count is the model\u2019s choice'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}')
});
export type PostItProps = z.infer<typeof PostItSchema>;

// ── 16. SocialPost — one feed post + stats + first reply only (§1.6).
export const SocialPostSchema = z.object({
	author: z.string().describe('Display name'),
	handle: z.string().describe('Handle without @ (the component prefixes it)'),
	body: z.string().describe('Post body, verbatim'),
	stats: z
		.object({
			likes: z.number().min(0).max(9999999).describe('Plausible like count'),
			reposts: z.number().min(0).max(9999999).describe('Plausible repost count'),
			comments: z.number().min(0).max(9999999).describe('Plausible comment count')
		})
		.describe('Engagement counts — believable for who this is'),
	reply: z
		.object({
			author: z.string().describe('Replier display name'),
			handle: z.string().describe('Replier handle without @'),
			text: z.string().describe('Reply text, verbatim — the FIRST reply only')
		})
		.optional()
		.describe('The first reply only; omit for post-only'),
	options: OptionsSchema.optional().describe('Trailing optional arg {glyph, art}')
});
export type SocialPostProps = z.infer<typeof SocialPostSchema>;

/** Every VTK flavor schema, keyed by its OpenUI component name.
 *
 * Declaration order is the order the model-facing library doc lists flavors in
 * and the order `flavors.json` mirrors; keep it aligned with the doc copy. */
export const flavorSchemas = {
	Mindscape: MindscapeSchema,
	Interface: InterfaceSchema,
	Document: DocumentSchema,
	Artifact: ArtifactSchema,
	DialogueSpotlight: DialogueSpotlightSchema,
	ReactionSpotlight: ReactionSpotlightSchema,
	FailureAchievement: FailureAchievementSchema,
	Memory: MemorySchema,
	Entrance: EntranceSchema,
	Clash: ClashSchema,
	Custom: CustomSchema,
	Phone: PhoneSchema,
	SMS: SMSSchema,
	Monitor: MonitorSchema,
	PostIt: PostItSchema,
	SocialPost: SocialPostSchema
} as const;

export type FlavorName = keyof typeof flavorSchemas;

/** Preset provenance + guidance, mirrored into flavors.json for the gate.
 *
 * `docHint` is the compact trailing clause the model-facing library doc shows
 * after the argument signature (`- Memory(era, fragments[], edge) — <docHint>`).
 * Keeping it here means the doc and the schema can never drift apart. */
export const flavorMeta: Record<
	FlavorName,
	{
		presetSection: string;
		presetEntry: string;
		bestFor: string;
		exampleElements: string;
		rules: string[];
		docHint: string;
	}
> = {
	Mindscape: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Mindscape',
		bestFor: 'Internal conflict, strong emotions, impulses, breakdowns, cognition, memories, altered perception',
		exampleElements:
			'Scattered mental debris of emoji/kaomoji/ASCII, contradictory thoughts, texture backgrounds; flashbacks use fragmented sepia treatment; hallucination uses warping/chromatic aberration',
		rules: ['Biological NPCs only', 'Never repeat prose content in visuals'],
		docHint: 'inner conflict; debris[] {text, weight, kind}.'
	},
	Interface: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Interface',
		bestFor: 'Tech, communication, digital media',
		exampleElements: 'Phone UI, terminal windows, social apps, holographic displays',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'machine screens; a styled terminal is Interface(terminal), not HTML in prose.'
	},
	Document: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Document',
		bestFor: 'Papers, letters, ledgers, lore',
		exampleElements:
			'Dark paper, ledgers, handwritten notes (stickynotes anchored), official forms, newspapers',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'letter, ledger, notice; body verbatim.'
	},
	Artifact: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Artifact',
		bestFor: 'Objects worth examining',
		exampleElements: 'RPG-style inspection cards with funny stats, icons, flavor text',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'inspection card.'
	},
	DialogueSpotlight: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Dialogue Spotlight',
		bestFor: 'A distinctive line of dialogue that should be styled to its voice',
		exampleElements: 'Quote-set typography, oversized glyphs, a signature line with a delivery tell',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'a signature line styled to its voice.'
	},
	ReactionSpotlight: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Reaction Spotlight',
		bestFor: 'One knee-jerk reaction, held for a beat',
		exampleElements: 'One enlarged emoji, one internal thought, staged comedic timing',
		rules: ['Rare: at most ~1 per turn', 'Never repeat prose content in visuals'],
		docHint: 'one emoji, one thought; rare.'
	},
	FailureAchievement: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Failure Achievement',
		bestFor: 'Comedic failures, backfires, embarrassment, social disasters',
		exampleElements:
			'Trophy container in the Style State palette: themed icon, punchy quoted title, brief description, sarcastic footer',
		rules: ['Max 1 per response', 'Skip for tragic moments'],
		docHint: 'trophy; max 1, skip tragedy.'
	},
	Memory: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Memory',
		bestFor: 'A flashback or recalled moment that distorts or dissolves',
		exampleElements: 'Sepia gradient treatment, dissolved/blurred edges, film-grain texture',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'recall that distorts or dissolves.'
	},
	Entrance: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Entrance',
		bestFor: 'A first appearance intro card',
		exampleElements: 'Cinematic card with name treatment and the signature visual that stamps them',
		rules: ['First appearance only', 'Never repeat prose content in visuals'],
		docHint: 'first appearance.'
	},
	Clash: {
		presetSection: 'Visual Toolkit (VTK)',
		presetEntry: 'Clash',
		bestFor: 'Physical conflict, combat, confrontations',
		exampleElements:
			'Impact frames, motion blur, staged stances, tension indicators, strike emphasis',
		rules: ['Never repeat prose content in visuals'],
		docHint: 'one exchange, 0-100 impact.'
	},
	Custom: {
		presetSection: 'Escape hatch (D60, artifact-components §1.1)',
		presetEntry: 'Custom',
		bestFor:
			'Hand-built html + css for the one-off artifact no typed shape covers; the home for any markup a card or scenario instructs',
		exampleElements: 'Tickets, diagrams, forms, tables — tone tokens inherit; single-quoted attributes',
		rules: [
			'First-listed for salience only — typed components stay the first choice',
			'Caps gate-enforced: html <=4000, css <=2000',
			'No scripts, no event handlers, no remote URLs',
			'Instructed HTML goes inside Custom(html=...), never raw into the prose'
		],
		docHint: 'the escape hatch — instructed HTML lives here, never in prose.'
	},
	Phone: {
		presetSection: 'Artifact reproductions (artifact-components §1.2)',
		presetEntry: 'Phone',
		bestFor: 'A group chat on a messaging app: named senders, bubbles, ticks, reactions',
		exampleElements:
			'Status bar with narrative clock, contact header, E2EE line, date separators, reaction pills with counts',
		rules: ['Likeness structural, never 1:1', 'Never repeat prose content in visuals'],
		docHint: 'group chat; pills carry counts.'
	},
	SMS: {
		presetSection: 'Artifact reproductions (artifact-components §1.3)',
		presetEntry: 'SMS',
		bestFor: 'A text message: lock-screen banner or full thread',
		rules: ['Likeness structural, never 1:1', 'Never repeat prose content in visuals'],
		exampleElements: 'Banner: app icon, title, preview, now. Thread: bubbles, ticks, timestamps',
		docHint: 'banner or thread; Phone\u2019s simpler sibling.'
	},
	Monitor: {
		presetSection: 'Artifact reproductions (artifact-components §1.4)',
		presetEntry: 'Monitor',
		bestFor: 'The machine\u2019s own screen: error dialog or BSOD, digital or CRT',
		exampleElements: 'System dialog with dead OK; full crash screen with stop code; phosphor scanlines',
		rules: ['CRT phosphor + BSOD blue are the only artifact-inherent literals'],
		docHint: 'error or bsod; digital or crt.'
	},
	PostIt: {
		presetSection: 'Artifact reproductions (artifact-components §1.5)',
		presetEntry: 'PostIt',
		bestFor: 'One to three sticky notes on a wall',
		exampleElements: 'Tilted overlapping squares, italic pen, sticky yellow warmed to theme',
		rules: ['1-3 notes, the model\u2019s choice'],
		docHint: 'sticky notes; two words beat a paragraph.'
	},
	SocialPost: {
		presetSection: 'Artifact reproductions (artifact-components §1.6)',
		presetEntry: 'SocialPost',
		bestFor: 'One feed post with plausible stats and the first reply only',
		exampleElements: 'Avatar, name + handle, body, engagement row, one nested reply',
		rules: ['First reply only, never a thread', 'Stats believable for who this is'],
		docHint: 'one post + first reply; plausible counts.'
	}
};
