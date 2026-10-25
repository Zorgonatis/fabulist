// Fabulist API client - the planner: the preset's steering config (W9.20) and a
// session's current plan.
//
// The planner is a turn step that runs INFREQUENTLY (turn 1 is mandatory, then
// every `cadence` committed turns) and writes a plan: story strings, a scene
// date/time, weather + forecast, and the facts the plan holds established. The
// field list in the config is the plan's DYNAMIC schema - core values plus
// preset-defined extras (a GM/RPG preset adds currency, reputation, …) - so it
// is preset data, saved and loaded alongside the cadence and the steer.
//
// Wire contract (backend lane C1, plan §7):
//
//   GET {API_BASE}/api/prompts/planner
//     -> {cadence, steer, fields: [{id, title, type, hint, required}]}
//   PUT {API_BASE}/api/prompts/planner   body = the same shape
//     -> the stored config; 422 {"detail": [<one reason per bad row>]}
//   GET {API_BASE}/api/sessions/{id}/plan
//     -> {plan: ScenarioPlan | null, due_in: number | null, ...}
//   PUT {API_BASE}/api/sessions/{id}/plan   body = the WHOLE plan
//     -> the same envelope; 422 {"detail": {"reason", "errors": [..]}}
//   POST {API_BASE}/api/sessions/{id}/plan/run
//     -> the same envelope; 409 while a turn streams (it claims the turn slot);
//        NEVER writes an opening - this is the plain run-now door (W9.23 §3.6.7)
//   POST {API_BASE}/api/sessions/{id}/plan/regenerate
//     -> the same envelope (plus `opening` on a zero-turn session); 409 while a
//        turn streams (it claims the turn slot)
//
// A manual save is a whole plan, never a diff: a plan EVENT is a complete plan
// and the fold takes the latest one (plan §3), so the body carries every core
// value plus the extras - FLATTENED at the top level under their field ids,
// which is what the engine validates a plan payload against (an `extras` object
// would be rejected as an unknown field).
//
// `due_in` is the number of committed turns until the next automatic run (0 =
// due now); `null` when the session has no plan yet.

import { API_BASE, StreamError, USE_MOCK, readJson, str } from './shared';

export type PlannerFieldType = 'string' | 'number' | 'boolean';

/** One preset-defined value the plan holds (the dynamic schema's rows). */
export interface PlannerField {
	/** Identifier-safe slot name; the key in `ScenarioPlan.extras`. */
	id: string;
	title: string;
	type: PlannerFieldType;
	/** What the planner should put here (shown as the row's placeholder). */
	hint: string;
	required: boolean;
}

/** The preset's planner config (editable with NO session open). */
export interface PlannerConfig {
	/** Committed turns between automatic runs; 1 or more. */
	cadence: number;
	/** Standing direction fed to every planning pass ("stay grounded", …). */
	steer: string;
	fields: PlannerField[];
}

/** One folded plan: the last `plan` event's values. */
export interface ScenarioPlan {
	/** Committed turns when the plan was made. */
	turn_seq: number;
	/** How it came to be (`planner`, `manual`, `regenerate`, …). */
	created_from: string;
	/** The story strings the next N turns steer toward. */
	strings: string[];
	datetime: string | null;
	weather: string | null;
	forecast: string | null;
	/** What the plan treats as already true in the world. */
	established: string[];
	/** Preset-defined values, keyed by `PlannerField.id`. */
	extras: Record<string, unknown>;
}

/** `GET/PUT /api/sessions/{id}/plan` envelope. */
export interface SessionPlanResponse {
	plan: ScenarioPlan | null;
	/** Committed turns until the next automatic run; 0 = due now. */
	due_in: number | null;
}

/**
 * A manual plan save: the WHOLE plan, extras kept in their typed bag here and
 * flattened onto the wire body (see the header note).
 */
export interface SessionPlanPayload {
	strings: string[];
	established: string[];
	datetime: string | null;
	weather: string | null;
	forecast: string | null;
	/** Preset extras, keyed by `PlannerField.id`. */
	extras: Record<string, unknown>;
}

/** Endpoint paths (kept in one place so the orchestrator can rewire them). */
export const PLANNER_ENDPOINTS = {
	config: (): string => `${API_BASE}/api/prompts/planner`,
	plan: (sessionId: string): string =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/plan`,
	run: (sessionId: string): string =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/plan/run`,
	regenerate: (sessionId: string): string =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/plan/regenerate`
} as const;

/** The engine's default cadence (plan §1: "somewhere sensible" = every 5). */
export const DEFAULT_CADENCE = 5;

/** The default config an absent preset row means (plan §6). */
export function defaultPlannerConfig(): PlannerConfig {
	return { cadence: DEFAULT_CADENCE, steer: '', fields: [] };
}

function fieldType(value: unknown): PlannerFieldType {
	return value === 'number' || value === 'boolean' ? value : 'string';
}

function stringList(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string') : [];
}

function nullableText(value: unknown): string | null {
	return typeof value === 'string' ? value : null;
}

/** Coerce one raw field row, or null when it has no usable id. */
function normalizeField(entry: unknown): PlannerField | null {
	if (!entry || typeof entry !== 'object') return null;
	const e = entry as Record<string, unknown>;
	const id = str(e.id).trim();
	if (!id) return null;
	return {
		id,
		title: str(e.title),
		type: fieldType(e.type),
		hint: str(e.hint),
		required: e.required === true
	};
}

/**
 * The config from a response body: `{planner: {...}}` (what the endpoints serve)
 * or the bare config. Both shapes are accepted so a rewired endpoint cannot
 * silently read as "defaults".
 */
function plannerEnvelope(raw: Record<string, unknown>): Record<string, unknown> {
	const nested = raw.planner;
	return nested && typeof nested === 'object' && !Array.isArray(nested)
		? (nested as Record<string, unknown>)
		: raw;
}

/** Coerce the config envelope; a missing cadence falls back to the default. */
export function normalizePlannerConfig(raw: Record<string, unknown>): PlannerConfig {
	const cadence =
		typeof raw.cadence === 'number' && Number.isInteger(raw.cadence) && raw.cadence >= 1
			? raw.cadence
			: DEFAULT_CADENCE;
	const fields: PlannerField[] = [];
	for (const entry of Array.isArray(raw.fields) ? raw.fields : []) {
		const field = normalizeField(entry);
		if (field) fields.push(field);
	}
	return { cadence, steer: str(raw.steer), fields };
}

/**
 * Coerce one folded plan (or null). `extras` is a free-form bag: its values are
 * whatever the preset's field list declares (`string`/`number`/`boolean`), so
 * the bag is copied as-is rather than validated here.
 */
export function normalizeScenarioPlan(raw: unknown): ScenarioPlan | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const extras: Record<string, unknown> = {};
	if (o.extras && typeof o.extras === 'object' && !Array.isArray(o.extras)) {
		Object.assign(extras, o.extras);
	}
	return {
		turn_seq: typeof o.turn_seq === 'number' && Number.isFinite(o.turn_seq) ? o.turn_seq : 0,
		created_from: str(o.created_from),
		strings: stringList(o.strings),
		datetime: nullableText(o.datetime),
		weather: nullableText(o.weather),
		forecast: nullableText(o.forecast),
		established: stringList(o.established),
		extras
	};
}

/**
 * Normalize the `{plan, due_in}` envelope. A bare plan body is accepted too: the
 * write endpoints' response shape is the envelope as well, but a `turn_seq`/`strings`
 * payload is unambiguously a plan, and treating it as "no plan" would blank the pane.
 */
export function normalizeSessionPlan(raw: Record<string, unknown>): SessionPlanResponse {
	const due = raw.due_in;
	const plan =
		raw.plan === undefined && ('turn_seq' in raw || 'strings' in raw)
			? normalizeScenarioPlan(raw)
			: normalizeScenarioPlan(raw.plan);
	return {
		plan,
		due_in: typeof due === 'number' && Number.isFinite(due) ? due : null
	};
}

/**
 * The config's validation rules, in one place: the pane checks them before a
 * PUT (inline feedback) and mock mode throws the same 422. Mirrors C1's
 * `planner_api` rules — cadence is a whole number ≥ 1 and every field id is a
 * unique identifier (it becomes the key the plan's extras carry).
 */
export function plannerConfigError(config: PlannerConfig): string | null {
	if (!Number.isInteger(config.cadence) || config.cadence < 1) {
		return 'Cadence must be a whole number of turns, 1 or more.';
	}
	const seen = new Set<string>();
	for (const [index, field] of config.fields.entries()) {
		const id = field.id.trim();
		const label = id ? `“${id}”` : `field ${index + 1}`;
		if (!id) return `${label}: the id is empty — it is the key the plan stores the value under.`;
		if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(id)) {
			return `${label}: the id must be identifier-safe (letters, digits, underscore; no leading digit).`;
		}
		if (seen.has(id)) return `${label}: duplicate id — every field needs its own id.`;
		seen.add(id);
	}
	return null;
}

/**
 * The server's reasons for a failed planner call, one entry per rejected row.
 * A 422 body is `{"detail": [<string>, …]}`; a 409 carries one message. The
 * pane renders these inline, so the engine's own wording stays visible.
 */
export function plannerErrorDetails(err: unknown): string[] {
	if (err instanceof StreamError) {
		const details = err.meta?.details;
		if (Array.isArray(details)) {
			const lines = details.filter((entry): entry is string => typeof entry === 'string');
			if (lines.length > 0) return lines;
		}
	}
	return [err instanceof Error ? err.message : String(err)];
}

/** One failed planner call: the detail list becomes the message + `meta.details`. */
async function failure(res: Response, call: string): Promise<StreamError> {
	let details: string[] = [];
	try {
		const body = (await res.json()) as Record<string, unknown>;
		const detail = body.detail;
		if (typeof detail === 'string') details = [detail];
		else if (Array.isArray(detail)) {
			details = detail.filter((entry): entry is string => typeof entry === 'string');
		} else if (detail && typeof detail === 'object') {
			// A validation rejection is `{reason, errors: [<one line per bad row>]}`.
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
// `VITE_MOCK=1` demos run with no engine, so the config lives in memory and a
// session's plan starts empty. Regenerating needs the real planner pass (it
// claims a turn slot), so it reports that instead of inventing a plan.
let mockConfig: PlannerConfig = defaultPlannerConfig();
const mockPlans = new Map<string, ScenarioPlan>();

// --- endpoints --------------------------------------------------------------

/** GET the active preset's planner config (merged with the defaults). */
export async function getPlannerConfig(): Promise<PlannerConfig> {
	if (USE_MOCK) return structuredClone(mockConfig);
	const res = await fetch(PLANNER_ENDPOINTS.config(), { headers: { Accept: 'application/json' } });
	if (!res.ok) throw await failure(res, 'getPlannerConfig');
	return normalizePlannerConfig(plannerEnvelope(await readJson(res)));
}

/** PUT the config; the response is the stored one (the pane re-seeds from it). */
export async function putPlannerConfig(config: PlannerConfig): Promise<PlannerConfig> {
	const body: PlannerConfig = {
		cadence: config.cadence,
		steer: config.steer,
		fields: config.fields.map((field) => ({ ...field }))
	};
	if (USE_MOCK) {
		const problem = plannerConfigError(body);
		if (problem) throw new StreamError(problem, { status: 422, details: [problem] });
		mockConfig = body;
		return structuredClone(mockConfig);
	}
	const res = await fetch(PLANNER_ENDPOINTS.config(), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await failure(res, 'putPlannerConfig');
	return normalizePlannerConfig(plannerEnvelope(await readJson(res)));
}

/** GET the session's folded plan plus the turns until the next automatic run. */
export async function getSessionPlan(sessionId: string): Promise<SessionPlanResponse> {
	if (USE_MOCK) {
		const plan = mockPlans.get(sessionId);
		return { plan: plan ? structuredClone(plan) : null, due_in: plan ? null : 0 };
	}
	const res = await fetch(PLANNER_ENDPOINTS.plan(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'getSessionPlan');
	return normalizeSessionPlan(await readJson(res));
}

/**
 * PUT a manual save: the whole plan, extras flattened onto the body under their
 * field ids (the core keys are assigned last, so a colliding extra id can never
 * shadow them).
 */
export async function putSessionPlan(
	sessionId: string,
	payload: SessionPlanPayload
): Promise<SessionPlanResponse> {
	const body = {
		...payload.extras,
		strings: [...payload.strings],
		established: [...payload.established],
		datetime: payload.datetime,
		weather: payload.weather,
		forecast: payload.forecast
	};
	if (USE_MOCK) {
		const previous = mockPlans.get(sessionId);
		if (!previous) throw new StreamError('this session has no plan yet', { status: 404 });
		const plan: ScenarioPlan = {
			...payload,
			extras: { ...payload.extras },
			turn_seq: previous.turn_seq,
			created_from: 'manual'
		};
		mockPlans.set(sessionId, plan);
		return { plan: structuredClone(plan), due_in: null };
	}
	const res = await fetch(PLANNER_ENDPOINTS.plan(sessionId), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await failure(res, 'putSessionPlan');
	return normalizeSessionPlan(await readJson(res));
}

/**
 * POST Run planner now: one planner pass off-turn, and nothing else. It never
 * writes an opening — Regenerate Scenario owns that exclusively — so on a chat
 * with no turns yet this is the safe way to refresh the plan. 409 while a turn
 * streams: it claims the turn slot exactly like Regenerate, so the pane surfaces
 * that verdict instead of retrying blind.
 */
export async function runPlanNow(sessionId: string): Promise<SessionPlanResponse> {
	if (USE_MOCK) {
		throw new StreamError('Run planner now needs the engine (mock mode has no planner).', {
			status: 501
		});
	}
	const res = await fetch(PLANNER_ENDPOINTS.run(sessionId), {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'runPlanNow');
	return normalizeSessionPlan(await readJson(res));
}

/**
 * POST Regenerate Scenario: one planner pass off-turn (on a zero-turn session it
 * also writes the opening message). 409 while a turn streams — it claims the
 * turn slot, so the pane surfaces that verdict instead of retrying blind.
 */
export async function regenerateScenario(sessionId: string): Promise<SessionPlanResponse> {
	if (USE_MOCK) {
		throw new StreamError('Regenerate Scenario needs the engine (mock mode has no planner).', {
			status: 501
		});
	}
	const res = await fetch(PLANNER_ENDPOINTS.regenerate(sessionId), {
		method: 'POST',
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'regenerateScenario');
	return normalizeSessionPlan(await readJson(res));
}
