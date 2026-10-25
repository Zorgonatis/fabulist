// Fabulist API client - card opening block + session module overrides.

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** One pinned Opening-block variant list + the elected index + the card. */
export interface OpeningInfo {
	/** `[first_mes, *alternate_greetings]`; empty when the session has no card. */
	greetings: string[];
	/** Latest-wins selected index (0 when no `greeting_selected` event exists). */
	index: number;
	/** The imported card dict (for the Details view), or null. */
	card: Record<string, unknown> | null;
	/** The card image asset id (PNG imports), or null. */
	cardAsset: string | null;
}


/** One session-carried module home (seeded from a card, editable per session). */
export interface SessionModule {
	module_id: string;
	name: string;
	/** Where the splice drops it: `top` | `post_history`. */
	anchor: string;
	role: string;
	content: string;
	enabled: boolean;
	/** Provenance label, e.g. `card:Lyra Voss`. */
	origin: string;
}


/** Session-override endpoints, kept in one place the orchestrator can rewire. */
export const SESSION_OVERRIDE_ENDPOINTS = {
	opening: (sid: string) => `${API_BASE}/api/sessions/${encodeURIComponent(sid)}/opening`,
	greeting: (sid: string) => `${API_BASE}/api/sessions/${encodeURIComponent(sid)}/greeting`,
	modules: (sid: string) => `${API_BASE}/api/sessions/${encodeURIComponent(sid)}/session-modules`,
	module: (sid: string, moduleId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sid)}/session-modules/${encodeURIComponent(moduleId)}`
} as const;


/** Normalize one raw session-module payload into the UI shape (tolerant). */
export function normalizeSessionModule(raw: unknown): SessionModule | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const moduleId = str(obj.module_id);
	if (!moduleId) return null;
	return {
		module_id: moduleId,
		name: str(obj.name) || moduleId,
		anchor: str(obj.anchor) || 'top',
		role: str(obj.role) || 'system',
		content: str(obj.content),
		enabled: obj.enabled !== false,
		origin: str(obj.origin)
	};
}


// Mock state (module singletons — one browser session per page load).
const MOCK_OPENING_GREETINGS = [
	'The rain drums on the canvas as you push into the night market.',
	'You wake to the smell of salt and tar — the docks, before dawn.'
];

const MOCK_OPENING_CARD: Record<string, unknown> = {
	name: 'Demo Character',
	description: 'A mock card for the offline demo.',
	first_mes: MOCK_OPENING_GREETINGS[0],
	alternate_greetings: [MOCK_OPENING_GREETINGS[1]],
	creator: 'Demo Author',
	character_version: '1.0',
	creator_notes: 'Shown in the mock demo only.',
	extensions: { talkativeness: '0.5', fav: true }
};

let mockOpeningIndex = 0;

let mockSessionModules: SessionModule[] = [
	{
		module_id: 'card:Demo Character:system_prompt',
		name: 'Card system prompt',
		anchor: 'top',
		role: 'system',
		content: 'Stay in the character’s voice.',
		enabled: true,
		origin: 'card:Demo Character'
	}
];


/** Read the pinned Opening block's data (`GET .../opening`). */
export async function getOpening(sessionId: string): Promise<OpeningInfo> {
	if (USE_MOCK) {
		return {
			greetings: [...MOCK_OPENING_GREETINGS],
			index: mockOpeningIndex,
			card: structuredClone(MOCK_OPENING_CARD),
			cardAsset: null
		};
	}
	const res = await fetch(SESSION_OVERRIDE_ENDPOINTS.opening(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`getOpening failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const greetings = Array.isArray(body.greetings)
		? (body.greetings as unknown[]).map((g) => (typeof g === 'string' ? g : String(g ?? '')))
		: [];
	return {
		greetings,
		index: typeof body.index === 'number' ? body.index : 0,
		card: body.card && typeof body.card === 'object' ? (body.card as Record<string, unknown>) : null,
		cardAsset: typeof body.card_asset === 'string' ? body.card_asset : null
	};
}


/** Persist the Opening swipe (`POST .../greeting`; append-only, latest-wins). */
export async function selectGreeting(sessionId: string, index: number): Promise<number> {
	if (USE_MOCK) {
		mockOpeningIndex = index;
		return index;
	}
	const res = await fetch(SESSION_OVERRIDE_ENDPOINTS.greeting(sessionId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ index })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `selectGreeting failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return typeof body.index === 'number' ? body.index : index;
}


/** List a session's module overrides (`GET .../session-modules`). */
export async function getSessionModules(sessionId: string): Promise<SessionModule[]> {
	if (USE_MOCK) return mockSessionModules.map((m) => ({ ...m }));
	const res = await fetch(SESSION_OVERRIDE_ENDPOINTS.modules(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`getSessionModules failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const raw = Array.isArray(body.modules)
		? (body.modules as unknown[])
		: Array.isArray(body)
			? (body as unknown[])
			: [];
	return raw.map(normalizeSessionModule).filter((m): m is SessionModule => m !== null);
}


/**
 * Append a session-module override (`PUT .../session-modules/{module_id}`).
 *
 * The whole module is sent so the backend can append it verbatim as a
 * `session_module` event (edit content, flip `enabled`, or re-anchor). Returns
 * the module as persisted; callers refresh their list from `getSessionModules`.
 */
export async function putSessionModule(
	sessionId: string,
	module: SessionModule
): Promise<SessionModule> {
	if (USE_MOCK) {
		const index = mockSessionModules.findIndex((m) => m.module_id === module.module_id);
		if (index >= 0) mockSessionModules[index] = { ...module };
		else mockSessionModules = [...mockSessionModules, { ...module }];
		return { ...module };
	}
	const res = await fetch(SESSION_OVERRIDE_ENDPOINTS.module(sessionId, module.module_id), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ ...module, event: 'session_module' })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `putSessionModule failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const normalized = normalizeSessionModule(body.module ?? body);
	return normalized ?? module;
}

// --- Wave C: worlds + books (D31/D32) ---------------------------------------
//
// Books (D31, reusable lore libraries) and worlds (D32, scenario + cast + books)
// are log-owned entities. The catalog lives behind `${API_BASE}/api/books` and
// `/api/worlds`; a world chat is materialized by `POST /api/worlds/{id}/chats`.
// Every path is absolute `${API_BASE}` (AGENTS.md gotcha: a relative `/api/...`
// 404s behind vite and gets swallowed by a cosmetic catch).
//
//   GET    /api/books                     -> {books: [Book]}
//   POST   /api/books {name, entries}     -> {book}
//   GET    /api/books/{book_id}           -> {book}
//   PUT    /api/books/{book_id}           -> {book}
//   DELETE /api/books/{book_id}           -> {book_id, deleted}
//   GET    /api/worlds                    -> {worlds: [World]}
//   POST   /api/worlds {name, cast, ...}  -> {world}
//   GET    /api/worlds/{world_id}         -> {world}
//   PUT    /api/worlds/{world_id}         -> {world}
//   DELETE /api/worlds/{world_id}         -> {world_id, deleted}
//   POST   /api/worlds/{world_id}/chats   -> {session_id, title, world_id}
//   GET    /api/sessions/{sid}/world      -> {world: {world_id,name,scenario}|null,
//                                             books: [Book]}
//
// Entries are SillyTavern character-book entries carried VERBATIM by the log
// (unknown keys preserved, D4). The editor's canonical key set mirrors what
// `fabulist.context.world_info.parse_book` actually reads: `keys` / `content` /
// `constant` / `insertion_order` / `case_sensitive` / `comment`, plus `enabled`
// (a `false` entry stays in the book but is dropped at activation). There is no
// `order` field upstream — the sort key is ST's `insertion_order`.
