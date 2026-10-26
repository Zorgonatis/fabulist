// Fabulist API client - message revisions, semantic search, card export.

import { API_BASE, errorMessage, isAbort, readErrorDetail } from './shared';

// --- revisions (message-edit door) ---------------------------------------------

/**
 * Hang guard for the revision POST, not a latency budget (#87).
 *
 * The append is local and returns in milliseconds (the re-embed is backgrounded
 * after the response), so a request still open after this long is never
 * settling. The abort turns that into an ordinary failure value, which lets the
 * editor's `finally` re-enable Cancel instead of wedging it at "saving…".
 */
const REVISION_TIMEOUT_MS = 30_000;

export async function postRevision(
	sessionId: string,
	targetId: number,
	newText: string
): Promise<{ revision_id: number } | { error: string }> {
	const controller = new AbortController();
	let timedOut = false;
	const timer = setTimeout(() => {
		timedOut = true;
		controller.abort();
	}, REVISION_TIMEOUT_MS);
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/revisions`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ target_id: targetId, new_text: newText }),
				signal: controller.signal
			}
		);
		if (!res.ok) {
			// Surface WHY: the engine answers 404/422 with a plain-string detail or
			// `{detail:{reason}}`; the editor shows it verbatim (#87).
			const { reason, status } = await readErrorDetail(res);
			return { error: reason || `revision failed (${status})` };
		}
		return (await res.json()) as { revision_id: number };
	} catch (err) {
		if (timedOut) return { error: `no answer after ${REVISION_TIMEOUT_MS / 1000} seconds` };
		if (isAbort(err, controller.signal)) return { error: 'the request was cancelled' };
		return { error: errorMessage(err) };
	} finally {
		clearTimeout(timer);
	}
}


// --- semantic search (SURFACE #28) ----------------------------------------------

export interface SearchHit {
	seq: number;
	event_id: number | null;
	kind: string;
	text: string;
	distance: number;
}


export async function searchSession(
	sessionId: string,
	query: string,
	k = 8
): Promise<{ results: SearchHit[] } | { error: string }> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/search`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ query, k })
			}
		);
		if (!res.ok) return { error: `search failed (${res.status})` };
		return (await res.json()) as { results: SearchHit[] };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


// --- card export (SURFACE #13) ---------------------------------------------------

export async function exportCard(
	sessionId: string
): Promise<{ card: Record<string, unknown> } | { error: string }> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/export/card`
		);
		if (!res.ok) return { error: `export failed (${res.status})` };
		return { card: (await res.json()) as Record<string, unknown> };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}

// --- personas & per-chat persona/memory (D16 drawer hierarchy) -------------------
//
// The persona catalog and the per-chat config folds that the drawer groups on.
// Personas are read from the reserved anchor session (like `__assistants__`); the
// per-session config is a thin read of the chat's own `chat_persona` / `chat_memory`
// system events. All of these are additive: existing exports are untouched.
//
//   GET    {API_BASE}/api/personas                   -> {personas: [...]}
//   GET    {API_BASE}/api/sessions/{sid}/chat-config -> {persona_id, memory_enabled}
//   PATCH  {API_BASE}/api/sessions/{sid}/memory       -> {session_id, enabled}
//   POST   {API_BASE}/api/sessions/{sid}/persona      -> {session_id, persona_id}
//   DELETE {API_BASE}/api/sessions/{sid}/persona      -> clears the assignment
//   POST   {API_BASE}/api/sessions                    -> {session_id, title}
