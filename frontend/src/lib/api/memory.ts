// Fabulist API client - memory lifecycle (D25: embedder switch + re-embed).

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** Re-embed job lifecycle state (`GET /api/memory/reembed`). */
export type ReembedState = 'idle' | 'running' | 'done' | 'failed';


/** The re-embed job's status snapshot. */
export interface ReembedStatus {
	state: ReembedState;
	done: number;
	total: number;
	/** Present only when a job finished with row failures / a crash. */
	error?: string;
}


/** `POST /api/memory/reembed` — the start result plus the status snapshot. */
export interface ReembedStart extends ReembedStatus {
	/** False when a job was already running (not restarted). */
	started: boolean;
}


/** `POST /api/memory/reset` — the wiped (now empty) collection set. */
export interface MemoryResetResult {
	reset: true;
	collections: string[];
	/** The collection ids that existed immediately before the wipe. */
	previous: string[];
}


/** Memory endpoint paths (kept in one place the orchestrator can rewire). */
export const MEMORY_ENDPOINTS = {
	reset: () => `${API_BASE}/api/memory/reset`,
	collections: () => `${API_BASE}/api/memory/collections`,
	reembed: () => `${API_BASE}/api/memory/reembed`
} as const;


/** Normalize a raw re-embed status wire (tolerant of absent fields). */
function normalizeReembedStatus(body: Record<string, unknown>): ReembedStatus {
	const rawState = str(body.state);
	const state: ReembedState =
		rawState === 'running' || rawState === 'done' || rawState === 'failed' ? rawState : 'idle';
	const status: ReembedStatus = {
		state,
		done: typeof body.done === 'number' ? body.done : 0,
		total: typeof body.total === 'number' ? body.total : 0
	};
	if (typeof body.error === 'string' && body.error) status.error = body.error;
	return status;
}


/** Wipe every vector collection (`POST /api/memory/reset`). */
export async function resetMemory(): Promise<MemoryResetResult> {
	if (USE_MOCK) return mockResetMemory();
	const res = await fetch(MEMORY_ENDPOINTS.reset(), {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `resetMemory failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const list = (value: unknown): string[] =>
		Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
	return { reset: true, collections: list(body.collections), previous: list(body.previous) };
}


/** List the store's collection ids (`GET /api/memory/collections`). */
export async function getMemoryCollections(): Promise<string[]> {
	if (USE_MOCK) return mockMemoryCollections();
	const res = await fetch(MEMORY_ENDPOINTS.collections(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`getMemoryCollections failed: ${res.status}`);
	const body = await readJson(res);
	return Array.isArray(body.collections)
		? body.collections.filter((v): v is string => typeof v === 'string')
		: [];
}


/** Start (or report) the background re-embed job (`POST /api/memory/reembed`). */
export async function startReembed(): Promise<ReembedStart> {
	if (USE_MOCK) return mockStartReembed();
	const res = await fetch(MEMORY_ENDPOINTS.reembed(), {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `startReembed failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	return { started: body.started === true, ...normalizeReembedStatus(body) };
}


/** Read the re-embed job status (`GET /api/memory/reembed`). */
export async function getReembedStatus(): Promise<ReembedStatus> {
	if (USE_MOCK) return mockReembedStatus();
	const res = await fetch(MEMORY_ENDPOINTS.reembed(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`getReembedStatus failed: ${res.status}`);
	return normalizeReembedStatus(await readJson(res));
}


// Mock state (VITE_MOCK=1): a tiny in-memory job so the drawer flow renders.
let mockMemoryCollectionsList: string[] = ['c_mock'];

let mockReembed: ReembedStatus = { state: 'idle', done: 0, total: 0 };


function mockResetMemory(): MemoryResetResult {
	const previous = mockMemoryCollectionsList;
	mockMemoryCollectionsList = [];
	return { reset: true, collections: [], previous };
}


function mockMemoryCollections(): string[] {
	return [...mockMemoryCollectionsList];
}


function mockStartReembed(): ReembedStart {
	if (mockReembed.state === 'running') return { started: false, ...mockReembed };
	mockReembed = { state: 'running', done: 0, total: 3 };
	return { started: true, ...mockReembed };
}


function mockReembedStatus(): ReembedStatus {
	if (mockReembed.state === 'running') {
		const done = Math.min(mockReembed.total, mockReembed.done + 1);
		mockReembed =
			done >= mockReembed.total
				? { state: 'done', done: mockReembed.total, total: mockReembed.total }
				: { ...mockReembed, done };
		mockMemoryCollectionsList = ['c_mock'];
	}
	return { ...mockReembed };
}
