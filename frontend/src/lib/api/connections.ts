// Fabulist API client - connections editor (definitions surface).

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** One connection entry as the API serializes it (secret redacted). */
export interface ConnectionEntry {
	name: string;
	base_url: string;
	model: string;
	/** Name of the env var holding the key, or null (keyless). */
	api_key_env: string | null;
	/** True when the named env var currently resolves to a non-empty value. */
	api_key_set: boolean;
	extra_body: Record<string, unknown>;
	sampling: Record<string, unknown>;
	timeout: number | null;
	/** W9.25 "Context Size": owner-declared prompt+response token ceiling (null = unknown). */
	context_window: number | null;
	/** W9.25 "Thinking": null/true = ON, `false` is the only off instruction. */
	thinking: boolean | null;
	/** W9.25 "Streaming": null/true = stream, `false` = one non-streaming request. */
	streaming: boolean | null;
	/**
	 * "Return thinking": null = AUTO (echo the provider's `reasoning_content` back
	 * inside a tool loop whenever it returned some — required by DeepSeek thinking
	 * models), true = force that echo, false = never send the field.
	 */
	return_thinking: boolean | null;
	/**
	 * "Reasoning effort": the provider's own TOP-LEVEL `reasoning_effort` body
	 * field (z.ai documents it for GLM-5.x). A documented enum string, or null =
	 * the parameter is NOT SENT at all — a connection that never declares it keeps
	 * behaving exactly as before. Labels in the UI are semantic; this is the raw
	 * wire value, so a stored value outside the documented set is preserved
	 * verbatim rather than silently reset.
	 */
	reasoning_effort: string | null;
	/** Free-text user note (document state the typed registry ignores). */
	note?: string;
}


/**
 * One slot row of the engine-derived org chart (W9.25 §3.7): a type, a task, or
 * a per-item slot, with the connection it RESOLVES to and how it got there.
 *
 * Rows arrive already ordered (type row, its task rows, its per-item rows) and
 * already grouped by `type`; the drawer renders them, it never re-derives the
 * hierarchy (that lives in `fabulist/connections/taxonomy.py`).
 */
export interface ConnectionSlot {
	/** The canonical `agents` key a rebind writes (e.g. `completion.narration`). */
	slot: string;
	/** Owning type (`completion`/`embedding`/`decision`); null for custom keys. */
	type: string | null;
	/** Task under the type (narration/extraction/state); null otherwise. */
	task: string | null;
	label: string;
	canonical: string;
	/** Legacy key spellings that alias this slot (never rendered as their own row). */
	aliases: string[];
	/** Resolved connection name, or null when nothing in the map covers the slot. */
	connection: string | null;
	/** `authored` | `legacy:<key>` | `type:<key>` | null (unresolved). */
	via: string | null;
}


/** The full connections read: every redacted entry, the agent map, the slot view. */
export interface ConnectionsCatalog {
	connections: Record<string, ConnectionEntry>;
	agents: Record<string, string>;
	/** The engine's ordered, grouped slot rows (empty when it reports none). */
	slots: ConnectionSlot[];
}


/** A create/patch body. Unset fields are omitted so PATCH stays partial. */
export interface ConnectionWrite {
	base_url?: string;
	model?: string;
	api_key_env?: string | null;
	/**
	 * A literal key the user pasted. The engine stores it in `.env` (mode 600) and
	 * keeps only the derived `api_key_env` NAME; the value is never returned. When
	 * both are sent the literal wins.
	 */
	api_key?: string;
	extra_body?: Record<string, unknown> | null;
	sampling?: Record<string, unknown> | null;
	timeout?: number | null;
	note?: string | null;
	/** W9.25 Context Size in tokens; null = unknown (the engine keeps its defaults). */
	context_window?: number | null;
	/** W9.25 Thinking; null (or absent) is ON — never write `true`. */
	thinking?: boolean | null;
	/** W9.25 Streaming; null (or absent) streams. */
	streaming?: boolean | null;
	/** "Return thinking"; null = AUTO (the engine echoes whatever it received). */
	return_thinking?: boolean | null;
	/**
	 * "Reasoning effort"; a documented enum string, or null = send nothing.
	 * `none` is a REAL value (an explicit disable instruction), never null.
	 */
	reasoning_effort?: string | null;
}


/** Options for the discovery probe (draft ping uses base_url/key overrides). */
export interface DiscoverModelsOptions {
	baseUrl?: string;
	/** Name of an env var to resolve server-side (the advanced path). */
	apiKeyEnv?: string;
	/**
	 * A literal key pasted into the UNSAVED draft. Sent as a header (never a query
	 * parameter: the engine's access log records the URL) and used for this one
	 * probe only — the engine never persists it.
	 */
	apiKey?: string;
}


/** Result of a server-side model discovery probe (also the save ping). */
export interface ModelDiscovery {
	connection: string;
	base_url: string;
	url: string;
	models: string[];
	count: number;
	/** True when an Authorization header was sent (key resolved server-side). */
	api_key_set: boolean;
}


/**
 * Result of storing a literal API key server-side: the env-var NAME the engine
 * resolves it through, plus the resulting state. The secret never comes back.
 */
export interface ConnectionKeyResult {
	api_key_env: string;
	api_key_set: boolean;
}


/** Connections endpoint paths (kept in one place the orchestrator can rewire). */
export const CONNECTION_ENDPOINTS = {
	list: () => `${API_BASE}/api/connections`,
	entry: (name: string) => `${API_BASE}/api/connections/${encodeURIComponent(name)}`,
	/** Literal-key upload for a saved connection (stored server-side, never read back). */
	key: (name: string) => `${API_BASE}/api/connections/${encodeURIComponent(name)}/key`,
	agent: (agent: string) => `${API_BASE}/api/connections/agents/${encodeURIComponent(agent)}`,
	models: (name: string, options?: DiscoverModelsOptions): string => {
		const params = new URLSearchParams();
		if (options?.baseUrl?.trim()) params.set('base_url', options.baseUrl.trim());
		if (options?.apiKeyEnv?.trim()) params.set('api_key_env', options.apiKeyEnv.trim());
		const query = params.toString();
		return `${API_BASE}/api/connections/${encodeURIComponent(name)}/models${
			query ? `?${query}` : ''
		}`;
	}
} as const;


/** Coerce one raw wire entry into the UI shape (tolerant of absent fields). */
export function normalizeConnectionEntry(name: string, raw: unknown): ConnectionEntry {
	const obj = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	const dict = (value: unknown): Record<string, unknown> =>
		value && typeof value === 'object' && !Array.isArray(value)
			? { ...(value as Record<string, unknown>) }
			: {};
	const entry: ConnectionEntry = {
		name: str(obj.name) || name,
		base_url: str(obj.base_url),
		model: str(obj.model),
		api_key_env: typeof obj.api_key_env === 'string' ? obj.api_key_env : null,
		api_key_set: obj.api_key_set === true,
		extra_body: dict(obj.extra_body),
		sampling: dict(obj.sampling),
		timeout: typeof obj.timeout === 'number' ? obj.timeout : null,
		context_window: typeof obj.context_window === 'number' ? obj.context_window : null,
		thinking: typeof obj.thinking === 'boolean' ? obj.thinking : null,
		streaming: typeof obj.streaming === 'boolean' ? obj.streaming : null,
		return_thinking: typeof obj.return_thinking === 'boolean' ? obj.return_thinking : null,
		// Kept VERBATIM (never coerced to a known value): an effort outside the
		// documented set is the engine's to reject, not the browser's to rewrite.
		reasoning_effort: str(obj.reasoning_effort) || null
	};
	if (typeof obj.note === 'string') entry.note = obj.note;
	return entry;
}


/** Coerce a raw agent -> connection map (string values only). */
function normalizeAgentMap(raw: unknown): Record<string, string> {
	const agents: Record<string, string> = {};
	if (raw && typeof raw === 'object') {
		for (const [agent, connection] of Object.entries(raw as Record<string, unknown>)) {
			if (typeof connection === 'string') agents[agent] = connection;
		}
	}
	return agents;
}


/** Coerce one raw slot row; null when it is not a usable row. */
function normalizeSlotRow(raw: unknown): ConnectionSlot | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const slot = str(obj.slot);
	if (!slot) return null;
	return {
		slot,
		type: str(obj.type) || null,
		task: str(obj.task) || null,
		label: str(obj.label) || slot,
		canonical: str(obj.canonical) || slot,
		aliases: Array.isArray(obj.aliases)
			? (obj.aliases as unknown[]).filter((alias): alias is string => typeof alias === 'string')
			: [],
		connection: str(obj.connection) || null,
		via: str(obj.via) || null
	};
}


/** Coerce the engine's ordered slot rows (absent = the engine reports none). */
function normalizeSlots(raw: unknown): ConnectionSlot[] {
	if (!Array.isArray(raw)) return [];
	return (raw as unknown[])
		.map(normalizeSlotRow)
		.filter((row): row is ConnectionSlot => row !== null);
}


/** Normalize the redacted list + agent map + slot view. */
function normalizeConnectionsCatalog(body: Record<string, unknown>): ConnectionsCatalog {
	const connections: Record<string, ConnectionEntry> = {};
	const rawConnections = body.connections && typeof body.connections === 'object'
		? (body.connections as Record<string, unknown>)
		: {};
	for (const [name, raw] of Object.entries(rawConnections)) {
		connections[name] = normalizeConnectionEntry(name, raw);
	}
	return {
		connections,
		agents: normalizeAgentMap(body.agents),
		slots: normalizeSlots(body.slots)
	};
}


/** Read the connections document (redacted) + the agent bindings. */
export async function listConnections(): Promise<ConnectionsCatalog> {
	if (USE_MOCK) return mockConnectionsCatalog();

	const res = await fetch(CONNECTION_ENDPOINTS.list(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`listConnections failed: ${res.status} ${res.statusText}`);
	return normalizeConnectionsCatalog(await readJson(res));
}


/** Create a new connection entry (`POST`; 201). */
export async function createConnection(
	name: string,
	body: ConnectionWrite
): Promise<ConnectionEntry> {
	if (USE_MOCK) return mockCreateConnection(name, body);

	const res = await fetch(CONNECTION_ENDPOINTS.entry(name), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `createConnection failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const payload = await readJson(res);
	return normalizeConnectionEntry(name, payload.connection ?? payload);
}


/** Patch an existing connection (`PATCH`; ``base_url`` is not writable). */
export async function updateConnection(
	name: string,
	body: ConnectionWrite
): Promise<ConnectionEntry> {
	if (USE_MOCK) return mockUpdateConnection(name, body);

	const res = await fetch(CONNECTION_ENDPOINTS.entry(name), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `updateConnection failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const payload = await readJson(res);
	return normalizeConnectionEntry(name, payload.connection ?? payload);
}


/** Delete a connection entry (`DELETE`; 409 when an agent still binds it). */
export async function deleteConnection(name: string): Promise<void> {
	if (USE_MOCK) {
		mockDeleteConnection(name);
		return;
	}
	const res = await fetch(CONNECTION_ENDPOINTS.entry(name), { method: 'DELETE' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `deleteConnection failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
}


/**
 * Store a literal API key for a SAVED connection (`POST .../key`).
 *
 * Contract (backend lane L1): body `{"key": "<literal>"}` in, `{api_key_env,
 * api_key_set}` out. The engine keeps the secret and resolves it through
 * `api_key_env`; only the NAME and the resulting state are returned, so the
 * caller can display them without ever holding the key again.
 */
export async function setConnectionKey(
	name: string,
	key: string
): Promise<ConnectionKeyResult> {
	if (USE_MOCK) return mockSetConnectionKey(name, key);

	const res = await fetch(CONNECTION_ENDPOINTS.key(name), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ key })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `setConnectionKey failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return {
		api_key_env: str(body.api_key_env),
		api_key_set: body.api_key_set === true
	};
}


/**
 * Discover a connection's models via the server-side proxy (also the save ping).
 *
 * For an unsaved draft pass `baseUrl` and the pasted `apiKey` (and/or an
 * `apiKeyEnv` name) so the server probes the typed endpoint BEFORE anything is
 * saved; the stored connection is used otherwise. Throws a `StreamError` with
 * the backend's reason on an unreachable endpoint.
 */
export async function discoverModels(
	name: string,
	options: DiscoverModelsOptions = {}
): Promise<ModelDiscovery> {
	if (USE_MOCK) return mockDiscoverModels(name, options);

	const headers: Record<string, string> = { Accept: 'application/json' };
	// The draft key travels in a HEADER, never the query string: the engine's
	// access log records the full path+query, so a secret must not be in a URL.
	if (options.apiKey?.trim()) headers['X-Fabulist-Key'] = options.apiKey.trim();
	const res = await fetch(CONNECTION_ENDPOINTS.models(name, options), { headers });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `discoverModels failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const models = Array.isArray(body.models)
		? (body.models as unknown[]).filter((m): m is string => typeof m === 'string')
		: [];
	return {
		connection: str(body.connection) || name,
		base_url: str(body.base_url),
		url: str(body.url),
		models,
		count: typeof body.count === 'number' ? body.count : models.length,
		api_key_set: body.api_key_set === true
	};
}


/**
 * Rebind one agent to another existing connection (`PATCH`; hot-reload).
 *
 * Returns the registry's fresh agent -> connection map so the caller can
 * reconcile its state without a follow-up GET. Throws a `StreamError` with the
 * backend's reason when the agent or connection is unknown.
 */
export async function setAgentConnection(
	agent: string,
	connection: string
): Promise<Record<string, string>> {
	if (USE_MOCK) return mockSetAgentConnection(agent, connection);

	const res = await fetch(CONNECTION_ENDPOINTS.agent(agent), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ connection })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `setAgentConnection failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return normalizeAgentMap(body.agents);
}


// Mock state (module singletons — one browser session per page load).
let mockConnectionsMap: Record<string, ConnectionEntry> = {
	'your-model-id': {
		name: 'your-model-id',
		base_url: 'http://localhost:4444/v1',
		model: 'your-model-id',
		api_key_env: null,
		api_key_set: false,
		extra_body: {},
		sampling: {
			temperature: 0.85,
			max_tokens: 16384,
			top_p: 0.95,
			top_k: 40,
			repetition_penalty: 1.05,
			stop: ['\n<|user|>', '\nUser:']
		},
		timeout: 900,
		context_window: 65536,
		thinking: null,
		streaming: null,
		return_thinking: null,
		reasoning_effort: null,
		note: 'Local llama.cpp narrator.'
	},
	'glm-api': {
		name: 'glm-api',
		base_url: 'https://api.z.ai/api/coding/paas/v4',
		model: 'glm-5.1',
		api_key_env: 'ZAI_API_KEY',
		api_key_set: false,
		// `reasoning_effort` is a FIRST-CLASS field now (not an extra-body row): the
		// demo shows the depth maxed while the shape stays the documented one.
		extra_body: { thinking: { type: 'enabled' } },
		sampling: {
			temperature: 0.85,
			max_tokens: 20000,
			top_p: 0.95,
			frequency_penalty: 0.1,
			presence_penalty: 0.1,
			seed: 42
		},
		timeout: 600,
		context_window: 131072,
		thinking: true,
		streaming: false,
		return_thinking: null,
		reasoning_effort: 'max'
	}
};

// W9.25 taxonomy demo: typed keys beat their legacy twins, the type key is the
// fallback everything under it inherits, and per-item keys still win per item.
let mockAgentMap: Record<string, string> = {
	completion: 'glm-api',
	'completion.narration': 'your-model-id',
	'completion.extraction': 'your-model-id',
	// A legacy twin still in the file while its typed twin is authored: the
	// drawer marks it inert (typed beats twin).
	narrator: 'glm-api',
	summarizer: 'glm-api',
	imagegen: 'your-model-id',
	embedding: 'your-model-id',
	embedder: 'your-model-id',
	decision: 'glm-api'
};

/** Legacy key -> canonical typed spelling (mirrors `connections/taxonomy.py`). */
const MOCK_LEGACY_TWINS: Record<string, string> = {
	narrator: 'completion.narration',
	extractor: 'completion.extraction'
};

/** One mock slot row: the engine's row fields plus its candidate keys, in order. */
interface MockSlotSeed {
	slot: string;
	type: string | null;
	task: string | null;
	label: string;
	aliases: string[];
	/** `agents` keys the engine's resolver would consult, highest precedence first. */
	candidates: string[];
}

/**
 * The engine's slot payload as FIXTURE DATA (VITE_MOCK has no engine to ask).
 *
 * The candidate lists record `taxonomy.candidate_slots`' order rather than
 * deriving it, so the demo's via lines stay truthful as the mock map is edited
 * and no resolution rule is re-implemented in the browser.
 */
const MOCK_SLOT_SEEDS: MockSlotSeed[] = [
	{ slot: 'completion', type: 'completion', task: null, label: 'Completion', aliases: [], candidates: ['completion'] },
	{
		slot: 'completion.narration',
		type: 'completion',
		task: 'narration',
		label: 'Narration',
		aliases: ['narrator'],
		candidates: ['completion.narration', 'narrator']
	},
	{
		slot: 'completion.extraction',
		type: 'completion',
		task: 'extraction',
		label: 'Extraction',
		aliases: ['extractor'],
		candidates: ['completion.extraction', 'extractor']
	},
	{ slot: 'completion.state', type: 'completion', task: 'state', label: 'State', aliases: [], candidates: ['completion.state', 'completion'] },
	{
		slot: 'summarizer',
		type: 'completion',
		task: null,
		label: 'Summarizer',
		aliases: [],
		candidates: ['summarizer', 'completion.extraction', 'extractor', 'completion']
	},
	{
		slot: 'captioner',
		type: 'completion',
		task: null,
		label: 'Captioner',
		aliases: [],
		candidates: ['captioner', 'completion.extraction', 'extractor', 'completion']
	},
	{ slot: 'imagegen', type: 'completion', task: null, label: 'Imagegen', aliases: [], candidates: ['imagegen', 'completion'] },
	{ slot: 'embedding', type: 'embedding', task: null, label: 'Embedding', aliases: [], candidates: ['embedding'] },
	{ slot: 'embedder', type: 'embedding', task: null, label: 'Embedder', aliases: [], candidates: ['embedder', 'embedding'] },
	{ slot: 'decision', type: 'decision', task: null, label: 'Decision', aliases: [], candidates: ['decision'] }
];

/** Every slot spelling the mock accepts as a rebind target (upsert, like the engine). */
const MOCK_DECLARED_SLOTS = new Set<string>([
	...MOCK_SLOT_SEEDS.map((seed) => seed.slot),
	...Object.keys(MOCK_LEGACY_TWINS)
]);

/** The slot payload computed over the current mock agent map. */
function mockSlotView(): ConnectionSlot[] {
	return MOCK_SLOT_SEEDS.map((seed) => {
		const matched = seed.candidates.find((key) => key in mockAgentMap) ?? null;
		return {
			slot: seed.slot,
			type: seed.type,
			task: seed.task,
			label: seed.label,
			canonical: seed.slot,
			aliases: seed.aliases,
			connection: matched ? mockAgentMap[matched] : null,
			via:
				matched === null
					? null
					: matched === seed.slot
						? 'authored'
						: matched in MOCK_LEGACY_TWINS
							? `legacy:${matched}`
							: `type:${matched}`
		};
	});
}


function mockConnectionsCatalog(): ConnectionsCatalog {
	return {
		connections: Object.fromEntries(
			Object.entries(mockConnectionsMap).map(([k, v]) => [k, JSON.parse(JSON.stringify(v))])
		),
		agents: { ...mockAgentMap },
		slots: mockSlotView()
	};
}


function mockCreateConnection(name: string, body: ConnectionWrite): ConnectionEntry {
	if (mockConnectionsMap[name]) throw new StreamError(`connection exists: ${name}`);
	// A pasted literal occupies the derived env name, exactly like the engine.
	const api_key_env = body.api_key ? mockApiKeyEnvName(name) : (body.api_key_env ?? null);
	const entry: ConnectionEntry = {
		name,
		base_url: body.base_url ?? '',
		model: body.model ?? '',
		api_key_env,
		api_key_set: Boolean(body.api_key),
		extra_body: (body.extra_body as Record<string, unknown>) ?? {},
		sampling: (body.sampling as Record<string, unknown>) ?? {},
		timeout: body.timeout ?? null,
		context_window: body.context_window ?? null,
		thinking: body.thinking ?? null,
		streaming: body.streaming ?? null,
		return_thinking: body.return_thinking ?? null,
		reasoning_effort: body.reasoning_effort ?? null,
		...(body.note ? { note: body.note } : {})
	};
	mockConnectionsMap = { ...mockConnectionsMap, [name]: entry };
	return JSON.parse(JSON.stringify(entry));
}


function mockUpdateConnection(name: string, body: ConnectionWrite): ConnectionEntry {
	const current = mockConnectionsMap[name];
	if (!current) throw new StreamError(`unknown connection: ${name}`);
	const literal = body.api_key?.trim();
	const next: ConnectionEntry = {
		...current,
		...(body.model !== undefined ? { model: body.model } : {}),
		...(body.api_key_env !== undefined ? { api_key_env: body.api_key_env } : {}),
		// The literal wins over a supplied name, and only its derived NAME shows.
		...(literal ? { api_key_env: mockApiKeyEnvName(name), api_key_set: true } : {}),
		...(body.extra_body !== undefined && body.extra_body !== null
			? { extra_body: body.extra_body as Record<string, unknown> }
			: {}),
		...(body.sampling !== undefined && body.sampling !== null
			? { sampling: body.sampling as Record<string, unknown> }
			: {}),
		...(body.timeout !== undefined ? { timeout: body.timeout } : {}),
		...(body.context_window !== undefined ? { context_window: body.context_window } : {}),
		...(body.thinking !== undefined ? { thinking: body.thinking } : {}),
		...(body.streaming !== undefined ? { streaming: body.streaming } : {}),
		...(body.return_thinking !== undefined
			? { return_thinking: body.return_thinking }
			: {}),
		// null is a REAL write here (it clears a stored effort): only an absent key
		// leaves the current value alone.
		...(body.reasoning_effort !== undefined
			? { reasoning_effort: body.reasoning_effort }
			: {})
	};
	if (body.note !== undefined) {
		if (body.note) next.note = body.note;
		else delete next.note;
	}
	mockConnectionsMap = { ...mockConnectionsMap, [name]: next };
	return JSON.parse(JSON.stringify(next));
}


function mockSetAgentConnection(agent: string, connection: string): Record<string, string> {
	if (!MOCK_DECLARED_SLOTS.has(agent) && !(agent in mockAgentMap)) {
		throw new StreamError(`unknown agent: ${agent}`);
	}
	if (!mockConnectionsMap[connection]) throw new StreamError(`unknown connection: ${connection}`);
	// A legacy-twin address normalizes to the canonical key, exactly as the
	// engine's PATCH does: the typed key is what gets written.
	const slot = MOCK_LEGACY_TWINS[agent] ?? agent;
	mockAgentMap = { ...mockAgentMap, [slot]: connection };
	return { ...mockAgentMap };
}


function mockDeleteConnection(name: string): void {
	const bound = Object.entries(mockAgentMap).filter(([, conn]) => conn === name).map(([a]) => a);
	if (bound.length) {
		throw new StreamError(
			`cannot delete '${name}': still bound to ${bound.join(', ')}`
		);
	}
	if (!mockConnectionsMap[name]) throw new StreamError(`unknown connection: ${name}`);
	const next = { ...mockConnectionsMap };
	delete next[name];
	mockConnectionsMap = next;
}


function mockDiscoverModels(name: string, options: DiscoverModelsOptions): ModelDiscovery {
	const base = options.baseUrl?.trim() || mockConnectionsMap[name]?.base_url || '';
	return {
		connection: name,
		base_url: base,
		url: `${base.replace(/\/$/, '')}/models`,
		models: ['your-model-id', 'qwen38-27b-heretic', 'glm-5.1'],
		count: 3,
		api_key_set: Boolean(options.apiKey?.trim() || options.apiKeyEnv?.trim())
	};
}


/** ``FABULIST_<NAME>_KEY`` — mirrors the engine's `api_key_env_name` derivation. */
function mockApiKeyEnvName(connectionName: string): string {
	return `FABULIST_${connectionName.replace(/[^A-Za-z0-9]/g, '_').toUpperCase()}_KEY`;
}


/** Mock of the paste-key route: mirrors the contract (env NAME + state back). */
function mockSetConnectionKey(name: string, key: string): ConnectionKeyResult {
	const current = mockConnectionsMap[name];
	if (!current) throw new StreamError(`unknown connection: ${name}`);
	if (!key.trim()) throw new StreamError('key is empty');
	const api_key_env = mockApiKeyEnvName(name);
	mockConnectionsMap = {
		...mockConnectionsMap,
		[name]: { ...current, api_key_env, api_key_set: true }
	};
	return { api_key_env, api_key_set: true };
}


// --- D25: memory lifecycle (embedder switch gate + re-embed) -----------------
//
// A live embedder rebind (PATCH /api/connections/agents/embedder) changes the
// vector dimensionality, so the single memory index must be WIPED and
// re-created for the new embedder — no data is retained across a switch (user
// ruling). The Connections drawer's confirm modal drives that:
//
//   PATCH /api/connections/agents/embedder {connection}
//   POST  /api/memory/reset                 -> {reset, collections, previous}
//   POST  /api/memory/reembed               -> {started, state, done, total}
//   GET   /api/memory/reembed               -> {state, done, total, error?}
//   GET   /api/memory/collections           -> {collections}
//
// Re-embed is a SEPARATE explicit action (the repair path): it re-embeds every
// visible, memory-enabled chat into its resolved scope. Job state is
// in-process; a restart mid-job loses it and the button simply re-runs it.
// All paths are absolute `${API_BASE}` (AGENTS.md gotcha: a relative `/api/...`
// 404s behind vite and gets swallowed by a cosmetic catch).
