// Fabulist API client - sessions, projection + branch tree.

import { ENDPOINTS, StreamError, USE_MOCK, errorMessage, isAbort, loadMock, mockSessions, readErrorDetail, readJson } from './shared';
import { streamTurn } from './streaming';
import type { BranchInfo, BranchSibling, CompactionJob, CompactionMarker, ContextLowRow, ForkOutcome, Projection, ProjectionItem, SessionSummary, StreamTurnHandlers, TurnKind, ValidationNotice, ValidationNoticeFlag } from './types';

/** Create a fresh session; resolves to the session id. */
export async function createSession(init: Record<string, unknown> = {}): Promise<string> {
	if (USE_MOCK) {
		const id = await (await loadMock()).createSession(init);
		mockSessions.set(id, {
			id,
			title: typeof init.title === 'string' ? init.title : '',
			updated_at: new Date().toISOString(),
			turns: 0,
			persona_id: typeof init.persona_id === 'string' ? init.persona_id : null,
			persona_cleared: false,
			world_id: typeof init.world_id === 'string' ? init.world_id : null,
			memory_enabled: init.memory_enabled !== false,
			compacted: false
		});
		return id;
	}

	const res = await fetch(ENDPOINTS.sessions(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ title: '', ...init })
	});
	if (!res.ok) {
		throw new StreamError(`createSession failed: ${res.status} ${res.statusText}`);
	}
	const body = (await readJson(res)) as { session_id?: string; id?: string };
	const id = body.session_id ?? body.id;
	if (!id) throw new StreamError('createSession response missing session id');
	return id;
}


/**
 * List visible sessions (newest activity first), derived from the event log.
 *
 * D40/D53: the optional `actor_id` / `world_id` scope the list server-side, so
 * selecting a world shows only that world's chats and selecting an actor shows
 * only the chats that actor is with. `actor_id` is the D53 rail scope (the
 * folded `chat_actor` record); `persona_id` remains the USER-persona filter. An
 * actor matches when it is one of a chat's recorded actors. Omitting all is
 * byte-identical to the old no-arg call.
 */
export async function listSessions(params?: {
	persona_id?: string;
	world_id?: string;
	actor_id?: string;
}): Promise<SessionSummary[]> {
	if (USE_MOCK) {
		let rows = [...mockSessions.values()];
		if (params?.persona_id) rows = rows.filter((row) => row.persona_id === params.persona_id);
		if (params?.world_id) rows = rows.filter((row) => row.world_id === params.world_id);
		if (params?.actor_id) {
			const wanted = params.actor_id;
			rows = rows.filter((row) => (row.actor_ids ?? []).includes(wanted));
		}
		return rows;
	}

	const query = new URLSearchParams();
	if (params?.persona_id) query.set('persona_id', params.persona_id);
	if (params?.world_id) query.set('world_id', params.world_id);
	if (params?.actor_id) query.set('actor_id', params.actor_id);
	const suffix = query.toString() ? `?${query.toString()}` : '';

	const res = await fetch(`${ENDPOINTS.sessions()}${suffix}`, {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`listSessions failed: ${res.status} ${res.statusText}`);
	}
	const body = await readJson(res);
	const raw = Array.isArray(body.sessions) ? body.sessions : [];
	return raw
		.map((item) => normalizeSession(item))
		.filter((item): item is SessionSummary => item !== null);
}


/** Rename a session (backend appends a revision event; nothing is overwritten). */
export async function renameSession(sessionId: string, title: string): Promise<void> {
	if (USE_MOCK) {
		const existing = mockSessions.get(sessionId);
		if (existing) mockSessions.set(sessionId, { ...existing, title });
		return;
	}

	const res = await fetch(ENDPOINTS.session(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ title })
	});
	if (!res.ok) {
		throw new StreamError(`renameSession failed: ${res.status} ${res.statusText}`);
	}
}


/** Soft-delete: the session leaves the list, its log rows survive. */
export async function deleteSession(sessionId: string): Promise<void> {
	if (USE_MOCK) {
		mockSessions.delete(sessionId);
		return;
	}

	const res = await fetch(ENDPOINTS.session(sessionId), { method: 'DELETE' });
	if (!res.ok && res.status !== 404) {
		throw new StreamError(`deleteSession failed: ${res.status} ${res.statusText}`);
	}
}


function normalizeSession(raw: unknown): SessionSummary | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const id = obj.session_id ?? obj.id;
	if (typeof id !== 'string' || !id) return null;
	const updated =
		typeof obj.updated_at === 'string' || obj.updated_at === null ? obj.updated_at : null;
	return {
		id,
		title: typeof obj.title === 'string' ? obj.title : '',
		updated_at: updated,
		turns: typeof obj.turns === 'number' ? obj.turns : 0,
		persona_id: typeof obj.persona_id === 'string' && obj.persona_id ? obj.persona_id : null,
		persona_cleared: obj.persona_cleared === true,
		world_id: typeof obj.world_id === 'string' && obj.world_id ? obj.world_id : null,
		memory_enabled: obj.memory_enabled !== false,
		// D53: the chat's actor record (cast), separate from the user persona.
		actor_id: typeof obj.actor_id === 'string' && obj.actor_id ? obj.actor_id : null,
		actor_ids: Array.isArray(obj.actor_ids)
			? obj.actor_ids.filter((id): id is string => typeof id === 'string' && id.length > 0)
			: [],
		// W9.26: absent on an engine that predates the compaction marker → false.
		compacted: obj.compacted === true
	};
}


/** Fetch the current derived view of a session (facts + transcript history). */
export async function getProjection(sessionId: string): Promise<Projection> {
	if (USE_MOCK) return (await loadMock()).getProjection(sessionId);

	const res = await fetch(ENDPOINTS.projection(sessionId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new StreamError(`getProjection failed: ${res.status} ${res.statusText}`);
	}
	return normalizeProjection(await readJson(res));
}


/**
 * Fetch the sibling set for a session's latest reply (W2.2). Returns `null`
 * when the session has no forks or the backend endpoint is unavailable (legacy
 * sessions, mock mode) — callers then render exactly as before branching.
 */
export async function listBranches(
	sessionId: string,
	selfCut: number | null = null
): Promise<BranchInfo | null> {
	if (USE_MOCK) {
		// W9.26: the mock seed carries a `kind:"compaction"` child beside a
		// `kind:"user"` alternate so the swipe-group exclusion is demoable offline.
		const raw = await (await loadMock()).getBranches(sessionId, selfCut);
		return raw ? normalizeBranches(sessionId, raw, selfCut) : null;
	}

	let res: Response;
	try {
		res = await fetch(ENDPOINTS.branches(sessionId), {
			headers: { Accept: 'application/json' }
		});
	} catch {
		return null;
	}
	if (!res.ok) return null;
	return normalizeBranches(sessionId, await readJson(res), selfCut);
}


/**
 * Fork the session at `at` (the last user message seq) and stream a fresh
 * reply into the new branch.
 *
 * Two steps, matching the orchestrator contract:
 *   1. `POST /api/sessions/{id}/fork {at_seq}` -> the new branch session id.
 *   2. `POST /api/sessions/{newId}/turns {content, kind, regenerate:true}` ->
 *      the same SSE stream as a normal turn; `regenerate` tells the transport
 *      to skip the user-event append (the branch inherited its user message at
 *      the cut — FORK-SEMANTICS F2).
 */
export async function forkTurn(
	sessionId: string,
	at: number,
	content: string,
	kind: TurnKind,
	handlers: StreamTurnHandlers = {}
): Promise<ForkOutcome> {
	if (USE_MOCK) {
		// Mock mode ships no branch endpoint; retry is simply unavailable.
		const error = new StreamError('branching is unavailable in mock mode');
		handlers.onError?.(error);
		return { status: 'error', error };
	}

	let branchId: string;
	try {
		const res = await fetch(ENDPOINTS.fork(sessionId), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify({ at_seq: at }),
			signal: handlers.signal
		});
		if (!res.ok) {
			const detail = await readErrorDetail(res);
			return failFork(
				new StreamError(detail.reason || `fork failed: ${res.status} ${res.statusText}`),
				handlers
			);
		}
		const body = await readJson(res);
		const id = body.session_id;
		if (typeof id !== 'string' || !id) {
			return failFork(new StreamError('fork response missing session id'), handlers);
		}
		branchId = id;
	} catch (err) {
		if (isAbort(err, handlers.signal)) return { status: 'stopped' };
		return failFork(new StreamError(errorMessage(err)), handlers);
	}

	const outcome = await streamTurn(branchId, content, kind, handlers, { regenerate: true });
	if (outcome.status === 'done') return { status: 'done', branchId, meta: outcome.meta };
	if (outcome.status === 'stopped') return { status: 'stopped', branchId };
	return { status: 'error', branchId, error: outcome.error };
}


function failFork(error: StreamError, handlers: StreamTurnHandlers): ForkOutcome {
	handlers.onError?.(error);
	return { status: 'error', error };
}


/**
 * Issue #35: the delete-tail outcome — a fork made just below the pivot.
 *
 * Deletion is NOT an edit of the log: the engine appends a fork bookkeeping row
 * (plus a title carry row) and hands back the new branch's session id. The old
 * line stays in the append-only log, reachable through the existing branches
 * switcher — which is why `sessionId` is all the caller needs to switch to.
 */
export interface DeleteTailOutcome {
	status: 'done' | 'error';
	/** The new branch's session id, present on `done`. */
	sessionId?: string;
	/** How many chain messages the tail carried away (pivot inclusive). */
	deletedCount?: number;
	/** Raw engine reason (`first_message` / `pivot_missing` / `not_a_message`). */
	reason?: string;
	error?: StreamError;
}


/** The engine's 409 reasons, said in the UI's words (the confirm already said
 *  what would leave, so a refusal must say WHY nothing did). */
const DELETE_TAIL_REASONS: Record<string, string> = {
	first_message: 'the first message of a chat cannot be deleted',
	pivot_missing: 'that message is not part of this chat',
	not_a_message: 'that row is not a deletable message'
};


/**
 * Delete a message and every message after it (issue #35).
 *
 * One call, matching the frozen contract:
 *
 *   `POST /api/sessions/{id}/delete-tail {pivot_event_id}`
 *     -> 200 `{session_id, base_session, base_seq, deleted_count}`
 *     -> 409 `{detail:{reason:"first_message"|"pivot_missing"|"not_a_message"}}`
 *     -> 404 `{detail:"unknown session"}`
 *
 * No stream rides this: the branch is created truncated and the caller simply
 * opens it (`openSession`), exactly like a swipe. Failures resolve a readable
 * `error` so the caller can surface the reason instead of a bare status.
 */
export async function deleteTail(
	sessionId: string,
	pivotEventId: number
): Promise<DeleteTailOutcome> {
	if (USE_MOCK) {
		// Mock mode ships no delete-tail endpoint; the door is unavailable.
		return {
			status: 'error',
			error: new StreamError('deleting messages is unavailable in mock mode')
		};
	}

	let res: Response;
	try {
		res = await fetch(ENDPOINTS.deleteTail(sessionId), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
			body: JSON.stringify({ pivot_event_id: pivotEventId })
		});
	} catch (err) {
		return { status: 'error', error: new StreamError(errorMessage(err)) };
	}

	if (!res.ok) {
		const detail = await readErrorDetail(res);
		const message =
			DELETE_TAIL_REASONS[detail.reason] ||
			detail.reason ||
			`delete failed: ${res.status} ${res.statusText}`;
		return {
			status: 'error',
			...(detail.reason ? { reason: detail.reason } : {}),
			error: new StreamError(message)
		};
	}

	const body = await readJson(res);
	const id = body.session_id;
	if (typeof id !== 'string' || !id) {
		return { status: 'error', error: new StreamError('delete response missing session id') };
	}
	return {
		status: 'done',
		sessionId: id,
		...(typeof body.deleted_count === 'number' ? { deletedCount: body.deleted_count } : {})
	};
}


/**
 * Ask the engine to compact a session's chain (W9.26 §2.3).
 *
 * Answers 202 `{job_id}` and the job then runs in the background — there is NO
 * stream for it (`turn.done` is set before the post-turn hooks, so no frame can
 * carry hook results). The caller polls {@link getCompactionJob}.
 *
 * Failure shapes the caller must distinguish:
 *   * 409 `{detail:{reason:"compacting"}}` — another compaction holds the claim;
 *   * 409 `{detail:{reason:"superseded", id}}` — this session is read-only
 *     history (idempotent successor return: the response may instead carry the
 *     successor's session id on a 200; both are surfaced as `successorId`).
 */
export async function startCompaction(
	sessionId: string
): Promise<{ job_id: string } | { error: string; reason?: string; successorId?: string }> {
	if (USE_MOCK) return (await loadMock()).startCompaction(sessionId);
	try {
		const res = await fetch(ENDPOINTS.compact(sessionId), {
			method: 'POST',
			headers: { Accept: 'application/json' }
		});
		const body = await readJson(res);
		const jobId = typeof body.job_id === 'string' ? body.job_id : '';
		if (res.ok && jobId) return { job_id: jobId };
		if (res.ok) {
			// The idempotent successor return: nothing new to run, the era already exists.
			const successor = typeof body.session_id === 'string' ? body.session_id : '';
			if (successor) return { error: 'already compacted', reason: 'superseded', successorId: successor };
		}
		const detail = await readErrorDetail(res);
		return {
			error: detail.reason || `compact failed (${res.status})`,
			reason: detail.reason || undefined,
			successorId: detail.id
		};
	} catch (err) {
		return { error: errorMessage(err) };
	}
}

/** Read one compaction job's status (the poll target; ~2 s cadence). */
export async function getCompactionJob(sessionId: string, jobId: string): Promise<CompactionJob> {
	if (USE_MOCK) return (await loadMock()).getCompactionJob(sessionId, jobId);
	const res = await fetch(ENDPOINTS.compactionJob(sessionId, jobId), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw new StreamError(`compaction job failed: ${res.status} ${res.statusText}`);
	const body = await readJson(res);
	const status = body.status;
	return {
		...(typeof body.job_id === 'string' ? { job_id: body.job_id } : { job_id: jobId }),
		status:
			status === 'done' || status === 'failed' || status === 'noop' || status === 'running'
				? status
				: 'running',
		...(typeof body.session_id === 'string' && body.session_id
			? { session_id: body.session_id }
			: {}),
		...(typeof body.title === 'string' ? { title: body.title } : {}),
		...(typeof body.error === 'string' ? { error: body.error } : {}),
		...(typeof body.stage === 'string' ? { stage: body.stage } : {})
	};
}

/** Poll cadence for the 202 job (W9.26 §2.2: ~2 s). */
export const COMPACTION_POLL_MS = 2000;
/** Give up polling after this long (the job is in-RAM; a lost engine never answers). */
export const COMPACTION_POLL_TIMEOUT_MS = 10 * 60 * 1000;

/**
 * Poll a compaction job until it reaches a terminal status.
 *
 * Returns the terminal job DTO (`done` carries the successor's `session_id`/`title`;
 * `failed` carries `error`/`stage`). A poll error is retried until the timeout —
 * a single dropped GET must not abandon a job the engine is still running — and
 * a timeout resolves `failed` with a readable reason so the caller re-arms.
 */
export async function pollCompactionJob(
	sessionId: string,
	jobId: string,
	options: {
		intervalMs?: number;
		timeoutMs?: number;
		onStatus?: (job: CompactionJob) => void;
		signal?: AbortSignal;
	} = {}
): Promise<CompactionJob> {
	const interval = options.intervalMs ?? COMPACTION_POLL_MS;
	const deadline = Date.now() + (options.timeoutMs ?? COMPACTION_POLL_TIMEOUT_MS);
	let last: CompactionJob = { job_id: jobId, status: 'running' };
	while (Date.now() < deadline) {
		if (options.signal?.aborted) return { ...last, status: 'failed', error: 'stopped' };
		try {
			last = await getCompactionJob(sessionId, jobId);
			options.onStatus?.(last);
			if (last.status === 'done' || last.status === 'failed' || last.status === 'noop') return last;
		} catch {
			// Transient read failure: keep polling — the job lives server-side.
		}
		await new Promise((resolve) => setTimeout(resolve, interval));
	}
	return { job_id: jobId, status: 'failed', error: 'compaction is still running (timed out waiting)' };
}

/** Ask the engine to cancel the in-flight turn (cooperative stop, HTTP 202). */
export async function stopTurn(sessionId: string): Promise<void> {
	if (USE_MOCK) {
		await (await loadMock()).stopTurn(sessionId);
		return;
	}
	try {
		await fetch(ENDPOINTS.stop(sessionId), { method: 'POST' });
	} catch {
		// Best-effort: the local AbortController already stopped the reader.
	}
}


/** Tolerantly normalize a projection payload into the UI's data shape. */
function normalizeProjection(raw: Record<string, unknown>): Projection {
	const items = (value: unknown): ProjectionItem[] =>
		Array.isArray(value) ? (value as ProjectionItem[]) : [];
	// Canonical shape: `state` is the fold of `state-delta` payloads. A raw
	// pipe-YAML `<state>` block (`state_block`/`facts`) is also accepted.
	let state: Record<string, unknown> = {};
	if (raw.state && typeof raw.state === 'object') {
		state = raw.state as Record<string, unknown>;
	} else if (typeof raw.state === 'string') {
		state = { raw: raw.state };
	} else if (typeof raw.facts === 'string') {
		state = { raw: raw.facts };
	} else if (typeof raw.state_block === 'string') {
		state = { raw: raw.state_block };
	}
	return {
		narratives: items(raw.narratives),
		state,
		user_messages: items(raw.user_messages),
		rolls: items(raw.rolls),
		assets: items(raw.assets),
		notices: notices(raw.notices),
		context_lows: chainRows<ContextLowRow>(raw.context_lows),
		compactions: chainRows<CompactionMarker>(raw.compactions)
	};
}

/**
 * Tolerantly read one chain-walked `{seq, session_id, payload}` list (W9.26).
 *
 * A row is usable only when it names its position (`seq`) and its owning session
 * (`session_id`) — those two fields ARE the ordering key and the entry id; a
 * payload-less row still renders its structural line, so it is kept with `{}`.
 * Absent key (an engine that predates W9.26) ⇒ an empty list, never undefined.
 */
function chainRows<T extends { seq: number; session_id: string; payload: Record<string, unknown> }>(
	value: unknown
): T[] {
	if (!Array.isArray(value)) return [];
	const out: T[] = [];
	for (const raw of value) {
		if (!raw || typeof raw !== 'object') continue;
		const obj = raw as Record<string, unknown>;
		const seq = obj.seq;
		const sessionId = obj.session_id;
		if (typeof seq !== 'number' || !Number.isInteger(seq)) continue;
		if (typeof sessionId !== 'string' || !sessionId) continue;
		const payload =
			obj.payload && typeof obj.payload === 'object'
				? (obj.payload as Record<string, unknown>)
				: {};
		out.push({ seq, session_id: sessionId, payload } as T);
	}
	return out.sort((a, b) => a.seq - b.seq);
}


/** Tolerantly read the projection's `notices` (D-F).
 *
 * A notice is only usable when it names the narrative it follows (a positive
 * integer `seq`) and carries at least one finding — the same rule the engine's
 * projection applies, restated here so a legacy engine (no key) degrades to an
 * empty list instead of an undefined surface. */
function notices(value: unknown): ValidationNotice[] {
	if (!Array.isArray(value)) return [];
	const out: ValidationNotice[] = [];
	for (const raw of value) {
		if (!raw || typeof raw !== 'object') continue;
		const seq = (raw as { seq?: unknown }).seq;
		if (typeof seq !== 'number' || !Number.isInteger(seq) || seq < 1) continue;
		const flags = (raw as { flags?: unknown }).flags;
		if (!Array.isArray(flags)) continue;
		const findings = flags.filter(
			(flag): flag is ValidationNoticeFlag => !!flag && typeof flag === 'object'
		);
		if (!findings.length) continue;
		const score = (raw as { score?: unknown }).score;
		out.push({
			seq,
			flags: findings,
			...(typeof score === 'number' ? { score } : {})
		});
	}
	return out.sort((a, b) => a.seq - b.seq);
}


/**
 * Normalize a branches payload tolerantly. Primary shape (orchestrator
 * contract): ``{session_id, parent: {session_id, base_seq, snippet}|null,
 * siblings: [{session_id, base_seq, created_at, snippet, is_self}], children}``
 * where ``siblings`` is parent-first ordered and includes self. Falls back to
 * the landed children-only shape (``branches`` of ``{id}``, where ``{id}`` is
 * the origin line). Returns `null` when there is nothing to swipe.
 */
function normalizeBranches(
	sessionId: string,
	raw: Record<string, unknown>,
	selfCut: number | null = null
): BranchInfo | null {
	const selfId =
		typeof raw.session_id === 'string' && raw.session_id ? raw.session_id : sessionId;

	const toSibling = (item: unknown, position: number): BranchSibling | null => {
		if (typeof item === 'string') {
			return {
				sessionId: item,
				position,
				current: item === selfId,
				reply: '',
				seq: null,
				kind: 'user'
			};
		}
		if (!item || typeof item !== 'object') return null;
		const obj = item as Record<string, unknown>;
		const id =
			typeof obj.session_id === 'string'
				? obj.session_id
				: typeof obj.id === 'string'
					? obj.id
					: '';
		if (!id) return null;
		return {
			sessionId: id,
			position: typeof obj.position === 'number' ? obj.position : position,
			current:
				obj.is_self === true || obj.current === true || obj.is_current === true || id === selfId,
			reply:
				typeof obj.snippet === 'string'
					? obj.snippet
					: typeof obj.reply === 'string'
						? obj.reply
						: typeof obj.summary === 'string'
							? obj.summary
							: typeof obj.text === 'string'
								? obj.text
								: '',
			seq:
				typeof obj.base_seq === 'number'
					? obj.base_seq
					: typeof obj.seq === 'number'
						? obj.seq
						: null,
			// W9.26 §2.6: a compaction child is a CONTINUATION era, never a swipe
			// alternative. Absent on engines that predate the marker → `user`.
			kind: obj.kind === 'compaction' ? 'compaction' : 'user'
		};
	};

	let siblings: BranchSibling[] = [];
	let parentId: string | null = null;
	let cut: number | null = null;

	const parentRaw = raw.parent;
	if (parentRaw && typeof parentRaw === 'object') {
		const parent = parentRaw as Record<string, unknown>;
		parentId = typeof parent.session_id === 'string' ? parent.session_id : null;
		cut = typeof parent.base_seq === 'number' ? parent.base_seq : null;
	}

	const siblingsRaw = raw.siblings;
	const childrenRaw = raw.children ?? raw.branches ?? raw.alternates;

	// W9.26 §2.6: compaction children are CONTINUATIONS — carried on their own
	// list (BranchInfo.continuations) so BranchControls can render them labelled
	// without their ever joining the swipe group or perturbing index/count.
	const continuations: BranchSibling[] = Array.isArray(childrenRaw)
		? childrenRaw
				.filter((item) => item && typeof item === 'object' && (item as Record<string, unknown>).kind === 'compaction')
				.map((item, i) => toSibling(item, i + 1))
				.filter((item): item is BranchSibling => item !== null)
		: [];

	if (Array.isArray(siblingsRaw) && siblingsRaw.length > 0) {
		// Primary shape: the full ordered sibling set (parent line first, self marked).
		siblings = siblingsRaw
			.map((item, i) => toSibling(item, i + 1))
			.filter((item): item is BranchSibling => item !== null);
		// A root (parent=null) may report only itself while owning forked children;
		// merge the children forked at this turn's cut so the original line can
		// still be swiped (task spec: children + parent's own reply = position 1).
		// W9.26 §2.6: a `kind:"compaction"` child is a CONTINUATION, not an
		// alternative — it must never join the swipe group (a swipe across eras
		// would silently move the reader into a different log). The branches LIST
		// keeps it (the DTO carries `kind`); only the merge filters.
		if (siblings.length === 1 && parentId === null && Array.isArray(childrenRaw)) {
			const targetCut = cut ?? selfCut;
			const extra = childrenRaw
				.filter((item) => {
					if (!item || typeof item !== 'object') return false;
					if ((item as Record<string, unknown>).kind === 'compaction') return false;
					const base = (item as Record<string, unknown>).base_seq;
					return targetCut === null || typeof base !== 'number' || base === targetCut;
				})
				.map((item, i) => toSibling(item, i + 2))
				.filter((item): item is BranchSibling => item !== null);
			siblings = [...siblings, ...extra];
		}
		if (cut === null) cut = siblings[1]?.seq ?? siblings[0]?.seq ?? null;
	} else if (Array.isArray(childrenRaw)) {
		// Landed shape: children of `{id}`, and `{id}` is treated as the origin line.
		// Compaction continuations are excluded for the same reason as above.
		siblings = [
			{ sessionId: selfId, position: 1, current: true, reply: '', seq: null, kind: 'user' },
			...childrenRaw
				.filter(
					(item) => !(item && typeof item === 'object' && (item as Record<string, unknown>).kind === 'compaction')
				)
				.map((item, i) => toSibling(item, i + 2))
				.filter((item): item is BranchSibling => item !== null)
		];
		if (cut === null) cut = siblings[1]?.seq ?? null;
	} else {
		return null;
	}

	if (siblings.length === 0) return null;

	// Honor backend order where given, then renumber 1..N for display.
	siblings.sort((a, b) => a.position - b.position);
	siblings.forEach((sibling, i) => {
		sibling.position = i + 1;
	});

	const current =
		siblings.find((sibling) => sibling.current) ??
		siblings.find((sibling) => sibling.sessionId === selfId) ??
		siblings[0];
	const count =
		typeof raw.count === 'number' && raw.count >= siblings.length ? raw.count : siblings.length;

	return { sessionId: selfId, parentId, cut, index: current.position, count, siblings, continuations };
}
