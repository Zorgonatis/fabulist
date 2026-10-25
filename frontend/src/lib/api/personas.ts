// Fabulist API client - personas, per-chat persona/memory.

import { createSession } from './sessions';
import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson } from './shared';

/** One persona definition from `GET /api/personas` (missing fields default `""`). */
export interface Persona {
	persona_id: string;
	name: string;
	description: string;
	personality: string;
	scenario: string;
	first_mes: string;
	/** Books this cast member carries (D31; refs into the book catalog). */
	book_ids: string[];
	/** Full-fidelity card carry-overs (D28): greeting swipes + raw extensions. */
	alternate_greetings: string[];
	extensions: Record<string, unknown>;
	provenance: Record<string, unknown>;
	/**
	 * Content-address of the definition image; absent/null = no image (W9.16).
	 *
	 * Carried through normalization so `PersonaPane.hydrateAvatars` can tell a
	 * persisted avatar from a bare initials row (#43).
	 */
	image_asset?: string | null;
}


/** The NPC lane-dispatch strategy choices (`PATCH .../chat-config`, W9.24). */
export type NpcStrategy = 'decision' | 'random' | 'lru';

/** One chat's persona + memory + actor + NPC dispatch config (`GET .../chat-config`). */
export interface ChatConfig {
	persona_id: string | null;
	memory_enabled: boolean;
	/**
	 * W9.22: is the player an ACTOR in this chat (default OFF)?
	 *
	 * Session-scoped by design — impersonation is opt-in per chat. ON makes the
	 * player a first-class cast presence and a lane candidate; OFF keeps the
	 * player out of the cast entirely. Read by the engine through
	 * `chat_config_api.resolve_user_as_actor`, never by rewriting an asset.
	 */
	user_as_actor: boolean;
	/**
	 * W9.24 §3.5: how this chat's NPC lanes are chosen (default `decision`).
	 *
	 * `decision` asks the decision model which cast members act this beat;
	 * `random` samples them from a log-seeded draw; `lru` keeps the presence
	 * ladder's own order. A session setting, never an asset rewrite — the
	 * engine reads it through `presence.npc_dispatch_policy`.
	 */
	npc_strategy: NpcStrategy;
	/**
	 * W9.24 §3.5: the most lanes a turn runs (`0` = no bound).
	 *
	 * Orthogonal to the fan-out's concurrency pool: this bounds the SET, that
	 * bounds how many run at once. A zero-lane turn is legal (nothing cleared
	 * the policy) — the narrator writes the scene alone.
	 */
	npc_max_dispatch: number;
	/**
	 * Did the config read reach the engine?
	 *
	 * A transport fact, not a setting: `getChatConfig` deliberately degrades to
	 * safe defaults instead of throwing, so the Cast pane needs one honest signal
	 * to disable a control it could not write (and to say why in its tooltip).
	 */
	reachable: boolean;
}


/** Result of a persona-scoped session create (`POST /api/sessions`). */
export interface SessionCreated {
	session_id: string;
	title: string;
}


/** Persona/memory endpoint paths (kept here so the orchestrator can rewire them). */
export const PERSONA_ENDPOINTS = {
	personas: () => `${API_BASE}/api/personas`,
	/** One persona definition by id (D39 CRUD). */
	entry: (personaId: string) => `${API_BASE}/api/personas/${encodeURIComponent(personaId)}`,
	chatConfig: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/chat-config`,
	persona: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/persona`,
	memory: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/memory`
} as const;


/** Coerce one raw persona row into the UI shape (missing fields -> `""`). */
function normalizePersona(raw: unknown): Persona | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = obj.persona_id ?? obj.id;
	if (typeof id !== 'string' || !id) return null;
	const str = (value: unknown): string => (typeof value === 'string' ? value : '');
	const strList = (value: unknown): string[] =>
		Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
	const mapping = (value: unknown): Record<string, unknown> =>
		value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
	return {
		persona_id: id,
		name: str(obj.name),
		description: str(obj.description),
		personality: str(obj.personality),
		scenario: str(obj.scenario),
		first_mes: str(obj.first_mes),
		book_ids: strList(obj.book_ids),
		alternate_greetings: strList(obj.alternate_greetings),
		extensions: mapping(obj.extensions),
		provenance: mapping(obj.provenance),
		// Content-addressed definition image (W9.16). A card persona's retained PNG
		// arrives here, so a persisted persona renders its art instead of initials.
		image_asset: typeof obj.image_asset === 'string' && obj.image_asset ? obj.image_asset : null
	};
}


/**
 * List persona definitions (`GET /api/personas`).
 *
 * Mock mode has no catalog and resolves to an empty list; the drawer then
 * renders every chat under the plain sandbox group.
 */
export async function listPersonas(): Promise<Persona[]> {
	if (USE_MOCK) return MOCK_PERSONAS.map((persona) => structuredClone(persona));

	const res = await fetch(PERSONA_ENDPOINTS.personas(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`listPersonas failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.personas) ? body.personas : [];
	return raw
		.map((item) => normalizePersona(item))
		.filter((item): item is Persona => item !== null);
}


/** Body of `POST /api/personas` (D39): create, or update when `persona_id` names one. */
export interface PersonaWrite {
	/** Explicit id updates that persona in place (latest-wins); omitted mints one. */
	persona_id?: string;
	name: string;
	description?: string;
	personality?: string;
	scenario?: string;
	first_mes?: string;
	book_ids?: string[];
	alternate_greetings?: string[];
	extensions?: Record<string, unknown>;
	provenance?: Record<string, unknown>;
}


/** Read one persona definition from a write/get response (`{persona: {...}}`). */
async function readPersonaOrThrow(res: Response, op: string): Promise<Persona> {
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `${op} failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const persona = normalizePersona(body.persona ?? body);
	if (!persona) throw new StreamError(`${op} response missing persona`);
	return persona;
}


/** Replace or append one mock persona (VITE_MOCK=1 only). */
function upsertMockPersona(persona: Persona): void {
	const index = MOCK_PERSONAS.findIndex((entry) => entry.persona_id === persona.persona_id);
	if (index >= 0) MOCK_PERSONAS[index] = persona;
	else MOCK_PERSONAS.push(persona);
}


/**
 * Create or update a persona definition (`POST /api/personas`, D39).
 *
 * An explicit `persona_id` supersedes that persona in place (append-only log,
 * latest-wins — D9/D10); omitted, the backend mints one. Resolves to the saved
 * persona so the caller can refresh its catalog row.
 */
export async function createPersona(body: PersonaWrite): Promise<Persona> {
	if (USE_MOCK) {
		const persona: Persona = {
			persona_id:
				body.persona_id || `mock-persona-${Math.random().toString(36).slice(2, 8)}`,
			name: body.name,
			description: body.description ?? '',
			personality: body.personality ?? '',
			scenario: body.scenario ?? '',
			first_mes: body.first_mes ?? '',
			book_ids: [...(body.book_ids ?? [])],
			alternate_greetings: [...(body.alternate_greetings ?? [])],
			extensions: { ...(body.extensions ?? {}) },
			provenance: { ...(body.provenance ?? {}) }
		};
		upsertMockPersona(persona);
		return structuredClone(persona);
	}

	const res = await fetch(PERSONA_ENDPOINTS.personas(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	return readPersonaOrThrow(res, 'createPersona');
}


/** Supersede one persona definition (`POST /api/personas` with its id). */
export async function updatePersona(personaId: string, body: PersonaWrite): Promise<Persona> {
	return createPersona({ ...body, persona_id: personaId });
}


/** Read one persona by id (`GET /api/personas/{id}`); 404 when unknown. */
export async function getPersona(personaId: string): Promise<Persona> {
	if (USE_MOCK) {
		const found = MOCK_PERSONAS.find((persona) => persona.persona_id === personaId);
		if (!found) throw new StreamError('getPersona failed: 404 unknown persona');
		return structuredClone(found);
	}

	const res = await fetch(PERSONA_ENDPOINTS.entry(personaId), {
		headers: { Accept: 'application/json' }
	});
	return readPersonaOrThrow(res, 'getPersona');
}


/** Tombstone a persona (`DELETE /api/personas/{id}`, append-only); 404 unknown. */
export async function deletePersona(personaId: string): Promise<void> {
	if (USE_MOCK) {
		const index = MOCK_PERSONAS.findIndex((persona) => persona.persona_id === personaId);
		if (index < 0) throw new StreamError('deletePersona failed: 404 unknown persona');
		MOCK_PERSONAS.splice(index, 1);
		return;
	}

	const res = await fetch(PERSONA_ENDPOINTS.entry(personaId), { method: 'DELETE' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `deletePersona failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
}


/**
 * Read one chat's config (`GET .../chat-config`: persona + memory + actor + NPC
 * dispatch policy).
 *
 * The drawer calls this lazily for each visible session and caches the result.
 * A failed read falls back to the neutral defaults (`sandbox`, memory on,
 * User-as-Actor off, `decision` dispatch unbounded) with `reachable: false`
 * rather than failing the whole drawer — grouping is best-effort, and a control
 * backed by an unreachable read must render disabled rather than silently
 * optimistic.
 */
export async function getChatConfig(sessionId: string): Promise<ChatConfig> {
	if (USE_MOCK) {
		// No engine in the mock demo: nothing a session setting could be written to.
		return {
			persona_id: null,
			memory_enabled: true,
			user_as_actor: false,
			npc_strategy: 'decision',
			npc_max_dispatch: 0,
			reachable: false
		};
	}

	const res = await fetch(PERSONA_ENDPOINTS.chatConfig(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		return {
			persona_id: null,
			memory_enabled: true,
			user_as_actor: false,
			npc_strategy: 'decision',
			npc_max_dispatch: 0,
			reachable: false
		};
	}
	const body = await readJson(res);
	return {
		persona_id: typeof body.persona_id === 'string' ? body.persona_id : null,
		memory_enabled: typeof body.memory_enabled === 'boolean' ? body.memory_enabled : true,
		user_as_actor: body.user_as_actor === true,
		npc_strategy: normalizeNpcStrategy(body.npc_strategy),
		npc_max_dispatch: normalizeMaxDispatch(body.npc_max_dispatch),
		reachable: true
	};
}

/** The dispatch strategies the engine accepts (`presence.NPC_STRATEGIES`). */
const NPC_STRATEGIES: readonly NpcStrategy[] = ['decision', 'random', 'lru'];

/** A wire value -> a known strategy; anything else falls back to `decision`. */
function normalizeNpcStrategy(value: unknown): NpcStrategy {
	return NPC_STRATEGIES.includes(value as NpcStrategy) ? (value as NpcStrategy) : 'decision';
}

/** A wire value -> a whole number `>= 0` (0 = no bound). */
function normalizeMaxDispatch(value: unknown): number {
	const parsed = typeof value === 'number' ? value : Number.NaN;
	return Number.isInteger(parsed) && parsed >= 0 ? parsed : 0;
}

/**
 * Write the NPC dispatch policy (`PATCH .../chat-config`, W9.24 §3.5).
 *
 * The body is PER-KEY OPTIONAL: a key left `undefined` keeps the engine's
 * current value. Returns the policy the ENGINE folded (the PATCH echoes the
 * whole config read) and throws on failure so the caller can revert its
 * optimistic change and say why.
 */
export async function setNpcDispatch(
	sessionId: string,
	patch: { strategy?: NpcStrategy; maxDispatch?: number }
): Promise<{ npc_strategy: NpcStrategy; npc_max_dispatch: number }> {
	if (USE_MOCK) throw new StreamError('setNpcDispatch: no engine in mock mode');

	const body: Record<string, unknown> = {};
	if (patch.strategy !== undefined) body.npc_strategy = patch.strategy;
	if (patch.maxDispatch !== undefined) body.npc_max_dispatch = patch.maxDispatch;

	const res = await fetch(PERSONA_ENDPOINTS.chatConfig(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) {
		throw new StreamError(`setNpcDispatch failed: ${res.status} ${res.statusText}`);
	}
	const echoed = await readJson(res);
	return {
		npc_strategy: normalizeNpcStrategy(echoed.npc_strategy),
		npc_max_dispatch: normalizeMaxDispatch(echoed.npc_max_dispatch)
	};
}


/**
 * Set a chat's User-as-Actor flag (`PATCH .../chat-config`, W9.22 plan §2).
 *
 * One latest-wins setting event per write (the engine appends, never rewrites),
 * so the state is immediate and survives a reload. Returns the value the ENGINE
 * folded (the PATCH echoes the whole config read) and throws on failure so the
 * caller can revert its optimistic flip and say why.
 */
export async function setUserAsActor(sessionId: string, enabled: boolean): Promise<boolean> {
	if (USE_MOCK) throw new StreamError('setUserAsActor: no engine in mock mode');

	const res = await fetch(PERSONA_ENDPOINTS.chatConfig(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ user_as_actor: enabled })
	});
	if (!res.ok) {
		throw new StreamError(`setUserAsActor failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	return typeof body.user_as_actor === 'boolean' ? body.user_as_actor : enabled;
}


/**
 * Set a chat's memory/embedding toggle (`PATCH .../memory`).
 *
 * Throws on failure so the caller can revert its optimistic flip.
 */
export async function setChatMemory(sessionId: string, enabled: boolean): Promise<void> {
	if (USE_MOCK) return;

	const res = await fetch(PERSONA_ENDPOINTS.memory(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ enabled })
	});
	if (!res.ok) {
		throw new StreamError(`setChatMemory failed: ${res.status} ${res.statusText}`);
	}
}


/**
 * Assign (or clear) a chat's parent persona.
 *
 * `null` sends `DELETE .../persona`; a string sends `POST .../persona`.
 * Throws on failure so the caller can revert its optimistic change.
 */
export async function assignChatPersona(
	sessionId: string,
	personaId: string | null
): Promise<void> {
	if (USE_MOCK) return;

	const res = await fetch(PERSONA_ENDPOINTS.persona(sessionId), {
		method: personaId === null ? 'DELETE' : 'POST',
		headers: personaId === null ? {} : { 'Content-Type': 'application/json' },
		...(personaId === null ? {} : { body: JSON.stringify({ persona_id: personaId }) })
	});
	if (!res.ok) {
		throw new StreamError(`assignChatPersona failed: ${res.status} ${res.statusText}`);
	}
}


/**
 * Create a session, optionally parented to a persona (`POST /api/sessions`).
 *
 * Reuses the existing {@link createSession} transport (mock-aware) so persona
 * and memory handling stay on one path.
 */
export async function createSessionWithPersona(
	title = '',
	personaId: string | null = null
): Promise<SessionCreated> {
	const init: Record<string, unknown> = {};
	if (title) init.title = title;
	if (personaId) init.persona_id = personaId;
	const session_id = await createSession(init);
	return { session_id, title };
}

// --- Prompt Manager (D18) ------------------------------------------------
//
// The prompt pipeline is runtime-editable DATA: ordered typed modules grouped
// into named presets, plus directive toggles, per-component prompt overrides
// and agent bindings. All reads/writes are absolute `${API_BASE}` (AGENTS.md
// gotcha: a relative `/api/...` path 404s behind vite and gets swallowed as a
// cosmetic catch).
//
//   GET  {API_BASE}/api/prompts/presets                 -> {presets: [preset]}
//   POST {API_BASE}/api/prompts/presets {name}          -> {preset}
//   PUT  {API_BASE}/api/prompts/presets/{pid} {fields}  -> {preset}
//   POST {API_BASE}/api/prompts/presets/{pid}/activate  -> 200
//   POST {API_BASE}/api/prompts/presets/deactivate      -> 200
//   GET  {API_BASE}/api/prompts/active                  -> {preset: preset|null}
//   GET  {API_BASE}/api/prompts/variables               -> {variables: [{name,description,example}]}
//   POST {API_BASE}/api/prompts/seed-from-default {name}-> {preset}
//   POST {API_BASE}/api/prompts/preview {session_id,user_text,preset_id?,interaction?}
//                                                         -> {messages,chars,preview_mode}
//        (`interaction` defaults to "narrate")
//
// A preset also carries per-interaction `flows` (narrate/ooc/extract/summarize/
// caption/assistant): the module list for each pipeline. `flows.narrate` and the
// legacy `modules` field are two views of the same list and are merged both ways
// in `normalizePromptPreset`.
//
// The default pipeline is the byte-faithful fallback when no preset is active;
// a preset is created by seeding from it (`seedFromDefault`).


// Mock state (module singletons — one browser session per page load).
export const MOCK_PERSONAS: Persona[] = [
	{
		persona_id: 'persona-lyra',
		name: 'Lyra Voss',
		description: 'A streetwise cartographer who maps the market by night.',
		personality: 'Wry, guarded, quick with a bargain.',
		scenario: 'She keeps a stall of hand-drawn charts near the harbour gate.',
		first_mes: 'You again. Brought coin this time, or just questions?',
		book_ids: ['book-night-market'],
		alternate_greetings: ['Mind the lanterns — they spill.'],
		extensions: { talkativeness: '0.6' },
		provenance: { origin: 'mock' }
	},
	{
		persona_id: 'persona-bran',
		name: 'Bran Okafor',
		description: 'A dock foreman who knows which crates to forget.',
		personality: 'Blunt, loyal, slow to trust.',
		scenario: 'He runs the night shift on the east wharf.',
		first_mes: 'Tide’s turning. Say your piece.',
		book_ids: [],
		alternate_greetings: [],
		extensions: {},
		provenance: { origin: 'mock' }
	}
];
