// Fabulist API client - worlds + books (D31/D32).

import { createSession } from './sessions';
import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** One SillyTavern world-info entry, canonicalized (unknown keys preserved, D4).
 * `insertion_order` is the sort key `world_info.parse_book` reads. */
export interface BookEntry {
	keys: string[];
	content: string;
	constant: boolean;
	insertion_order: number;
	case_sensitive: boolean;
	comment: string;
	/** Activation flag `parse_book` honors — `false` keeps the entry but skips
	 * it at render time. Absent = enabled. */
	enabled?: boolean;
	[key: string]: unknown;
}


/** A reusable lore book (D31). */
export interface Book {
	book_id: string;
	name: string;
	entries: BookEntry[];
	provenance: Record<string, unknown>;
}


/** A reusable world (D32): scenario + cast persona refs + book refs. */
export interface World {
	world_id: string;
	name: string;
	description: string;
	/** Context, never the opening: renders in the narrator's `### World` block. */
	scenario: string;
	/** The world's OPENING first message (issue #51) — the card's `first_mes`.
	 * Empty when the world has none: the Opening block hides (same degradation
	 * as an empty persona greeting). */
	first_mes: string;
	/** Swipe openings for the world's first message (the card's
	 * `alternate_greetings`), in card order. */
	alternate_greetings: string[];
	cast: string[];
	book_ids: string[];
	provenance: Record<string, unknown>;
	/** Content-address of the definition image; absent/null = no image (W9.16). */
	image_asset?: string | null;
}


/** The world snapshot + registered books a session carries (D32). */
export interface SessionWorld {
	world: {
		world_id: string;
		name: string;
		scenario: string;
		/** The opening snapshot the chat was seeded with (issue #51). */
		first_mes?: string;
		alternate_greetings?: string[];
	} | null;
	books: Book[];
}


/** Result of materializing a world into a chat (`POST /api/worlds/{id}/chats`). */
export interface WorldChatCreated {
	session_id: string;
	title: string;
	world_id: string;
}

/** Result of `POST /api/worlds/import` (issue #40): a card materialized as a
 * world. Failures RESOLVE (never throw) with `{ok: false, error}` so the studio
 * can render the backend's precise message inline. */
export interface WorldCardImportResult {
	ok: boolean;
	/** The created world wire — the same shape `GET /api/worlds` rows fold to. */
	world?: World;
	/** The lore Book materialized from the card's `character_book` (signature-
	 * deduped); absent when the card carried none — a lorebook is a bonus, not a
	 * requirement (owner ruling D3, 2026-10-24). */
	book_id?: string;
	/** Precise parser message when `ok` is false. */
	error?: string;
}


/** World + book + session-world endpoint paths (kept rewirable in one place). */
export const WORLD_BOOK_ENDPOINTS = {
	worlds: () => `${API_BASE}/api/worlds`,
	world: (worldId: string) => `${API_BASE}/api/worlds/${encodeURIComponent(worldId)}`,
	worldChats: (worldId: string) =>
		`${API_BASE}/api/worlds/${encodeURIComponent(worldId)}/chats`,
	/** Issue #40: raw card bytes (PNG or card JSON) -> a new world. */
	worldImport: () => `${API_BASE}/api/worlds/import`,
	books: () => `${API_BASE}/api/books`,
	book: (bookId: string) => `${API_BASE}/api/books/${encodeURIComponent(bookId)}`,
	sessionWorld: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/world`,
	sessionBooks: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/books`
} as const;


// Keys the editor owns; every other entry key is unknown and rides through
// verbatim (D4: "unknown keys MUST be preserved").
const BOOK_ENTRY_KNOWN_KEYS = new Set([
	'keys',
	'key',
	'content',
	'constant',
	'insertion_order',
	'order',
	'case_sensitive',
	'comment',
	'enabled',
	'disable',
	'disabled'
]);


/** Tolerant JSON-ish bool (mirrors `world_info._as_bool`). */
function bookBool(value: unknown, fallback: boolean): boolean {
	if (value === undefined || value === null) return fallback;
	if (typeof value === 'boolean') return value;
	if (typeof value === 'number') return value !== 0;
	if (typeof value === 'string') {
		return !['', '0', 'false', 'no', 'off'].includes(value.trim().toLowerCase());
	}
	return fallback;
}


/** Tolerant JSON-ish int (mirrors `world_info._as_int`). */
function bookInt(value: unknown, fallback: number): number {
	if (value === undefined || value === null || typeof value === 'boolean') return fallback;
	const n = typeof value === 'number' ? value : Number.parseInt(String(value), 10);
	return Number.isFinite(n) ? Math.trunc(n) : fallback;
}


/** Keys from a wire `keys` list or the legacy singular `key`; empties dropped. */
function bookKeys(value: unknown): string[] {
	const list = value === undefined || value === null ? [] : Array.isArray(value) ? value : [value];
	const out: string[] = [];
	for (const item of list) {
		const key = typeof item === 'string' ? item : typeof item === 'number' ? String(item) : '';
		if (key) out.push(key);
	}
	return out;
}


/** Canonicalize one raw entry, preserving unknown keys (D4). */
export function normalizeBookEntry(raw: unknown): BookEntry {
	const src = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	const entry: Record<string, unknown> = {};
	for (const [key, value] of Object.entries(src)) {
		if (!BOOK_ENTRY_KNOWN_KEYS.has(key)) entry[key] = value;
	}
	entry.keys = bookKeys(src.keys ?? src.key);
	entry.content = typeof src.content === 'string' ? src.content : '';
	entry.constant = bookBool(src.constant, false);
	entry.insertion_order = bookInt(src.insertion_order ?? src.order, 0);
	entry.case_sensitive = bookBool(src.case_sensitive, false);
	entry.comment = typeof src.comment === 'string' ? src.comment : '';
	entry.enabled =
		'enabled' in src
			? bookBool(src.enabled, true)
			: 'disable' in src
				? !bookBool(src.disable, false)
				: 'disabled' in src
					? !bookBool(src.disabled, false)
					: true;
	return entry as BookEntry;
}


/** Coerce a raw book wire into the UI shape, or null when unusable. */
export function normalizeBook(raw: unknown): Book | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const bookId = str(obj.book_id);
	if (!bookId) return null;
	const rawEntries = Array.isArray(obj.entries) ? obj.entries : [];
	return {
		book_id: bookId,
		name: str(obj.name),
		entries: rawEntries.map((entry) => normalizeBookEntry(entry)),
		provenance:
			obj.provenance && typeof obj.provenance === 'object'
				? (obj.provenance as Record<string, unknown>)
				: {}
	};
}


/** Coerce a raw world wire into the UI shape, or null when unusable. */
export function normalizeWorld(raw: unknown): World | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const worldId = str(obj.world_id);
	if (!worldId) return null;
	const strList = (value: unknown): string[] =>
		Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
	return {
		world_id: worldId,
		name: str(obj.name),
		description: str(obj.description),
		scenario: str(obj.scenario),
		// The world's opening (issue #51): the card's `first_mes` /
		// `alternate_greetings` under the persona names. Absent (a legacy world
		// imported before the fix) degrades to "no opening".
		first_mes: str(obj.first_mes),
		alternate_greetings: strList(obj.alternate_greetings),
		cast: strList(obj.cast),
		book_ids: strList(obj.book_ids),
		provenance:
			obj.provenance && typeof obj.provenance === 'object'
				? (obj.provenance as Record<string, unknown>)
				: {},
		// Content-addressed definition image (W9.16). A card world's retained PNG
		// arrives here, so the imported world round-trips with its artwork.
		image_asset: typeof obj.image_asset === 'string' && obj.image_asset ? obj.image_asset : null
	};
}



export let mockBooks: Book[] = [
	{
		book_id: 'book-night-market',
		name: 'Night Market lore',
		entries: [
			{
				keys: ['night market', 'market'],
				content: 'Lanterns and salt air; the stalls never close before dawn.',
				constant: false,
				insertion_order: 10,
				case_sensitive: false,
				comment: 'Setting',
				enabled: true
			},
			{
				keys: [],
				content: 'The fog rolls in off the harbour at dusk and hides the far piers.',
				constant: true,
				insertion_order: 5,
				case_sensitive: false,
				comment: 'Always on',
				enabled: true
			}
		],
		provenance: { origin: 'mock' }
	}
];


export let mockWorlds: World[] = [
	{
		world_id: 'world-aurora',
		name: 'Aurora Falls',
		description: 'A rain-soaked harbour town of cartographers and smugglers.',
		scenario: 'It always rains in Aurora Falls, and everyone owes someone.',
		first_mes: 'The rain drums on the market canvases as a stranger steps under the awning.',
		alternate_greetings: ['You wake to the smell of salt and tar — the docks, before dawn.'],
		cast: ['persona-lyra', 'persona-bran'],
		book_ids: ['book-night-market'],
		provenance: { origin: 'mock' }
	}
];


/** Session id -> world snapshot + books, for mock `getSessionWorld`. */
const mockSessionWorlds = new Map<string, SessionWorld>();


/**
 * Deep-copy mock data as plain JSON. Svelte 5 `$state` values are `Proxy`
 * objects and `structuredClone` refuses to clone a proxy, so the mock layer
 * goes through JSON (its data is JSON-only by construction).
 */
function mockClone<T>(value: T): T {
	return JSON.parse(JSON.stringify(value)) as T;
}


async function readEntityOrThrow<T>(
	res: Response,
	op: string,
	normalize: (raw: unknown) => T | null
): Promise<T> {
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `${op} failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const value = normalize(body.book ?? body.world ?? body);
	if (!value) throw new StreamError(`${op} response missing entity`);
	return value;
}

// -- books (D31) -------------------------------------------------------------


/** List the reusable lore books (`GET /api/books`). */
export async function listBooks(): Promise<Book[]> {
	if (USE_MOCK) return mockBooks.map((book) => mockClone(book));
	const res = await fetch(WORLD_BOOK_ENDPOINTS.books(), { headers: { Accept: 'application/json' } });
	if (!res.ok) throw new StreamError(`listBooks failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const raw = Array.isArray(body.books) ? body.books : [];
	return raw.map((item) => normalizeBook(item)).filter((book): book is Book => book !== null);
}


/** Read one book by id (`GET /api/books/{book_id}`). */
export async function getBook(bookId: string): Promise<Book> {
	if (USE_MOCK) {
		const found = mockBooks.find((book) => book.book_id === bookId);
		if (!found) throw new StreamError('getBook failed: 404');
		return mockClone(found);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.book(bookId), {
		headers: { Accept: 'application/json' }
	});
	return readEntityOrThrow(res, 'getBook', normalizeBook);
}


/** Create a book (`POST /api/books`); resolves to the persisted book. */
export async function createBook(body: {
	name: string;
	entries?: BookEntry[];
	provenance?: Record<string, unknown>;
}): Promise<Book> {
	if (USE_MOCK) {
		const book: Book = {
			book_id: `mock-book-${Math.random().toString(36).slice(2, 8)}`,
			name: body.name,
			entries: (body.entries ?? []).map((entry) => normalizeBookEntry(entry)),
			provenance: body.provenance ?? {}
		};
		mockBooks = [...mockBooks, book];
		return mockClone(book);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.books(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			name: body.name,
			entries: body.entries ?? [],
			provenance: body.provenance ?? {}
		})
	});
	return readEntityOrThrow(res, 'createBook', normalizeBook);
}


/** Supersede a book definition (`PUT /api/books/{book_id}`); latest-wins. */
export async function putBook(
	bookId: string,
	body: { name: string; entries: BookEntry[]; provenance?: Record<string, unknown> }
): Promise<Book> {
	if (USE_MOCK) {
		const next: Book = {
			book_id: bookId,
			name: body.name,
			entries: body.entries.map((entry) => normalizeBookEntry(entry)),
			provenance: body.provenance ?? {}
		};
		const index = mockBooks.findIndex((book) => book.book_id === bookId);
		mockBooks = index >= 0 ? mockBooks.map((b, i) => (i === index ? next : b)) : [...mockBooks, next];
		return mockClone(next);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.book(bookId), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			name: body.name,
			entries: body.entries,
			provenance: body.provenance ?? {}
		})
	});
	return readEntityOrThrow(res, 'putBook', normalizeBook);
}


/** Tombstone a book (`DELETE /api/books/{book_id}`). */
export async function deleteBook(bookId: string): Promise<void> {
	if (USE_MOCK) {
		mockBooks = mockBooks.filter((book) => book.book_id !== bookId);
		return;
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.book(bookId), { method: 'DELETE' });
	if (!res.ok) throw new StreamError(`deleteBook failed: ${res.status} ${res.statusText}`);
}

// -- worlds (D32) ------------------------------------------------------------


/** List the reusable worlds (`GET /api/worlds`). */
export async function listWorlds(): Promise<World[]> {
	if (USE_MOCK) return mockWorlds.map((world) => mockClone(world));
	const res = await fetch(WORLD_BOOK_ENDPOINTS.worlds(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`listWorlds failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const raw = Array.isArray(body.worlds) ? body.worlds : [];
	return raw.map((item) => normalizeWorld(item)).filter((world): world is World => world !== null);
}


/** Read one world by id (`GET /api/worlds/{world_id}`). */
export async function getWorld(worldId: string): Promise<World> {
	if (USE_MOCK) {
		const found = mockWorlds.find((world) => world.world_id === worldId);
		if (!found) throw new StreamError('getWorld failed: 404');
		return mockClone(found);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.world(worldId), {
		headers: { Accept: 'application/json' }
	});
	return readEntityOrThrow(res, 'getWorld', normalizeWorld);
}


/** Create or update a world (`POST /api/worlds`); resolves to the world. */
export async function createWorld(body: {
	name: string;
	description?: string;
	scenario?: string;
	first_mes?: string;
	alternate_greetings?: string[];
	cast?: string[];
	book_ids?: string[];
	provenance?: Record<string, unknown>;
}): Promise<World> {
	if (USE_MOCK) {
		const world: World = {
			world_id: `mock-world-${Math.random().toString(36).slice(2, 8)}`,
			name: body.name,
			description: body.description ?? '',
			scenario: body.scenario ?? '',
			first_mes: body.first_mes ?? '',
			alternate_greetings: body.alternate_greetings ?? [],
			cast: body.cast ?? [],
			book_ids: body.book_ids ?? [],
			provenance: body.provenance ?? {}
		};
		mockWorlds = [...mockWorlds, world];
		return mockClone(world);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.worlds(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			name: body.name,
			description: body.description ?? '',
			scenario: body.scenario ?? '',
			// Omitted when the caller carries no opening: the backend keeps the
			// stored one on an update (issue #51) rather than erasing it.
			...(body.first_mes !== undefined ? { first_mes: body.first_mes } : {}),
			...(body.alternate_greetings !== undefined
				? { alternate_greetings: body.alternate_greetings }
				: {}),
			cast: body.cast ?? [],
			book_ids: body.book_ids ?? [],
			provenance: body.provenance ?? {}
		})
	});
	return readEntityOrThrow(res, 'createWorld', normalizeWorld);
}


/** Supersede a world definition (`PUT /api/worlds/{world_id}`); latest-wins. */
export async function updateWorld(
	worldId: string,
	body: {
		name: string;
		description?: string;
		scenario?: string;
		first_mes?: string;
		alternate_greetings?: string[];
		cast?: string[];
		book_ids?: string[];
		provenance?: Record<string, unknown>;
	}
): Promise<World> {
	if (USE_MOCK) {
		const previous = mockWorlds.find((world) => world.world_id === worldId);
		const next: World = {
			world_id: worldId,
			name: body.name,
			description: body.description ?? '',
			scenario: body.scenario ?? '',
			// Omission keeps the stored opening (mirrors the backend, issue #51).
			first_mes: body.first_mes ?? previous?.first_mes ?? '',
			alternate_greetings: body.alternate_greetings ?? previous?.alternate_greetings ?? [],
			cast: body.cast ?? [],
			book_ids: body.book_ids ?? [],
			provenance: body.provenance ?? {}
		};
		const index = mockWorlds.findIndex((world) => world.world_id === worldId);
		mockWorlds = index >= 0 ? mockWorlds.map((w, i) => (i === index ? next : w)) : [...mockWorlds, next];
		return mockClone(next);
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.world(worldId), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			name: body.name,
			description: body.description ?? '',
			scenario: body.scenario ?? '',
			...(body.first_mes !== undefined ? { first_mes: body.first_mes } : {}),
			...(body.alternate_greetings !== undefined
				? { alternate_greetings: body.alternate_greetings }
				: {}),
			cast: body.cast ?? [],
			book_ids: body.book_ids ?? [],
			provenance: body.provenance ?? {}
		})
	});
	return readEntityOrThrow(res, 'updateWorld', normalizeWorld);
}


/** Tombstone a world (`DELETE /api/worlds/{world_id}`). */
export async function deleteWorld(worldId: string): Promise<void> {
	if (USE_MOCK) {
		mockWorlds = mockWorlds.filter((world) => world.world_id !== worldId);
		return;
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.world(worldId), { method: 'DELETE' });
	if (!res.ok) throw new StreamError(`deleteWorld failed: ${res.status} ${res.statusText}`);
}


// -- card imports (issue #40) ------------------------------------------------
//
// A character card (PNG with an embedded STv2 payload, or card JSON) can be
// imported as a WORLD: the card's own prose becomes the world definition and
// its `character_book` is materialized into a reusable Book (signature-deduped
// server-side). Owner ruling D3 (2026-10-24): ANY valid card imports — the
// lorebook is a bonus, not a requirement; only non-card bodies are rejected.
//
//   POST {API_BASE}/api/worlds/import   (raw card bytes)
//        -> {"ok": true, "world": wire, "book_id": str|null}
//        422 {"detail": {...}} for a non-card body (rendered inline)
//
// Body resolution (byte vs text + Content-Type) mirrors the sibling
// `cards.ts:cardImportBody`: PNG -> ArrayBuffer/`image/png`, JSON ->
// text/`application/json`, anything else -> ArrayBuffer/`application/octet-stream`
// and let the backend sniff (PNG signature or JSON). Absolute `${API_BASE}` per
// the AGENTS.md gotcha (a relative `/api/...` 404s behind vite).

/** Resolve the POST body + Content-Type for a card import source. */
async function worldCardImportBody(
	source: File | Blob
): Promise<{ body: BodyInit; contentType: string }> {
	if (typeof File !== 'undefined' && source instanceof File) {
		const name = source.name.toLowerCase();
		const type = (source.type || '').toLowerCase();
		if (type === 'image/png' || name.endsWith('.png')) {
			return { body: await source.arrayBuffer(), contentType: 'image/png' };
		}
		if (type === 'application/json' || name.endsWith('.json')) {
			return { body: await source.text(), contentType: 'application/json' };
		}
		// Unknown file type: let the backend sniff (PNG signature or JSON).
		return { body: await source.arrayBuffer(), contentType: 'application/octet-stream' };
	}
	const type = (source.type || '').toLowerCase();
	if (type === 'image/png') return { body: await source.arrayBuffer(), contentType: 'image/png' };
	if (type === 'application/json') return { body: await source.text(), contentType: 'application/json' };
	return { body: await source.arrayBuffer(), contentType: 'application/octet-stream' };
}

/** Extract the precise rejection message from any error-body shape (422). */
function worldCardErrorFromBody(body: Record<string, unknown>): string {
	if (typeof body.error === 'string' && body.error) return body.error;
	const detail = body.detail;
	if (typeof detail === 'string' && detail) return detail;
	if (Array.isArray(detail) && detail.length > 0) {
		const first = detail[0] as Record<string, unknown> | null;
		if (first && typeof first.msg === 'string' && first.msg) return first.msg;
		return 'world card import rejected';
	}
	if (detail && typeof detail === 'object') {
		const d = detail as Record<string, unknown>;
		// The route's frozen shape nests the parser message; accept the sibling
		// key spellings so a wording change never blanks the inline error.
		for (const key of ['reason', 'message', 'error', 'detail']) {
			const value = d[key];
			if (typeof value === 'string' && value) return value;
		}
	}
	return 'world card import failed';
}


/**
 * Import a character card (PNG / card JSON) as a NEW world (issue #40).
 *
 * The backend parses the card, materializes its `character_book` when present
 * and retains the PNG as the world's definition image; `cast` stays empty (the
 * Persona studio owns persona capture — no cross-channel write). Mock mode has
 * no card parser, so it degrades to the sibling card import's precise refusal
 * style rather than pretending to import (owner ruling D2).
 *
 * Never throws: every failure resolves `{ok: false, error}`.
 */
export async function importWorldCard(source: File | Blob): Promise<WorldCardImportResult> {
	if (USE_MOCK) return { ok: false, error: 'world card import needs the backend (mock mode)' };

	let body: BodyInit;
	let contentType: string;
	try {
		({ body, contentType } = await worldCardImportBody(source));
	} catch (err) {
		return { ok: false, error: err instanceof Error ? err.message : String(err) };
	}

	try {
		const res = await fetch(WORLD_BOOK_ENDPOINTS.worldImport(), {
			method: 'POST',
			headers: { 'Content-Type': contentType, Accept: 'application/json' },
			body
		});
		const parsed = await readJson(res);
		if (!res.ok) return { ok: false, error: worldCardErrorFromBody(parsed) };
		const world = normalizeWorld(parsed.world);
		// `ok: true` without a usable world would crash a caller's next line; treat
		// it as the failure it is rather than a silent empty success.
		if (parsed.ok === false || !world) {
			return { ok: false, error: worldCardErrorFromBody(parsed) };
		}
		const bookId = str(parsed.book_id);
		return { ok: true, world, book_id: bookId || undefined };
	} catch (err) {
		return { ok: false, error: err instanceof Error ? err.message : String(err) };
	}
}

/**
 * Materialize a world into a new chat (`POST /api/worlds/{world_id}/chats`).
 *
 * The backend seeds the cast as card events, snapshots the world and registers
 * its books, then the chat is ordinary. Callers open the returned session id.
 */
export async function startWorldChat(worldId: string, title?: string): Promise<WorldChatCreated> {
	if (USE_MOCK) {
		const world = mockWorlds.find((w) => w.world_id === worldId);
		if (!world) throw new StreamError('startWorldChat failed: 404');
		const chatTitle = title?.trim() || world.name;
		const session_id = await createSession({ title: chatTitle, world_id: world.world_id });
		mockSessionWorlds.set(session_id, {
			world: {
				world_id: world.world_id,
				name: world.name,
				scenario: world.scenario,
				first_mes: world.first_mes,
				alternate_greetings: [...world.alternate_greetings]
			},
			books: mockBooks
				.filter((book) => world.book_ids.includes(book.book_id))
				.map((book) => mockClone(book))
		});
		return { session_id, title: chatTitle, world_id: world.world_id };
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.worldChats(worldId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ title: title?.trim() || null })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `startWorldChat failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const session_id = str(body.session_id);
	if (!session_id) throw new StreamError('startWorldChat response missing session id');
	return { session_id, title: str(body.title), world_id: str(body.world_id) || worldId };
}


/**
 * Read a session's world origin + registered books (`GET .../world`, D32).
 *
 * Returns `{world: null, books: []}` for an ordinary (legacy) session, and for
 * a 404 the read is treated as "no world" rather than an error — the drawer's
 * world section is additive and must never block the chat list.
 */
export async function getSessionWorld(sessionId: string): Promise<SessionWorld> {
	if (USE_MOCK) return mockSessionWorlds.get(sessionId) ?? { world: null, books: [] };
	const res = await fetch(WORLD_BOOK_ENDPOINTS.sessionWorld(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (res.status === 404) return { world: null, books: [] };
	if (!res.ok) throw new StreamError(`getSessionWorld failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const worldRaw = body.world && typeof body.world === 'object' ? (body.world as Record<string, unknown>) : null;
	const rawBooks = Array.isArray(body.books) ? body.books : [];
	return {
		world: worldRaw
			? {
					world_id: str(worldRaw.world_id),
					name: str(worldRaw.name),
					scenario: str(worldRaw.scenario),
					first_mes: str(worldRaw.first_mes),
					alternate_greetings: Array.isArray(worldRaw.alternate_greetings)
						? worldRaw.alternate_greetings.filter(
								(value): value is string => typeof value === 'string'
							)
						: []
				}
			: null,
		books: rawBooks.map((item) => normalizeBook(item)).filter((book): book is Book => book !== null)
	};
}


/** The registered book set after `POST /api/sessions/{id}/books`. */
export interface SessionBooksResult {
	session_id: string;
	book_ids: string[];
}


/**
 * Attach a chat's book set from the catalog (`POST .../books`, D40 gap).
 *
 * Appends a `session_books` system event (latest-wins, D31); an explicit `[]`
 * clears the attachment, and the backend rejects an unknown id with a 422.
 * Uses absolute `${API_BASE}` (AGENTS.md gotcha: a relative `/api/...` 404s
 * behind vite and gets swallowed by a cosmetic catch).
 */
export async function attachSessionBooks(
	sessionId: string,
	bookIds: string[]
): Promise<SessionBooksResult> {
	if (USE_MOCK) {
		const existing = mockSessionWorlds.get(sessionId) ?? { world: null, books: [] };
		const books = bookIds
			.map((id) => mockBooks.find((book) => book.book_id === id))
			.filter((book): book is Book => book !== undefined)
			.map((book) => mockClone(book));
		mockSessionWorlds.set(sessionId, { world: existing.world, books });
		return { session_id: sessionId, book_ids: books.map((book) => book.book_id) };
	}
	const res = await fetch(WORLD_BOOK_ENDPOINTS.sessionBooks(sessionId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ book_ids: bookIds })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `attachSessionBooks failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const ids = Array.isArray(body.book_ids)
		? body.book_ids.filter((id): id is string => typeof id === 'string')
		: [];
	return { session_id: str(body.session_id) || sessionId, book_ids: ids };
}

// --- Wave D: experience profiles (D33) ---------------------------------------
//
// A profile (D33) is a portable, definitions-only bundle of a world (+ its
// inlined cast and books), a prompt preset, and (opt-in) the assistant roster.
// Export is a JSON download; import supersedes the matching anchor folds.
// Both paths are absolute `${API_BASE}` (AGENTS.md gotcha: a relative
// `/api/...` 404s behind vite and gets swallowed by a cosmetic catch).
//
//   GET  {API_BASE}/api/profiles/export?preset=<id>[&world=<id>][&assistants=1][&name=<str>]
//        -> the v1 envelope as a JSON attachment (Content-Disposition filename)
//           `preset` is REQUIRED; `world` + `assistants` are opt-in. An unknown
//           world/preset id is a precise 404.
//   POST {API_BASE}/api/profiles/import   (body = the envelope)
//        -> {imported: [{kind,id,name?,count?}], skipped: [same],
//            warnings: [{type: "agent_binding"|"preset_id", ...}]}
//
// ABSENT != EMPTY for assistants (owner-ratified amendment to D33): an envelope
// without an `assistants` key NEVER touches the local roster; only an explicit
// `assistants: []` clears it. The export toggle therefore defaults ON, and the
// UI gates an explicit-`[]` import behind a roster-clear confirmation. Only the
// new spelling is ever written; the pre-rename roster key is folded on read by
// `envelopeAssistantRoster` so an old file's pre-flight summary is accurate.
