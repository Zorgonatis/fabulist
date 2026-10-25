// Assistant card prop schema (docs/UI-LANGUAGE.md §6) — the side-channel
// register: a commentary/margin frame that talks to <USER> from OUTSIDE the
// fiction, never a diegetic document.
//
// The narrator emits positional-args OpenUI Lang, so the zod key order below IS
// the DSL argument order: AssistantCard(avatar, name, role, mood, commentary,
// options, footer).
//
// Deliberately NOT part of `schemas/flavors.ts`: that file is the VTK flavor
// table, mirrored to `flavors.json` for the Python-side validation gate. This is
// a single named surface with its own registry entry (`library.ts`, owned by
// the orchestrator), so it needs no generated JSON mirror.

import { z } from 'zod';

// One suggested reply. `emphasis` marks the single option the card draws with a
// STRUCTURAL emphasis (heavier frame, bolder text, `▶` marker) — never by hue
// (docs/UI-LANGUAGE.md §2).
export const AssistantOptionSchema = z.object({
	text: z.string().describe('Sample dialogue or action, quoted as the user would send it'),
	tag: z.string().describe("Short descriptor, e.g. 'bold', 'deflect', 'inspect'"),
	note: z.string().optional().describe('Optional in-voice rationale for this option'),
	emphasis: z
		.boolean()
		.optional()
		.describe('True for the one option to emphasise; omit on every other option')
});
export type AssistantOption = z.infer<typeof AssistantOptionSchema>;

export const AssistantCardSchema = z.object({
	avatar: z
		.string()
		.optional()
		.describe('Avatar image URL; omit or leave empty to render initials from `name`'),
	name: z.string().describe('Assistant persona name'),
	role: z.string().describe('Subtitle / role label under the name'),
	mood: z.string().describe('One or two mood emoji reacting to the current beat'),
	commentary: z
		.string()
		.describe('The persona speaking to the user, out of fiction; may address <USER>'),
	options: z
		.array(AssistantOptionSchema)
		.min(3)
		.max(6)
		.describe('3-6 suggested replies, the model chooses how many fit the turn'),
	footer: z.string().optional().describe('Optional flavour line under the options')
});
export type AssistantCardProps = z.infer<typeof AssistantCardSchema>;
