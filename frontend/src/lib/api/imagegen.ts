// Fabulist API client - imagegen definitions (COMFY-PLAN Wave 4, lane V2).
//
// An imagegen definition is a Studio-editable, session-free asset: a ComfyUI
// API-format `workflow` plus the user-supplied `prompt_template` the generator
// agent renders a request through, plus a `param_map` pointing the adjustables
// (seed / positive / negative / resolution) at the workflow nodes that own them.
// CRUD lives on the dedicated `__imagegen__` anchor server-side and needs NO
// chat open (non-negotiable 7).
//
//   GET    {API_BASE}/api/imagegen/definitions          -> {definitions: [wire]}
//   POST   {API_BASE}/api/imagegen/definitions          -> {definition: wire}
//        body {name, workflow (object | JSON text), prompt_template?, imagegen_id?}
//   DELETE {API_BASE}/api/imagegen/definitions/{id}      -> {imagegen_id, deleted}
//
// `detectWorkflowParameters` mirrors the node classes the engine maps
// (`KSampler`/`KSamplerAdvanced`/`ClownsharKSampler_Beta`, `CLIPTextEncode`,
// `EmptyLatentImage`) so the pane previews the same map the backend stores.
//
// Chat-side (lane V3):
//   GET  {API_BASE}/api/imagegen/config            -> {enabled, base_url, reachable}
//   POST {API_BASE}/api/imagegen/render            -> {positive, negative, parse}
//        body {request, imagegen_id}                  (session-LESS: Studio preview)
//   POST {API_BASE}/api/sessions/{sid}/imagegen/render -> same, WITH the session's
//        cast + recent scene injected (the chat path). 404 for an unknown session.
//   POST {API_BASE}/api/sessions/{sid}/imagegen    -> {asset_id, seq, prompt_id, unmapped}
//        | {error: {stage, message, node_errors?}}  (HTTP 200 on failure)
//        body {request?, imagegen_id, overrides?}

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** A pointer at one workflow node input (seed / positive / negative). */
export interface ImageGenParamTarget {
	node_id: string;
	input: string;
}


/** The adjustables a workflow exposes; an absent key means "not adjustable". */
export interface ImageGenParamMap {
	seed?: ImageGenParamTarget;
	positive?: ImageGenParamTarget;
	negative?: ImageGenParamTarget;
	resolution?: { node_id: string; width_input: string; height_input: string };
}


/** A reusable image-generation definition. */
export interface ImageGenDefinition {
	imagegen_id: string;
	name: string;
	workflow: Record<string, unknown>;
	prompt_template: string;
	param_map: ImageGenParamMap;
	provenance: Record<string, unknown>;
}


/** Feature/config probe payload (`GET /api/imagegen/config`). */
export interface ImageGenConfig {
	enabled: boolean;
	base_url: string;
	reachable: boolean;
}


/** The generator agent's rendered prompts (`POST /api/imagegen/render`). */
export interface ImageGenPrompts {
	positive: string;
	negative: string;
	/** How the reply was parsed: `"json" | "labeled" | "fallback"`. */
	parse: string;
}


/** User-edited adjustables applied onto a copy of the workflow on submit. */
export interface ImageGenOverrides {
	seed?: number;
	positive?: string;
	negative?: string;
	width?: number;
	height?: number;
}


/** The run response; a failure rides `error` (HTTP 200, never a broken run). */
export interface ImageGenRunResult {
	asset_id?: string | null;
	seq?: number | null;
	prompt_id?: string | null;
	unmapped?: string[];
	error?: { stage: string; message: string; node_errors?: unknown };
}


/** Definition endpoint paths (kept rewirable in one place). */
export const IMAGEGEN_ENDPOINTS = {
	definitions: () => `${API_BASE}/api/imagegen/definitions`,
	definition: (imagegenId: string) =>
		`${API_BASE}/api/imagegen/definitions/${encodeURIComponent(imagegenId)}`,
	config: () => `${API_BASE}/api/imagegen/config`,
	render: () => `${API_BASE}/api/imagegen/render`,
	renderSession: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/imagegen/render`,
	run: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/imagegen`
} as const;


// Node classes the engine maps (kept in one place so the preview mirrors it).
// RES4LYF's `ClownsharKSampler_Beta` uses `seed` like `KSampler`.
const SAMPLER_CLASSES = new Set(['KSampler', 'KSamplerAdvanced', 'ClownsharKSampler_Beta']);
const TEXT_ENCODE_CLASS = 'CLIPTextEncode';
const LATENT_CLASS = 'EmptyLatentImage';


interface WorkflowNode {
	class_type?: unknown;
	inputs?: unknown;
}


function workflowNodes(workflow: Record<string, unknown>): [string, WorkflowNode][] {
	const out: [string, WorkflowNode][] = [];
	for (const [id, node] of Object.entries(workflow)) {
		if (typeof node === 'object' && node !== null && !Array.isArray(node)) {
			out.push([id, node as WorkflowNode]);
		}
	}
	return out;
}


function nodeInputs(node: WorkflowNode): Record<string, unknown> {
	return typeof node.inputs === 'object' && node.inputs !== null
		? (node.inputs as Record<string, unknown>)
		: {};
}


/** Resolve a ComfyUI link (`[node_id, output_index]`) to its node id. */
function linkNodeId(value: unknown): string | null {
	if (!Array.isArray(value) || value.length === 0) return null;
	const head = value[0];
	return typeof head === 'string' || typeof head === 'number' ? String(head) : null;
}


/** Map a sampler's `positive`/`negative` link to its text node's `text` input. */
function textTarget(
	workflow: Record<string, unknown>,
	inputs: Record<string, unknown>,
	key: string
): ImageGenParamTarget | null {
	const nodeId = linkNodeId(inputs[key]);
	if (nodeId === null) return null;
	const target = workflow[nodeId];
	if (
		typeof target !== 'object' ||
		target === null ||
		(target as WorkflowNode).class_type !== TEXT_ENCODE_CLASS
	) {
		return null;
	}
	if (typeof nodeInputs(target as WorkflowNode).text !== 'string') return null;
	return { node_id: nodeId, input: 'text' };
}


/**
 * Detect the adjustables (seed / positive / negative / resolution) from a raw
 * workflow object. Mirrors `fabulist.imagegen.registry.detect_parameters`; an
 * unresolvable field is simply absent.
 */
export function detectWorkflowParameters(workflow: unknown): ImageGenParamMap {
	const map: ImageGenParamMap = {};
	if (typeof workflow !== 'object' || workflow === null || Array.isArray(workflow)) {
		return map;
	}
	const graph = workflow as Record<string, unknown>;
	const nodes = workflowNodes(graph);

	const sampler = nodes.find(([, node]) => SAMPLER_CLASSES.has(String(node.class_type)));
	if (sampler) {
		const [samplerId, node] = sampler;
		const inputs = nodeInputs(node);
		if ('seed' in inputs) map.seed = { node_id: samplerId, input: 'seed' };
		else if ('noise_seed' in inputs) map.seed = { node_id: samplerId, input: 'noise_seed' };
		const positive = textTarget(graph, inputs, 'positive');
		if (positive) map.positive = positive;
		const negative = textTarget(graph, inputs, 'negative');
		if (negative) map.negative = negative;
	}

	const latent = nodes.find(([, node]) => node.class_type === LATENT_CLASS);
	if (latent) {
		const [latentId, node] = latent;
		const inputs = nodeInputs(node);
		if (typeof inputs.width === 'number' && typeof inputs.height === 'number') {
			map.resolution = {
				node_id: latentId,
				width_input: 'width',
				height_input: 'height'
			};
		}
	}
	return map;
}


/**
 * Read the current value of a param target's node input from a definition's
 * workflow — used to prefill the adjustables strip with the workflow defaults.
 * Absent/mistyped values return `undefined` (the strip falls back to a default).
 */
export function definitionParamValue(
	definition: ImageGenDefinition | null | undefined,
	target: ImageGenParamTarget | undefined
): unknown {
	if (!definition || !target) return undefined;
	const node = definition.workflow[target.node_id];
	if (typeof node !== 'object' || node === null) return undefined;
	return nodeInputs(node as WorkflowNode)[target.input];
}


/** Coerce a raw definition wire into the UI shape, or null when unusable. */
export function normalizeImageGenDefinition(raw: unknown): ImageGenDefinition | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const imagegenId = str(obj.imagegen_id);
	if (!imagegenId) return null;
	const workflow =
		obj.workflow && typeof obj.workflow === 'object' && !Array.isArray(obj.workflow)
			? (obj.workflow as Record<string, unknown>)
			: {};
	return {
		imagegen_id: imagegenId,
		name: str(obj.name),
		workflow,
		prompt_template: str(obj.prompt_template),
		param_map: detectWorkflowParameters(workflow),
		provenance:
			obj.provenance && typeof obj.provenance === 'object'
				? (obj.provenance as Record<string, unknown>)
				: {}
	};
}


/** Mock definitions (VITE_MOCK=1 demo); JSON-only by construction. */
export let mockImageGenDefinitions: ImageGenDefinition[] = [
	{
		imagegen_id: 'imagegen-portrait',
		name: 'Portrait (mock)',
		workflow: {
			'3': {
				class_type: 'KSampler',
				inputs: { seed: 42, positive: ['6', 0], negative: ['7', 0] }
			},
			'5': { class_type: 'EmptyLatentImage', inputs: { width: 512, height: 768 } },
			'6': { class_type: 'CLIPTextEncode', inputs: { text: 'a portrait' } },
			'7': { class_type: 'CLIPTextEncode', inputs: { text: 'blurry' } }
		},
		prompt_template: 'Render the user request as a single positive prompt.',
		param_map: {
			seed: { node_id: '3', input: 'seed' },
			positive: { node_id: '6', input: 'text' },
			negative: { node_id: '7', input: 'text' },
			resolution: { node_id: '5', width_input: 'width', height_input: 'height' }
		},
		provenance: { origin: 'mock' }
	}
];


/** Svelte 5 `$state` values are proxies; the mock clones through JSON. */
function mockClone<T>(value: T): T {
	return JSON.parse(JSON.stringify(value)) as T;
}


/** List the imagegen definitions (`GET /api/imagegen/definitions`). */
export async function listImageGenDefinitions(): Promise<ImageGenDefinition[]> {
	if (USE_MOCK) return mockImageGenDefinitions.map((item) => mockClone(item));
	const res = await fetch(IMAGEGEN_ENDPOINTS.definitions(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`listImageGenDefinitions failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.definitions) ? body.definitions : [];
	return raw
		.map((item) => normalizeImageGenDefinition(item))
		.filter((item): item is ImageGenDefinition => item !== null);
}


/** Create or update a definition (`POST /api/imagegen/definitions`). */
export async function createImageGenDefinition(body: {
	name: string;
	workflow: Record<string, unknown>;
	prompt_template?: string;
	imagegen_id?: string;
	provenance?: Record<string, unknown>;
}): Promise<ImageGenDefinition> {
	if (USE_MOCK) {
		const imagegenId = body.imagegen_id ?? `imagegen-${Date.now().toString(36)}`;
		const saved: ImageGenDefinition = {
			imagegen_id: imagegenId,
			name: body.name,
			workflow: body.workflow,
			prompt_template: body.prompt_template ?? '',
			param_map: detectWorkflowParameters(body.workflow),
			provenance: body.provenance ?? {}
		};
		const index = mockImageGenDefinitions.findIndex((item) => item.imagegen_id === imagegenId);
		mockImageGenDefinitions =
			index >= 0
				? mockImageGenDefinitions.map((item, i) => (i === index ? saved : item))
				: [...mockImageGenDefinitions, saved];
		return mockClone(saved);
	}
	const res = await fetch(IMAGEGEN_ENDPOINTS.definitions(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({
			name: body.name,
			workflow: body.workflow,
			prompt_template: body.prompt_template ?? '',
			imagegen_id: body.imagegen_id,
			provenance: body.provenance ?? {}
		})
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `createImageGenDefinition failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const payload = await readJson(res);
	const saved = normalizeImageGenDefinition(payload.definition ?? payload);
	if (!saved) throw new StreamError('createImageGenDefinition response missing definition');
	return saved;
}


/** Tombstone a definition (`DELETE /api/imagegen/definitions/{id}`). */
export async function deleteImageGenDefinition(imagegenId: string): Promise<void> {
	if (USE_MOCK) {
		mockImageGenDefinitions = mockImageGenDefinitions.filter(
			(item) => item.imagegen_id !== imagegenId
		);
		return;
	}
	const res = await fetch(IMAGEGEN_ENDPOINTS.definition(imagegenId), { method: 'DELETE' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `deleteImageGenDefinition failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
}


// --- chat-side image generation (lane V3) ------------------------------------

/**
 * Probe whether image generation is usable right now (`GET
 * /api/imagegen/config`). The engine folds `comfy.enabled` with a cached
 * ``/system_stats`` reachability check; the Composer shows the Image button only
 * when this reports `enabled`.
 */
export async function getImageGenConfig(): Promise<ImageGenConfig> {
	if (USE_MOCK) return { enabled: false, base_url: 'mock', reachable: false };
	const res = await fetch(IMAGEGEN_ENDPOINTS.config(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`getImageGenConfig failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return {
		enabled: body.enabled === true,
		base_url: str(body.base_url),
		reachable: body.reachable === true
	};
}


/**
 * Render the user's request into positive/negative prompt text through the
 * definition's `prompt_template`. The user reviews and edits the result in the
 * adjustables strip BEFORE the run is submitted. A render failure is an HTTP-200
 * `{error: {stage, message}}` payload (the engine never 500s) — surfaced here as
 * a thrown error so the caller shows it.
 *
 * `sessionId` selects the route: WITH it, the session-scoped route injects the
 * chat's cast (top active layer + its fork children) and last ~5 turns, so "a
 * picture of Tessa" carries Tessa's definition into the prompt. WITHOUT it, the
 * session-less route is the Studio template-preview path (no chat open —
 * non-negotiable 7).
 */
export async function renderImageGenPrompts(
	request: string,
	imagegenId: string,
	sessionId?: string
): Promise<ImageGenPrompts> {
	if (USE_MOCK) return { positive: request, negative: '', parse: 'fallback' };
	const url = sessionId
		? IMAGEGEN_ENDPOINTS.renderSession(sessionId)
		: IMAGEGEN_ENDPOINTS.render();
	const res = await fetch(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ request, imagegen_id: imagegenId })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `renderImageGenPrompts failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	if (body.error && typeof body.error === 'object' && !Array.isArray(body.error)) {
		const err = body.error as { stage?: unknown; message?: unknown };
		throw new StreamError(str(err.message) || 'image prompt rendering failed', {
			stage: str(err.stage),
			status: res.status
		});
	}
	return {
		positive: str(body.positive),
		negative: str(body.negative),
		parse: str(body.parse)
	};
}


/**
 * Submit a generation (`POST /api/sessions/{sid}/imagegen`). The engine stores
 * the image as a chat asset and appends the `image` row, so the frontend reuses
 * the existing image-row path — no new row renderer. Success returns
 * `{asset_id, seq, prompt_id, unmapped}`; any failure is an HTTP-200
 * `{error: {stage, message, node_errors?}}` payload (never a thrown 500).
 */
export async function runImageGen(
	sessionId: string,
	body: { request?: string; imagegen_id: string; overrides?: ImageGenOverrides }
): Promise<ImageGenRunResult> {
	if (USE_MOCK) return { asset_id: null, seq: null, prompt_id: null, unmapped: [] };
	const payload: Record<string, unknown> = {
		imagegen_id: body.imagegen_id,
		overrides: body.overrides ?? {}
	};
	if (body.request !== undefined) payload.request = body.request;
	const res = await fetch(IMAGEGEN_ENDPOINTS.run(sessionId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(payload)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `runImageGen failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const raw = await readJson(res);
	const result: ImageGenRunResult = {
		asset_id: typeof raw.asset_id === 'string' ? raw.asset_id : null,
		seq: typeof raw.seq === 'number' ? raw.seq : null,
		prompt_id: typeof raw.prompt_id === 'string' ? raw.prompt_id : null,
		unmapped: Array.isArray(raw.unmapped) ? raw.unmapped.map((key) => String(key)) : []
	};
	if (raw.error && typeof raw.error === 'object' && !Array.isArray(raw.error)) {
		const err = raw.error as { stage?: unknown; message?: unknown; node_errors?: unknown };
		result.error = {
			stage: str(err.stage),
			message: str(err.message),
			...(err.node_errors !== undefined ? { node_errors: err.node_errors } : {})
		};
	}
	return result;
}
