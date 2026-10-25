// Fabulist API client - the historian: the asset-proposal lanes' cadence + on-demand run.
//
// The historian is the world/actor/book lane system run OUTSIDE the turn flow
// (W9.23 §3.6). It behaves like the planner — automatic every `cadence` committed
// turns (preset data, owner-set, DEFAULT 10, and with NO turn-1 invoke) plus a
// synchronous on-demand run — except that its output is proposals, which feed
// nothing in the turn flow and are decided by the owner afterwards.
//
// PINNED CROSS-LANE CONTRACT (C1 builds the routes; this file codes to the wire,
// never to their implementation — the W9.22 cross-lane pattern):
//
//   GET  {API_BASE}/api/prompts/historian -> {"historian": {"cadence": 10}}
//   PUT  {API_BASE}/api/prompts/historian   body = the same shape
//        -> the stored read; 422 {"detail": {"reason", "errors": [..]}} (nothing written)
//   GET  {API_BASE}/api/sessions/{sid}/historian
//        -> {"running": bool, "last_run": {...} | null, "committed_turns": n,
//            "cadence": n, "due_in": n}
//   POST {API_BASE}/api/sessions/{sid}/historian/run
//        -> {"gate", "filed", "dropped", "failed_lanes", "turn_seq", "duration_ms"};
//           404 unknown session, 409 {"detail": "historian already running"},
//           503 when the engine wiring supplied no runner
//
// A run does NOT claim the session turn slot (unlike the planner's runs, which
// steer the NEXT turn): proposals feed nothing in the turn flow, so a send issued
// while the historian works neither waits nor 409s. The 409 here is the
// per-session double-run guard, not a turn lock.
//
// `last_run` is the LATEST `historian_run` row on the session's chain — the run
// trace §3.6.3 pins:
//
//   {"event": "historian_run", "status": "started"|"finished"|"failed",
//    "trigger": "cadence"|"manual", "turn_seq": 27, "window_turns": 10,
//    "gate": "proceeded"|"no_evidence"|"gate_off"|"verdict_no"|"unsure"|"no_generator"|"error",
//    "filed": 3, "dropped": 2, "failed_lanes": [{"lane": "books", "reason": "..."}],
//    "duration_ms": 41230, "ts": "..."}
//
// The STARTED row stamps the cadence clock, so `last_run.status` is `started` while
// a run is in flight and stays whatever the run ended as afterwards. Parsing is
// defensive the same way `proposals.ts` is: a status read the drawer cannot parse
// must still render the run-now action, so every field is coerced and an
// unreadable `last_run` degrades to "no run recorded yet" rather than throwing.

import { API_BASE, StreamError, USE_MOCK, readJson, str } from './shared';

/** The engine's default cadence (W9.23 §3.6.1) — an absent preset block means this. */
export const DEFAULT_HISTORIAN_CADENCE = 10;

/** The preset's historian config (editable with NO session open). */
export interface HistorianConfig {
	/** Committed turns between automatic runs; 1 or more. */
	cadence: number;
}

/** How a run came to be: the cadence clock, or the owner pressing the button. */
export type HistorianTrigger = 'cadence' | 'manual';

/** One lane that failed during a run (the run still files what succeeded). */
export interface HistorianFailedLane {
	lane: string;
	reason: string;
}

/** The latest `historian_run` row, as the status read serves it. */
export interface HistorianRun {
	/** `started` | `finished` | `failed` (the run-level trace). */
	status: string;
	trigger: HistorianTrigger | '';
	/** Committed turns when the run started — the number the clock advances to. */
	turn_seq: number;
	gate: string;
	filed: number;
	dropped: number;
	failed_lanes: HistorianFailedLane[];
	/** ISO timestamp the row was appended (empty when the engine omits it). */
	ts: string;
}

/** `GET /api/sessions/{sid}/historian` — the drawer line + button state. */
export interface HistorianStatus {
	/** True while a run is in flight in this process (the 409 guard's state). */
	running: boolean;
	last_run: HistorianRun | null;
	/** Committed narratives on the session's chain (the due rule's left side). */
	committed_turns: number;
	cadence: number;
	/** Committed turns until the next automatic run; 0 = due now. */
	due_in: number;
}

/** `POST /api/sessions/{sid}/historian/run` — the run's own counts. */
export interface HistorianRunResult {
	/** `proceeded` | `no_evidence` | `gate_off` | `verdict_no` | `unsure` | `no_generator` | `error`. */
	gate: string;
	filed: number;
	dropped: number;
	/** Lanes whose call failed (a hung provider surfaces here, bounded by that
	 * connection's `timeout` — there is no run-total timeout, so such a run
	 * still finishes with the other lanes' `filed` rows). */
	failed_lanes: HistorianFailedLane[];
	/** The committed-turn count the run stamped. */
	turn_seq: number;
	duration_ms: number;
	/** The failure text when a runner-level error stamped the run `failed`. */
	error?: string;
}

/** Endpoint paths (kept in one place so the orchestrator can rewire them). */
export const HISTORIAN_ENDPOINTS = {
	config: (): string => `${API_BASE}/api/prompts/historian`,
	status: (sessionId: string): string =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/historian`,
	run: (sessionId: string): string =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/historian/run`
} as const;

/** The default config an absent preset row means (W9.23 §3.6.1). */
export function defaultHistorianConfig(): HistorianConfig {
	return { cadence: DEFAULT_HISTORIAN_CADENCE };
}

// --- parsing ------------------------------------------------------------------

function count(value: unknown, fallback = 0): number {
	return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function wholeCount(value: unknown): number {
	return Math.max(0, Math.trunc(count(value, 0)));
}

function trigger(value: unknown): HistorianTrigger | '' {
	return value === 'cadence' || value === 'manual' ? value : '';
}

function failedLanes(value: unknown): HistorianFailedLane[] {
	if (!Array.isArray(value)) return [];
	const lanes: HistorianFailedLane[] = [];
	for (const entry of value) {
		if (!entry || typeof entry !== 'object') continue;
		const row = entry as Record<string, unknown>;
		const lane = str(row.lane).trim();
		if (!lane) continue;
		lanes.push({ lane, reason: str(row.reason) });
	}
	return lanes;
}

/**
 * The config from a response body: `{historian: {...}}` (what the endpoints
 * serve) or the bare config — both shapes are accepted so a rewired endpoint
 * cannot silently read as "defaults".
 */
function historianEnvelope(raw: Record<string, unknown>): Record<string, unknown> {
	const nested = raw.historian;
	return nested && typeof nested === 'object' && !Array.isArray(nested)
		? (nested as Record<string, unknown>)
		: raw;
}

/** Coerce the config envelope; a missing or illegal cadence falls back to the default. */
export function normalizeHistorianConfig(raw: Record<string, unknown>): HistorianConfig {
	const cadence = raw.cadence;
	return {
		cadence:
			typeof cadence === 'number' && Number.isInteger(cadence) && cadence >= 1
				? cadence
				: DEFAULT_HISTORIAN_CADENCE
	};
}

/** Coerce one `historian_run` row, or null when there is no row to read. */
export function normalizeHistorianRun(raw: unknown): HistorianRun | null {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
	const row = raw as Record<string, unknown>;
	return {
		status: str(row.status),
		trigger: trigger(row.trigger),
		turn_seq: wholeCount(row.turn_seq),
		gate: str(row.gate),
		filed: wholeCount(row.filed),
		dropped: wholeCount(row.dropped),
		failed_lanes: failedLanes(row.failed_lanes),
		ts: str(row.ts)
	};
}

/** Coerce the status read; a bare `{cadence}` body still renders the run-now line. */
export function normalizeHistorianStatus(raw: Record<string, unknown>): HistorianStatus {
	return {
		running: raw.running === true,
		last_run: normalizeHistorianRun(raw.last_run),
		committed_turns: wholeCount(raw.committed_turns),
		cadence: normalizeHistorianConfig(historianEnvelope(raw)).cadence,
		due_in: wholeCount(raw.due_in)
	};
}

/** Coerce the run response (the runner's own counts; a bare body reads as zeroes). */
export function normalizeHistorianRunResult(raw: Record<string, unknown>): HistorianRunResult {
	return {
		gate: str(raw.gate),
		filed: wholeCount(raw.filed),
		dropped: wholeCount(raw.dropped),
		failed_lanes: failedLanes(raw.failed_lanes),
		turn_seq: wholeCount(raw.turn_seq),
		duration_ms: wholeCount(raw.duration_ms)
	};
}

/**
 * The config's validation rule, in one place: the studio checks it before a PUT
 * (inline feedback) and mock mode throws the same 422. Mirrors C1's
 * `normalize_historian_config` — cadence is a whole number ≥ 1.
 */
export function historianConfigError(config: HistorianConfig): string | null {
	if (!Number.isInteger(config.cadence) || config.cadence < 1) {
		return 'Cadence must be a whole number of turns, 1 or more.';
	}
	return null;
}

/**
 * The server's reasons for a failed historian call. A config 422 is
 * `{"detail": {"reason", "errors": [..]}}`; the run door's 409/503 carry one
 * message. The surfaces render these inline, so the engine's wording stays visible.
 */
export function historianErrorDetails(err: unknown): string[] {
	if (err instanceof StreamError) {
		const details = err.meta?.details;
		if (Array.isArray(details)) {
			const lines = details.filter((entry): entry is string => typeof entry === 'string');
			if (lines.length > 0) return lines;
		}
	}
	return [err instanceof Error ? err.message : String(err)];
}

/** One failed historian call: the detail list becomes the message + `meta.details`. */
async function failure(res: Response, call: string): Promise<StreamError> {
	let details: string[] = [];
	try {
		const body = (await res.json()) as Record<string, unknown>;
		const detail = body.detail;
		if (typeof detail === 'string') details = [detail];
		else if (Array.isArray(detail)) {
			details = detail.filter((entry): entry is string => typeof entry === 'string');
		} else if (detail && typeof detail === 'object') {
			// A config rejection is `{reason, errors: [<one line per bad row>]}`.
			const wrapped = detail as Record<string, unknown>;
			if (Array.isArray(wrapped.errors)) {
				details = wrapped.errors.filter((entry): entry is string => typeof entry === 'string');
			}
			if (details.length === 0 && typeof wrapped.reason === 'string') {
				details = [wrapped.reason];
			}
		}
	} catch {
		// Non-JSON body: fall through to the status line.
	}
	if (details.length === 0) details = [`${call} failed: ${res.status} ${res.statusText}`];
	return new StreamError(details.join('; '), { status: res.status, details });
}

// --- mock mode --------------------------------------------------------------
// `VITE_MOCK=1` demos run with no engine, so the config lives in memory and the
// status is synthesized from it: a run 4 turns back that filed 3, which is the
// shape Q5 asked the drawer's run-now line to render offline. Running needs the
// real lane system, so the run door reports that instead of inventing proposals.
let mockConfig: HistorianConfig = defaultHistorianConfig();

function mockStatus(): HistorianStatus {
	const turn_seq = 4;
	return {
		running: false,
		last_run: {
			status: 'finished',
			trigger: 'cadence',
			turn_seq,
			gate: 'proceeded',
			filed: 3,
			dropped: 1,
			failed_lanes: [],
			ts: ''
		},
		committed_turns: 8,
		cadence: mockConfig.cadence,
		due_in: Math.max(0, mockConfig.cadence - turn_seq)
	};
}

// --- endpoints --------------------------------------------------------------

/** GET the active preset's historian config (merged with the defaults). */
export async function getHistorianConfig(): Promise<HistorianConfig> {
	if (USE_MOCK) return { ...mockConfig };
	const res = await fetch(HISTORIAN_ENDPOINTS.config(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'getHistorianConfig');
	return normalizeHistorianConfig(historianEnvelope(await readJson(res)));
}

/** PUT the config; the response is the stored one (the pane re-seeds from it). */
export async function putHistorianConfig(config: HistorianConfig): Promise<HistorianConfig> {
	const body: HistorianConfig = { cadence: config.cadence };
	if (USE_MOCK) {
		const problem = historianConfigError(body);
		if (problem) throw new StreamError(problem, { status: 422, details: [problem] });
		mockConfig = body;
		return { ...mockConfig };
	}
	const res = await fetch(HISTORIAN_ENDPOINTS.config(), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await failure(res, 'putHistorianConfig');
	return normalizeHistorianConfig(historianEnvelope(await readJson(res)));
}

/** GET the session's run state: in flight? last run? how many turns until due? */
export async function historianStatus(sessionId: string): Promise<HistorianStatus> {
	if (USE_MOCK) return mockStatus();
	const res = await fetch(HISTORIAN_ENDPOINTS.status(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'historianStatus');
	return normalizeHistorianStatus(await readJson(res));
}

/**
 * POST run the historian NOW (synchronously). Independent of due-ness — the owner
 * asked for it now — and it does NOT claim the session turn slot, so no send is
 * blocked behind it. `409` means a run is already in flight for this session.
 */
export async function runHistorian(sessionId: string): Promise<HistorianRunResult> {
	if (USE_MOCK) {
		throw new StreamError('Run historian now needs the engine (mock mode has no lanes).', {
			status: 501
		});
	}
	const res = await fetch(HISTORIAN_ENDPOINTS.run(sessionId), {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'runHistorian');
	return normalizeHistorianRunResult(await readJson(res));
}
