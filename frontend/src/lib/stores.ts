// Client-side UI state for the chat screen.
//
// Plain Svelte stores (not runes) so this module stays a `.ts` file and any
// component can auto-subscribe with `$store`. The stores hold projections of
// the backend state — the event log stays the source of truth (PLAN D9); these
// are just the view.

import { writable } from 'svelte/store';

import type { SessionSummary, SettingsCatalog, ValidationNotice } from '#lib/api';

export type TurnKind = 'in_character' | 'ooc';
export type Role = 'user' | 'narrator';

/** One ordered piece of a narrator message (see `routes/chat/segment.ts`). */
export type NarratorSegmentKind = 'prose' | 'dsl';
export interface NarratorSegment {
	kind: NarratorSegmentKind;
	text: string;
}

/**
 * One rendered message. `reasoning`/`error`/`kind` extend the minimal
 * `{role, prose, dsl, streaming}` shape from the work-unit spec so the
 * transcript can show a live "thinking…" block and failures inline.
 */
export interface TranscriptEntry {
	id: string;
	/** Log event id (hydrated entries only) — enables the revision door. */
	logId?: number;
	role: Role;
	/** Narrator prose, or the raw user message. */
	prose: string;
	/** Captured OpenUI DSL text (rendered by `DslFallback` for v0). */
	dsl: string;
	/** Image shown on the user's message (data URL or object URL). */
	image?: string;
	/** Inline audio/video on the user's message (raw-endpoint or object URL). */
	media?: { kind: string; mime: string; url: string };
	/**
	 * Ordered prose/DSL segments for interwoven rendering. `undefined` means
	 * the payload predates segments (legacy) — render prose then DSL stacked.
	 * A present-but-empty array renders no content (empty turn).
	 */
	segments?: NarratorSegment[];
	/** Reasoning-channel text, shown collapsed while streaming. */
	reasoning: string;
	streaming: boolean;
	error?: string;
	kind?: TurnKind;
	/**
	 * D-F: the post-commit validation notice for THIS narrator turn, when the
	 * engine flagged it. Rendered as a quiet system line at the end of the turn
	 * it refers to (never a toast or a trailing digest), so it travels WITH the
	 * entry: the post-`done` rebuild from the committed projection carries it in
	 * the same patch as the final text, and a reload rebuilds it identically.
	 */
	notice?: ValidationNotice;
}

export type ConnectionStatus = 'idle' | 'connecting' | 'streaming' | 'error' | 'closed';

/** Id of the current session, or `null` before one is created. */
export const sessionId = writable<string | null>(null);
/** Display title of the current session ('' for a fresh, unsaved chat). */
export const sessionTitle = writable<string>('');
/** Visible sessions from the log, newest activity first (drawer list). */
export const sessions = writable<SessionSummary[]>([]);
/** Toggle catalog + metadata from the settings surface (W2.11), or null. */
export const settings = writable<SettingsCatalog | null>(null);
/** Ordered transcript, oldest first. */
export const transcript = writable<TranscriptEntry[]>([]);
/** Parsed facts: `time`, `loc`, `weather`, `tone`, `inv`, ... (string values). */
export const facts = writable<Record<string, string>>({});
/** Connection lifecycle, shown in the sidebar. */
export const connection = writable<ConnectionStatus>('idle');
/**
 * WHY the connection is in `error` ('' when it is not).
 *
 * The transcript carries the failure for the turn it belongs to, but the rail's
 * status alone said "error" with no cause in view — and nothing in the rail could
 * clear it. This is the one line the status line shows beside the retry door; it
 * is cleared by the same code that leaves `error` (a successful probe, a new
 * turn), so it can never outlive the state it explains.
 */
export const connectionError = writable<string>('');
/**
 * Nudge for the state views that read the object stores (registry/entities).
 *
 * `facts` only changes when a kept turn's projection lands, so a view that refetches
 * on `facts` shows nothing until the turn is over. The page bumps this counter when a
 * stage that WRITES those stores completes (the planner's bootstrap, the post-turn
 * tracker, the historian lanes), so the pane fills in mid-turn (see
 * `routes/chat/+page.svelte`'s stage handler for the debounce).
 */
export const stateNonce = writable<number>(0);

/** Ask every state view to refetch now (one GET each — never per frame). */
export function bumpStateNonce(): void {
	stateNonce.update((n) => n + 1);
}

let counter = 0;

export function addUserEntry(
	prose: string,
	kind: TurnKind = 'in_character',
	image?: string,
	media?: { kind: string; mime: string; url: string }
): string {
	const id = `user-${++counter}`;
	transcript.update((entries) => [
		...entries,
		{
			id,
			role: 'user',
			prose,
			dsl: '',
			reasoning: '',
			streaming: false,
			kind,
			...(image ? { image } : {}),
			...(media ? { media } : {})
		}
	]);
	return id;
}

export function addNarratorEntry(): string {
	const id = `narrator-${++counter}`;
	transcript.update((entries) => [
		...entries,
		{ id, role: 'narrator', prose: '', dsl: '', reasoning: '', streaming: true }
	]);
	return id;
}

export function updateEntry(id: string, patch: Partial<TranscriptEntry>): void {
	transcript.update((entries) =>
		entries.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry))
	);
}

export function appendReasoning(id: string, delta: string): void {
	if (!delta) return;
	transcript.update((entries) =>
		entries.map((entry) =>
			entry.id === id ? { ...entry, reasoning: entry.reasoning + delta } : entry
		)
	);
}

export function finishEntry(id: string): void {
	updateEntry(id, { streaming: false });
}

/** Drop one entry (used when a fork preview is abandoned by a stop). */
export function removeEntry(id: string): void {
	transcript.update((entries) => entries.filter((entry) => entry.id !== id));
}

export function resetTranscript(): void {
	transcript.set([]);
}

/** Replace the transcript wholesale (used when hydrating from a projection). */
export function setTranscript(entries: TranscriptEntry[]): void {
	transcript.set(entries);
}

export function setFacts(next: Record<string, string>): void {
	facts.set(next);
}

/** Replace the drawer's session list. */
export function setSessions(next: SessionSummary[]): void {
	sessions.set(next);
}

/** Replace the settings catalog (after GET / refresh). */
export function setSettings(next: SettingsCatalog | null): void {
	settings.set(next);
}

/** Clear the transcript + facts for a fresh, unsaved chat view. */
export function clearSessionView(): void {
	transcript.set([]);
	facts.set({});
	sessionTitle.set('');
	processingStage.set(null);
	resetTurnSteps();
}

/**
 * Parse the pre-turn facts block into key/values.
 *
 * Accepts the injected `<state>...</state>` form and the bare pipe-YAML line
 * (`time: 14:35 | loc: night market | ...`), with or without the `Δ[...]`
 * extractor wrapper. Later occurrences of a key win.
 */
export function parseFactsBlock(raw: string): Record<string, string> {
	let text = raw.trim();
	const block = text.match(/<state>([\s\S]*?)<\/state>/i);
	if (block) text = block[1];
	text = text.replace(/^\s*Δ?\s*\[/, '').replace(/\]\s*$/, '');

	const out: Record<string, string> = {};
	for (const part of text.split('|')) {
		const segment = part.trim();
		if (!segment) continue;
		const colon = segment.indexOf(':');
		if (colon === -1) continue;
		const key = segment.slice(0, colon).trim();
		const value = segment.slice(colon + 1).trim();
		if (key) out[key] = value;
	}
	return out;
}

/** Coerce a projection `state` object into displayable string facts. */
export function factsFromState(state: Record<string, unknown>): Record<string, string> {
	const keys = Object.keys(state);
	if (keys.length === 1 && typeof state.raw === 'string') {
		return parseFactsBlock(state.raw);
	}
	const out: Record<string, string> = {};
	for (const [key, value] of Object.entries(state)) {
		if (value === null || value === undefined) out[key] = '';
		else if (Array.isArray(value)) out[key] = value.join(', ');
		else if (typeof value === 'object') out[key] = JSON.stringify(value);
		else out[key] = String(value);
	}
	return out;
}

// --- assistants (D38 full rename of the W2.5 extra-assistant lane) ----------
// Appended section only (lane coordination). Assistant replies render as
// distinct asides in the thread; the event log remains the source of truth.

/** One assistant reply shown as a distinct transcript aside. */
export interface AssistantReplyEntry {
	id: string;
	assistant: string;
	assistantName: string;
	text: string;
	agent: string;
	/** Epoch ms when the reply arrived (ordering against the transcript). */
	at: number;
	/** The triggering message, when known. */
	user_msg?: string;
	/** True while the consult is in flight. */
	pending?: boolean;
	error?: string;
}

/** Ordered assistant replies for the current session view. */
export const assistantReplies = writable<AssistantReplyEntry[]>([]);

let assistantCounter = 0;

/** Append a reply aside; returns its generated id. */
export function addAssistantReply(
	entry: Omit<AssistantReplyEntry, 'id' | 'at'> & { at?: number }
): string {
	const { at, ...rest } = entry;
	const id = `assistant-${++assistantCounter}`;
	assistantReplies.update((entries) => [...entries, { ...rest, at: at ?? Date.now(), id }]);
	return id;
}

/** Patch one reply aside in place (pending -> text/error). */
export function updateAssistantReply(id: string, patch: Partial<AssistantReplyEntry>): void {
	assistantReplies.update((entries) =>
		entries.map((entry) => (entry.id === id ? { ...entry, ...patch } : entry))
	);
}

/** Replace the reply list wholesale (used when hydrating from the log). */
export function setAssistantReplies(entries: AssistantReplyEntry[]): void {
	assistantReplies.set(entries);
}

/** Clear reply asides (fresh / session-less view). */
export function resetAssistantReplies(): void {
	assistantReplies.set([]);
}

// --- composer fill ----------------------------------------------------------
// An Assistant option is a suggestion, not a turn: clicking it fills the
// composer and waits for the user to send (docs/UI-LANGUAGE.md §6 — never
// auto-send). `Composer.svelte` owns its text as local `$state` bound to the
// textarea, so the request crosses the tree as a store; the composer assigns
// this into that same `text` state and clears the store back to `null` (no
// second input path).

/** Pending composer text, or `null` when there is nothing to fill. */
export const composerFill = writable<string | null>(null);

/** Request the composer be filled with `text`; blank input is ignored. */
export function fillComposer(text: string | null | undefined): void {
	const value = (text ?? '').trim();
	if (!value) return;
	composerFill.set(value);
}

// --- D42 turn progress ------------------------------------------------------
// The backend emits a `stage` frame at every in-graph step boundary (narrate,
// dsl, critic, regen, world_state, env_hud, attribution) and the `regen` step is
// conditional, so only the raw frame is stored here — `DslHeader.svelte` derives
// the percentage (and clamps it) rather than trusting `index / total`. The same
// frames are folded into `turnSteps`, the ordered trace the header's click
// summary reads.

/** One in-graph step boundary, normalized for the pinned DSL header. */
export interface ProcessingStage {
	/** Stable machine id (`narrate`, `critic`, …), or `''` when absent. */
	stage: string;
	/** Short human label, or `''` when absent (the bar falls back). */
	label: string;
	state: 'start' | 'done';
	/** 1-based step position, or `null` when the frame omitted it. */
	index: number | null;
	/** Number of steps in the plan, or `null` when the frame omitted it. */
	total: number | null;
	/** Engine-emitted ISO-8601 timestamp, or `''` when the frame omitted it. */
	ts: string;
	/** Validator one-liners attached when the step ran (usually empty). */
	notes: string[];
}

/** The running turn's current step, or `null` when no turn is running. */
export const processingStage = writable<ProcessingStage | null>(null);

/**
 * One ordered step of a turn's engine trace (W3B). It is the same record the
 * `stage` frame carries, folded into an ordered list: `name` is the node/agent
 * identity the inspector's per-turn `calls[].agent` reads too, so the live
 * header and the post-turn inspector name a step identically.
 */
export interface TurnStep {
	/** Node/agent name (`narrate`, `dsl`, `critic`, …); `''` if the frame had none. */
	name: string;
	/** Short human label for the summary row. */
	label: string;
	/** `start` while the step runs, `done` once its boundary closed. */
	state: 'start' | 'done';
	index: number | null;
	total: number | null;
	/** Engine-emitted ISO-8601 timestamp, or `''` when the frame omitted it. */
	ts: string;
	/** Validator one-liners attached at validation time (usually empty). */
	notes: string[];
}

/**
 * The running (or most recently finished) turn's ordered step trace. It
 * PERSISTS after `done` so the header's click summary still reads it — the
 * next turn's first step replaces it (see `foldTurnStep`).
 */
export const turnSteps = writable<TurnStep[]>([]);

/**
 * True once THIS stream's turn has recorded a `plan` step.
 *
 * `turnSteps` is a cross-turn accumulator, so it cannot tell this turn's plan
 * step from the previous turn's — the guard "no plan step in the CURRENT turn's
 * trace" (W9.20 §5.6, round 3) needs a per-turn window. The window opens at
 * stream open (`beginTurnWindow`, called by the page) and on every reset below.
 */
let windowHasPlan = false;

/**
 * Open a fresh per-turn trace window. The page calls this when a turn's stream
 * opens — the one boundary `foldTurnStep` cannot see (a stream closing emits no
 * frame). Until it runs, a skip turn that follows a plan turn keeps the previous
 * turn's plan step in the list and would wrongly append instead of resetting.
 */
export function beginTurnWindow(): void {
	windowHasPlan = false;
}

/** Drop the accumulated trace (fresh/session-switched view). */
export function resetTurnSteps(): void {
	windowHasPlan = false;
	turnSteps.set([]);
}

/**
 * Seed the ordered trace from a persisted projection payload (hydrate path).
 *
 * The header reads the `turnSteps` store, which otherwise only the LIVE stream
 * fills — so a reload, or a swipe/regenerate that lands the view on a sibling
 * branch, showed "No steps recorded for this turn yet" (the reported bug). The
 * backend now persists the turn's trace as a `turn_stages` system event and the
 * projection re-attaches it to the narrative it describes.
 */
export function setTurnSteps(steps: TurnStep[]): void {
	turnSteps.set(steps);
}

/**
 * Coerce a persisted projection `steps` payload into `TurnStep[]`; a malformed
 * or absent payload yields `[]` rather than throwing (the header degrades to
 * its empty state). Mirrors `setProcessingStage`'s tolerant frame handling.
 */
export function coerceTurnSteps(raw: unknown): TurnStep[] {
	if (!Array.isArray(raw)) return [];
	const out: TurnStep[] = [];
	for (const item of raw) {
		if (!item || typeof item !== 'object') continue;
		const obj = item as Record<string, unknown>;
		const name = typeof obj.name === 'string' ? obj.name : '';
		const label = typeof obj.label === 'string' ? obj.label : '';
		if (!name && !label) continue;
		const state: 'start' | 'done' = obj.state === 'done' ? 'done' : 'start';
		const index =
			typeof obj.index === 'number' && Number.isFinite(obj.index) ? obj.index : null;
		const total =
			typeof obj.total === 'number' && Number.isFinite(obj.total) ? obj.total : null;
		const ts = typeof obj.ts === 'string' ? obj.ts : '';
		const notes = Array.isArray(obj.notes)
			? obj.notes.filter((note): note is string => typeof note === 'string')
			: [];
		out.push({ name, label, state, index, total, ts, notes });
	}
	return out;
}

/**
 * Fold one normalized stage frame into the ordered trace.
 *
 * Reset rule (W9.20 §5.6, round 3 — the plan and the lanes are the only steps
 * that can open a turn): a `start` frame REPLACES the previous turn's trace when
 * it is the planner (`plan`, always index 1 on a planner turn) OR the cast lanes
 * run and no `plan` step was recorded in THIS turn's window (a skip turn starts
 * at the lanes). A planner turn's mid-turn `lanes` start therefore keeps the
 * plan step it just recorded. A `done` closes the most recent OPEN step of the
 * same name (a FAIL regeneration re-emits the critic's slot, so matching by name
 * keeps the order honest); an unmatched `done` still records a closed step
 * rather than being dropped.
 */
function foldTurnStep(step: TurnStep): void {
	const startsPlan = step.state === 'start' && step.name === 'plan';
	const startsSkipTurn = step.state === 'start' && step.name === 'lanes' && !windowHasPlan;
	if (startsPlan) windowHasPlan = true;
	turnSteps.update((steps) => {
		if (startsPlan || startsSkipTurn) {
			return [step];
		}
		// A RE-OPENED stage replaces its slot in place. The repair loop re-opens
		// `narrate` after the gate fails; appending it produced a duplicate,
		// out-of-order trace (the persisted server-side trace is keyed by index —
		// mirror that here, `index` is null only for tolerated legacy frames).
		if (step.state === 'start' && step.index !== null) {
			const at = steps.findIndex((s) => s.index === step.index);
			if (at >= 0) {
				return [...steps.slice(0, at), step, ...steps.slice(at + 1)];
			}
		}
		if (step.state === 'done') {
			for (let i = steps.length - 1; i >= 0; i--) {
				if (steps[i].name === step.name && steps[i].state === 'start') {
					const closed: TurnStep = {
						...steps[i],
						state: 'done',
						ts: step.ts || steps[i].ts,
						notes: step.notes.length ? step.notes : steps[i].notes
					};
					return [...steps.slice(0, i), closed, ...steps.slice(i + 1)];
				}
			}
		}
		return [...steps, step];
	});
}

/** Record one `stage` frame; a null/partial payload never throws (D42). */
export function setProcessingStage(frame: Partial<ProcessingStage> | null | undefined): void {
	if (!frame || typeof frame !== 'object') {
		processingStage.set(null);
		return;
	}
	const stage = typeof frame.stage === 'string' ? frame.stage : '';
	const label = typeof frame.label === 'string' ? frame.label : '';
	// A frame that names no step at all (empty/foreign payload) is not a step:
	// ignoring it keeps the last real one instead of fabricating progress.
	if (!stage && !label) return;
	const state: 'start' | 'done' = frame.state === 'done' ? 'done' : 'start';
	const index = typeof frame.index === 'number' && Number.isFinite(frame.index) ? frame.index : null;
	const total = typeof frame.total === 'number' && Number.isFinite(frame.total) ? frame.total : null;
	const ts = typeof frame.ts === 'string' ? frame.ts : '';
	const notes = Array.isArray(frame.notes)
		? frame.notes.filter((note): note is string => typeof note === 'string')
		: [];
	processingStage.set({ stage, label, state, index, total, ts, notes });
	foldTurnStep({ name: stage, label, state, index, total, ts, notes });
}
