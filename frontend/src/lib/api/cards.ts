// Fabulist API client - character-card import.

import { createSession } from './sessions';
import { API_BASE, USE_MOCK, mockSessions, readJson } from './shared';

/** A parsed character card as returned by the import endpoint. */
export interface ImportedCard {
	name: string;
	description: string;
	first_mes?: string | null;
	personality?: string | null;
	scenario?: string | null;
	tags?: string[];
	[k: string]: unknown;
}


/** Response of `POST /api/cards/import` (success or precise failure). */
export interface CardImportResult {
	ok: boolean;
	card?: ImportedCard;
	/** Present when the request also seeded a new chat (`seed: true`). */
	session_id?: string;
	/** Title of the seeded chat (the card name). */
	title?: string;
	/** Present on the definition-only path (`persona: true`, D51 L1.c): the
	 * upserted persona wire — byte-identical to `GET /api/personas` rows. */
	persona?: Record<string, unknown>;
	/** Present on the definition-only world path (`world: true`, issue #60): the
	 * created world wire — byte-identical to `GET /api/worlds` rows. */
	world?: Record<string, unknown>;
	/** The lore book materialized from the card's `character_book`, if any
	 * (world destination only — a lorebook is a bonus, never a requirement). */
	book_id?: string;
	/** Precise parser message when `ok` is false. */
	error?: string;
}


/** Endpoint for card import, kept next to the other endpoint builders. */
export const CARD_IMPORT_ENDPOINT = (): string => `${API_BASE}/api/cards/import`;


/** Resolve the POST body + Content-Type for a card import source. */
async function cardImportBody(
	source: File | Blob | string | Record<string, unknown>
): Promise<{ body: BodyInit; contentType: string }> {
	if (typeof source === 'string') {
		return { body: source, contentType: 'application/json' };
	}
	if (typeof File !== 'undefined' && source instanceof File) {
		const name = source.name.toLowerCase();
		const type = source.type.toLowerCase();
		if (type === 'image/png' || name.endsWith('.png')) {
			return { body: await source.arrayBuffer(), contentType: 'image/png' };
		}
		if (type === 'application/json' || name.endsWith('.json')) {
			return { body: await source.text(), contentType: 'application/json' };
		}
		// Unknown file type: let the backend sniff (PNG signature or JSON).
		return { body: await source.arrayBuffer(), contentType: 'application/octet-stream' };
	}
	if (typeof Blob !== 'undefined' && source instanceof Blob) {
		const type = (source.type || '').toLowerCase();
		if (type === 'image/png') return { body: await source.arrayBuffer(), contentType: 'image/png' };
		if (type === 'application/json') return { body: await source.text(), contentType: 'application/json' };
		return { body: await source.arrayBuffer(), contentType: 'application/octet-stream' };
	}
	return { body: JSON.stringify(source), contentType: 'application/json' };
}


/** In mock mode there is no backend parser: accept inline JSON, reject PNGs. */
async function mockImportCard(
	source: File | Blob | string | Record<string, unknown>,
	seed: boolean,
	target: 'persona' | 'world'
): Promise<CardImportResult> {
	if (target === 'world') {
		// Mock mode has no card parser *and* no world writer for cards: refuse with
		// the same precise style the persona door uses rather than pretend.
		return { ok: false, error: 'importing a card as a world needs the backend (mock mode)' };
	}
	let raw: unknown = source;
	if (typeof File !== 'undefined' && source instanceof File) {
		if (source.name.toLowerCase().endsWith('.json')) {
			try {
				raw = JSON.parse(await source.text());
			} catch {
				return { ok: false, error: 'card JSON is not valid JSON' };
			}
		} else {
			return { ok: false, error: 'PNG card import needs the backend (mock mode)' };
		}
	} else if (typeof source === 'string') {
		try {
			raw = JSON.parse(source);
		} catch {
			return { ok: false, error: 'card JSON is not valid JSON' };
		}
	}
	const obj = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	const card = (obj.card && typeof obj.card === 'object' ? obj.card : obj) as Record<string, unknown>;
	if (typeof card.name !== 'string' || !card.name.trim()) {
		return { ok: false, error: "card is missing a non-empty 'name'" };
	}
	if (typeof card.description !== 'string' || !card.description.trim()) {
		return { ok: false, error: "card is missing a non-empty 'description'" };
	}
	const parsed: ImportedCard = {
		...card,
		name: card.name,
		description: card.description,
		first_mes: typeof card.first_mes === 'string' ? card.first_mes : null
	};
	const result: CardImportResult = { ok: true, card: parsed };
	if (seed) {
		const id = await createSession({ title: parsed.name });
		mockSessions.set(id, {
			id,
			title: parsed.name,
			updated_at: new Date().toISOString(),
			turns: 0,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true
		});
		result.session_id = id;
		result.title = parsed.name;
	}
	return result;
}


/**
 * Import a character card. Parses PNG (`chara` tEXt) or JSON via the backend;
 * when `seed` is true the backend also creates a new chat titled after the card
 * and returns its `session_id` for the caller to open.
 *
 * One entry point for both definition destinations (issue #60): `persona`
 * captures the card as a persona, `world` materializes it as a world — the
 * backend answers at most one of `seed` / `persona` / `world` per request and
 * refuses an ambiguous pair, so no caller has to pick a precedence.
 *
 * Failures resolve (never throw) with `{ ok: false, error }` so a component can
 * render the precise parser message inline.
 */
export async function importCard(
	source: File | Blob | string | Record<string, unknown>,
	options: { seed?: boolean; persona?: boolean; world?: boolean } = {}
): Promise<CardImportResult> {
	const seed = options.seed === true;
	const persona = options.persona === true;
	const world = options.world === true;
	if (persona && USE_MOCK) {
		return { ok: false, error: 'persona card import needs the backend (mock mode)' };
	}
	if (USE_MOCK) return mockImportCard(source, seed, world ? 'world' : 'persona');

	let body: BodyInit;
	let contentType: string;
	try {
		({ body, contentType } = await cardImportBody(source));
	} catch (err) {
		return { ok: false, error: err instanceof Error ? err.message : String(err) };
	}

	const query = seed ? '?seed=1' : persona ? '?persona=1' : world ? '?world=1' : '';
	const url = `${CARD_IMPORT_ENDPOINT()}${query}`;
	try {
		const res = await fetch(url, {
			method: 'POST',
			headers: { 'Content-Type': contentType, Accept: 'application/json' },
			body
		});
		const parsed = await readJson(res);
		if (!res.ok) {
			return { ok: false, error: cardErrorFromBody(parsed) };
		}
		return {
			ok: parsed.ok !== false,
			card: parsed.card as ImportedCard | undefined,
			session_id: typeof parsed.session_id === 'string' ? parsed.session_id : undefined,
			title: typeof parsed.title === 'string' ? parsed.title : undefined,
			persona:
				parsed.persona && typeof parsed.persona === 'object'
					? (parsed.persona as Record<string, unknown>)
					: undefined,
			world:
				parsed.world && typeof parsed.world === 'object'
					? (parsed.world as Record<string, unknown>)
					: undefined,
			book_id: typeof parsed.book_id === 'string' && parsed.book_id ? parsed.book_id : undefined,
			error: typeof parsed.error === 'string' ? parsed.error : undefined
		};
	} catch (err) {
		return { ok: false, error: err instanceof Error ? err.message : String(err) };
	}
}


/** Extract the precise parser message from any error-body shape. */
function cardErrorFromBody(body: Record<string, unknown>): string {
	if (typeof body.error === 'string' && body.error) return body.error;
	const detail = body.detail;
	if (typeof detail === 'string' && detail) return detail;
	if (detail && typeof detail === 'object') {
		const d = detail as Record<string, unknown>;
		if (typeof d.reason === 'string' && d.reason) return d.reason;
	}
	return 'card import failed';
}

// --- assistants (D38 full rename of the W2.5 extra-assistant lane) -----------
// Backend contract: src/fabulist/api/assistants_api.py
//
//   GET  {API_BASE}/api/assistants
//        -> {"assistants": [{id,name,persona,style_notes,agent,trigger,keywords,demo}],
//            "meta": {count, source, session, event, demo}}
//   GET  {API_BASE}/api/sessions/{id}/assistants/replies
//        -> {"session_id", "replies": [{id, seq, ts, assistant, text, agent, user_msg}]}
//   POST {API_BASE}/api/sessions/{id}/assistants/{id}/consult {"user_msg": str}
//        -> {"assistant": str, "text": str, "agent": str}
//        (server appends an `assistant_reply` system event; state never advances)
//
// Per-turn enablement + panel (D38 lane 2b/2c): at most ONE assistant is enabled
// per session, and the enabled assistant owns the pinned panel above the composer.
//
//   GET   {API_BASE}/api/sessions/{id}/assistant        -> {"assistant_id": str|null}
//   PATCH {API_BASE}/api/sessions/{id}/assistant {"assistant_id": str|null}
//                                                       -> {"assistant_id": str|null}
//   GET   {API_BASE}/api/sessions/{id}/assistant-panel  -> {"statement": str}
//        (latest-wins engine statement; 404 until the first consult — not an error)
//
// The backend normalizes its reads server-side (pre-rename logs answer under the
// canonical keys), so this module reads and writes the new spelling only.
