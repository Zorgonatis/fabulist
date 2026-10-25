// Fabulist API client - shared runtime spine (base URL, error class, endpoints, helpers).

import type { SessionSummary } from './types';

/** A structured stream failure (HTTP error, server `error` event, dropped stream). */
export class StreamError extends Error {
	readonly meta?: Record<string, unknown>;

	constructor(message: string, meta?: Record<string, unknown>) {
		super(message);
		this.name = 'StreamError';
		this.meta = meta;
	}
}

/**
 * Same-origin by default: the engine serves the built frontend, so a production
 * build talks to `/api/...` on its own origin (``API_BASE`` is the empty
 * string — every consumer writes ``${API_BASE}/api/...``, which stays relative).
 * Only the Vite dev server runs on a different origin (:5173 -> :2727), and a
 * `VITE_API_BASE` env var still overrides both (spare-port lanes).
 */
const API_BASE_FALLBACK = import.meta.env.DEV ? 'http://localhost:2727' : '';

export const API_BASE: string = (import.meta.env.VITE_API_BASE ?? API_BASE_FALLBACK).replace(
	/\/+$/,
	''
);

export const USE_MOCK: boolean = import.meta.env.VITE_MOCK === '1';


/** In-memory session book for mock mode (no backend to derive it from). */
export const mockSessions = new Map<string, SessionSummary>();

/**
 * Mock-mode seed session ids (W9.26 demo data). The projection/recap/branches
 * bodies for these live in `#lib/mock/mockApi` (which is code-split out of the
 * production bundle); the LIST rows live here so `listSessions()` — called by
 * the chat page before any mock module has loaded — already shows them.
 */
export const MOCK_SEED_SESSIONS = {
	/** Two-era chain: the current era (break + notice + settled row + new turns). */
	eraNow: 'mock-era-now',
	/** The compaction continuation of `eraNow` (a `kind:"compaction"` child). */
	eraNext: 'mock-era-next',
	/** A THIRD era: two breaks in one chain, so each collapse chevron's span is demoable. */
	eraFinal: 'mock-era-final',
	/** A plain swipe alternate of `eraNow` (a `kind:"user"` child). */
	swipeAlt: 'mock-swipe-alt',
	/** Single-era chat demoing the plain / noop / over_budget context-low variants. */
	contextLow: 'mock-context-low'
} as const;

if (USE_MOCK) {
	const now = new Date().toISOString();
	const seed: SessionSummary[] = [
		{
			id: MOCK_SEED_SESSIONS.eraNow,
			title: 'Rusty Anchor — two eras',
			updated_at: now,
			turns: 6,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true,
			compacted: true
		},
		{
			id: MOCK_SEED_SESSIONS.eraNext,
			title: 'Rusty Anchor — two eras',
			updated_at: now,
			turns: 2,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true,
			compacted: false
		},
		{
			id: MOCK_SEED_SESSIONS.eraFinal,
			title: 'Rusty Anchor — three eras',
			updated_at: now,
			turns: 1,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true,
			compacted: true
		},
		{
			id: MOCK_SEED_SESSIONS.swipeAlt,
			title: 'Rusty Anchor — alternate line',
			updated_at: now,
			turns: 3,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true,
			compacted: false
		},
		{
			id: MOCK_SEED_SESSIONS.contextLow,
			title: 'Night market — context low',
			updated_at: now,
			turns: 4,
			persona_id: null,
			persona_cleared: false,
			world_id: null,
			memory_enabled: true,
			compacted: false
		}
	];
	for (const row of seed) mockSessions.set(row.id, row);
}


/** Endpoint paths, kept in one place so the orchestrator can rewire them. */
export const ENDPOINTS = {
	sessions: () => `${API_BASE}/api/sessions`,
	settings: () => `${API_BASE}/api/settings`,
	session: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}`,
	projection: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/events`,
	branches: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/branches`,
	fork: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/fork`,
	/** Issue #35: delete a message and everything after it (a fork just below the
	 *  pivot). 200 `{session_id, base_session, base_seq, deleted_count}`; 409
	 *  `{detail:{reason:"first_message"|"pivot_missing"|"not_a_message"}}`. */
	deleteTail: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/delete-tail`,
	turn: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/turns`,
	stream: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/stream`,
	stop: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/stop`,
	/** W9.26: the manual compaction door — 202 `{job_id}` (a claim already held
	 *  answers 409 `{detail:{reason:"compacting"}}`; a superseded parent answers
	 *  the idempotent successor instead). */
	compact: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/compact`,
	/** W9.26: the job-status poll (`{status, session_id?, title?, error?, stage?}`). */
	compactionJob: (sessionId: string, jobId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/context/compaction/${encodeURIComponent(jobId)}`
} as const;


type MockApi = typeof import('#lib/mock/mockApi')['mockApi'];


let mockModule: Promise<MockApi> | null = null;


/** Lazily load the mock so it is code-split out of the production bundle. */
export function loadMock(): Promise<MockApi> {
	mockModule ??= import('#lib/mock/mockApi').then((m) => m.mockApi);
	return mockModule;
}


/** Extract a human-readable reason from a FastAPI error body. */
export async function readErrorDetail(
	res: Response
): Promise<{ reason: string; status: number; id?: string }> {
	let reason = '';
	let id: string | undefined;
	try {
		const body = (await res.json()) as Record<string, unknown>;
		const detail = body.detail;
		if (typeof detail === 'string') reason = detail;
		else if (detail && typeof detail === 'object') {
			const d = detail as Record<string, unknown>;
			if (typeof d.reason === 'string') reason = d.reason;
			if (typeof d.id === 'string') id = d.id;
			if (!reason && Array.isArray(d)) reason = 'invalid request';
		}
	} catch {
		// Non-JSON body: fall through with an empty reason.
	}
	return { reason, status: res.status, id };
}



export function isAbort(err: unknown, signal?: AbortSignal): boolean {
	if (signal?.aborted) return true;
	return typeof err === 'object' && err !== null && (err as { name?: string }).name === 'AbortError';
}


export function errorMessage(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}


export async function readJson(res: Response): Promise<Record<string, unknown>> {
	try {
		const value = await res.json();
		return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
	} catch {
		return {};
	}
}



export const str = (value: unknown, fallback = ''): string =>
	typeof value === 'string' ? value : fallback;
