// Fabulist API client - prompt manager (D18).

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';
import type { PromptDirective } from './types';

export type { PromptDirective } from './types';

/** Structural module kinds. `text` is a free-template block; the others are
 * inserters the engine fills from live state. Which structural kinds are valid
 * depends on the interaction flow (D18): narrate uses `narrator_system` /
 * `library` / `history` / `facts` / `user`; the other flows use `system` /
 * `history` / `facts` / `user` / `turn_payload` / `payload` per their contract. */
export type PromptModuleType =
	| 'text'
	| 'narrator_system'
	| 'library'
	| 'history'
	| 'facts'
	| 'user'
	| 'recalled'
	| 'system'
	| 'turn_payload'
	| 'payload';


/** The D18 interaction flows a preset carries. `narrate` is the canonical flow
 * (its module list also mirrors the legacy `modules` field). Order mirrors the
 * engine's `fabulist.prompts.modules.INTERACTIONS` (`bts` landed Wave 6;
 * `critic` landed W6.3; `npc` W9.13, `planner` W9.20 and `proposals` W9.22 came
 * later and were missing here until W9.24 — a flow missing from this list is a
 * prompt surface the drawer silently cannot open, which is exactly what the
 * flow manifest's `step_editor` doors point at; `attribution` was removed with
 * its model flow — the pass is deterministic engine code now). */
export const PROMPT_FLOWS = [
	'narrate',
	'ooc',
	'extract',
	'summarize',
	'caption',
	'assistant',
	'bts',
	'critic',
	'npc',
	'planner',
	'proposals'
] as const;

export type PromptFlow = (typeof PROMPT_FLOWS)[number];


/** Chat role a module renders as (`text` modules only; inserters fix their own). */
export type PromptRole = 'system' | 'user' | 'assistant';


/** One runtime-editable prompt block. */
export interface PromptModule {
	id: string;
	name: string;
	type: PromptModuleType;
	role: PromptRole;
	content: string;
	enabled: boolean;
}


/** One regex stage row inside the D27 `surfaces.regexes` list. Shape mirrors
 * the backend `surfaces.serialize_regex` (canonical field order). */
export interface PromptRegexRow {
	id: string;
	name: string;
	find: string;
	replace: string;
	flags: string;
	/** Apply only to the prompt projection (not the display history). */
	prompt_only: boolean;
	min_depth: number;
	enabled: boolean;
}


/** The `<recalled>` framing copy + section headers (D27 surface 2). */
export interface PromptRecalledSurfaces {
	framing?: string;
	open?: string;
	close?: string;
	headers?: {
		recall?: string;
		world_info?: string;
		recap?: string;
		bts?: string;
	};
}


/** History projection knobs (D27 surface 4). */
export interface PromptHistorySurfaces {
	depth?: number;
	strip_dsl?: boolean;
}


/** The D27 `surfaces` block: every formerly code-only token surface, carried by
 * the preset and authored with `{{expression}}` support. Absent/empty keys fall
 * back to the code constants (byte-parity), so `{}` behaves like pre-D27. */
export interface PromptSurfaces {
	library_doc?: string;
	recalled?: PromptRecalledSurfaces;
	regexes?: PromptRegexRow[];
	history?: PromptHistorySurfaces;
	directives?: Record<string, string>;
}


/** A named preset: ordered modules + toggles + overrides + agent bindings. */
export interface PromptPreset {
	preset_id: string;
	name: string;
	/** Legacy/canonical narrate flow (mirrors `flows.narrate`). */
	modules: PromptModule[];
	/** Per-interaction module lists (D18). `flows.narrate` mirrors `modules`. */
	flows?: Record<string, PromptModule[]>;
	toggles: Record<string, boolean>;
	prompt_overrides: Record<string, string>;
	agent_bindings: Record<string, string>;
	/** D27 token surfaces (library doc / recalled / regexes / history / directives). */
	surfaces?: PromptSurfaces;
	/** Ordered directive rows (W3). Absent/empty = inherit the factory catalog;
	 * a non-empty list replaces it wholesale (order = render order). */
	directives?: PromptDirective[];
	version: number | string;
}


/** One `{{name}}` variable the inserter exposes (documented for the editor). */
export interface PromptVariableSpec {
	name: string;
	description: string;
	example: string;
}


/** One prompt-manager STEP (`GET /api/prompts/steps`, W6.4).
 *
 * The step catalog is display meta authored server-side
 * (`fabulist.prompts.catalog`): a plain-language `title`, a one-line `blurb`,
 * the engine-facing `trigger` timing, the `default_agent` and the structural
 * types valid for the step's flow. The per-preset fields (`bound_agent`,
 * `block_count`, `enabled`, `system_source`) describe the ACTIVE preset at read
 * time; the drawer recomputes them from the draft it is editing so edits pull
 * through immediately. */
export interface PromptStep {
	id: string;
	title: string;
	blurb: string;
	trigger: string;
	default_agent: string | null;
	bound_agent: string | null;
	structural_types: string[];
	block_count: number;
	enabled: boolean;
	system_source: 'authored' | 'engine';
}


/** One rendered message from the preview endpoint. */
export interface PromptPreviewMessage {
	role: string;
	content: string;
}


/** Result of `POST /api/prompts/preview`. `preview_mode` names the render path
 * and is non-empty when the preview is degraded (e.g. no active preset). */
export interface PromptPreview {
	messages: PromptPreviewMessage[];
	chars: number;
	preview_mode: string;
}


/** Prompt endpoints, kept beside `ENDPOINTS` so the orchestrator can rewire them. */
export const PROMPT_ENDPOINTS = {
	presets: () => `${API_BASE}/api/prompts/presets`,
	preset: (pid: string) => `${API_BASE}/api/prompts/presets/${encodeURIComponent(pid)}`,
	activate: (pid: string) =>
		`${API_BASE}/api/prompts/presets/${encodeURIComponent(pid)}/activate`,
	deactivate: () => `${API_BASE}/api/prompts/presets/deactivate`,
	active: () => `${API_BASE}/api/prompts/active`,
	default: () => `${API_BASE}/api/prompts/default`,
	variables: () => `${API_BASE}/api/prompts/variables`,
	steps: () => `${API_BASE}/api/prompts/steps`,
	factoryDirectives: () => `${API_BASE}/api/prompts/directives`,
	exportPreset: (pid: string) =>
		`${API_BASE}/api/prompts/presets/${encodeURIComponent(pid)}/export`,
	importPreset: () => `${API_BASE}/api/prompts/presets/import`,
	seedFromDefault: () => `${API_BASE}/api/prompts/seed-from-default`,
	defaultReset: () => `${API_BASE}/api/prompts/default/reset`,
	defaultCommit: () => `${API_BASE}/api/prompts/default/commit`,
	preview: () => `${API_BASE}/api/prompts/preview`
} as const;


const PROMPT_MODULE_TYPES: readonly PromptModuleType[] = [
	'text',
	'narrator_system',
	'library',
	'history',
	'facts',
	'user',
	'recalled',
	'system',
	'turn_payload',
	'payload'
];

const PROMPT_ROLES: readonly PromptRole[] = ['system', 'user', 'assistant'];

/** Coerce one raw module row into the UI shape (missing fields defaulted). */
function normalizePromptModule(raw: unknown): PromptModule | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = str(obj.id);
	if (!id) return null;
	const type = PROMPT_MODULE_TYPES.includes(obj.type as PromptModuleType)
		? (obj.type as PromptModuleType)
		: 'text';
	const role = PROMPT_ROLES.includes(obj.role as PromptRole)
		? (obj.role as PromptRole)
		: 'system';
	return {
		id,
		name: str(obj.name, id),
		type,
		role,
		content: str(obj.content),
		enabled: obj.enabled !== false
	};
}


/** Coerce a raw module array into the UI shape (dropping malformed rows). */
function normalizePromptModules(raw: unknown): PromptModule[] {
	if (!Array.isArray(raw)) return [];
	return raw
		.map((m) => normalizePromptModule(m))
		.filter((m): m is PromptModule => m !== null);
}


/** Coerce one raw directive row into the UI shape (missing fields defaulted,
 * mirroring the engine's tolerant `parts.normalize_directive_rows`). Rows
 * without an id are dropped — every backend-sourced row mints one. */
export function normalizePromptDirective(raw: unknown): PromptDirective | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = str(obj.id);
	if (!id) return null;
	const num = (value: unknown, fallback: number): number =>
		typeof value === 'number' && Number.isFinite(value) ? value : fallback;
	return {
		id,
		name: str(obj.name, id),
		content: str(obj.content),
		priority: num(obj.priority, 50),
		tier: num(obj.tier, 2),
		core: obj.core === true,
		protected: obj.protected === true,
		enabled: obj.enabled !== false,
		description: str(obj.description)
	};
}


/** Coerce a raw directive array into the UI shape (dropping malformed rows). */
export function normalizePromptDirectives(raw: unknown): PromptDirective[] {
	if (!Array.isArray(raw)) return [];
	return raw
		.map((row) => normalizePromptDirective(row))
		.filter((row): row is PromptDirective => row !== null);
}


/** Coerce one raw preset row into the UI shape. */
export function normalizePromptPreset(raw: unknown): PromptPreset | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const preset_id = str(obj.preset_id) || str(obj.id);
	if (!preset_id) return null;
	let modules = normalizePromptModules(obj.modules);
	const asStringMap = (value: unknown): Record<string, string> => {
		const out: Record<string, string> = {};
		if (value && typeof value === 'object') {
			for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
				if (typeof val === 'string') out[key] = val;
			}
		}
		return out;
	};
	// Per-interaction flows (D18). `flows.narrate` and the legacy `modules`
	// field are two views of one list: merge BOTH directions so a payload that
	// carries only one of them still yields a consistent preset.
	const flows: Record<string, PromptModule[]> = {};
	if (obj.flows && typeof obj.flows === 'object') {
		for (const [flow, value] of Object.entries(obj.flows as Record<string, unknown>)) {
			flows[flow] = normalizePromptModules(value);
		}
	}
	const narrate = flows['narrate'];
	if (narrate && narrate.length > 0) {
		// flows.narrate is authoritative when populated; mirror it into modules.
		modules = narrate.map((m) => ({ ...m }));
	} else if (modules.length > 0) {
		// Only the legacy field was populated; mirror it into flows.narrate.
		flows['narrate'] = modules.map((m) => ({ ...m }));
	} else if (!narrate) {
		// Neither view was present: keep both present and empty.
		flows['narrate'] = [];
	}
	return {
		preset_id,
		name: str(obj.name, preset_id),
		modules,
		flows,
		toggles: valueAsRecord(obj.toggles),
		prompt_overrides: asStringMap(obj.prompt_overrides),
		agent_bindings: asStringMap(obj.agent_bindings),
		surfaces: normalizePromptSurfaces(obj.surfaces),
		directives: normalizePromptDirectives(obj.directives),
		version: typeof obj.version === 'number' || typeof obj.version === 'string' ? obj.version : 0
	};
}


/** Coerce one raw regex row into the canonical UI shape. */
function normalizePromptRegex(raw: unknown): PromptRegexRow | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	return {
		id: str(o.id) || `regex_${Math.random().toString(36).slice(2, 8)}`,
		name: str(o.name),
		find: str(o.find),
		replace: str(o.replace),
		flags: str(o.flags),
		prompt_only: o.prompt_only !== false,
		min_depth:
			typeof o.min_depth === 'number' && Number.isFinite(o.min_depth) ? o.min_depth : 0,
		enabled: o.enabled !== false
	};
}


/** Tolerantly normalize a D27 `surfaces` block (unknown shapes drop to fallback).
 * Always returns an object so callers can read/destructure safely. */
export function normalizePromptSurfaces(raw: unknown): PromptSurfaces {
	const out: PromptSurfaces = {};
	if (!raw || typeof raw !== 'object') return out;
	const o = raw as Record<string, unknown>;
	if (typeof o.library_doc === 'string') out.library_doc = o.library_doc;

	const recalled = o.recalled;
	if (recalled && typeof recalled === 'object') {
		const r = recalled as Record<string, unknown>;
		const rec: PromptRecalledSurfaces = {};
		for (const key of ['framing', 'open', 'close'] as const) {
			if (typeof r[key] === 'string') rec[key] = r[key] as string;
		}
		if (r.headers && typeof r.headers === 'object') {
			const h = r.headers as Record<string, unknown>;
			const headers: NonNullable<PromptRecalledSurfaces['headers']> = {};
			for (const key of ['recall', 'world_info', 'recap', 'bts'] as const) {
				if (typeof h[key] === 'string') headers[key] = h[key] as string;
			}
			if (Object.keys(headers).length > 0) rec.headers = headers;
		}
		if (Object.keys(rec).length > 0) out.recalled = rec;
	}

	if (Array.isArray(o.regexes)) {
		out.regexes = o.regexes
			.map((entry) => normalizePromptRegex(entry))
			.filter((row): row is PromptRegexRow => row !== null);
	}

	const history = o.history;
	if (history && typeof history === 'object') {
		const h = history as Record<string, unknown>;
		const knobs: PromptHistorySurfaces = {};
		if (typeof h.depth === 'number' && Number.isFinite(h.depth)) knobs.depth = h.depth;
		if (typeof h.strip_dsl === 'boolean') knobs.strip_dsl = h.strip_dsl;
		if (Object.keys(knobs).length > 0) out.history = knobs;
	}

	if (o.directives && typeof o.directives === 'object') {
		const directives: Record<string, string> = {};
		for (const [key, value] of Object.entries(o.directives as Record<string, unknown>)) {
			if (typeof value === 'string') directives[key] = value;
		}
		out.directives = directives;
	}

	return out;
}


/** Boolean-valued map helper (toggles may arrive omitted). */
function valueAsRecord(value: unknown): Record<string, boolean> {
	const out: Record<string, boolean> = {};
	if (value && typeof value === 'object') {
		for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
			if (typeof val === 'boolean') out[key] = val;
		}
	}
	return out;
}


// --- mock-mode store (VITE_MOCK=1): keeps the demo honest without a backend ---

let mockPromptSeq = 0;

export const mockPromptPresets: PromptPreset[] = [];

let mockActivePromptId: string | null = null;

/** Mock factory directive rows (VITE_MOCK=1 demo): a three-row slice of the
 * live 15-row catalog (`GET /api/prompts/directives`), same row shape. */
const MOCK_FACTORY_DIRECTIVES: PromptDirective[] = [
	{
		id: 'director',
		name: 'Director',
		content: 'You are an immersive experience director. (mock demo row)',
		priority: 100,
		tier: 1,
		core: true,
		protected: false,
		enabled: true,
		description: 'Frames every turn as a crafted scene.'
	},
	{
		id: 'story_strings',
		name: 'Story strings (plan pass)',
		content: 'Before drafting, list 4-6 forward paths. (mock demo row)',
		priority: 58,
		tier: 2,
		core: false,
		protected: false,
		enabled: true,
		description: 'Plans hidden paths before drafting.'
	},
	{
		id: 'perspective',
		name: 'Perspective',
		content: 'Third person, present tense. (mock demo row)',
		priority: 60,
		tier: 1,
		core: false,
		protected: false,
		enabled: true,
		description: 'Third-person, present-tense narration.'
	}
];


/** Default module flow (mirrors the D18 structural inserters). */
function mockDefaultPromptModules(): PromptModule[] {
	const make = (id: string, name: string, type: PromptModuleType, role: PromptRole): PromptModule => ({
		id,
		name,
		type,
		role,
		content: '',
		enabled: true
	});
	return [
		make('mod_narrator_system', 'Narrator system', 'narrator_system', 'system'),
		make('mod_library', 'OpenUI library', 'library', 'system'),
		make('mod_history', 'Chat history', 'history', 'user'),
		make('mod_facts', 'World facts', 'facts', 'user'),
		make('mod_user', 'User message', 'user', 'user')
	];
}


/** Default per-interaction flows, mirroring the engine's `DEFAULT_FLOWS`
 * shape (mock demo only; the live reader is `GET /api/prompts/default`). */
function mockDefaultPromptFlows(): Record<string, PromptModule[]> {
	const make = (
		id: string,
		name: string,
		type: PromptModuleType,
		role: PromptRole
	): PromptModule => ({ id, name, type, role, content: '', enabled: true });
	const narrate = mockDefaultPromptModules();
	narrate.push(make('mod_recalled', 'Recalled context (RAG reference)', 'recalled', 'system'));
	return {
		narrate,
		ooc: [
			make('ooc_system', 'OOC system prompt', 'system', 'system'),
			make('ooc_history', 'Chat history', 'history', 'system'),
			make('ooc_facts', 'World facts', 'facts', 'system'),
			make('ooc_user', 'User message', 'user', 'user')
		],
		extract: [
			make('extract_system', 'Extractor system prompt', 'system', 'system'),
			make('extract_turn', 'Turn payload', 'turn_payload', 'user')
		],
		summarize: [
			make('summarize_system', 'Summarizer system prompt', 'system', 'system'),
			make('summarize_payload', 'Transcript payload', 'payload', 'user')
		],
		caption: [
			make('caption_system', 'Captioner system prompt', 'system', 'system'),
			make('caption_payload', 'Image payload', 'payload', 'user')
		],
		assistant: [
			make('assistant_system', 'Assistant system prompt', 'system', 'system'),
			make('assistant_history', 'Chat history', 'history', 'system'),
			make('assistant_user', 'Scene digest', 'user', 'user')
		],
		bts: [
			make('bts_system', 'BTS system prompt', 'system', 'system'),
			make('bts_payload', 'Turn + prior-state payload', 'payload', 'user')
		]
	};
}


/** Mock D27 surfaces (mirror of the live factory block; demo only). */
function mockDefaultSurfaces(): PromptSurfaces {
	return {
		library_doc: '# Component reference\n\n(OpenUI components rendered by the narrator.)',
		recalled: {
			// Static placeholder, no macro: the real `RECALLED_FRAMING`
			// (context/facts.py) is a static string deliberately (a trailing
			// system message carries override authority, so the copy defuses it).
			framing: 'Reference material for this turn — memory, lore, earlier events.',
			open: '<recalled>',
			close: '</recalled>',
			headers: {
				recall: 'Recall',
				world_info: 'World info',
				recap: 'Recap',
				bts: 'Behind the scenes'
			}
		},
		regexes: [
			{
				id: 'mock_regex_vtk',
				name: 'Strip VTK from sent context',
				find: '<!-- VTK_START -->[\\s\\S]*?<!-- VTK_END -->',
				replace: '',
				flags: 'gi',
				prompt_only: true,
				min_depth: 3,
				enabled: true
			}
		],
		history: { depth: 20, strip_dsl: true },
		directives: {
			story_strings: 'Plan forward paths before drafting. Current turn: {{message}}',
			perspective: 'Third person, present tense.'
		}
	};
}


/** The built-in default pipeline as a preset shape (mock mirror of the live
 * `GET /api/prompts/default`). */
export function mockDefaultPreset(): PromptPreset {
	const flows = mockDefaultPromptFlows();
	return {
		preset_id: 'default',
		name: 'default (pipeline)',
		modules: flows['narrate'],
		flows,
		toggles: {},
		prompt_overrides: {},
		agent_bindings: {},
		surfaces: mockDefaultSurfaces(),
		version: 1
	};
}


const MOCK_PROMPT_VARIABLES: PromptVariableSpec[] = [
	{ name: 'user', description: 'The latest user message.', example: 'I push open the door.' },
	{ name: 'persona', description: 'The active persona block.', example: 'Mara, a wary scout.' },
	{ name: 'facts', description: 'The current world-facts block.', example: 'time=dawn | place=gate' },
	{ name: 'history', description: 'Prose-only chat history.', example: 'You waited by the gate…' },
	{ name: 'ooc', description: 'Out-of-character directive text.', example: 'Answer concisely.' }
];


/** Mock step catalog (VITE_MOCK=1 demo): the nine interactions with the same
 * display meta the live `GET /api/prompts/steps` serves. Block counts and
 * bindings are recomputed by the drawer from the mock default preset. */
const MOCK_PROMPT_STEPS: PromptStep[] = [
	{
		id: 'narrate',
		title: 'Narrator turn',
		blurb: 'The in-character turn: prose plus OpenUI panels.',
		trigger: 'pre-turn in-character turn (mock)',
		default_agent: 'narrator',
		bound_agent: null,
		structural_types: ['facts', 'history', 'library', 'narrator_system', 'recalled', 'user'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'ooc',
		title: 'Out-of-character reply',
		blurb: 'Answers an [OOC] message directly; the scene does not advance.',
		trigger: 'out-of-character turn (mock)',
		default_agent: 'narrator',
		bound_agent: null,
		structural_types: ['facts', 'history', 'system', 'user'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'extract',
		title: 'State extractor',
		blurb: 'Turns the committed narrative into a world-state delta.',
		trigger: 'post-turn state delta (mock)',
		default_agent: 'extractor',
		bound_agent: null,
		structural_types: ['system', 'turn_payload'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'summarize',
		title: 'History summarizer',
		blurb: 'Compacts long history into a summary when the budget is exceeded.',
		trigger: 'post-turn compaction hook (mock)',
		default_agent: 'summarizer',
		bound_agent: null,
		structural_types: ['payload', 'system'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'caption',
		title: 'Image captioner',
		blurb: 'Describes an uploaded image or clip so the scene can reference it.',
		trigger: 'media attachment upload (mock)',
		default_agent: 'captioner',
		bound_agent: null,
		structural_types: ['payload', 'system'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'assistant',
		title: 'Assistant consult',
		blurb: "A side assistant's reply, manual or via the per-turn hook.",
		trigger: 'manual consult + per-turn hook (mock)',
		default_agent: 'narrator',
		bound_agent: null,
		structural_types: ['history', 'system', 'user'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'bts',
		title: 'Behind-the-scenes world tracker',
		blurb: 'Post-turn world-state delta.',
		trigger: 'post-turn, after the narrative commits (mock)',
		default_agent: 'extractor',
		bound_agent: null,
		structural_types: ['payload', 'system'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	},
	{
		id: 'critic',
		title: 'Turn critic',
		blurb: 'Post-turn quality gate: ok, revision diffs, or a re-run.',
		trigger: 'post-turn, in-graph at the tail of post_turn (mock)',
		default_agent: 'extractor',
		bound_agent: null,
		structural_types: ['payload', 'system'],
		block_count: 0,
		enabled: true,
		system_source: 'engine'
	}
];


function mockSavePreset(preset: PromptPreset): PromptPreset {
	const index = mockPromptPresets.findIndex((p) => p.preset_id === preset.preset_id);
	if (index >= 0) mockPromptPresets[index] = preset;
	else mockPromptPresets.push(preset);
	return preset;
}


/** List all presets (`GET /api/prompts/presets`). */
export async function listPresets(): Promise<PromptPreset[]> {
	if (USE_MOCK) return mockPromptPresets.map((p) => structuredClone(p));

	const res = await fetch(PROMPT_ENDPOINTS.presets(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`listPresets failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.presets) ? body.presets : [];
	return raw
		.map((item) => normalizePromptPreset(item))
		.filter((item): item is PromptPreset => item !== null);
}


/** Create an empty preset by name (`POST /api/prompts/presets`). */
export async function createPreset(name: string): Promise<PromptPreset> {
	if (USE_MOCK) {
		const preset: PromptPreset = {
			preset_id: `mock_preset_${++mockPromptSeq}`,
			name,
			modules: [],
			flows: { narrate: [] },
			toggles: {},
			prompt_overrides: {},
			agent_bindings: {},
			version: 1
		};
		return structuredClone(mockSavePreset(preset));
	}

	const res = await fetch(PROMPT_ENDPOINTS.presets(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ name })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `createPreset failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const preset = normalizePromptPreset(body.preset ?? body);
	if (!preset) throw new StreamError('createPreset: malformed response');
	return preset;
}


/** Persist edited preset fields (`PUT /api/prompts/presets/{pid}`). */
export async function updatePreset(pid: string, preset: PromptPreset): Promise<PromptPreset> {
	if (USE_MOCK) {
		const stored = { ...structuredClone(preset), preset_id: pid };
		return structuredClone(mockSavePreset(stored));
	}

	const res = await fetch(PROMPT_ENDPOINTS.preset(pid), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(preset)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `updatePreset failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const updated = normalizePromptPreset(body.preset ?? body);
	if (!updated) throw new StreamError('updatePreset: malformed response');
	return updated;
}


/** Activate a preset for the engine (`POST .../{pid}/activate`). */
export async function activatePreset(pid: string): Promise<void> {
	if (USE_MOCK) {
		mockActivePromptId = pid;
		return;
	}

	const res = await fetch(PROMPT_ENDPOINTS.activate(pid), { method: 'POST' });
	if (!res.ok) {
		throw new StreamError(`activatePreset failed: ${res.status} ${res.statusText}`);
	}
}


/** Clear the active preset, reverting to the default pipeline (`POST .../deactivate`). */
export async function deactivatePreset(): Promise<void> {
	if (USE_MOCK) {
		mockActivePromptId = null;
		return;
	}

	const res = await fetch(PROMPT_ENDPOINTS.deactivate(), { method: 'POST' });
	if (!res.ok) {
		throw new StreamError(`deactivatePreset failed: ${res.status} ${res.statusText}`);
	}
}


/** Read the active preset, if any (`GET /api/prompts/active`). */
export async function getActivePreset(): Promise<PromptPreset | null> {
	if (USE_MOCK) {
		const found = mockPromptPresets.find((p) => p.preset_id === mockActivePromptId);
		return found ? structuredClone(found) : null;
	}

	const res = await fetch(PROMPT_ENDPOINTS.active(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`getActivePreset failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return normalizePromptPreset(body.preset);
}


/**
 * Read the built-in default pipeline (`GET /api/prompts/default`).
 *
 * Read-only and never seeded: the D18 UI uses it as the inheritance source for
 * an unset interaction flow (a preset that predates flow completion still shows
 * the running defaults) and to render the `default (pipeline)` selection.
 */
export async function getDefaultPreset(): Promise<PromptPreset | null> {
	if (USE_MOCK) return structuredClone(mockDefaultPreset());

	const res = await fetch(PROMPT_ENDPOINTS.default(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`getDefaultPreset failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return normalizePromptPreset(body.preset ?? body);
}


/** Response of `GET /api/prompts/default` (and reset/commit): the persisted
 * default row plus the D26 factory-adoption flags the drawer renders. */
export interface DefaultPromptStatus {
	preset: PromptPreset | null;
	/** False pre-boot: the default row has not been persisted yet. */
	present: boolean;
	/** True when an engine update advanced the factory past the seeded row. */
	factory_changed: boolean;
	seeded_version: number | null;
	factory_version: number;
}


function normalizeDefaultStatus(body: Record<string, unknown>): DefaultPromptStatus {
	return {
		preset: normalizePromptPreset(body.preset ?? null),
		present: body.present !== false,
		factory_changed: body.factory_changed === true,
		seeded_version: typeof body.seeded_version === 'number' ? body.seeded_version : null,
		factory_version: typeof body.factory_version === 'number' ? body.factory_version : 0
	};
}


/** Read the persisted default row + factory status (`GET /api/prompts/default`).
 *
 * The default preset is a persisted log row the owner edits directly (D26);
 * `factory_changed` drives the "review/reset" notice. */
export async function getDefaultPromptStatus(): Promise<DefaultPromptStatus> {
	if (USE_MOCK) {
		return {
			preset: structuredClone(mockDefaultPreset()),
			present: true,
			factory_changed: false,
			seeded_version: 1,
			factory_version: 1
		};
	}
	const res = await fetch(PROMPT_ENDPOINTS.default(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`getDefaultPromptStatus failed: ${res.status} ${res.statusText}`);
	}
	return normalizeDefaultStatus(await readJson(res));
}


/** Re-seed the persisted default from the factory image (`POST .../default/reset`).
 * Discards the owner's edits to the default row (D26). */
export async function resetDefaultPreset(): Promise<DefaultPromptStatus> {
	if (USE_MOCK) {
		return {
			preset: structuredClone(mockDefaultPreset()),
			present: true,
			factory_changed: false,
			seeded_version: 1,
			factory_version: 1
		};
	}
	const res = await fetch(PROMPT_ENDPOINTS.defaultReset(), { method: 'POST' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `resetDefaultPreset failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	return normalizeDefaultStatus(await readJson(res));
}


/** Result of `POST /api/prompts/default/commit` — the written artifact + row. */
export interface DefaultCommitResult {
	artifact_path: string;
	factory_version: number;
	preset: PromptPreset | null;
}


/** Export the persisted default to the versioned repo artifact
 * (`POST .../default/commit`); 409 when there is no persisted default. */
export async function commitDefaultPreset(): Promise<DefaultCommitResult> {
	if (USE_MOCK) {
		return {
			artifact_path: 'prompts/factory-defaults.json (mock)',
			factory_version: 1,
			preset: structuredClone(mockDefaultPreset())
		};
	}
	const res = await fetch(PROMPT_ENDPOINTS.defaultCommit(), { method: 'POST' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `commitDefaultPreset failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return {
		artifact_path: str(body.artifact_path),
		factory_version: typeof body.factory_version === 'number' ? body.factory_version : 0,
		preset: normalizePromptPreset(body.preset ?? null)
	};
}


/** List documented `{{variable}}` names (`GET /api/prompts/variables`). */
export async function listPromptVariables(): Promise<PromptVariableSpec[]> {
	if (USE_MOCK) return MOCK_PROMPT_VARIABLES.map((v) => ({ ...v }));

	const res = await fetch(PROMPT_ENDPOINTS.variables(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`listPromptVariables failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.variables) ? body.variables : [];
	const out: PromptVariableSpec[] = [];
	for (const item of raw) {
		if (!item || typeof item !== 'object') continue;
		const obj = item as Record<string, unknown>;
		const name = str(obj.name);
		if (!name) continue;
		out.push({
			name,
			description: str(obj.description),
			example: str(obj.example)
		});
	}
	return out;
}


/** Seed a new preset from the default pipeline (`POST /api/prompts/seed-from-default`). */
export async function seedFromDefault(name: string): Promise<PromptPreset> {
	if (USE_MOCK) {
		const modules = mockDefaultPromptModules();
		const preset: PromptPreset = {
			preset_id: `mock_seeded_${++mockPromptSeq}`,
			name,
			modules,
			flows: { narrate: modules.map((m) => ({ ...m })) },
			toggles: {},
			prompt_overrides: {},
			agent_bindings: {},
			version: 1
		};
		return structuredClone(mockSavePreset(preset));
	}

	const res = await fetch(PROMPT_ENDPOINTS.seedFromDefault(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ name })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `seedFromDefault failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const preset = normalizePromptPreset(body.preset ?? body);
	if (!preset) throw new StreamError('seedFromDefault: malformed response');
	return preset;
}


/** Coerce one raw step row into the UI shape (dropping rows without an id). */
export function normalizePromptStep(raw: unknown): PromptStep | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = str(obj.id);
	if (!id) return null;
	const nullable = (value: unknown): string | null => {
		const text = str(value);
		return text === '' ? null : text;
	};
	const structural = Array.isArray(obj.structural_types)
		? obj.structural_types.map((t) => str(t)).filter((t) => t !== '')
		: [];
	return {
		id,
		title: str(obj.title, id),
		blurb: str(obj.blurb),
		trigger: str(obj.trigger),
		default_agent: nullable(obj.default_agent),
		bound_agent: nullable(obj.bound_agent),
		structural_types: structural,
		block_count:
			typeof obj.block_count === 'number' && Number.isFinite(obj.block_count)
				? obj.block_count
				: 0,
		enabled: obj.enabled !== false,
		system_source: obj.system_source === 'authored' ? 'authored' : 'engine'
	};
}


/** List the prompt-manager steps (`GET /api/prompts/steps`, W6.4). */
export async function listPromptSteps(): Promise<PromptStep[]> {
	if (USE_MOCK) return MOCK_PROMPT_STEPS.map((step) => ({ ...step }));

	const res = await fetch(PROMPT_ENDPOINTS.steps(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`listPromptSteps failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.steps) ? body.steps : [];
	return raw
		.map((item) => normalizePromptStep(item))
		.filter((step): step is PromptStep => step !== null);
}


/** List the factory's directive rows (`GET /api/prompts/directives`) — the
 * materialization source for a preset's inherited directive list (W3). */
export async function getFactoryDirectives(): Promise<PromptDirective[]> {
	if (USE_MOCK) return MOCK_FACTORY_DIRECTIVES.map((row) => ({ ...row }));

	const res = await fetch(PROMPT_ENDPOINTS.factoryDirectives(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`getFactoryDirectives failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return normalizePromptDirectives(body.directives);
}


// --- preset export / import (W3 Part B) -------------------------------------
// Mirrors the profiles.ts download path; those helpers are module-private
// there, so the two small ones are duplicated here.

/** Extract the quoted download name from a Content-Disposition header. */
function filenameFromHeader(header: string | null): string | null {
	if (!header) return null;
	const match = /filename="?([^"]+)"?/i.exec(header);
	return match ? match[1].trim() : null;
}

/** `<slug>.preset.json` (mirrors the backend exporter's `_filename`). */
function presetFilename(name: string): string {
	const slug = [...name]
		.map((c) => (/[A-Za-z0-9_-]/.test(c) && c.charCodeAt(0) < 128 ? c : '-'))
		.join('')
		.replace(/^[-_]+/, '')
		.replace(/[-_]+$/, '');
	return `${slug || 'preset'}.preset.json`;
}

/** Trigger a browser download of `blob` as `filename` (anchor + object URL). */
function downloadBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	anchor.click();
	URL.revokeObjectURL(url);
}


/** Export one preset and trigger its download
 * (`GET /api/prompts/presets/{pid}/export`). The body is the store's export
 * shape — exactly what `importPreset` accepts back. */
export async function exportPreset(pid: string): Promise<{ filename: string }> {
	if (USE_MOCK) {
		const preset = mockPromptPresets.find((p) => p.preset_id === pid) ?? mockDefaultPreset();
		const filename = presetFilename(preset.name);
		downloadBlob(
			new Blob([JSON.stringify({ ...preset }, null, 2)], { type: 'application/json' }),
			filename
		);
		return { filename };
	}

	const res = await fetch(PROMPT_ENDPOINTS.exportPreset(pid), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `exportPreset failed: ${res.status} ${res.statusText}`, {
			...detail,
			status: res.status
		});
	}
	const blob = await res.blob();
	let name = '';
	try {
		const parsed = JSON.parse(await blob.text()) as Record<string, unknown>;
		if (typeof parsed?.name === 'string' && parsed.name) name = parsed.name;
	} catch {
		// Non-JSON body: fall back to the slug rule below.
	}
	const filename = filenameFromHeader(res.headers.get('content-disposition')) ?? presetFilename(name);
	downloadBlob(blob, filename);
	return { filename };
}


/** Import a preset export as a NEW preset (`POST /api/prompts/presets/import`).
 *
 * `envelope` is the raw parsed export JSON (a `{"preset": {...}}` wrapper is
 * accepted by the backend too). The engine always mints a fresh id — importing
 * never overwrites an existing preset. */
export async function importPreset(envelope: Record<string, unknown>): Promise<PromptPreset> {
	if (USE_MOCK) {
		const source =
			envelope.preset && typeof envelope.preset === 'object'
				? (envelope.preset as Record<string, unknown>)
				: envelope;
		const preset = normalizePromptPreset(source);
		if (!preset) throw new StreamError('importPreset: malformed preset export');
		const minted: PromptPreset = { ...preset, preset_id: `mock_imported_${++mockPromptSeq}` };
		return structuredClone(mockSavePreset(minted));
	}

	const res = await fetch(PROMPT_ENDPOINTS.importPreset(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(envelope)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `importPreset failed: ${res.status} ${res.statusText}`, {
			...detail,
		status: res.status
		});
	}
	const body = await readJson(res);
	const preset = normalizePromptPreset(body.preset ?? body);
	if (!preset) throw new StreamError('importPreset: malformed response');
	return preset;
}


/**
 * Render a preview through the SAME builder the engine uses
 * (`POST /api/prompts/preview`). `presetId` selects the preset when given;
 * omitted means the active preset / default pipeline. `interaction` selects
 * which flow's modules to render (defaults to `"narrate"`). `preview_mode` is
 * non-empty when the render is degraded.
 */
export async function previewPrompt(
	sessionId: string,
	userText: string,
	presetId?: string | null,
	interaction?: string | null
): Promise<PromptPreview> {
	const flow = interaction || 'narrate';
	if (USE_MOCK) {
		const preset = mockPromptPresets.find((p) => p.preset_id === (presetId ?? mockActivePromptId));
		let modules: PromptModule[] | undefined = preset?.flows?.[flow];
		if (!modules && flow === 'narrate') modules = preset?.modules;
		if (!modules && flow === 'narrate') modules = mockDefaultPromptModules();
		modules ??= [];
		const messages: PromptPreviewMessage[] = [];
		for (const module of modules) {
			if (!module.enabled) continue;
			messages.push({
				role: module.role,
				content: module.content || `[${module.type}]`
			});
		}
		messages.push({ role: 'user', content: userText });
		const chars = messages.reduce((sum, m) => sum + m.content.length, 0);
		return { messages, chars, preview_mode: preset ? 'mock' : 'default (mock)' };
	}

	const payload: Record<string, unknown> = {
		session_id: sessionId,
		user_text: userText,
		interaction: flow
	};
	if (presetId) payload.preset_id = presetId;
	const res = await fetch(PROMPT_ENDPOINTS.preview(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `previewPrompt failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const rawMessages = Array.isArray(body.messages) ? body.messages : [];
	const messages: PromptPreviewMessage[] = [];
	for (const item of rawMessages) {
		if (!item || typeof item !== 'object') continue;
		const obj = item as Record<string, unknown>;
		messages.push({ role: str(obj.role, 'system'), content: str(obj.content) });
	}
	const chars =
		typeof body.chars === 'number'
			? body.chars
			: messages.reduce((sum, m) => sum + m.content.length, 0);
	return { messages, chars, preview_mode: str(body.preview_mode) };
}

// --- Wave 4b Player HUD reads (H1/H2/H4/H5) --------------------------------------
//
// Additive readers for the player-facing panes. The backend halves land in the
// HB lane; each function degrades to a neutral value when its endpoint is
// absent (legacy engine) so the panes stay additive to the U2 shell — a missing
// route must never throw into the render path.
//
//   GET   {API_BASE}/api/sessions/{sid}/recap           -> {recap: [...]}
//   GET   {API_BASE}/api/sessions/{sid}/memory-config    -> recall knobs
//   PATCH {API_BASE}/api/sessions/{sid}/memory-config    -> partial knobs
//   PATCH {API_BASE}/api/sessions/{sid}/memory           -> {enabled} (chat_memory)
//   GET   {API_BASE}/api/sessions/{sid}/speaker_colors   -> cast registry
//   GET   {API_BASE}/api/sessions/{sid}/revisions        -> edit reasons
