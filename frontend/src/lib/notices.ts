// Notices — ONE notification surface for two producers (issue #34).
//
// Why this module exists: the engine used to swallow optional-service failures
// (recall / lane strategy / post-turn hooks / the memory indexer) into stderr, so
// a turn produced prose while half the wiring was dead and nothing on screen
// said so. The owner ruling (2026-10-23): "A small prompting exclamation/
// highlight of the Inspector button; a tab or something for the actual failures.
// Re-use this notification for the proposals too when they fire."
//
// So this is the notification mechanism, built once:
//   * DEGRADATIONS — the engine's typed `system` rows (`recall_degraded`,
//     `lane_degraded`, `index_degraded`, `hook_degraded`), read from the
//     inspector endpoint the chat page already calls (no second read path);
//   * PROPOSALS — the review queue (`GET /api/proposals?session=`), read THROUGH
//     the existing client (`#lib/api/proposals`) so the badge and the proposals
//     drawer can never disagree about what is pending.
//
// Acknowledgement is a UI fact, not a log fact: the log is append-only (ADR-001)
// and a "seen" marker is not a session event. It lives in localStorage keyed by
// session, holding the highest notice seq the reader has seen; opening the
// inspector's notices tab moves it forward. Pending proposals need no ack — a
// review queue item stays pending until it is decided or dismissed.

import { derived, get, writable } from 'svelte/store';
import { getProposals } from '#lib/api/proposals';

/** One swallowed-service notice as the inspector endpoint serves it. */
export type DegradationNotice = {
	/** The notice row's own seq — the value acknowledgement moves past. */
	seq: number;
	/** Row timestamp (ISO), when the engine recorded one. */
	ts?: string;
	/** `recall_degraded` | `lane_degraded` | `index_degraded` | `hook_degraded`. */
	event: string;
	/** WHICH step degraded (`memory_recall`, `npc_lane_strategy`, …). */
	stage: string;
	/** Short actionable reason — "what broke — what to check". */
	reason: string;
	/** Seq context: the newest event when the notice landed (null when unknown). */
	turn: number | null;
	/** The post-turn hook's name, for `hook_degraded` / `index_degraded`. */
	hook?: string | null;
};

/** One pending proposal, as the badge and the notices tab name it. */
export type ProposalNotice = {
	id: string;
	kind: string;
	action: string;
	label: string;
	evidence: string;
	age_turns: number;
};

type NoticeState = {
	sessionId: string;
	notices: DegradationNotice[];
	proposals: ProposalNotice[];
	/** Highest notice seq already seen for this session (0 = none seen). */
	ackSeq: number;
};

const EMPTY: NoticeState = { sessionId: '', notices: [], proposals: [], ackSeq: 0 };

/** The one store both the header badge and the Inspector read. */
export const noticeState = writable<NoticeState>(EMPTY);

/** The badge: unacknowledged degradations + the pending proposal queue. */
export const noticeBadge = derived(noticeState, ($state) => {
	const unacked = $state.notices.filter((notice) => notice.seq > $state.ackSeq);
	return {
		count: unacked.length + $state.proposals.length,
		degradations: unacked,
		proposals: $state.proposals,
		/** Every degradation this session recorded, seen or not (the tab body). */
		allDegradations: $state.notices
	};
});

function ackKey(sessionId: string): string {
	return `fabulist.notices.ack.${sessionId}`;
}

function readAck(sessionId: string): number {
	if (!sessionId || typeof localStorage === 'undefined') return 0;
	try {
		const raw = localStorage.getItem(ackKey(sessionId));
		const value = raw === null ? 0 : Number(raw);
		return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
	} catch {
		return 0;
	}
}

function writeAck(sessionId: string, seq: number): void {
	if (!sessionId || typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(ackKey(sessionId), String(seq));
	} catch {
		/* a blocked storage must never break the notice surface */
	}
}

function text(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

function seqNumber(value: unknown): number {
	return typeof value === 'number' && Number.isFinite(value) ? Math.floor(value) : 0;
}

/** Coerce one wire notice; a row with no name is dropped (nothing to render). */
function normalizeNotice(raw: unknown): DegradationNotice | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const event = text(obj.event);
	if (!event) return null;
	const turn = obj.turn;
	return {
		seq: seqNumber(obj.seq),
		ts: text(obj.ts) || undefined,
		event,
		stage: text(obj.stage),
		reason: text(obj.reason),
		turn: typeof turn === 'number' && Number.isFinite(turn) ? turn : null,
		hook: text(obj.hook) || null
	};
}

/**
 * Point the store at `sessionId`, clearing the previous session's rows.
 *
 * The chat page owns the session lifecycle; calling this on switch and on
 * "New chat" is what keeps a stale session's badge from prompting in the next
 * one (the same rule the Inspector follows for its turn table).
 */
export function syncNoticeSession(sessionId: string | null): void {
	const id = sessionId ?? '';
	if (get(noticeState).sessionId === id) return;
	noticeState.set(
		id ? { sessionId: id, notices: [], proposals: [], ackSeq: readAck(id) } : { ...EMPTY }
	);
}

/** Replace the session's degradation notices (the inspector read's `notices`). */
export function setDegradationNotices(sessionId: string, notices: unknown): void {
	const id = sessionId ?? '';
	if (!id) return;
	const rows = Array.isArray(notices)
		? notices
				.map(normalizeNotice)
				.filter((notice): notice is DegradationNotice => notice !== null)
		: [];
	noticeState.update((state) =>
		state.sessionId === id
			? { ...state, notices: rows }
			: { sessionId: id, notices: rows, proposals: [], ackSeq: readAck(id) }
	);
}

/**
 * Read the pending proposal queue through the existing client and fold it into
 * the badge (issue #34: "Re-use this notification for the proposals too").
 *
 * Best-effort: a failed read leaves the previous rows (proposals are optional
 * surface, exactly as the drawer treats them) and never throws.
 */
export async function loadPendingProposals(sessionId: string | null): Promise<void> {
	const id = sessionId ?? '';
	if (!id) return;
	const result = await getProposals(id);
	if (!result || 'error' in result) return;
	const pending: ProposalNotice[] = result.pending.map((proposal) => ({
		id: proposal.id,
		kind: proposal.kind,
		action: proposal.action,
		label: proposal.target_name || proposal.kind,
		evidence: proposal.evidence,
		age_turns: proposal.age_turns
	}));
	noticeState.update((state) =>
		state.sessionId === id
			? { ...state, proposals: pending }
			: { sessionId: id, notices: [], proposals: pending, ackSeq: readAck(id) }
	);
}

/**
 * Mark every notice the session has recorded as seen (the badge clears).
 *
 * Called by the Inspector when its notices tab is showing — "seen" is the tab
 * being read, not a per-row dismissal (the ruling asks for a prompt, not a
 * triage workflow).
 */
export function acknowledgeNotices(sessionId: string | null): void {
	const id = sessionId ?? '';
	if (!id) return;
	const state = get(noticeState);
	if (state.sessionId !== id) return;
	const newest = state.notices.reduce((max, notice) => Math.max(max, notice.seq), 0);
	// Converge, never re-set: a reader effect calls this while tracking the
	// badge, so a no-op write (an identical state) would notify forever.
	if (newest <= state.ackSeq) return;
	writeAck(id, newest);
	noticeState.set({ ...state, ackSeq: newest });
}

/** The label one degradation row shows in the notices tab. */
export function degradationLabel(event: string): string {
	switch (event) {
		case 'recall_degraded':
			return 'Memory recall degraded';
		case 'lane_degraded':
			return 'Lane strategy degraded';
		case 'index_degraded':
			return 'Memory index degraded';
		case 'hook_degraded':
			return 'Post-turn hook degraded';
		default:
			return event;
	}
}
