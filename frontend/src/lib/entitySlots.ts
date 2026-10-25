// Per-kind slot registry (D51 §2.2) ------------------------------------------
//
// One table decides which definition fields the Design Studio renders and with
// which control. This is pure data + types: it imports nothing but the write /
// definition types from `#lib/api` (read-only) so a slot `key` is guaranteed to
// name a real wire field. The generic renderer (`SlotForm.svelte`) is driven
// entirely by these specs, so panes never hardcode a field list.
//
// `key` is the EXACT wire field name (D4: definitions ride through as data).
// `passthrough` slots (`extensions`, `provenance`) ride through untouched and
// are rendered read-only, never as editable inputs. `idlist` carries a
// `string[]` (checkbox list from a catalog); `idlist-single` carries a scalar
// `string` (a `<select>` whose `""` value means "none"). The latter is the
// assistant `actor` slot (D47).
import type { AssistantDefinition, PersonaWrite, World } from '#lib/api';

/** How a slot is rendered and what shape it carries on the wire. */
export type SlotControl =
	| 'text'
	| 'textarea'
	| 'lines'
	| 'number'
	| 'boolean'
	| 'select'
	| 'idlist'
	| 'idlist-single'
	| 'json';

/** One editable (or read-only passthrough) field in a definition body. */
export interface SlotSpec {
	/** Exact wire field name. */
	key: string;
	label: string;
	help?: string;
	control: SlotControl;
	required?: boolean;
	/** Rides through untouched; rendered read-only. */
	passthrough?: boolean;
	/** select only: the allowed static values. */
	options?: readonly string[];
	/** idlist / idlist-single: which idlist catalog the options come from. */
	source?: 'books' | 'personas';
	rows?: number;
	placeholder?: string;
}

/**
 * Compile-time guard: every slot `key` must be a real field of the kind's wire
 * body. Keeps the registry honest without duplicating key unions by hand.
 */
type SlotTable<W> = readonly (SlotSpec & { key: Extract<keyof W, string> })[];

const personaSlots = [
	{ key: 'name', label: 'Name', control: 'text', required: true, placeholder: 'Lyra Voss' },
	{
		key: 'description',
		label: 'Description',
		control: 'textarea',
		rows: 3,
		help: 'Who they are.',
		placeholder: 'Who they are'
	},
	{
		key: 'personality',
		label: 'Personality',
		control: 'textarea',
		rows: 2,
		help: 'How they behave.',
		placeholder: 'How they behave'
	},
	{
		key: 'scenario',
		label: 'Scenario',
		control: 'textarea',
		rows: 2,
		help: 'Where and when they appear.',
		placeholder: 'Where and when they appear'
	},
	{
		key: 'first_mes',
		label: 'First message',
		control: 'textarea',
		rows: 3,
		placeholder: 'Opening line'
	},
	{
		key: 'alternate_greetings',
		label: 'Alternate greetings',
		control: 'lines',
		rows: 2,
		help: 'One greeting per line — swipe openings.',
		placeholder: 'Optional swipe openings'
	},
	{ key: 'book_ids', label: 'Lore books', control: 'idlist', source: 'books' },
	{ key: 'extensions', label: 'Extensions', control: 'json', passthrough: true },
	{ key: 'provenance', label: 'Provenance', control: 'json', passthrough: true }
] satisfies SlotTable<PersonaWrite>;

const worldSlots = [
	{ key: 'name', label: 'Name', control: 'text', required: true, placeholder: 'Harbour district' },
	{ key: 'description', label: 'Description', control: 'textarea', rows: 3, help: 'What the world is.' },
	{ key: 'scenario', label: 'Scenario', control: 'textarea', rows: 2, help: 'The starting situation.' },
	{ key: 'cast', label: 'Cast', control: 'idlist', source: 'personas', help: 'Personas native to this world.' },
	{ key: 'book_ids', label: 'Lore books', control: 'idlist', source: 'books' },
	{ key: 'provenance', label: 'Provenance', control: 'json', passthrough: true }
] satisfies SlotTable<World>;

const assistantSlots = [
	{ key: 'name', label: 'Name', control: 'text', required: true, placeholder: 'Continuity auditor' },
	{
		key: 'persona',
		label: 'Persona',
		control: 'textarea',
		rows: 3,
		required: true,
		help: 'The assistant’s standing instruction.',
		placeholder: 'You keep the timeline honest.'
	},
	{ key: 'style_notes', label: 'Style notes', control: 'textarea', rows: 2 },
	{
		key: 'agent',
		label: 'Model tier',
		control: 'text',
		help: 'Resolved through the connections registry.',
		placeholder: 'narrator'
	},
	{
		key: 'trigger',
		label: 'Trigger',
		control: 'select',
		help: 'The backend accepts only these values.',
		options: ['manual', 'keyword']
	},
	{
		key: 'keywords',
		label: 'Keywords',
		control: 'lines',
		rows: 2,
		help: 'One keyword per line (used when trigger is “keyword”).'
	},
	{
		key: 'actor',
		label: 'Actor',
		control: 'idlist-single',
		source: 'personas',
		help: 'The persona this assistant runs as; “— none —” leaves it unset (D47).'
	}
] satisfies SlotTable<AssistantDefinition>;

/** The slot tables, keyed by the kinds the Design Studio edits. */
export const ENTITY_SLOTS: Record<'persona' | 'world' | 'assistant', SlotSpec[]> = {
	persona: personaSlots,
	world: worldSlots,
	assistant: assistantSlots
};
