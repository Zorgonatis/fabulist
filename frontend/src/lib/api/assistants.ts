// Fabulist API client - assistants (D38).

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson } from './shared';

/** One assistant persona (Extra Assistant). `demo` marks the backend's placeholders. */
export interface AssistantDefinition {
	id: string;
	name: string;
	persona: string;
	style_notes: string;
	/** Model tier resolved through the connections registry. */
	agent: string;
	trigger: 'manual' | 'keyword' | string;
	keywords: string[];
	/** Optional persona/actor reference this assistant runs as (D47). */
	actor?: string;
	/** True for the built-in placeholder personas (not preset ports). */
	demo: boolean;
}


/** The assistant catalog + provenance metadata. */
export interface AssistantCatalog {
	assistants: AssistantDefinition[];
	meta: {
		count?: number;
		source?: string;
		session?: string;
		event?: string;
		demo?: boolean;
		[k: string]: unknown;
	};
}


/** One assistant's drained reply (the consult endpoint's response). */
export interface AssistantReply {
	assistant: string;
	text: string;
	agent: string;
}


/** One persisted `assistant_reply` system event, read back for hydration. */
export interface AssistantReplyRecord {
	id: number;
	seq?: number;
	ts?: string;
	assistant: string;
	text: string;
	agent: string;
	user_msg?: string;
}


/** Endpoints for the assistant surface (kept next to the other builders). */
export const ASSISTANT_ENDPOINTS = {
	list: () => `${API_BASE}/api/assistants`,
	/** One definition by id (D47 CRUD). */
	entry: (assistantId: string) =>
		`${API_BASE}/api/assistants/${encodeURIComponent(assistantId)}`,
	replies: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/assistants/replies`,
	consult: (sessionId: string, assistantId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/assistants/${encodeURIComponent(assistantId)}/consult`,
	/** The session's single enablement slot. */
	session: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/assistant`,
	/** The latest-wins panel statement for the enabled assistant. */
	panel: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/assistant-panel`
} as const;


/** Demo catalog so VITE_MOCK=1 shows the picker with no backend. */
export const MOCK_ASSISTANTS: AssistantDefinition[] = [
	{
		id: 'demo_oracle',
		name: 'Demo Oracle (mock)',
		persona: 'A laconic oracle watching from just off-stage. (MOCK)',
		style_notes: 'Short, knowing lines.',
		agent: 'narrator',
		trigger: 'manual',
		keywords: [],
		demo: true
	},
	{
		id: 'demo_gremlin',
		name: 'Demo Gremlin (mock)',
		persona: 'A gleeful gremlin cheering from the rafters. (MOCK)',
		style_notes: 'Chaotic, always at the wrong moment.',
		agent: 'narrator',
		trigger: 'manual',
		keywords: [],
		demo: true
	}
];


/** In-memory enablement so the VITE_MOCK demo can toggle an assistant on/off. */
const mockAssistantBySession = new Map<string, string | null>();


/** Hand-built panel statement mirroring the engine's `asst = AssistantCard(...)`. */
function mockAssistantPanelStatement(assistantId: string): string {
	const persona = MOCK_ASSISTANTS.find((a) => a.id === assistantId) ?? MOCK_ASSISTANTS[0];
	const options = [
		{ text: 'What did you notice this turn?', tag: 'inspect', emphasis: true },
		{ text: 'What should I watch for next?', tag: 'forward' }
	]
		.map((o) => `{text: "${o.text}", tag: "${o.tag}"${o.emphasis ? ', emphasis: true' : ''}}`)
		.join(', ');
	return `asst = AssistantCard("", "${persona.name}", "assistant", "🎴", "${persona.persona}", [${options}], "MOCK panel — click an option to fill the composer.")`;
}


/** List assistant personas (log-defined, else the backend's demo placeholders). */
export async function listAssistants(): Promise<AssistantCatalog> {
	if (USE_MOCK) {
		return {
			assistants: MOCK_ASSISTANTS.map((a) => ({ ...a, keywords: [...a.keywords] })),
			meta: { count: MOCK_ASSISTANTS.length, source: 'mock', demo: true }
		};
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.list(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`listAssistants failed: ${res.status} ${res.statusText}`);
	}
	return normalizeAssistantCatalog(await readJson(res));
}


function normalizeAssistantCatalog(raw: Record<string, unknown>): AssistantCatalog {
	const source = Array.isArray(raw.assistants) ? raw.assistants : [];
	const assistants: AssistantDefinition[] = [];
	for (const entry of source) {
		const definition = normalizeAssistantDefinition(entry);
		if (definition) assistants.push(definition);
	}
	const meta =
		raw.meta && typeof raw.meta === 'object'
			? (raw.meta as AssistantCatalog['meta'])
			: {};
	return { assistants, meta };
}


/** Coerce one raw assistant wire into the UI shape, or null when unusable. */
function normalizeAssistantDefinition(entry: unknown): AssistantDefinition | null {
	if (!entry || typeof entry !== 'object') return null;
	const e = entry as Record<string, unknown>;
	const id = typeof e.id === 'string' ? e.id : '';
	if (!id) return null;
	const definition: AssistantDefinition = {
		id,
		name: typeof e.name === 'string' ? e.name : id,
		persona: typeof e.persona === 'string' ? e.persona : '',
		style_notes: typeof e.style_notes === 'string' ? e.style_notes : '',
		agent: typeof e.agent === 'string' ? e.agent : 'narrator',
		trigger: typeof e.trigger === 'string' ? e.trigger : 'manual',
		keywords: Array.isArray(e.keywords)
			? e.keywords.filter((k): k is string => typeof k === 'string')
			: [],
		demo: e.demo === true
	};
	if (typeof e.actor === 'string' && e.actor) definition.actor = e.actor;
	return definition;
}


/** Body of `POST/PUT /api/assistants` (D47). `id` on POST upserts in place. */
export interface AssistantWrite {
	id?: string;
	name: string;
	persona: string;
	style_notes?: string;
	agent?: string;
	trigger?: string;
	keywords?: string[];
	/** Optional persona/actor reference (D47); `""` clears it. */
	actor?: string;
}


/** Read one assistant definition from a write response (`{assistant: {...}}`). */
async function readAssistantOrThrow(res: Response, op: string): Promise<AssistantDefinition> {
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `${op} failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const definition = normalizeAssistantDefinition(body.assistant ?? body);
	if (!definition) throw new StreamError(`${op} response missing assistant`);
	return definition;
}


/** Replace or append one mock definition (VITE_MOCK=1 only). */
function upsertMockAssistant(definition: AssistantDefinition): void {
	const index = MOCK_ASSISTANTS.findIndex((entry) => entry.id === definition.id);
	if (index >= 0) MOCK_ASSISTANTS[index] = definition;
	else MOCK_ASSISTANTS.push(definition);
}


/**
 * Create an assistant definition (`POST /api/assistants`, D47).
 *
 * An explicit `id` upserts that definition in place (latest-wins); omitted, the
 * backend mints one. `actor` is the optional persona reference it runs as.
 */
export async function createAssistant(body: AssistantWrite): Promise<AssistantDefinition> {
	if (USE_MOCK) {
		const definition: AssistantDefinition = {
			id: body.id || `mock-assistant-${Math.random().toString(36).slice(2, 8)}`,
			name: body.name,
			persona: body.persona,
			style_notes: body.style_notes ?? '',
			agent: body.agent || 'narrator',
			trigger: body.trigger || 'manual',
			keywords: [...(body.keywords ?? [])],
			demo: false
		};
		if (body.actor) definition.actor = body.actor;
		upsertMockAssistant(definition);
		return { ...definition, keywords: [...definition.keywords] };
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.list(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	return readAssistantOrThrow(res, 'createAssistant');
}


/** Supersede an assistant definition (`PUT /api/assistants/{id}`); 404 unknown. */
export async function updateAssistant(
	assistantId: string,
	body: AssistantWrite
): Promise<AssistantDefinition> {
	if (USE_MOCK) {
		if (!MOCK_ASSISTANTS.some((entry) => entry.id === assistantId)) {
			throw new StreamError(`updateAssistant failed: 404 unknown assistant`);
		}
		const definition: AssistantDefinition = {
			id: assistantId,
			name: body.name,
			persona: body.persona,
			style_notes: body.style_notes ?? '',
			agent: body.agent || 'narrator',
			trigger: body.trigger || 'manual',
			keywords: [...(body.keywords ?? [])],
			demo: false
		};
		if (body.actor) definition.actor = body.actor;
		upsertMockAssistant(definition);
		return { ...definition, keywords: [...definition.keywords] };
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.entry(assistantId), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ ...body, id: assistantId })
	});
	return readAssistantOrThrow(res, 'updateAssistant');
}


/** Tombstone an assistant definition (`DELETE /api/assistants/{id}`); 404 unknown. */
export async function deleteAssistant(assistantId: string): Promise<void> {
	if (USE_MOCK) {
		const index = MOCK_ASSISTANTS.findIndex((entry) => entry.id === assistantId);
		if (index < 0) throw new StreamError('deleteAssistant failed: 404 unknown assistant');
		MOCK_ASSISTANTS.splice(index, 1);
		return;
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.entry(assistantId), { method: 'DELETE' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `deleteAssistant failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
}


/**
 * Consult one assistant. The session's facts + recent prose are read server-side
 * from the event log, so the caller only supplies the trigger message.
 */
export async function consultAssistant(
	sessionId: string,
	assistantId: string,
	userMsg: string
): Promise<AssistantReply> {
	if (USE_MOCK) {
		await new Promise((resolve) => setTimeout(resolve, 120));
		const persona = MOCK_ASSISTANTS.find((a) => a.id === assistantId);
		return {
			assistant: assistantId,
			text: `[MOCK ${persona?.name ?? assistantId}] Considering “${userMsg}” — no backend and no LLM.`,
			agent: persona?.agent ?? 'narrator'
		};
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.consult(sessionId, assistantId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ user_msg: userMsg })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `consultAssistant failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	const body = await readJson(res);
	return {
		assistant: typeof body.assistant === 'string' ? body.assistant : assistantId,
		text: typeof body.text === 'string' ? body.text : '',
		agent: typeof body.agent === 'string' ? body.agent : 'narrator'
	};
}


/** Read back the session's persisted assistant replies (system-event hydration). */
export async function listAssistantReplies(sessionId: string): Promise<AssistantReplyRecord[]> {
	if (USE_MOCK) {
		// The enabled assistant's latest reply, so the demo shows the panel's
		// history section exactly as the live per-turn consult fills it.
		const enabled = mockAssistantBySession.get(sessionId);
		if (!enabled) return [];
		const persona = MOCK_ASSISTANTS.find((a) => a.id === enabled);
		return [
			{
				id: 1,
				seq: 1,
				ts: new Date().toISOString(),
				assistant: enabled,
				text: `[MOCK ${persona?.name ?? enabled}] I read that turn with you — no backend, no LLM.`,
				agent: persona?.agent ?? 'narrator'
			}
		];
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.replies(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`listAssistantReplies failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const list = Array.isArray(body.replies) ? body.replies : [];
	const replies: AssistantReplyRecord[] = [];
	for (const entry of list) {
		if (!entry || typeof entry !== 'object') continue;
		const e = entry as Record<string, unknown>;
		const assistant = typeof e.assistant === 'string' ? e.assistant : '';
		if (!assistant) continue;
		replies.push({
			id: typeof e.id === 'number' ? e.id : 0,
			seq: typeof e.seq === 'number' ? e.seq : undefined,
			ts: typeof e.ts === 'string' ? e.ts : undefined,
			assistant,
			text: typeof e.text === 'string' ? e.text : '',
			agent: typeof e.agent === 'string' ? e.agent : 'narrator',
			user_msg: typeof e.user_msg === 'string' ? e.user_msg : undefined
		});
	}
	return replies;
}


/**
 * The session's enabled assistant id, or `null` when none is on.
 *
 * An absent event means `null`, and a 404 (the route not yet deployed) is
 * treated the same way: enablement is a soft read and must never block the pane.
 */
export async function getSessionAssistant(sessionId: string): Promise<string | null> {
	if (USE_MOCK) return mockAssistantBySession.get(sessionId) ?? null;

	const res = await fetch(ASSISTANT_ENDPOINTS.session(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (res.status === 404) return null;
	if (!res.ok) {
		throw new StreamError(`getSessionAssistant failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return typeof body.assistant_id === 'string' && body.assistant_id ? body.assistant_id : null;
}


/**
 * Set (or clear, with `null`) the session's single assistant slot.
 * Returns the resulting enablement; a failure throws a `StreamError`.
 */
export async function setSessionAssistant(
	sessionId: string,
	assistantId: string | null
): Promise<string | null> {
	if (USE_MOCK) {
		mockAssistantBySession.set(sessionId, assistantId);
		return assistantId;
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.session(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ assistant_id: assistantId })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `setSessionAssistant failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	const body = await readJson(res);
	return typeof body.assistant_id === 'string' && body.assistant_id ? body.assistant_id : null;
}


/**
 * The latest-wins panel statement (`asst = AssistantCard(...)`) for the session,
 * or `null` when the engine has not produced one yet.
 *
 * Latest-wins means a miss is not an error: a 404 (route pending) and an empty
 * body both resolve to `null` so the panel stays quiet instead of flashing one.
 */
export async function getAssistantPanel(sessionId: string): Promise<string | null> {
	if (USE_MOCK) {
		const id = mockAssistantBySession.get(sessionId) ?? null;
		return id ? mockAssistantPanelStatement(id) : null;
	}

	const res = await fetch(ASSISTANT_ENDPOINTS.panel(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (res.status === 404) return null;
	if (!res.ok) {
		throw new StreamError(`getAssistantPanel failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const statement = typeof body.statement === 'string' ? body.statement.trim() : '';
	return statement || null;
}
