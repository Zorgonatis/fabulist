// Fabulist API client - turn streaming + SSE parsing.

import { ENDPOINTS, StreamError, USE_MOCK, errorMessage, isAbort, loadMock, readErrorDetail } from './shared';
import type { RevisionFrame, StageFrame, StreamEvent, StreamEventType, StreamOutcome, StreamTurnHandlers, StreamTurnOptions, TurnKind } from './types';

/**
 * Run one turn and stream its events. Resolves when the turn reaches a terminal
 * state; partial content is delivered through `onReasoning`/`onContent`.
 */
export async function streamTurn(
	sessionId: string,
	content: string,
	kind: TurnKind,
	handlers: StreamTurnHandlers = {},
	options: StreamTurnOptions = {}
): Promise<StreamOutcome> {
	if (USE_MOCK) return (await loadMock()).streamTurn(sessionId, content, kind, handlers);

	const createUrl = ENDPOINTS.turn(sessionId);
	let lastEventId: string | undefined;

	// At most one resume attempt (spec: "Last-Event-ID reconnect with one retry").
	for (let attempt = 0; attempt <= 1; attempt++) {
		if (handlers.signal?.aborted) return { status: 'stopped' };

		// attempt 0 starts the turn (POST); attempt 1 resumes the in-flight turn
		// (GET /stream?lastEventId=N) after a dropped connection.
		const isResume = attempt > 0;
		const url = isResume
			? `${ENDPOINTS.stream(sessionId)}?lastEventId=${encodeURIComponent(lastEventId ?? '0')}`
			: createUrl;
		const init: RequestInit = isResume
			? { method: 'GET', headers: { Accept: 'text/event-stream' }, signal: handlers.signal }
			: {
					method: 'POST',
					headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
					body: JSON.stringify({
						content,
						kind,
						...(options.regenerate ? { regenerate: true } : {}),
						...(options.imageAssetId ? { image_asset_id: options.imageAssetId } : {})
					}),
					signal: handlers.signal
				};

		let response: Response;
		try {
			response = await fetch(url, init);
		} catch (err) {
			if (isAbort(err, handlers.signal)) return { status: 'stopped' };
			if (attempt === 0) continue;
			return fail(new StreamError(errorMessage(err)), handlers);
		}

		if (!response.ok) {
			// W9.26 §2.2: a turn request may be REFUSED with a structured reason —
			// 409 `{detail:{reason:"compacting"}}` while a compaction holds the claim,
			// or 409 `{detail:{reason:"superseded", id:<successor>}}` when this
			// session was compacted into a later era. Discarding the body (the old
			// behavior) made both indistinguishable from a 500 and silently lost the
			// successor id, so the caller could never follow it. `readErrorDetail`
			// already understands this exact shape; the reason/id ride `meta`.
			const detail = await readErrorDetail(response);
			const fallback = `${isResume ? 'stream resume' : 'turn request'} failed: ${response.status} ${response.statusText}`;
			return fail(
				new StreamError(detail.reason || fallback, {
					status: detail.status,
					reason: detail.reason,
					...(detail.id ? { id: detail.id } : {})
				}),
				handlers
			);
		}
		if (!response.body) {
			return fail(new StreamError('turn response had no body'), handlers);
		}

		let sawDone = false;
		let sawStop = false;
		let doneMeta: Record<string, unknown> | undefined;
		let errorOutcome: StreamOutcome | undefined;

		const parser = createSseParser((ev) => {
			if (ev.id !== undefined) lastEventId = ev.id;
			const event = normalizeStreamEvent(ev.event, ev.data);
			switch (event.type) {
				case 'reasoning':
					handlers.onReasoning?.(event.delta ?? '', event.meta);
					break;
				case 'content':
					handlers.onContent?.(event.delta ?? '', event.meta);
					break;
				case 'done':
					sawDone = true;
					doneMeta = event.meta;
					break;
				case 'stage':
					handlers.onStage?.((event.meta ?? {}) as unknown as StageFrame);
					break;
				case 'committed':
					// The barrier: the turn is kept; the stream stays open for the
					// tail (revision/usage/stage frames) but the user may act again.
					handlers.onCommitted?.(event.meta);
					break;
				case 'revision':
					handlers.onRevision?.((event.meta ?? {}) as unknown as RevisionFrame);
					break;
				case 'error': {
					// A cooperative stop surfaces as `reason: "interrupted"`; that is a
					// user stop, not a failure.
					const reason = stringField(event.meta, 'reason');
					if (reason === 'interrupted') {
						sawStop = true;
						break;
					}
					const error = new StreamError(
						event.delta || stringField(event.meta, 'message') || reason || 'stream error',
						event.meta
					);
					errorOutcome = { status: 'error', error };
					handlers.onError?.(error, event.meta);
					break;
				}
			}
		});

		try {
			const reader = response.body.getReader();
			const decoder = new TextDecoder();
			let streamClosed = false;
			while (!sawDone && !sawStop && !errorOutcome) {
				const { done, value } = await reader.read();
				if (done) {
					streamClosed = true;
					break;
				}
				parser.feed(decoder.decode(value, { stream: true }));
			}
			if (sawDone && !streamClosed) {
				// `done` is not the end of the wire: the server keeps the stream open
				// for a TAIL whose frames are the post-turn work (the state-writing
				// `world_state`/`attribution`/`env_hud` steps, the critic's revision,
				// usage). Hand the still-open reader to a detached pump so those frames
				// reach the same handlers; the outcome below must not wait on it.
				pumpTail(reader, decoder, parser, handlers);
			} else {
				parser.end();
				if (!streamClosed) await reader.cancel().catch(() => undefined);
			}
		} catch (err) {
			if (isAbort(err, handlers.signal)) return { status: 'stopped' };
			if (attempt === 0) continue;
			return fail(new StreamError(errorMessage(err)), handlers);
		}

		if (handlers.signal?.aborted) return { status: 'stopped' };
		if (sawDone) return { status: 'done', meta: doneMeta };
		if (sawStop) return { status: 'stopped' };
		if (errorOutcome) return errorOutcome;

		// Stream closed without a `done` event: resume once, then give up.
		if (attempt === 0) continue;
		return fail(new StreamError('stream ended before a done event'), handlers);
	}

	return fail(new StreamError('turn stream failed'), handlers);
}


/** Read grace window after `done` (ms). The tail is post-turn work, so it is
 *  bounded: past this the pump releases the reader and stops. */
const TAIL_GRACE_MS = 30_000;

/**
 * Keep reading the turn stream AFTER the `done` frame.
 *
 * The turn's promise has already resolved, so this runs detached — a caller is
 * never delayed by tail work. The pump OWNS the reader and the parser from here:
 * it releases both exactly once, when the stream closes, when the grace window
 * expires, or when the caller's signal aborts. Errors are swallowed: the turn
 * itself already succeeded, and a tail failure only means the live-state frames
 * stopped arriving (the views still settle from the projection read).
 */
function pumpTail(
	reader: ReadableStreamDefaultReader<Uint8Array>,
	decoder: TextDecoder,
	parser: ReturnType<typeof createSseParser>,
	handlers: StreamTurnHandlers
): void {
	let finished = false;
	const signal = handlers.signal;

	const finish = (): void => {
		if (finished) return;
		finished = true;
		clearTimeout(grace);
		signal?.removeEventListener('abort', finish);
		try {
			parser.end();
		} catch {
			// A handler threw on the final flush; the tail is already best-effort.
		}
		void reader.cancel().catch(() => undefined);
	};

	const grace = setTimeout(finish, TAIL_GRACE_MS);
	signal?.addEventListener('abort', finish);

	void (async () => {
		try {
			while (!finished) {
				const { done, value } = await reader.read();
				if (done) break;
				if (finished) break;
				parser.feed(decoder.decode(value, { stream: true }));
			}
		} catch {
			// Tail read failed (abort, reset) — the turn is unaffected.
		} finally {
			finish();
		}
	})();
}


function fail(error: StreamError, handlers: StreamTurnHandlers): StreamOutcome {
	handlers.onError?.(error);
	return { status: 'error', error };
}


function stringField(meta: Record<string, unknown> | undefined, key: string): string | undefined {
	const value = meta?.[key];
	return typeof value === 'string' ? value : undefined;
}

/**
 * Map a raw SSE event name + data blob to the shared `StreamEvent` shape.
 * A missing event name defaults to `content`; `data: [DONE]` maps to `done`.
 */
export function normalizeStreamEvent(name: string | undefined, data: string): StreamEvent {
	const trimmed = data.trim();
	const raw = name?.trim();
	if (!raw && (trimmed === '[DONE]' || trimmed === 'DONE')) return { type: 'done' };

	const type = (raw || 'content') as StreamEventType;
	let delta: string | undefined;
	let meta: Record<string, unknown> | undefined;

	if (trimmed !== '') {
		try {
			const parsed: unknown = JSON.parse(trimmed);
			if (typeof parsed === 'string') {
				delta = parsed;
			} else if (parsed && typeof parsed === 'object') {
				const obj = parsed as Record<string, unknown>;
				if (typeof obj.delta === 'string') delta = obj.delta;
				if (obj.meta && typeof obj.meta === 'object') meta = obj.meta as Record<string, unknown>;
				if (typeof obj.message === 'string') meta = { ...(meta ?? {}), message: obj.message };
			}
		} catch {
			delta = data;
		}
	}

	return { type, delta, meta };
}


export interface SseEvent {
	event?: string;
	data: string;
	id?: string;
}


/**
 * Incremental `text/event-stream` parser (WHATWG subset: `event`, `data`, `id`,
 * comments, CR/LF/CRLF line endings). Feed decoded chunks; call `end()` once the
 * stream closes. Blank lines dispatch the buffered event.
 */
export function createSseParser(emit: (event: SseEvent) => void) {
	let buffer = '';
	let pendingCr = false;
	let eventName: string | undefined;
	let dataLines: string[] = [];
	let lastId: string | undefined;

	function dispatch(): void {
		if (dataLines.length === 0) {
			eventName = undefined;
			return;
		}
		emit({ event: eventName, data: dataLines.join('\n'), id: lastId });
		eventName = undefined;
		dataLines = [];
	}

	return {
		feed(chunk: string): void {
			buffer += chunk;
			if (pendingCr) {
				buffer = '\r' + buffer;
				pendingCr = false;
			}
			buffer = buffer.replace(/\r\n/g, '\n');
			if (buffer.endsWith('\r')) {
				pendingCr = true;
				buffer = buffer.slice(0, -1);
			}
			buffer = buffer.replace(/\r/g, '\n');

			let nl: number;
			while ((nl = buffer.indexOf('\n')) !== -1) {
				const line = buffer.slice(0, nl);
				buffer = buffer.slice(nl + 1);
				if (line === '') {
					dispatch();
					continue;
				}
				if (line.startsWith(':')) continue; // comment / keep-alive
				const colon = line.indexOf(':');
				const field = colon === -1 ? line : line.slice(0, colon);
				let value = colon === -1 ? '' : line.slice(colon + 1);
				if (value.startsWith(' ')) value = value.slice(1);
				if (field === 'event') eventName = value;
				else if (field === 'data') dataLines.push(value);
				else if (field === 'id') lastId = value;
				// `retry` is intentionally ignored.
			}
		},
		/** Flush a trailing event that was not terminated by a blank line. */
		end(): void {
			dispatch();
			buffer = '';
		},
		get lastEventId(): string | undefined {
			return lastId;
		}
	};
}


/** Parse a complete SSE body (used by the headless parser check). */
export function parseSseStream(text: string): SseEvent[] {
	const events: SseEvent[] = [];
	const parser = createSseParser((event) => events.push(event));
	parser.feed(text);
	parser.end();
	return events;
}

// ---------------------------------------------------------------------------
// Character-card import (W2.10b) — new section, appended at file end.
//
//   POST {API_BASE}/api/cards/import          -> {"ok": true, "card": {...}}
//   POST {API_BASE}/api/cards/import?seed=1    -> adds {"session_id", "title"}
//        body: raw PNG bytes (image/png) or a JSON card object
//              (a V2 envelope, a V1/flat card, or {"card": {...}})
//        failure: {"ok": false, "error": "<precise CardError message>"}
//
// The created session id is opened through the existing URL param the session
// controller supports (`/chat?s=<id>`). RAW bytes are posted (no FormData), so
// the endpoint needs no python-multipart dependency.
// ---------------------------------------------------------------------------
