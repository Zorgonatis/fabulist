// Design Studio kind registry (D51 §2.3).
//
// The studio's kinds used to be mirrored in four places: the shell's own union
// plus its `KINDS` / `TAB_LABELS` / `PANE_LABELS` tables, the rail footer's map
// (five kinds), the drawer's door type (the same five) and the page's studio
// request type (eight). Six kinds — flow, proposals, queries, catalog,
// templates, planner — therefore had NO door from the rail, and a new kind had
// to be added four times to stay reachable.
//
// This module is the ONE spelling. Pure data + types: it imports nothing, so any
// component, store or test can read it without pulling in a surface.
//
// ORDER is the shell's tab order and is not up for reordering: `flow` is the
// landing kind (R11: the graph is the Studio's home) and `proposals` is the
// review home's studio half (W9.22 §4) sitting beside it, not among the
// definition editors. The three GROUPS are contiguous slices of that order, so
// the grouped presentation (both the shell's tab strip and the rail's footer
// map) never changes where a kind lives.

/** Top-level band a kind belongs to — the caption above its chips/pills. */
export type StudioGroup = 'home' | 'assets' | 'engine';

/** Every pane the shell can show. */
export type StudioKind =
	| 'flow'
	| 'proposals'
	| 'persona'
	| 'world'
	| 'book'
	| 'assistant'
	| 'profile'
	| 'queries'
	| 'catalog'
	| 'templates'
	| 'planner';

/** One kind: its id, its two labels and the group it renders under. */
export interface StudioKindSpec {
	kind: StudioKind;
	/** Tab-strip pill label — plural where the pane lists rows. */
	tab: string;
	/** Pane title / fallback label — singular where the pane edits one row. */
	pane: string;
	group: StudioGroup;
}

/** The groups, in render order. */
export const STUDIO_GROUPS: readonly { group: StudioGroup; label: string }[] = [
	{ group: 'home', label: 'Home' },
	{ group: 'assets', label: 'Assets' },
	{ group: 'engine', label: 'Engine' }
];

/** Every kind, in shell tab order. `group` is a contiguous slice of this array. */
export const STUDIO_KINDS: readonly StudioKindSpec[] = [
	{ kind: 'flow', tab: 'Flow', pane: 'Flow', group: 'home' },
	{ kind: 'proposals', tab: 'Proposals', pane: 'Proposals', group: 'home' },
	{ kind: 'persona', tab: 'Personas', pane: 'Persona', group: 'assets' },
	{ kind: 'world', tab: 'Worlds', pane: 'World', group: 'assets' },
	{ kind: 'book', tab: 'Books', pane: 'Book', group: 'assets' },
	{ kind: 'assistant', tab: 'Assistants', pane: 'Assistant', group: 'assets' },
	{ kind: 'profile', tab: 'Profiles', pane: 'Profile', group: 'assets' },
	{ kind: 'queries', tab: 'Queries', pane: 'Queries', group: 'engine' },
	{ kind: 'catalog', tab: 'Catalog', pane: 'Catalog', group: 'engine' },
	{ kind: 'templates', tab: 'Templates', pane: 'Templates', group: 'engine' },
	{ kind: 'planner', tab: 'Planner', pane: 'Planner', group: 'engine' }
];

/** The kinds of one group, in registry order. */
export function kindsInGroup(group: StudioGroup): StudioKindSpec[] {
	return STUDIO_KINDS.filter((spec) => spec.group === group);
}

/** The spec for one kind. Throws only on a kind the registry does not carry. */
export function studioKindSpec(kind: StudioKind): StudioKindSpec {
	const spec = STUDIO_KINDS.find((item) => item.kind === kind);
	if (!spec) throw new Error(`unknown studio kind: ${kind}`);
	return spec;
}

/** Type guard for serialized strings (the shell's localStorage restore). */
export function isStudioKind(value: unknown): value is StudioKind {
	return typeof value === 'string' && STUDIO_KINDS.some((spec) => spec.kind === value);
}
