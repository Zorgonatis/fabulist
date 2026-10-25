// Node templates (W9.10, R19/R21) — the graph editor's palette LIBRARY client.
//
//   GET    {API_BASE}/api/node-templates           -> {templates: [..], klasses: [..]}
//   POST   {API_BASE}/api/node-templates {..}      -> {template}   (id minted when absent)
//   PATCH  {API_BASE}/api/node-templates/{id} {..} -> {template}   (partial merge)
//   DELETE {API_BASE}/api/node-templates/{id}      -> {deleted: id}
//
// The WRITES are what makes the library an ASSET (R21): `NodeTemplatesPane`
// does plain CRUD on it with no chat open, like personas/worlds. Two server
// refusals are the pane's interesting cases and both carry a `reason`:
// `409` = a stored topology still references the template (nothing is written),
// `422` = the row would be unrunnable (name empty / unknown klass / `implements`
// is not a canonical node contract). They reach the pane as a `StreamError`
// message — never swallowed.
//
// Fold/patch caveat the client honours: the PATCH route drops JSON nulls when it
// merges, so a CLEARED optional field is sent as `''` (the backend reads
// `"" -> None`); sending `null` would leave the stored value in place.

// A node template is a session-independent asset (AGENTS.md NN7): the Studio
// mixes/edits them with no chat open. The palette renders them GROUPED BY
// `klass` (the primitive vocabulary below), and creating a node from one writes
// a topology node row that REFERENCES it:
//
//     { "id": "my_speak", "kind": "stage", "template": "tpl_narrate" }
//
// The engine resolves that row to the factory of the template's `implements`
// (`fabulist.graph.graph.build_graph`), which is why the palette may offer
// behaviours under any label the owner likes. When the fetch fails the caller
// falls back to the canonical node set it already holds — the palette is never
// empty (see `GraphHomePane`).

import { API_BASE, StreamError, USE_MOCK, readJson, str } from './shared';
import type { FlowNodeKind } from '#lib/flow/types';

/** Where `/api/node-templates` lives (one place for the orchestrator to rewire). */
export const NODE_TEMPLATE_ENDPOINTS = {
	list: () => `${API_BASE}/api/node-templates`,
	item: (id: string) => `${API_BASE}/api/node-templates/${encodeURIComponent(id)}`
} as const;

/** The primitive vocabulary a template declares (mirrors `NodeContract.klass`). */
export type NodeTemplateKlass = 'model_pass' | 'gate' | 'deterministic' | 'terminal';

/** The klass order the palette renders (the engine's own order). */
export const NODE_TEMPLATE_KLASSES: readonly NodeTemplateKlass[] = [
	'model_pass',
	'gate',
	'deterministic',
	'terminal'
];

/** One template asset as the API serves it. */
export interface NodeTemplate {
	id: string;
	name: string;
	klass: NodeTemplateKlass;
	/** The canonical node id whose behaviour this template EXECUTES. */
	implements: string;
	/** Prompt door: the flow interaction / W6 step that owns the prompt (or null). */
	interaction: string | null;
	step_id: string | null;
	config: Record<string, unknown>;
	description: string;
	builtin: boolean;
}

/** The palette payload: rows plus the klass grouping order. */
export interface NodeTemplateLibrary {
	templates: NodeTemplate[];
	klasses: NodeTemplateKlass[];
}

/**
 * The body `POST`/`PATCH /api/node-templates` accepts (`NodeTemplateInput`).
 * Optional fields are plain strings — `''` is how a field is CLEARED (see the
 * header note); `builtin` is server-owned and never sent.
 */
export interface NodeTemplateWrite {
	id?: string;
	name: string;
	klass: NodeTemplateKlass;
	implements: string;
	interaction: string;
	step_id: string;
	config: Record<string, unknown>;
	description: string;
}

/**
 * The topology node `kind` a template class lands on. The two vocabularies are
 * deliberately separate (a `model_pass` is drawn as a `stage`; a `gate` stays a
 * gate), and this is the ONE mapping between them.
 */
export function kindForKlass(klass: NodeTemplateKlass): FlowNodeKind {
	if (klass === 'terminal') return 'terminal';
	if (klass === 'gate') return 'gate';
	return 'stage';
}

/** `{template id: implements}` — what the local validator checks ids against. */
export function behaviourMap(library: NodeTemplateLibrary): Record<string, string> {
	const out: Record<string, string> = {};
	for (const template of library.templates) out[template.id] = template.implements;
	return out;
}

function normalizeKlass(raw: unknown): NodeTemplateKlass {
	const value = str(raw);
	return (NODE_TEMPLATE_KLASSES as readonly string[]).includes(value)
		? (value as NodeTemplateKlass)
		: 'model_pass';
}

function normalizeTemplate(raw: unknown): NodeTemplate | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	const config =
		o.config && typeof o.config === 'object' && !Array.isArray(o.config)
			? (o.config as Record<string, unknown>)
			: {};
	return {
		id,
		name: str(o.name, id),
		klass: normalizeKlass(o.klass),
		implements: str(o.implements),
		interaction: str(o.interaction) || null,
		step_id: str(o.step_id) || null,
		config,
		description: str(o.description),
		builtin: o.builtin === true
	};
}

/** Normalize the `{templates, klasses}` envelope. */
function normalizeLibrary(raw: Record<string, unknown>): NodeTemplateLibrary {
	const templates = Array.isArray(raw.templates)
		? raw.templates.map(normalizeTemplate).filter((t): t is NodeTemplate => t !== null)
		: [];
	const klasses = Array.isArray(raw.klasses)
		? raw.klasses.map(normalizeKlass)
		: [...NODE_TEMPLATE_KLASSES];
	return { templates, klasses };
}

/**
 * One route failure as the pane displays it: the server's OWN reason, plus the
 * `known_implements` list a 422 carries (that is the canonical vocabulary, so a
 * refused `implements` is actionable instead of a guessing game).
 */
async function failure(res: Response, call: string): Promise<StreamError> {
	let reason = '';
	let knownImplements: string[] = [];
	let presets: string[] = [];
	try {
		const body = (await res.json()) as Record<string, unknown>;
		const detail = body.detail;
		if (typeof detail === 'string') reason = detail;
		else if (detail && typeof detail === 'object') {
			const d = detail as Record<string, unknown>;
			reason = str(d.reason);
			if (Array.isArray(d.known_implements)) {
				knownImplements = d.known_implements.map((item) => str(item)).filter(Boolean);
			}
			if (Array.isArray(d.presets)) {
				presets = d.presets.map((item) => str(item)).filter(Boolean);
			}
		}
	} catch {
		/* non-JSON body — fall through to the status line */
	}
	let message = reason || `${call} failed: ${res.status} ${res.statusText}`.trim();
	if (knownImplements.length) message += ` (known implements: ${knownImplements.join(', ')})`;
	if (presets.length) message += ` (referenced by: ${presets.join(', ')})`;
	return new StreamError(message, { reason, status: res.status, presets });
}

/** Mock-mode library: the demo has no event log (same role as the palette's). */
let mockTemplates: NodeTemplate[] = [];
let mockSeq = 0;

/** Mock-mode write: mint/replace one row and return it (create and edit alike). */
function mockWrite(write: NodeTemplateWrite): NodeTemplate {
	const id = write.id ?? `tpl_mock_${++mockSeq}`;
	const existing = mockTemplates.find((row) => row.id === id);
	const template: NodeTemplate = {
		id,
		name: write.name,
		klass: write.klass,
		implements: write.implements,
		interaction: write.interaction || null,
		step_id: write.step_id || null,
		config: { ...write.config },
		description: write.description,
		builtin: existing?.builtin ?? false
	};
	mockTemplates = [...mockTemplates.filter((row) => row.id !== id), template];
	return template;
}

function writeBody(write: NodeTemplateWrite): Record<string, unknown> {
	const body: Record<string, unknown> = {
		name: write.name,
		klass: write.klass,
		implements: write.implements,
		interaction: write.interaction,
		step_id: write.step_id,
		config: write.config,
		description: write.description
	};
	if (write.id) body.id = write.id;
	return body;
}

/**
 * The library, THROWING on failure — the studio pane shows the reason.
 * `listNodeTemplates` is the palette's tolerant twin.
 */
export async function getNodeTemplateLibrary(): Promise<NodeTemplateLibrary> {
	if (USE_MOCK) return { templates: [...mockTemplates], klasses: [...NODE_TEMPLATE_KLASSES] };
	const res = await fetch(NODE_TEMPLATE_ENDPOINTS.list(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'listNodeTemplates');
	return normalizeLibrary(await readJson(res));
}

/** Create (or replace, when `write.id` is set) one template. */
export async function createNodeTemplate(write: NodeTemplateWrite): Promise<NodeTemplate> {
	if (USE_MOCK) return mockWrite(write);
	const res = await fetch(NODE_TEMPLATE_ENDPOINTS.list(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(writeBody(write))
	});
	if (!res.ok) throw await failure(res, 'createNodeTemplate');
	const raw = await readJson(res);
	const template = normalizeTemplate(raw.template);
	if (!template) throw new StreamError('the server returned no template', { status: res.status });
	return template;
}

/** Merge `write` over one template (never creates; 404/422 are thrown). */
export async function updateNodeTemplate(
	id: string,
	write: NodeTemplateWrite
): Promise<NodeTemplate> {
	if (USE_MOCK) return mockWrite({ ...write, id });
	const res = await fetch(NODE_TEMPLATE_ENDPOINTS.item(id), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(writeBody({ ...write, id: undefined }))
	});
	if (!res.ok) throw await failure(res, 'updateNodeTemplate');
	const raw = await readJson(res);
	const template = normalizeTemplate(raw.template);
	if (!template) throw new StreamError('the server returned no template', { status: res.status });
	return template;
}

/** Tombstone one template; a stored topology still referencing it is a 409. */
export async function deleteNodeTemplate(id: string): Promise<void> {
	if (USE_MOCK) {
		mockTemplates = mockTemplates.filter((row) => row.id !== id);
		return;
	}
	const res = await fetch(NODE_TEMPLATE_ENDPOINTS.item(id), { method: 'DELETE' });
	if (!res.ok) throw await failure(res, 'deleteNodeTemplate');
}

/**
 * The palette library. `null` means the fetch failed — the caller falls back to
 * the canonical node set (the palette's pre-library source) instead of showing
 * an empty list.
 */
export async function listNodeTemplates(): Promise<NodeTemplateLibrary | null> {
	if (USE_MOCK) return null; // mock demos use the canonical fallback
	try {
		return await getNodeTemplateLibrary();
	} catch {
		return null;
	}
}
