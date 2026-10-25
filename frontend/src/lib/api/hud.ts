// Fabulist API client - player HUD reads (recap, memory-config, speaker colors, revisions).

import { API_BASE, StreamError, USE_MOCK, loadMock, readErrorDetail, readJson, str } from './shared';

/** One compaction recap row (`GET .../recap`, H1; chain-aware shape W9.26). */
export interface RecapEntry {
	seq: number;
	text: string;
	/** Inclusive `[low, high]` SEQ span the summary covers (never turn indices). */
	covers_seqs: [number | null, number | null];
	/** W9.26 §2.3 step 7: the prior summary this one ABSORBED, as
	 *  `[session_id, seq]`. Omitted when this era's summary absorbed nothing
	 *  (the engine omits the key rather than sending null; both read the same). */
	absorbs: [string, number] | null;
	/** `manual` (the button) or `forced_90` (the gauge); null when unrecorded. */
	trigger: string | null;
	/** Per-compaction measured stats: calls, prompt tokens, serving model. */
	stats: { calls: number | null; prompt_tokens: number | null; model: string | null };
}

/** Read a session's compaction summaries in seq order (H1 Recap pane).
 *
 * W9.26 §2.5: the transcript's break notice expands by fetching THIS read and
 * showing the raw `summary` v2 row — text, covered span, trigger, model, calls,
 * measured tokens. The `/recap` endpoint is chain-aware (it lists every era's
 * summary, oldest first), so the pane and the notice agree.
 */
export async function getRecap(
	sessionId: string
): Promise<{ recap: RecapEntry[] } | { error: string }> {
	if (USE_MOCK) {
		const raw = await (await loadMock()).getRecap(sessionId);
		return { recap: normalizeRecap(raw.recap) };
	}
	try {
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/recap`);
		if (!res.ok) return { error: `recap failed (${res.status})` };
		const body = await readJson(res);
		return { recap: normalizeRecap(body.recap) };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}

/** Tolerant parse of the recap list (older engines omit the v2 fields). */
function normalizeRecap(value: unknown): RecapEntry[] {
	const raw = Array.isArray(value) ? value : [];
	const recap: RecapEntry[] = [];
	for (const item of raw) {
		if (!item || typeof item !== 'object') continue;
		const obj = item as Record<string, unknown>;
		const covers = Array.isArray(obj.covers_seqs) ? obj.covers_seqs : [];
		const low = typeof covers[0] === 'number' ? (covers[0] as number) : null;
		const high = typeof covers[1] === 'number' ? (covers[1] as number) : null;
		const absorbsRaw = obj.absorbs;
		const absorbs =
			Array.isArray(absorbsRaw) && typeof absorbsRaw[0] === 'string' && typeof absorbsRaw[1] === 'number'
				? ([absorbsRaw[0], absorbsRaw[1]] as [string, number])
				: null;
		const statsRaw =
			obj.stats && typeof obj.stats === 'object' ? (obj.stats as Record<string, unknown>) : {};
		recap.push({
			seq: typeof obj.seq === 'number' ? obj.seq : 0,
			text: typeof obj.text === 'string' ? obj.text : '',
			covers_seqs: [low, high],
			absorbs,
			trigger: typeof obj.trigger === 'string' && obj.trigger ? obj.trigger : null,
			stats: {
				calls: typeof statsRaw.calls === 'number' ? statsRaw.calls : null,
				prompt_tokens:
					typeof statsRaw.prompt_tokens === 'number' ? statsRaw.prompt_tokens : null,
				model: typeof statsRaw.model === 'string' && statsRaw.model ? statsRaw.model : null
			}
		});
	}
	return recap.sort((a, b) => a.seq - b.seq);
}


/**
 * One task surface's row in the W9.26 §2.7 context ledger
 * (`GET .../context`) — read-only provenance, never a control.
 *
 * Every number is either CONFIGURED (`context_window`, `response_length`,
 * `budget_tokens`) or MEASURED (`prompt_tokens` from the newest usage row for
 * that slot); there is deliberately no estimate column (⚑R1a). An undeclared
 * Context Size (⚑C — every live connection today) leaves `window_declared`
 * false and every derived number null, with the engine's `note` naming it.
 */
export interface ContextLedgerRow {
	/** The human task surface (`narrate`, `ooc`, `bts`, `summarizer`, …). */
	task: string;
	/** The taxonomy interaction id the task resolves through. */
	interaction: string;
	/** The agent slot the interaction resolves to right now. */
	agent: string;
	/** The model the slot's newest usage row names (null before any call). */
	model: string | null;
	/** The connection serving that slot (null when nothing is bound). */
	connection: string | null;
	/** Owner-declared Context Size; null = undeclared (never probed). */
	context_window: number | null;
	window_declared: boolean;
	/** Effective `max_tokens` (`merge_sampling`), the only correct source. */
	response_length: number | null;
	/** `context_window − response_length − reserve_tokens`; null when undeclared. */
	budget_tokens: number | null;
	/** Last MEASURED prompt tokens (newest own usage row); null = not measured. */
	prompt_tokens: number | null;
	/** `prompt_tokens / budget_tokens`, only when the budget is positive. */
	ratio: number | null;
	reserve_tokens: number;
	/** Always `measured` — the ledger never estimates. */
	basis: 'measured';
	/** The engine's own note for a row it could not compute ("window undeclared"). */
	note: string | null;
}

/** Read the per-task context ledger (`GET .../context`, W9.26 §2.7).
 *
 * `VITE_MOCK` has no ledger fixture (the mock layer scripts the chat surface,
 * not the Inspector's reads — the pane's `/inspect/turns` read is a plain
 * fetch for the same reason), so mock mode reports the section's error state
 * rather than inventing windows and ratios that no engine measured.
 */
export async function getContextLedger(
	sessionId: string
): Promise<{ tasks: ContextLedgerRow[] } | { error: string }> {
	if (USE_MOCK) return { error: 'context ledger unavailable in mock mode' };
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/context`,
			{ headers: { Accept: 'application/json' } }
		);
		if (res.status === 404) return { error: 'context ledger unavailable (unknown session)' };
		if (!res.ok) return { error: `context ledger failed (${res.status})` };
		return { tasks: normalizeLedger(await readJson(res)) };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}

function numOrNull(value: unknown): number | null {
	return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function textOrNull(value: unknown): string | null {
	return typeof value === 'string' && value ? value : null;
}

/** Tolerant parse of the ledger's `tasks` list (a pre-W9.26 engine ⇒ empty). */
function normalizeLedger(body: Record<string, unknown>): ContextLedgerRow[] {
	const raw = Array.isArray(body.tasks) ? body.tasks : [];
	const tasks: ContextLedgerRow[] = [];
	for (const item of raw) {
		if (!item || typeof item !== 'object') continue;
		const obj = item as Record<string, unknown>;
		const task = str(obj.task);
		if (!task) continue; // the task name IS the row identity
		const window = numOrNull(obj.context_window);
		tasks.push({
			task,
			interaction: str(obj.interaction, task),
			agent: str(obj.agent, '—'),
			model: textOrNull(obj.model),
			connection: textOrNull(obj.connection),
			context_window: window,
			window_declared: obj.window_declared === true && window != null,
			response_length: numOrNull(obj.response_length),
			budget_tokens: numOrNull(obj.budget_tokens),
			prompt_tokens: numOrNull(obj.prompt_tokens),
			ratio: numOrNull(obj.ratio),
			reserve_tokens: numOrNull(obj.reserve_tokens) ?? 0,
			basis: 'measured',
			note: textOrNull(obj.note)
		});
	}
	return tasks;
}


/** The per-session recall policy (`GET .../memory-config`, D25 C2). */
export interface MemoryConfig {
	/** Canonical `chat_memory` toggle (folded from its own event). */
	enabled: boolean;
	k: number;
	block_chars: number;
	rerank_enabled: boolean;
	query_mode: 'message' | 'message+scene';
	overfetch: number;
}


/** Endpoint paths for the memory pane (kept here so a rewire is one line). */
export const MEMORY_CONFIG_ENDPOINTS = {
	config: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/memory-config`,
	memory: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/memory`
} as const;


/** Recall defaults — mirror `fabulist.memory.recall.RecallConfig`. */
export const MEMORY_CONFIG_DEFAULTS: MemoryConfig = {
	enabled: true,
	k: 6,
	block_chars: 1500,
	rerank_enabled: false,
	query_mode: 'message',
	overfetch: 25
};


/** A partial knob patch for `PATCH .../memory-config` (`enabled` goes elsewhere). */
export interface MemoryConfigPatch {
	k?: number;
	block_chars?: number;
	rerank_enabled?: boolean;
	query_mode?: 'message' | 'message+scene';
	overfetch?: number;
}


function normalizeMemoryConfig(body: Record<string, unknown>): MemoryConfig {
	return {
		enabled: typeof body.enabled === 'boolean' ? body.enabled : true,
		k: typeof body.k === 'number' ? body.k : MEMORY_CONFIG_DEFAULTS.k,
		block_chars:
			typeof body.block_chars === 'number' ? body.block_chars : MEMORY_CONFIG_DEFAULTS.block_chars,
		rerank_enabled:
			typeof body.rerank_enabled === 'boolean'
				? body.rerank_enabled
				: MEMORY_CONFIG_DEFAULTS.rerank_enabled,
		query_mode: body.query_mode === 'message+scene' ? 'message+scene' : 'message',
		overfetch: typeof body.overfetch === 'number' ? body.overfetch : MEMORY_CONFIG_DEFAULTS.overfetch
	};
}


/** Read the folded recall knobs (`GET .../memory-config`); defaults on failure. */
export async function getMemoryConfig(sessionId: string): Promise<MemoryConfig> {
	if (USE_MOCK) return { ...MEMORY_CONFIG_DEFAULTS };
	try {
		const res = await fetch(MEMORY_CONFIG_ENDPOINTS.config(sessionId), {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { ...MEMORY_CONFIG_DEFAULTS };
		return normalizeMemoryConfig(await readJson(res));
	} catch {
		return { ...MEMORY_CONFIG_DEFAULTS };
	}
}


/**
 * Patch the non-`enabled` recall knobs (`PATCH .../memory-config`).
 *
 * The route appends a FULL snapshot and is idempotent on read; it rejects
 * `enabled` (that lives on the canonical `chat_memory` event — use
 * {@link setChatMemory}). Throws so the caller can surface a failed save.
 */
export async function patchMemoryConfig(
	sessionId: string,
	patch: MemoryConfigPatch
): Promise<MemoryConfig> {
	if (USE_MOCK) return { ...MEMORY_CONFIG_DEFAULTS, ...patch };
	const res = await fetch(MEMORY_CONFIG_ENDPOINTS.config(sessionId), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch)
	});
	if (!res.ok) {
		throw new StreamError(`patchMemoryConfig failed: ${res.status} ${res.statusText}`);
	}
	return normalizeMemoryConfig(await readJson(res));
}


/** One cast colour registry row (`GET .../speaker_colors`, H5). */
export interface SpeakerColor {
	name: string;
	color: string;
	assigned_by: string;
}


/** Read a session's cast colours, first-assignment order (H5 Cast pane). */
export async function getSpeakerColors(
	sessionId: string
): Promise<{ speaker_colors: SpeakerColor[] } | { error: string }> {
	if (USE_MOCK) return { speaker_colors: [] };
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/speaker_colors`
		);
		if (!res.ok) return { error: `speaker_colors failed (${res.status})` };
		const body = await readJson(res);
		const raw = Array.isArray(body.speaker_colors) ? body.speaker_colors : [];
		const colors: SpeakerColor[] = [];
		for (const item of raw) {
			if (!item || typeof item !== 'object') continue;
			const obj = item as Record<string, unknown>;
			if (typeof obj.name !== 'string' || typeof obj.color !== 'string') continue;
			colors.push({
				name: obj.name,
				color: obj.color,
				assigned_by: typeof obj.assigned_by === 'string' ? obj.assigned_by : 'engine'
			});
		}
		return { speaker_colors: colors };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


// --- W9.22 cast bar: the two tiers (plan §2, "Cast model") ------------------
//
// Doctrine (plan §1.1): the cast is the ATTACHED ACTOR LIST — `card`, `world`,
// promoted `actor`, and `user` when User-as-Actor is ON. Discovery stays free:
// any named presence is tracked by BTS from the turn it appears, but a presence
// is NOT an actor. Both are cast members (the engine's read is membership-based,
// so the walk, the colours and attribution all key on members); what differs is
// the member's TIER:
//
//   * actor tier    — lane-eligible, never cap-evicted;
//   * presence tier — never lanes, never pins, retires by silent LRU under its
//                     own budget.
//
// The cast read already returns each row's `provenance`, so the tier is DERIVED
// here rather than added to the wire: one constant below decides it, and the
// engine's own `fabulist.state.presence.ACTOR_PROVENANCE` is the other spelling
// of the same rule — change the two together. (Presence-tier rows currently
// arrive as provenance `bts`: BTS-learned members, plus the read-only virtual
// display rows the cast builder adds for legacy sessions.)
export const ACTOR_PROVENANCE: ReadonlySet<string> = new Set([
	'card',
	'world',
	'actor',
	'user'
]);

/** Which group a cast row belongs to in the pane / the top-bar strip. */
export type CastTier = 'actor' | 'presence';

/**
 * The row's tier: `actor` for an attached actor's provenance, else `presence`.
 *
 * Unknown/absent provenance (a legacy colour row, an `unknown` member) resolves
 * to `presence` — the conservative direction: a member the engine cannot attest
 * as an attached actor must not be advertised as one.
 */
export function castTierOf(provenance: string | null | undefined): CastTier {
	return provenance && ACTOR_PROVENANCE.has(provenance) ? 'actor' : 'presence';
}

/** One merged cast row (`GET .../cast`), tier resolved (W9.22). */
export interface CastRow {
	/** Immutable member handle; `null` on a read-only virtual (unadmitted) row. */
	member_id: string | null;
	name: string;
	color: string | null;
	assigned_by: string | null;
	/** Kept for wire back-compat: `provenance === 'card'`. New code reads `tier`. */
	from_card: boolean;
	/** Where the member came from: `card` | `world` | `actor` | `user` | `bts` | `unknown`. */
	provenance: string;
	status: string;
	/** A descriptor admitted before its name was known. */
	provisional: boolean;
	/** Names the character has carried before. */
	aliases: string[];
	/**
	 * The persona card minted from this member by the cast ladder (W9.30 §5.3), or
	 * `null`. Read from the persona's own `provenance.member_id` fold — a read, not
	 * a cast write — and only ever displayed: the promote door stays open either way.
	 */
	promoted_persona_id?: string | null;
	/**
	 * The definition this name resolves to (a promoted persona, else the
	 * name-keyed definition a card import captured), or `null`. It is the id the
	 * definition image route is keyed by, so the badge can show the character's
	 * picture instead of initials; `image_asset` says whether one exists.
	 */
	persona_id?: string | null;
	image_asset?: string | null;
	/** Derived from `provenance` — see {@link castTierOf}. */
	tier: CastTier;
}

/**
 * Read a session's cast rows, tier-resolved (`GET .../cast`, W9.22).
 *
 * The read is also the engine's first-sighting colour capture site, but it never
 * admits members (a read that wrote would undo the cast cap) — so a row with
 * `member_id: null` is a display-only presence the roster does not contain.
 * Best-effort like its siblings: a failed read returns `{ error }` rather than
 * throwing into the render path.
 */
export async function getCast(
	sessionId: string
): Promise<{ cast: CastRow[] } | { error: string }> {
	if (USE_MOCK) return { cast: [] };
	try {
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/cast`, {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { error: `cast failed (${res.status})` };
		const body = await readJson(res);
		const raw = Array.isArray(body.cast) ? body.cast : [];
		const cast: CastRow[] = [];
		for (const item of raw) {
			if (!item || typeof item !== 'object') continue;
			const obj = item as Record<string, unknown>;
			if (typeof obj.name !== 'string' || !obj.name) continue;
			const provenance = typeof obj.provenance === 'string' ? obj.provenance : 'unknown';
			cast.push({
				member_id:
					typeof obj.member_id === 'string' && obj.member_id ? obj.member_id : null,
				name: obj.name,
				color: typeof obj.color === 'string' && obj.color ? obj.color : null,
				assigned_by: typeof obj.assigned_by === 'string' ? obj.assigned_by : null,
				from_card: obj.from_card === true,
				provenance,
				status: typeof obj.status === 'string' ? obj.status : 'active',
				provisional: obj.provisional === true,
				aliases: Array.isArray(obj.aliases)
					? obj.aliases.filter((a): a is string => typeof a === 'string' && a.length > 0)
					: [],
				// Absent on an engine that predates W9.30 — an unbadged row, not an error.
				promoted_persona_id:
					typeof obj.promoted_persona_id === 'string' && obj.promoted_persona_id
						? obj.promoted_persona_id
						: null,
				persona_id:
					typeof obj.persona_id === 'string' && obj.persona_id ? obj.persona_id : null,
				image_asset:
					typeof obj.image_asset === 'string' && obj.image_asset ? obj.image_asset : null,
				tier: castTierOf(provenance)
			});
		}
		return { cast };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


// --- W10 person nodes: the definition and live layers ----------------------
//
// `GET .../tree` splits a being into the THREE layers that must never be
// conflated (`fabulist.state.session_tree`, plan §2 "Cast model"):
//
//   * `participation` — the roster's own facts. The pane already reads those
//     through `getCast` (colours, provisional, tier, promotion badge), so the
//     tree nodes are JOINED onto those rows by `participation.member_id` — the
//     tree's ONE identity field — and only the two layers below come from here.
//     One source per fact, so the layers cannot drift apart on a rename.
//   * `definition` — the card the member was seeded from. `null` for a person
//     who never had one: a session node is not a card, and not an alias of the
//     persona card promotion mints (the `promoted` badge is that link).
//   * `live` — where the being is right now, its top active condition, and its
//     body's visible surface. The `Player` node carries the same layer: the
//     player is a being in the registry, not a special case.
//
// The read is PURE (nothing appended, no member minted) and best-effort like its
// siblings: `{ error }` leaves the pane rendering the roster alone.

/** Registry-first presence verdict (`live.presence`, `entities.same_room`). */
export type BeingPresence = 'in_scene' | 'elsewhere' | 'off_screen' | 'unknown';

const BEING_PRESENCES: ReadonlySet<string> = new Set<BeingPresence>([
	'in_scene',
	'elsewhere',
	'off_screen',
	'unknown'
]);

/** A being's live layer, narrowed to what a HUD line renders. */
export interface BeingLive {
	/** Location path (`Secluded Forest > the tent`), or `null` when unassigned. */
	location: string | null;
	presence: BeingPresence;
	/** The first condition the fold has NOT expired, or `null` (the top line). */
	condition: string | null;
	/** The body's `surface` — what is visibly wrong (dirt/blood/injury) — or `null`. */
	bodySurface: string | null;
}

/** The card a cast member was seeded from; `null` when they never had one. */
export interface CastDefinition {
	name: string;
}

/** One cast node's layers, joined to a roster row by `member_id`. */
export interface CastTreeNode {
	/** `participation.member_id` — the join key. */
	member_id: string;
	definition: CastDefinition | null;
	live: BeingLive | null;
}

/** The tree's `Player` node: identity label + the same live layer as a cast node. */
export interface PlayerTreeNode {
	/** `Player (<Name>)`, or plain `Player` when the session has no persona. */
	display: string;
	live: BeingLive | null;
}

/** The cast pane's slice of `GET .../tree` (W10). */
export interface CastTree {
	cast: CastTreeNode[];
	/** `null` when the tree omits the node — there is then no player row to draw. */
	player: PlayerTreeNode | null;
}

/** Narrow one node's `live` payload to the fields a HUD line renders. */
function beingLive(raw: unknown): BeingLive | null {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
	const obj = raw as Record<string, unknown>;
	const body =
		obj.body && typeof obj.body === 'object' ? (obj.body as Record<string, unknown>) : null;
	const conditions = Array.isArray(obj.conditions) ? obj.conditions : [];
	let condition: string | null = null;
	for (const item of conditions) {
		if (!item || typeof item !== 'object') continue;
		const node = item as Record<string, unknown>;
		// `expired` is the fold's own verdict; the first condition it has NOT
		// expired is the one worth a line.
		if (node.expired === true) continue;
		condition = str(node.key) || null;
		break;
	}
	const presence = str(obj.presence);
	return {
		location: str(obj.location) || null,
		presence: (BEING_PRESENCES.has(presence) ? presence : 'unknown') as BeingPresence,
		condition,
		bodySurface: body ? str(body.surface) || null : null
	};
}

/**
 * Read the cast pane's slice of the session tree (`GET .../tree`, W10).
 *
 * Returns every cast node (joined to the roster by `member_id`) plus the `Player`
 * node. The tree's `definition` also carries `description`/`personality`/
 * `scenario`, and `live` carries meters, items and the full body: nothing renders
 * those, so only the card's NAME and the four live fields are parsed — a field
 * read here that no surface shows would be a second shape to keep in sync for no
 * reader.
 *
 * Best-effort like its siblings: a failed read returns `{ error }` and the pane
 * renders the roster alone — the two extra layers are enrichment, never a
 * prerequisite for showing who is in the chat.
 */
export async function getCastTree(sessionId: string): Promise<CastTree | { error: string }> {
	if (USE_MOCK) return { cast: [], player: null };
	try {
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/tree`, {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { error: `tree failed (${res.status})` };
		const body = await readJson(res);
		const tree =
			body.tree && typeof body.tree === 'object' ? (body.tree as Record<string, unknown>) : {};

		const cast: CastTreeNode[] = [];
		const rawCast = Array.isArray(tree.cast) ? tree.cast : [];
		for (const item of rawCast) {
			if (!item || typeof item !== 'object') continue;
			const node = item as Record<string, unknown>;
			const participation =
				node.participation && typeof node.participation === 'object'
					? (node.participation as Record<string, unknown>)
					: null;
			const member_id = participation ? str(participation.member_id) : '';
			if (!member_id) continue; // no join key, no node to join
			const definition =
				node.definition && typeof node.definition === 'object'
					? (node.definition as Record<string, unknown>)
					: null;
			const cardName = definition ? str(definition.name) : '';
			cast.push({
				member_id,
				definition: cardName ? { name: cardName } : null,
				live: beingLive(node.live)
			});
		}

		const rawPlayer =
			tree.Player && typeof tree.Player === 'object'
				? (tree.Player as Record<string, unknown>)
				: null;
		const identity =
			rawPlayer?.identity && typeof rawPlayer.identity === 'object'
				? (rawPlayer.identity as Record<string, unknown>)
				: null;
		const display = identity ? str(identity.display) : '';
		return {
			cast,
			player:
				rawPlayer && display ? { display, live: beingLive(rawPlayer.live) } : null
		};
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


/**
 * One promotion door's pre-flight (`GET .../cast/{member_id}/promotion`, W9.30 §5.3).
 *
 * `eligible` is the door's own rule (an active member whose provenance is `actor`
 * — admitted from an owner-accepted cast proposal). `suggested_name` is what the
 * dialog PRE-FILLS: the member's display name, collision-suffixed against the live
 * persona catalog (`Tessa` → `Tessa1`), so the common case is one click. `reason`
 * is the owner-readable sentence either way, and it names the existing card when
 * the member was already promoted — the door itself never refuses.
 */
export interface PromotionOffer {
	eligible: boolean;
	suggested_name: string;
	reason: string;
}

/** The created persona the manual door answers with (the persona wire, W9.30 §5.3). */
export interface PromotedPersona {
	persona_id: string;
	name: string;
	session_id: string;
	member_id: string;
}

/** The promote door's path — one member, one session (no new mount). */
export function promotionEndpoint(sessionId: string, memberId: string): string {
	return `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/cast/${encodeURIComponent(
		memberId
	)}/promotion`;
}

/** Read the promote dialog's pre-flight (suggestion + eligibility + reason). */
export async function getPromotionOffer(
	sessionId: string,
	memberId: string
): Promise<{ offer: PromotionOffer } | { error: string }> {
	if (USE_MOCK) return { error: 'promotion needs the engine (mock mode has no cast)' };
	try {
		const res = await fetch(promotionEndpoint(sessionId, memberId), {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) {
			const { reason, status } = await readErrorDetail(res);
			return { error: reason || `promotion read failed (${status})` };
		}
		const body = await readJson(res);
		return {
			offer: {
				eligible: body.eligible === true,
				suggested_name: str(body.suggested_name),
				reason: str(body.reason)
			}
		};
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}

/**
 * Promote the member NOW — a studio-class persona write, not a proposal (W9.30
 * §5.3): the engine appends through the persona registry, so no proposal row and
 * no decision row is filed.
 *
 * `name` GIVEN is written VERBATIM (a duplicate the owner typed is deliberate);
 * `name` OMITTED lets the engine apply the collision suffix. The door never
 * refuses a name — only an ineligible member, and that refusal carries the reason.
 */
export async function promoteMember(
	sessionId: string,
	memberId: string,
	name?: string
): Promise<{ persona: PromotedPersona } | { error: string }> {
	if (USE_MOCK) return { error: 'promotion needs the engine (mock mode has no cast)' };
	try {
		const trimmed = (name ?? '').trim();
		const res = await fetch(promotionEndpoint(sessionId, memberId), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(trimmed ? { name: trimmed } : {})
		});
		const body = await readJson(res);
		if (!res.ok) {
			const { reason, status } = await readErrorDetail(res);
			return { error: reason || `promotion failed (${status})` };
		}
		const persona_id = str(body.persona_id);
		if (!persona_id) return { error: 'promotion returned no persona' };
		return {
			persona: {
				persona_id,
				name: str(body.name),
				session_id: str(body.session_id) || sessionId,
				member_id: str(body.member_id) || memberId
			}
		};
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


/** One revision row (`GET .../revisions`, H4 edit markers). */
export interface RevisionRow {
	id: number;
	/** Event id this revision supersedes (matches a transcript `logId`). */
	supersedes: number | null;
	reason: string;
	ts: string;
}


/** List a session's revision rows (append order) for inline edit markers. */
export async function listRevisions(sessionId: string): Promise<RevisionRow[]> {
	if (USE_MOCK) return [];
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/revisions`,
			{ headers: { Accept: 'application/json' } }
		);
		if (!res.ok) return [];
		const body = await readJson(res);
		const raw = Array.isArray(body.revisions) ? body.revisions : [];
		const rows: RevisionRow[] = [];
		for (const item of raw) {
			if (!item || typeof item !== 'object') continue;
			const obj = item as Record<string, unknown>;
			rows.push({
				id: typeof obj.id === 'number' ? obj.id : 0,
				supersedes: typeof obj.supersedes === 'number' ? obj.supersedes : null,
				reason: typeof obj.reason === 'string' ? obj.reason : '',
				ts: typeof obj.ts === 'string' ? obj.ts : ''
			});
		}
		return rows;
	} catch {
		return [];
	}
}

// --- Behind-the-Scenes world state + speaker attribution (W6C) ---------------
//
// The engine appends a structured `system`/`world_state` event each turn, but no
// read endpoint exposes it to the browser. The same raw BTS notation is captured
// verbatim on the `bts` agent_call (`GET .../agent-calls` -> `response_text`), so
// this reader reconstructs the structured state the dashboard needs by parsing
// that notation exactly like `fabulist.state.bts.parse_bts`, then folding the
// checkpoint/deltas in seq order. Attribution rides the same capture endpoint
// (the `attribution` agent_call's `response_text` is the JSON array).
//
// Everything here is additive + best-effort: a missing capture (or an engine
// without these interactions) yields no rows and the caller falls back to the
// 5-key fold. Nothing throws into the render path.
