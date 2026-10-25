// Fabulist API client - shared public types (moved out of the `api.ts` monolith).

import type { StreamError } from './shared';

export type TurnKind = 'in_character' | 'ooc';

export type StreamEventType =
	| 'reasoning'
	| 'content'
	| 'done'
	| 'error'
	| 'stage'
	| 'revision'
	| 'committed';


/** One event on the turn stream (shared contract shape). */
export interface StreamEvent {
	type: StreamEventType;
	delta?: string;
	meta?: Record<string, unknown>;
}


/**
 * The `stage` frame payload (D42): the current in-graph processing step.
 * Delivered through `StreamTurnHandlers.onStage`. `index`/`total` are 1-based
 * so a caller can derive a percentage; `state` marks the step's boundary.
 * The frame carries this object under `meta`.
 */
export interface StageFrame {
	/** Stable machine id: narrate | dsl | critic | regen | world_state | env_hud | attribution. */
	stage: string;
	/** Short human label for the status bar. */
	label: string;
	/** `start` when the step begins, `done` when it finishes. */
	state: 'start' | 'done';
	index: number;
	total: number;
}


/** The `revision` frame payload (D41): a critic patch/regeneration landed live. */
export interface RevisionFrame {
	target_id: number;
	revision_id: number | null;
	verdict: string;
	reason: string;
	prose: string;
	dsl: string;
	segments: Array<Record<string, unknown>>;
}


export interface StreamTurnHandlers {
	onReasoning?: (delta: string, meta?: Record<string, unknown>) => void;
	/** W9.19 (A): the commit barrier — the turn is kept, the tail is background
	 *  work; the host unlocks the composer now instead of at done. */
	onCommitted?: (meta?: Record<string, unknown>) => void;
	onContent?: (delta: string, meta?: Record<string, unknown>) => void;
	onDone?: (meta?: Record<string, unknown>) => void;
	onError?: (error: StreamError, meta?: Record<string, unknown>) => void;
	/** A `stage` frame: the current in-graph processing step (D42), before `done`. */
	onStage?: (stage: StageFrame) => void;
	/** A `revision` frame: a critic patch/regeneration landed live (D41), before `done`. */
	onRevision?: (revision: RevisionFrame) => void;
	/** Abort to stop the turn (the backend sees the client disconnect). */
	signal?: AbortSignal;
}


/**
 * Terminal result of a turn stream.
 *  - `done`    the server sent a `done` event
 *  - `stopped` the caller aborted via `handlers.signal`
 *  - `error`   failure before `done` (already reported through `onError`)
 */
export type StreamOutcome =
	| { status: 'done'; meta?: Record<string, unknown> }
	| { status: 'stopped' }
	| { status: 'error'; error: StreamError };


/** One item in a projection: an event row with a `payload` object. */
export interface ProjectionItem {
	id: number;
	seq: number;
	type: string;
	payload: Record<string, unknown>;
	[k: string]: unknown;
}

/** One teaching finding on a validation notice (D-F). Mirrors the row the
 *  validate stage appends (`graph/nodes._notice_flags`): `message` names what
 *  tripped the check, `fix` the correction to make. */
export interface ValidationNoticeFlag {
	rule_id?: string;
	check?: string;
	severity?: string;
	message?: string;
	evidence?: string;
	fix?: string;
	anchor?: string;
}

/** A flagged turn's post-commit teaching notice (D-F). `seq` is the FLAGGED
 *  NARRATIVE's seq: the transcript renders the notice directly after that turn
 *  — its natural position, never a toast or a trailing digest. */
export interface ValidationNotice {
	seq: number;
	flags: ValidationNoticeFlag[];
	/** Validator score the flags produced, when the row recorded one. */
	score?: number;
}


/** Derived view of the event log (mirrors `EventLog.get_projection`). */
export interface Projection {
	narratives: ProjectionItem[];
	/** Shallow fold of `state-delta` payloads, last write wins. */
	state: Record<string, unknown>;
	user_messages: ProjectionItem[];
	rolls: ProjectionItem[];
	assets: ProjectionItem[];
	/** D-F: teaching notices for flagged turns, ordered by anchor seq. Each one
	 *  belongs at the tail of the narrative entry whose seq it names. */
	notices: ValidationNotice[];
	/** W9.26 §2.2: chain-walked `context_low` system rows, in seq order. An
	 *  inherited row (from an earlier era) renders above the break and settled. */
	context_lows?: ContextLowRow[];
	/** W9.26 §2.2/§2.5: chain-walked compaction MARKER rows, in seq order — one
	 *  transcript break per marker. The LAST one is the current era's. */
	compactions?: CompactionMarker[];
}

/**
 * One chain-walked `system` row (`{seq, session_id, payload}`).
 *
 * The chain read inherits ancestor rows with the ancestor's own seqs, so the
 * seq ordering is the transcript ordering (compaction forks at the tip).
 */
export interface ChainRow<TPayload = Record<string, unknown>> {
	seq: number;
	session_id: string;
	payload: TPayload;
}

/**
 * The `system`/`context_low` payload (W9.26 §2.2) — the context gauge's notice.
 *
 * `ratio` is ALWAYS a float (the over-budget sentinel is `1.0`); the optional
 * flags are omitted unless true (never emitted false), so `=== true` is the
 * only correct read.
 */
export interface ContextLowPayload extends Record<string, unknown> {
	event?: string;
	/** The resolved interaction slot that produced the row. */
	agent?: string;
	ratio?: number;
	prompt_tokens?: number;
	budget_tokens?: number;
	/** `measured` (the only basis the gauge may act on, ⚑R1a). */
	basis?: string;
	/** The declared window cannot pay for a reply: a declaration problem. */
	over_budget?: boolean;
	/** The forced compaction ran and cannot free more window this era. */
	noop?: boolean;
}

/** One chain-walked context-low row. */
export type ContextLowRow = ChainRow<ContextLowPayload>;

/**`summary` v2 stats (W9.26 §2.3 step 7): per-compaction measured cost. */
export interface CompactionStats extends Record<string, unknown> {
	calls?: number;
	prompt_tokens?: number;
	model?: string;
}

/**
 * The `system`/`compaction` MARKER payload (W9.26 §2.3 step 9). Appended to the
 * NEW era's session; its presence is what makes a child a continuation
 * (`kind: "compaction"`) and what settles an earlier context-low row.
 */
export interface CompactionMarkerPayload extends Record<string, unknown> {
	event?: string;
	/** The session the compaction ran in (the parent era). */
	source_session?: string;
	/** `[session_id, seq]` of the `summary` v2 row the era forks from. */
	summary_ref?: unknown[];
	/** Inclusive `[low, high]` seq span the summary covers. */
	covers_seqs?: unknown[];
	/** `manual` | `forced_90` (the compact pipeline's trigger). */
	trigger?: string;
	stats?: CompactionStats;
}

/** One chain-walked compaction marker. */
export type CompactionMarker = ChainRow<CompactionMarkerPayload>;

/** The 202 job state (W9.26 §2.3), polled from the compaction-status route. */
export type CompactionJobStatus = 'running' | 'done' | 'failed' | 'noop';

/**
 * One compaction job's status DTO (bound cross-lane contract).
 *
 * `session_id`/`title` are the SUCCESSOR era on `done`; `error`/`stage` are the
 * failure detail on `failed` (a `noop` is not a failure — the button re-arms).
 */
export interface CompactionJob {
	job_id?: string;
	status: CompactionJobStatus;
	session_id?: string;
	title?: string;
	error?: string;
	stage?: string;
}

/** One session row in the drawer's list (derived from the event log). */
export interface SessionSummary {
	id: string;
	title: string;
	/** ISO timestamp of the session's most recent event, or null. */
	updated_at: string | null;
	/** Number of user turns logged in the session. */
	turns: number;
	/** The chat's USER persona (D16/D53), or null for a chat with none assigned.
	 *  D53 keeps this the ``{{user}}`` record only; the chat's CAST/actor is the
	 *  separate ``actor_id`` / ``actor_ids`` pair below. */
	persona_id: string | null;
	/** True when the chat EXPLICITLY has no persona (the picker's "No persona").
	 *  Distinct from never-set, which inherits the pinned default. Optional so an
	 *  older engine payload degrades to "never set". */
	persona_cleared?: boolean;
	/** World snapshot id (D32/D40), or null for a plain chat. */
	world_id: string | null;
	/** Per-chat embedding toggle (D16); defaults on. */
	memory_enabled: boolean;
	/** D53: the chat's primary actor (first of ``actor_ids``), or null. This is
	 *  the CAST record (``chat_actor``), distinct from the user persona. Optional
	 *  so an older engine payload degrades to "no actor". */
	actor_id?: string | null;
	/** D53: the full actor set (``chat_actor`` fold, latest-wins). Optional for
	 *  the same backward-compatible reason as ``actor_id``. */
	actor_ids?: string[];
	/** W9.26: true when this session is a SUPERSEDED PARENT — branches-derived
	 *  (it has a child whose own rows carry a `compaction` marker), i.e. the chat
	 *  was compacted into a later era and this row is the old one. Optional so an
	 *  engine that predates W9.26 degrades to "not compacted". The era child
	 *  (the live continuation) carries no badge. */
	compacted?: boolean;
}


/**
 * One alternate reply in a reply's sibling set (W2.2). Position 1 is the
 * original/parent line; every fork of the same cut follows it.
 */
export interface BranchSibling {
	/** Session id of this alternate line. */
	sessionId: string;
	/** 1-based position within `BranchInfo.siblings`. */
	position: number;
	/** True for the line the UI is currently standing on. */
	current: boolean;
	/** One-line summary of this alternate's reply (may be empty). */
	reply: string;
	/** Seq of this alternate's reply event, when the backend reports it. */
	seq: number | null;
	/**
	 * W9.26 §2.6: what kind of child this is.
	 *
	 * `user` — a swipe alternate (a forked reply to the same cut). `compaction` —
	 * a CONTINUATION era (the session that continues this one after a compaction
	 * marker): never a swipe alternative, always labeled as such. `normalizeBranches`
	 * keeps it OUT of every sibling/alternate merge group (a swipe may not silently
	 * cross eras) while the DTO still reports the kind so the branches list can
	 * label it (lane F's rendering). Older engines omit it → `user`. */
	kind: BranchSiblingKind;
}

/** W9.26 §2.6: a branch child is either a swipe alternate or a compaction era. */
export type BranchSiblingKind = 'user' | 'compaction';


/** A reply's siblings, as returned by `GET /api/sessions/{id}/branches`. */
export interface BranchInfo {
	/** The session the request was made for. */
	sessionId: string;
	/** Parent session id (null for the original line), when known. */
	parentId: string | null;
	/** Fork cut (the user event seq the siblings share), when known. */
	cut: number | null;
	/** 1-based position of the current session within the set. */
	index: number;
	/** Total alternates (1 = no fork; no controls are shown). */
	count: number;
	siblings: BranchSibling[];
	/** W9.26 §2.6: compaction-era children of this session — labelled
	 *  continuations, NEVER part of the swipe group (`siblings`). Rendered by
	 *  BranchControls as their own marked row. */
	continuations: BranchSibling[];
}


/** Options for `streamTurn` beyond the turn content. */
export interface StreamTurnOptions {
	/**
	 * Regenerate the reply inherited at a branch's fork cut: the backend skips
	 * the user-event append (FORK-SEMANTICS F2). Used by `forkTurn`.
	 */
	regenerate?: boolean;
	/** Attached image asset id: the narrator sees the image natively (VLM
	 * content part) instead of a caption prefix. */
	imageAssetId?: string;
}


/** Terminal result of a fork-and-stream request (see `forkTurn`). */
export interface ForkOutcome {
	status: 'done' | 'stopped' | 'error';
	/** The freshly forked branch session id, present on `done`. */
	branchId?: string;
	meta?: Record<string, unknown>;
	error?: StreamError;
}


/** One ordered narrator directive row a preset may carry (W3: directives
 * became preset data). Field-for-field the engine's `parts.Directive` storage
 * row; an absent/empty preset list = inherit the factory catalog. */
export interface PromptDirective {
	id: string;
	name: string;
	content: string;
	/** Budget-trim order (lower trims first). */
	priority: number;
	/** The factory TIER 0–4 grouping. */
	tier: number;
	/** Core / protected rows are never trimmed by the budget manager. */
	core: boolean;
	protected: boolean;
	enabled: boolean;
	/** Short user-facing copy; never rendered into the narrator prompt. */
	description: string;
}


/** One toggleable narrator directive (mirrors `parts.Directive` + UI fields). */
export interface ToggleDefinition {
	id: string;
	label: string;
	description: string;
	enabled: boolean;
	/** Core / panel-placement directives cannot be disabled. */
	protected: boolean;
	core: boolean;
	tier: number;
	priority: number;
}


/** One engine FEATURE flag (a capability the ENGINE gates — not a directive
 * row, so it can never leak into the prompt as text). Mirrors
 * `fabulist.api.settings.FEATURE_FLAGS`. */
export interface FeatureDefinition {
	id: string;
	/** Human label for the settings surface (e.g. "Anti Injection"). */
	name: string;
	enabled: boolean;
}


/** The settings surface (W2.11): ordered toggle catalog + metadata. */
export interface SettingsCatalog {
	toggles: Record<string, ToggleDefinition>;
	/** Engine FEATURE flags keyed by id (absent on older payloads). */
	features?: Record<string, FeatureDefinition>;
	/**
	 * Pinned global default persona (user request: "pin a Default persona"), or
	 * `null` when unpinned. Read-time fallback for chats with no own persona.
	 */
	default_persona_id: string | null;
	/** Display name of the pinned default persona (resolved server-side). */
	default_persona_label: string | null;
	meta: {
		/** Declared render order of toggle ids. */
		order?: string[];
		count?: number;
		protected?: string[];
		core?: string[];
		updated_at?: string | null;
		source?: string;
		tier_labels?: Record<string, string>;
		[k: string]: unknown;
	};
}
