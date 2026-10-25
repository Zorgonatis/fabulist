// Fabulist API client - component-set selection (issues #16/#24).
//
// Selection is PER-SESSION and the swap is a LOG FACT: the POST appends one
// `set_swapped` row and the engine resolves it on the NEXT turn (the taught doc
// sits in the stable prompt prefix, so a swap re-prefills everything after the
// library block once — ADR-025). This client never mutates the running
// vocabulary client-side; it re-reads the catalog's `active_set_id`.
//
//   GET  {API_BASE}/api/component-sets?session_id={sid}
//        -> {active_set_id, default_set_id,
//            sets: [{id, name, version, kind, loadable, reason}]}
//   POST {API_BASE}/api/sessions/{sid}/component-set  {"set_id": str}
//        -> {session_id, set_id, name, version, previous_set_id,
//            changed, applies: "next_turn"}
//        422 -> detail {reason, set_id, unrenderable: [name...]} when the
//        client cannot render the set (ADR-025: set ⊆ union).

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** One declared set, as the picker needs it. */
export interface ComponentSetOption {
	id: string;
	name: string;
	/** The set's own identity version (e.g. `1.0.0`), from the registry. */
	version: string;
	/** `shipped` | `fixture` (declared in prompts/sets/index.json). */
	kind: string;
	/** ADR-025: components ⊆ the client's union — the POST enforces this. */
	loadable: boolean;
	/** Readable reason when `loadable` is false (or the check was skipped). */
	reason: string;
}

export interface ComponentSetsCatalog {
	active_set_id: string;
	default_set_id: string;
	sets: ComponentSetOption[];
}

export interface ComponentSetSelection {
	session_id: string;
	set_id: string;
	name: string;
	version: string;
	previous_set_id: string;
	/** False when the session already used this set (no row appended). */
	changed: boolean;
	/** Always `next_turn`: the swap takes effect at the next narrator build. */
	applies: string;
}

export const COMPONENT_SET_ENDPOINTS = {
	list: (sessionId?: string | null) =>
		`${API_BASE}/api/component-sets${
			sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
		}`,
	select: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/component-set`
} as const;

function normalizeOption(raw: unknown): ComponentSetOption | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = str(obj.id);
	if (!id) return null;
	return {
		id,
		name: str(obj.name) || id,
		version: str(obj.version),
		kind: str(obj.kind) || 'shipped',
		loadable: obj.loadable !== false,
		reason: str(obj.reason)
	};
}

// Mock state: the offline demo has two sets and one session-local selection.
const MOCK_SETS: ComponentSetOption[] = [
	{
		id: 'technology',
		name: 'Technology',
		version: '1.0.0',
		kind: 'shipped',
		loadable: true,
		reason: ''
	},
	{
		id: 'fixture-core',
		name: 'Core-only fixture set (test)',
		version: '0.0.0',
		kind: 'fixture',
		loadable: true,
		reason: ''
	}
];
let mockActiveSetId = 'technology';

/** The catalog plus the set the given session currently uses. */
export async function listComponentSets(
	sessionId?: string | null
): Promise<ComponentSetsCatalog> {
	if (USE_MOCK) {
		return {
			active_set_id: mockActiveSetId,
			default_set_id: 'technology',
			sets: MOCK_SETS.map((set) => ({ ...set }))
		};
	}
	const res = await fetch(COMPONENT_SET_ENDPOINTS.list(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`listComponentSets failed: ${res.status} ${res.statusText}`, {
			status: res.status
		});
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.sets) ? (body.sets as unknown[]) : [];
	return {
		active_set_id: str(body.active_set_id),
		default_set_id: str(body.default_set_id),
		sets: raw.map(normalizeOption).filter((set): set is ComponentSetOption => set !== null)
	};
}

/** Append a per-session set selection (takes effect on the next turn). */
export async function selectComponentSet(
	sessionId: string,
	setId: string
): Promise<ComponentSetSelection> {
	if (USE_MOCK) {
		const previous = mockActiveSetId;
		mockActiveSetId = setId;
		const set = MOCK_SETS.find((candidate) => candidate.id === setId) ?? MOCK_SETS[0];
		return {
			session_id: sessionId,
			set_id: set.id,
			name: set.name,
			version: set.version,
			previous_set_id: previous,
			changed: previous !== set.id,
			applies: 'next_turn'
		};
	}
	const res = await fetch(COMPONENT_SET_ENDPOINTS.select(sessionId), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ set_id: setId })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `selectComponentSet failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return {
		session_id: str(body.session_id) || sessionId,
		set_id: str(body.set_id) || setId,
		name: str(body.name),
		version: str(body.version),
		previous_set_id: str(body.previous_set_id),
		changed: body.changed === true,
		applies: str(body.applies) || 'next_turn'
	};
}
