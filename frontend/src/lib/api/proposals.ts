// Fabulist API client + render model — the proposal review home (W9.23 §3.1/§4).
//
// Why this module exists: proposals arrive as DIFFS (W9.23 §3.4 — the owner's
// "diffs first" reversal), so the review home is no longer a list of patches. It
// is a TREE: kind → object → field section → operation row, and every row shows
// the delta (`old -> new`, or `new` only for an add, or a struck-through `old`
// for a delete). The model never asks the owner to regenerate a blob she cannot
// see the edges of; she approves exactly what changes.
//
// PINNED CROSS-LANE CONTRACT (the API/store lanes build these; this lane codes to
// the contract, never to their file — plan §4):
//
//   GET  /api/proposals?session={sid}
//        -> {"pending": [Proposal], "archived": [Proposal], "committed": [Decision]}
//   GET  /api/proposals/inbox
//        -> {"by_session": {sid: {"title", "pending": [Proposal], "archived": [Proposal]}}}
//   POST /api/proposals/{sid}/decide  {"ids":[str], "decision":"accept"|"reject"|"dismiss"}
//                                     -> {"decisions":[Decision], "rejected":[{"id","error"}]}
//   POST /api/proposals/{sid}/undo    {"decision_id": str} -> {"decision": Decision}
//   ProposalError -> 400 {"detail": str}; unknown ids -> 400 with the per-id list.
//
// W9.23 wire additions (all additive): `Proposal` gains `ops` (the operation
// list; `null`/absent for whole-value add/remove/cast rows), `target_name`,
// `archived` / `age_turns` (the derived archive partition — §3.2), and
// `stale_paths` (op paths whose `old` no longer matches the current fold, so the
// accept will refuse — §3.4.5). `Decision` gains `kind` / `action`. The wire read
// for a session also carries the `archived` partition.
//
// W9.30 wire additions (additive): `target_new` on both `Proposal` and
// `Decision` — a BORN-PARENTED books row names a world book that does not exist
// yet, so its accept CREATES the book and applies its entry adds (§3.1/§4.1), and
// the surface renders it as a CREATE (its own `t:<name>` object, the `new` chip,
// and a one-line "accepting creates this book and adds N entries" hint). A lane
// book `add` (`patch.name` + `patch.entries`) is equally a create and renders the
// same affordance. Promotion rows need no new key: they are ordinary `actor`
// `add` rows whose `origin` is `promotion` (the cast ladder minting a persona).
//
// Why the reads are defensive: a proposal the surface cannot parse must not take
// the owner's whole queue down with it. Only a row with NO handle is dropped — it
// could not be acted on anyway — and every other field is coerced, so a schema
// addition lands as a visible item with a blank detail rather than a silent
// disappearance. The same rule covers the op list: an op missing its `op` verb or
// its `path` is dropped, but the proposal it belonged to stays reviewable.
//
// Why the fold lives here and not in the component: both surfaces (the chat
// drawer and the cross-session studio inbox) must group IDENTICALLY, and a fold
// this fiddly (per-kind section vocabulary, extensions keys, entry-per-operation
// rows, archive partition) has to be testable without a DOM.

import { API_BASE, errorMessage, readJson, USE_MOCK } from './shared';


// --- wire types ---------------------------------------------------------------

/**
 * One operation in an amend patch (W9.23 §3.4.1).
 *
 * `add` — a fact at a NOT-YET-EXISTING path; carries `new`, `old` forbidden.
 * `modify` — an existing fact; carries `old -> new` (the apply refuses when
 *   `old` no longer matches the current fold).
 * `delete` — an existing fact; carries `old` for display/verification and
 *   removes EXACTLY that path (siblings survive byte-identical).
 *
 * `hasOld`/`hasNew` are parsed presence flags, not opinions: an `old` of `null`
 * is a legitimate value (a cleared image reference), so "absent" and "null" must
 * stay distinguishable for the diff to render honestly.
 */
export interface OpRow {
	/** `add` | `modify` | `delete` (schema_gate.ASSET_PROPOSAL_OP_ACTIONS). */
	op: string;
	/** The dotted path: a field, a nested key (`extensions.mood`), or an entry (`entries.<id>`). */
	path: string;
	/** The replaced value, exactly as filed (absent for `add`). */
	old: unknown;
	hasOld: boolean;
	/** The proposed value (absent for `delete`). */
	new: unknown;
	hasNew: boolean;
	/**
	 * A server-HYDRATED legacy whole-value row (§4): the patch key of a pre-op
	 * amend projected as one whole-field modify row. It carries no anchored `old`,
	 * so it is never stale-tracked — it applies newest-wins as it always did.
	 */
	legacy: boolean;
}

/** One live `asset_proposal` as the API serves it (contract shape). */
export interface Proposal {
	id: string;
	/** `world` | `actor` | `book` | `cast` (schema_gate.ASSET_PROPOSAL_KINDS). */
	kind: string;
	/** `add` | `amend` | `remove`. */
	action: string;
	/** The existing asset for amend/remove; `null` for add. */
	target: string | null;
	/** The readable label for `target`, resolved server-side (never a bare id). */
	target_name: string;
	/**
	 * The BORN-PARENTED marker (W9.30 §3.1/§6): the row names a world book that
	 * does not exist yet, so accepting CREATES the book and applies its entry adds
	 * in one step. Set by the lane's parser, never by the model; old rows fold false.
	 */
	target_new: boolean;
	/** The whole-value definition fields (legacy amend / object add); `{}` for op rows. */
	patch: Record<string, unknown>;
	/** The operation list; empty for whole-value add/remove/cast rows. */
	ops: OpRow[];
	/** `promotion` (AUTO) | `bts` (the proposal lanes / the historian). */
	origin: string;
	/** The decision model's P(propose); `null` when the item was never judged. */
	signal: number | null;
	/** The lane's justification line, when the producer recorded one. */
	evidence: string;
	/** Older proposals for the same asset that collapsed into this one (supersede). */
	supersedes: string[];
	/** Derived archive partition (§3.2): true = aged out of the queue, never deleted. */
	archived: boolean;
	/** Non-ephemeral narratives on the read chain since this row was filed. */
	age_turns: number;
	/** Op paths whose filed `old` no longer matches the fold — the accept will refuse. */
	stale_paths: string[];
}

/** One owner decision — the row UNDO compensates (finding 13). */
export interface Decision {
	decision_id: string;
	proposal_id: string;
	/** `accept` | `reject` | `dismiss`. */
	decision: string;
	/** The asset class the decision acted on (W9.23 §4 amendment). */
	kind: string;
	/** `add` | `amend` | `remove` (W9.23 §4 amendment — closes the D77 residual). */
	action: string;
	/** The accept created its target — a born-parented book row (W9.30 §4.1). */
	target_new: boolean;
	/** The definition an accepted amend/remove replaced (`null` for an add). */
	pre_image: Record<string, unknown> | null;
	/** The definition the accept wrote (`null` for an accepted remove). */
	applied: Record<string, unknown> | null;
	/** One human line for the committed list; empty when the API omits it. */
	applied_summary: string;
}

/** The three verdicts the decide door accepts. */
export type ProposalDecision = 'accept' | 'reject' | 'dismiss';

/** One id the decide door refused (stale, superseded, already decided). */
export interface RejectedDecision {
	id: string;
	error: string;
}

/**
 * The decide door's whole outcome.
 *
 * `error` is the CALL failure (network / engine error / a 400 whose only detail is
 * a message); `rejected` is the per-id verdict list. Both can be non-empty at once
 * on a 400 that names ids, which is why this is one object instead of a union: the
 * surface shows the banner AND keeps the refused rows visible.
 */
export interface DecideOutcome {
	decisions: Decision[];
	rejected: RejectedDecision[];
	error: string;
}

/** A session's review queue: the live fold, the archive partition, applied decisions. */
export interface SessionQueue {
	pending: Proposal[];
	/** Aged-out rows (§3.2) — a separate drawer, never a deletion, never mass-selected. */
	archived: Proposal[];
	committed: Decision[];
}

/** One session's slice of the cross-session inbox (the studio home's unit). */
export interface InboxSession {
	sessionId: string;
	title: string;
	pending: Proposal[];
	archived: Proposal[];
}

/**
 * The inbox read state, owned by `ProposalsHome` and handed to the shared body.
 *
 * The studio reads the inbox itself (the review home's studio half is a queue over
 * every chat, not over the open one), so the shared component receives data +
 * honesty state rather than a session id — that keeps ONE loading/empty/error
 * vocabulary in the component while the read stays where the mode lives.
 */
export interface InboxState {
	sessions: InboxSession[];
	loading: boolean;
	error: string;
}


// --- endpoints ----------------------------------------------------------------

/** Endpoint paths, kept here so a rewire is one line. */
export const PROPOSAL_ENDPOINTS = {
	session: (sessionId: string) =>
		`${API_BASE}/api/proposals?session=${encodeURIComponent(sessionId)}`,
	inbox: () => `${API_BASE}/api/proposals/inbox`,
	decide: (sessionId: string) =>
		`${API_BASE}/api/proposals/${encodeURIComponent(sessionId)}/decide`,
	undo: (sessionId: string) =>
		`${API_BASE}/api/proposals/${encodeURIComponent(sessionId)}/undo`
} as const;


// --- parsing ------------------------------------------------------------------

function text(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

/** A handle as the wire spells it (the engine serves string ids; ints are accepted). */
function handle(value: unknown): string {
	if (typeof value === 'string') return value.trim();
	if (typeof value === 'number' && Number.isFinite(value)) return String(value);
	return '';
}

function record(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

function optionalRecord(value: unknown): Record<string, unknown> | null {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

function flag(value: unknown): boolean {
	return value === true;
}

function int(value: unknown): number {
	if (typeof value === 'number' && Number.isFinite(value)) return Math.trunc(value);
	if (typeof value === 'string' && value.trim() !== '') {
		const parsed = Number(value);
		if (Number.isFinite(parsed)) return Math.trunc(parsed);
	}
	return 0;
}

function has(object: Record<string, unknown>, key: string): boolean {
	return Object.prototype.hasOwnProperty.call(object, key);
}

/** One operation row, or `null` when it names nothing (no verb or no path). */
export function normalizeOp(raw: unknown): OpRow | null {
	const obj = record(raw);
	const op = text(obj.op).trim();
	const path = text(obj.path).trim();
	if (!op || !path) return null;
	return {
		op,
		path,
		old: obj.old,
		hasOld: has(obj, 'old'),
		new: obj.new,
		hasNew: has(obj, 'new'),
		legacy: flag(obj.legacy)
	};
}

function parseOps(raw: unknown): OpRow[] {
	if (!Array.isArray(raw)) return [];
	const rows: OpRow[] = [];
	for (const item of raw) {
		const op = normalizeOp(item);
		if (op) rows.push(op);
	}
	return rows;
}

/** One proposal row, or `null` when it carries no handle (it could not be decided). */
export function normalizeProposal(raw: unknown): Proposal | null {
	const obj = record(raw);
	const id = handle(obj.id);
	if (!id) return null;
	const signal = obj.signal;
	return {
		id,
		kind: text(obj.kind),
		action: text(obj.action),
		target: typeof obj.target === 'string' && obj.target.trim() ? obj.target : null,
		target_name: text(obj.target_name),
		target_new: flag(obj.target_new),
		patch: record(obj.patch),
		ops: parseOps(obj.ops),
		origin: text(obj.origin),
		signal:
			typeof signal === 'number' && Number.isFinite(signal) && !Number.isNaN(signal)
				? signal
				: null,
		evidence: text(obj.evidence),
		supersedes: Array.isArray(obj.supersedes)
			? obj.supersedes.map(handle).filter((value) => value !== '')
			: [],
		archived: flag(obj.archived),
		age_turns: int(obj.age_turns),
		stale_paths: Array.isArray(obj.stale_paths)
			? obj.stale_paths.map(text).filter((value) => value.trim() !== '')
			: []
	};
}

/** One decision row, or `null` when it carries no undo handle. */
export function normalizeDecision(raw: unknown): Decision | null {
	const obj = record(raw);
	const decisionId = handle(obj.decision_id);
	if (!decisionId) return null;
	return {
		decision_id: decisionId,
		proposal_id: handle(obj.proposal_id),
		decision: text(obj.decision),
		kind: text(obj.kind),
		action: text(obj.action),
		target_new: flag(obj.target_new),
		pre_image: optionalRecord(obj.pre_image),
		applied: optionalRecord(obj.applied),
		applied_summary: text(obj.applied_summary)
	};
}

function parseProposals(raw: unknown): Proposal[] {
	if (!Array.isArray(raw)) return [];
	const rows: Proposal[] = [];
	for (const item of raw) {
		const proposal = normalizeProposal(item);
		if (proposal) rows.push(proposal);
	}
	return rows;
}

function parseDecisions(raw: unknown): Decision[] {
	if (!Array.isArray(raw)) return [];
	const rows: Decision[] = [];
	for (const item of raw) {
		const decision = normalizeDecision(item);
		if (decision) rows.push(decision);
	}
	return rows;
}

function parseRejected(raw: unknown): RejectedDecision[] {
	if (!Array.isArray(raw)) return [];
	const rows: RejectedDecision[] = [];
	for (const item of raw) {
		const obj = record(item);
		const id = handle(obj.id);
		if (!id) continue;
		rows.push({ id, error: text(obj.error) || 'refused' });
	}
	return rows;
}

/** Map the inbox envelope's `by_session` onto the review home's session list. */
export function parseInbox(raw: unknown): InboxSession[] {
	const bySession = record(record(raw).by_session);
	const sessions: InboxSession[] = [];
	for (const [sessionId, value] of Object.entries(bySession)) {
		const obj = record(value);
		const partition = partitionArchived([
			...parseProposals(obj.pending),
			...parseProposals(obj.archived)
		]);
		sessions.push({
			sessionId,
			title: text(obj.title) || sessionId,
			pending: partition.live,
			archived: partition.archived
		});
	}
	return sessions;
}

/**
 * One session read payload (`pending` + the derived `archived` partition).
 *
 * The wire serves the two lists separately, but the partition is applied over
 * BOTH: a row that is flagged `archived` can never leak into the live queue, so
 * the queue surface can never mass-select it — the derived-partition rule (§3.2)
 * holds even if a producer ever misfiles a row.
 */
export function parseSessionQueue(raw: unknown): SessionQueue {
	const obj = record(raw);
	const partition = partitionArchived([
		...parseProposals(obj.pending),
		...parseProposals(obj.archived)
	]);
	return {
		pending: partition.live,
		archived: partition.archived,
		committed: parseDecisions(obj.committed)
	};
}


// --- wire reads ---------------------------------------------------------------

/** Read one session's review queue (`GET /api/proposals?session=`). */
export async function getProposals(
	sessionId: string
): Promise<SessionQueue | { error: string }> {
	if (USE_MOCK) return mockSessionQueue(sessionId);
	try {
		const res = await fetch(PROPOSAL_ENDPOINTS.session(sessionId), {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { error: `proposals failed (${res.status})` };
		return parseSessionQueue(await readJson(res));
	} catch (err) {
		return { error: errorMessage(err) };
	}
}

/** Read the cross-session inbox (`GET /api/proposals/inbox`). */
export async function getProposalInbox(): Promise<{ sessions: InboxSession[] } | { error: string }> {
	if (USE_MOCK) return { sessions: await mockInbox() };
	try {
		const res = await fetch(PROPOSAL_ENDPOINTS.inbox(), {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { error: `proposal inbox failed (${res.status})` };
		return { sessions: parseInbox(await readJson(res)) };
	} catch (err) {
		return { error: errorMessage(err) };
	}
}

/**
 * Decide one or more proposals in ONE call (`POST .../{sid}/decide`).
 *
 * Granular and batched are the same door: the log writes one decision row per id
 * (:func:`fabulist.state.proposals.accept_many`), so a mass accept stays
 * individually undoable — which is exactly why the surface never needs a
 * "batch" concept of its own.
 *
 * Archived ids are refused in any MULTI-id body (§3.3, server-side teeth); a
 * single-id action on an archived row stays legal, which is why the archive
 * surface renders per-row actions and no mass buttons.
 */
export async function decideProposals(
	sessionId: string,
	ids: string[],
	decision: ProposalDecision
): Promise<DecideOutcome> {
	if (ids.length === 0) return { decisions: [], rejected: [], error: '' };
	if (USE_MOCK) return mockDecide(ids, decision);
	try {
		const res = await fetch(PROPOSAL_ENDPOINTS.decide(sessionId), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ids, decision })
		});
		const body = await readJson(res);
		if (!res.ok) {
			// A refused batch is two facts at once: the message the owner reads
			// (ProposalError's `detail`) and, for unknown ids, the per-id list.
			const detail = text(body.detail);
			return {
				decisions: [],
				rejected: parseRejected(body.rejected),
				error: detail || `${decision} failed (${res.status})`
			};
		}
		return {
			decisions: parseDecisions(body.decisions),
			rejected: parseRejected(body.rejected),
			error: ''
		};
	} catch (err) {
		return { decisions: [], rejected: [], error: errorMessage(err) };
	}
}

/** Undo one accepted decision (`POST .../{sid}/undo`) — a compensating append. */
export async function undoDecision(
	sessionId: string,
	decisionId: string
): Promise<{ decision: Decision } | { error: string }> {
	if (USE_MOCK) return mockUndo(decisionId);
	try {
		const res = await fetch(PROPOSAL_ENDPOINTS.undo(sessionId), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ decision_id: decisionId })
		});
		const body = await readJson(res);
		const decision = normalizeDecision(body.decision);
		if (!res.ok) {
			const detail = text(body.detail);
			return { error: detail || `undo failed (${res.status})` };
		}
		if (!decision) return { error: 'undo returned no decision' };
		return { decision };
	} catch (err) {
		return { error: errorMessage(err) };
	}
}


// --- the review tree (W9.23 §3.1) ---------------------------------------------
//
//   ReviewTree = KindSection[]                 // world → actor → book → cast
//   KindSection  { kind, label, hint, objects, items, ids }   // cast: objects empty, rows flat
//   ObjectGroup  { key, label, isNew, sections, items, ids }  // the collapsible unit
//   FieldSection { field, label, hint?, items, ids }          // one writable field
//   ReviewRow    { id, proposal, op, stale }                  // ONE operation (or a whole value)
//
// The fold groups OPERATIONS, not proposals: a proposal carrying ops on
// `description` and `extensions.mood` contributes one row to each section, so the
// owner scans a FIELD (her stated use: "an actor whose example dialogue is empty")
// rather than a proposal. Every node exposes its flattened `ids`, which is what
// makes cascade selection a derivation rather than bookkeeping.

/** The display label for a writable field or section (presentation only — the
 *  *writability* truth stays `schema_gate.py` on the server). */
export const FIELD_LABELS: Record<string, string> = {
	name: 'Name',
	description: 'Description',
	personality: 'Personality',
	scenario: 'Scenario',
	book_ids: 'Books',
	cast: 'Cast list',
	image_asset: 'Image',
	provenance: 'Provenance',
	entries: 'Entries',
	extensions: 'Extensions',
	aliases: 'Aliases',
	origin: 'Origin',
	other: 'Other',
	removal: 'Removal'
};

interface SectionSpec {
	field: string;
	label: string;
}

/** The ordered section vocabulary per kind (§3.1). Greeting fields are absent by
 *  owner ruling Q1 — the gate refuses them, so no section renders. */
const SECTION_SPECS: Record<string, readonly SectionSpec[]> = {
	world: [
		{ field: 'description', label: 'Description' },
		{ field: 'scenario', label: 'Scenario' },
		{ field: 'cast', label: 'Cast list' },
		{ field: 'book_ids', label: 'Books' },
		{ field: 'image_asset', label: 'Image' },
		{ field: 'name', label: 'Name' },
		{ field: 'provenance', label: 'Where it came from' }
	],
	actor: [
		// The name leads for an ACTOR: the promotion row mints a persona card and its
		// final (collision-suffixed) name is the first thing the owner approves
		// (W9.30 §6/req 7). A rename in an amend reads fine first too.
		{ field: 'name', label: 'Name' },
		{ field: 'description', label: 'Description' },
		{ field: 'personality', label: 'Personality' },
		{ field: 'scenario', label: 'Scenario' },
		{ field: 'book_ids', label: 'Books' },
		{ field: 'image_asset', label: 'Image' },
		{ field: 'provenance', label: 'Where it came from' }
	],
	book: [
		{ field: 'entries', label: 'Entries' },
		{ field: 'name', label: 'Name' },
		{ field: 'provenance', label: 'Where it came from' }
	],
	cast: []
};

/** A path root's section, per kind — the fields the asset append APIs persist. */
const ROOT_SECTIONS: Record<string, Record<string, string>> = {
	world: {
		name: 'name',
		description: 'description',
		scenario: 'scenario',
		cast: 'cast',
		book_ids: 'book_ids',
		provenance: 'provenance',
		image_asset: 'image_asset'
	},
	actor: {
		name: 'name',
		description: 'description',
		personality: 'personality',
		scenario: 'scenario',
		book_ids: 'book_ids',
		provenance: 'provenance',
		image_asset: 'image_asset'
	},
	book: { name: 'name', entries: 'entries', provenance: 'provenance' },
	cast: { name: 'name', aliases: 'aliases', origin: 'origin' }
};

const OTHER_HINT =
	'outside the proposal vocabulary (D4 extra) — an amend ignores it; an add keeps the known fields only';
const REMOVAL_HINT = 'removes the whole asset — nothing is diffed, there is no prior content to show';

/** Data-type order + display labels (§3.1: world → actor → book → cast). */
const KIND_ORDER: readonly string[] = ['world', 'actor', 'book', 'cast'];
const KIND_LABELS: Record<string, string> = {
	world: 'World',
	actor: 'Actor',
	book: 'Book',
	// `cast` is the OWNER-GATED admission (an unaccepted person a BTS pass
	// detected): accepting ADMITS the member through the existing cast door.
	cast: 'Cast member'
};
const KIND_HINTS: Record<string, string> = {
	world: 'amendments to a world definition',
	actor: 'amendments to an actor definition',
	book: 'amendments to a lorebook — one row per entry operation',
	cast: 'new people a scene surfaced — accepting admits them to this chat’s cast'
};

/** A data type's singular display label (`world` -> `World`). */
export function kindLabel(kind: string): string {
	return KIND_LABELS[kind] ?? (kind || 'Asset');
}

/** A kind header's plural label (`actor` -> `Actors`). */
export function kindSectionLabel(kind: string): string {
	if (kind === 'cast') return 'Cast members';
	return `${kindLabel(kind)}s`;
}

/** One line under a kind header, so a group never reads as unexplained noise. */
export function kindHint(kind: string): string {
	return KIND_HINTS[kind] ?? 'proposed changes';
}

/**
 * The section a dotted path belongs to, per kind.
 *
 * `extensions.<key>` gets its OWN section per key (`extensions.mood` ≠
 * `extensions.card`) — that per-key grouping is the extensions-tree the owner
 * asked for. A path outside the vocabulary folds into `Other` (annotated as
 * ignored, because an amend's apply ignores non-writable paths).
 */
export function sectionFor(
	kind: string,
	path: string
): { field: string; label: string; hint?: string } {
	const cleaned = String(path || '').trim();
	const root = cleaned.split('.')[0] ?? '';
	if (root === 'extensions') {
		const key = cleaned.split('.').slice(1).join('.');
		return { field: `extensions:${key}`, label: key ? `Extensions · ${key}` : 'Extensions' };
	}
	const mapped = ROOT_SECTIONS[kind]?.[root];
	if (mapped) return { field: mapped, label: FIELD_LABELS[mapped] ?? mapped };
	return { field: 'other', label: FIELD_LABELS.other, hint: OTHER_HINT };
}

/** A single path's display label, kind-agnostic (for a row's own path chip). */
export function sectionLabel(path: string): string {
	const cleaned = String(path || '').trim();
	const root = cleaned.split('.')[0] ?? '';
	if (root === 'extensions') {
		const key = cleaned.split('.').slice(1).join('.');
		return key ? `Extensions · ${key}` : 'Extensions';
	}
	return FIELD_LABELS[root] ?? FIELD_LABELS.other;
}

/**
 * The object a proposal acts on — the store's supersede identity minus the action.
 *
 * `t:<target>` for amend/remove, `n:<lowercased-trimmed name>` for add. An add
 * and an amend can name the same asset and STILL be separate objects (the add's
 * group renders "(new)"): one creates the asset, the other edits it, and merging
 * them would hide which accept mints content and which replaces it.
 *
 * A born-parented books row (W9.30 §3.2) carries the MISSING book's NAME as its
 * `target`, so it keys as `t:<name>` — its own object, labelled with the name the
 * accept will create.
 */
export function objectKey(proposal: Proposal): string {
	if (proposal.target) return `t:${proposal.target}`;
	if (proposal.target_new) {
		const named =
			proposal.target_name ||
			(typeof proposal.patch.name === 'string' ? proposal.patch.name : '');
		if (named.trim()) return `t:${named.trim()}`;
	}
	const name = proposal.patch.name;
	if (typeof name === 'string' && name.trim()) return `n:${name.trim().toLowerCase()}`;
	return `x:${proposal.id}`;
}

/** The object group's label: the readable target name, else the name it proposes. */
export function objectLabel(proposal: Proposal): string {
	const name =
		proposal.target_name ||
		(typeof proposal.patch.name === 'string' ? proposal.patch.name : '') ||
		(proposal.target ? proposal.target.slice(-8) : '') ||
		kindLabel(proposal.kind);
	// A create row's label is the asset's NAME; the `new` chip carries the fact
	// that accepting mints it (W9.30 §6) — never a second "(new)" marker.
	if (proposal.target_new) return name;
	return proposal.action === 'add' ? `${name} (new)` : name;
}

/** One operation row's one-line summary (also the aria-label stem). */
export function opLabel(op: OpRow): string {
	return `${op.op} ${op.path}`.trim();
}

/** The collapsed object's summary: how many operations, over which fields. */
function changeSummary(sections: FieldSection[]): string {
	const count = sections.reduce((total, section) => total + section.items.length, 0);
	if (count === 0) return '';
	const fields = sections.map((section) => section.label).join(', ');
	return `${count} operation${count === 1 ? '' : 's'} — ${fields}`;
}

/** One review row: a single operation of one proposal (or a whole-value row). */
export interface ReviewRow {
	/** The PROPOSAL id — the decide handle. An op is never independently decidable. */
	id: string;
	proposal: Proposal;
	/** The operation, or `null` for a whole-value row (object remove / cast add). */
	op: OpRow | null;
	/** This op's path is in the proposal's `stale_paths` — the accept will refuse. */
	stale: boolean;
}

export interface FieldSection {
	field: string;
	label: string;
	hint?: string;
	items: ReviewRow[];
	/** Distinct proposal ids under this section (cascade selection + mass accept). */
	ids: string[];
}

export interface ObjectGroup {
	key: string;
	label: string;
	isNew: boolean;
	/**
	 * The one-line note an object that MINTS its asset on accept carries in place
	 * of a stale badge (W9.30 §6): "accepting creates this book and adds N entries".
	 * Empty for every ordinary object.
	 */
	createHint: string;
	/** The collapsed-state summary: the operation count and the fields it touches. */
	change: string;
	sections: FieldSection[];
	items: ReviewRow[];
	ids: string[];
}

export interface KindSection {
	kind: string;
	label: string;
	hint: string;
	/** Empty for `cast` — candidates have no object behind them; rows ride flat. */
	objects: ObjectGroup[];
	items: ReviewRow[];
	ids: string[];
}

function distinct(values: string[]): string[] {
	return [...new Set(values)];
}

/**
 * A value the engine's card assembly deliberately leaves empty (W9.30 §5.5): a
 * promotion card writes only what play established, so its authored fields arrive
 * as `""`/`[]`/`null` and would otherwise render as ten rows of nothing.
 */
function emptyValue(value: unknown): boolean {
	if (value === null || value === undefined) return true;
	if (typeof value === 'string') return value.trim() === '';
	if (Array.isArray(value)) return value.length === 0;
	return false;
}

/**
 * A promotion row (W9.30 §5.2/§6): the persona card the cast ladder mints from a
 * session-cast member. It IS an ordinary `actor` `add` — the accept path is the
 * one that already exists — whose `origin` is the ladder rather than a BTS lane.
 */
export function isPromotion(proposal: Proposal): boolean {
	return proposal.origin === 'promotion' && proposal.kind === 'actor' && proposal.action === 'add';
}

/**
 * The operations a proposal contributes, derived from where its content lives.
 *
 * Op-typed rows carry their own list. Whole-value shapes are normalized here so
 * the tree has ONE row vocabulary:
 *   - `add` → one `add` row per patch key (the new path shows no prior content —
 *     per-field rows are what let the owner scan a new card for weak fields);
 *   - a legacy `amend` (patch, no ops — pre-R2 rows, and the shape the server
 *     hydrates at read) → one whole-field `modify` row per patch key, `legacy`
 *     tagged and with no anchored `old`;
 *   - `remove` and `cast` → no ops (whole-value rows).
 */
export function opsOf(proposal: Proposal): OpRow[] {
	if (proposal.ops.length > 0) return proposal.ops;
	const promotion = isPromotion(proposal);
	const keys = Object.keys(proposal.patch).filter(
		(key) => !promotion || !emptyValue(proposal.patch[key])
	);
	if (proposal.action === 'add') {
		// A promotion card leads with its FINAL (collision-suffixed) name: that is
		// the fact the owner approves before accepting (req 7).
		if (promotion) keys.sort((a, b) => (a === 'name' ? -1 : b === 'name' ? 1 : 0));
		return keys.map((key) => ({
			op: 'add',
			path: key,
			old: undefined,
			hasOld: false,
			new: proposal.patch[key],
			hasNew: true,
			legacy: false
		}));
	}
	if (proposal.action === 'amend') {
		return keys.map((key) => ({
			op: 'modify',
			path: key,
			old: undefined,
			hasOld: false,
			new: proposal.patch[key],
			hasNew: true,
			legacy: true
		}));
	}
	return [];
}

function orderSections(kind: string, fields: string[]): string[] {
	const canonical = (SECTION_SPECS[kind] ?? []).map((spec) => spec.field);
	const present = new Set(fields);
	const extensions = fields.filter((field) => field.startsWith('extensions')).sort();
	const ordered = [
		...canonical.filter((field) => present.has(field)),
		...extensions,
		...(present.has('other') ? ['other'] : []),
		...(present.has('removal') ? ['removal'] : [])
	];
	// Any unknown field would be a fold bug; keep it visible rather than dropping it.
	for (const field of fields) {
		if (!ordered.includes(field)) ordered.push(field);
	}
	return ordered;
}

function buildFieldSections(kind: string, rows: ReviewRow[]): FieldSection[] {
	const buckets = new Map<string, { label: string; hint?: string; items: ReviewRow[] }>();
	for (const row of rows) {
		const spec = row.op
			? sectionFor(kind, row.op.path)
			: row.proposal.action === 'remove'
				? { field: 'removal', label: FIELD_LABELS.removal, hint: REMOVAL_HINT }
				: { field: 'other', label: FIELD_LABELS.other, hint: OTHER_HINT };
		const bucket = buckets.get(spec.field) ?? { label: spec.label, hint: spec.hint, items: [] };
		bucket.items.push(row);
		buckets.set(spec.field, bucket);
	}
	return orderSections(kind, [...buckets.keys()]).map((field) => {
		const bucket = buckets.get(field) ?? { label: field, hint: undefined, items: [] };
		return {
			field,
			label: bucket.label,
			hint: bucket.hint,
			items: bucket.items,
			ids: distinct(bucket.items.map((item) => item.id))
		};
	});
}

/**
 * The create hint for an object whose accept MINTS its asset (W9.30 §6).
 *
 * Two wire shapes are creates: a born-parented books amend (`target_new`, whose
 * `ops` are the entry adds it will apply to the book it creates) and a lane book
 * add (`patch.name` + `patch.entries`, equally a create — the accept attaches it).
 * Both say the same thing to the owner, so both render the same line; anything
 * else renders nothing.
 */
function createHintOf(kind: string, proposals: Proposal[]): string {
	if (kind !== 'book') return '';
	const parented = proposals.find((proposal) => proposal.target_new);
	const adder = parented ?? proposals.find((proposal) => proposal.action === 'add');
	if (!adder) return '';
	const entries = parented
		? parented.ops.filter((op) => op.op === 'add' && op.path.split('.')[0] === 'entries').length
		: Array.isArray(adder.patch.entries)
			? adder.patch.entries.length
			: 0;
	return `accepting creates this book and adds ${entries} ${entries === 1 ? 'entry' : 'entries'}`;
}

function buildObjects(kind: string, proposals: Proposal[]): ObjectGroup[] {
	const buckets = new Map<string, { proposals: Proposal[]; rows: ReviewRow[] }>();
	for (const proposal of proposals) {
		const key = objectKey(proposal);
		const bucket = buckets.get(key) ?? { proposals: [], rows: [] };
		const ops = opsOf(proposal);
		if (ops.length === 0) {
			bucket.rows.push({ id: proposal.id, proposal, op: null, stale: false });
		} else {
			for (const op of ops) {
				bucket.rows.push({
					id: proposal.id,
					proposal,
					op,
					stale: proposal.stale_paths.includes(op.path)
				});
			}
		}
		bucket.proposals.push(proposal);
		buckets.set(key, bucket);
	}
	return [...buckets.entries()].map(([key, bucket]) => {
		const sections = buildFieldSections(kind, bucket.rows);
		const items = sections.flatMap((section) => section.items);
		return {
			key,
			label: objectLabel(bucket.proposals[0]),
			isNew:
				bucket.proposals.some((proposal) => proposal.target_new) ||
				bucket.proposals.every((proposal) => proposal.action === 'add'),
			createHint: createHintOf(kind, bucket.proposals),
			change: changeSummary(sections),
			sections,
			items,
			ids: distinct(bucket.rows.map((row) => row.id))
		};
	});
}

/**
 * Fold proposals into the review tree (W9.23 §3.1).
 *
 * The fold is PURE and list-local: the caller decides which partition it folds
 * (live queue or the archive drawer — see :func:`partitionArchived`), so the same
 * function serves both surfaces and the archive can never inherit the queue's
 * selection affordances by accident.
 *
 * An unknown kind cannot happen (schema_gate pins the four) but is folded as its
 * own trailing section rather than dropped: a visible anomaly beats a silent one.
 */
export function buildReviewTree(pending: Proposal[]): KindSection[] {
	const byKind = new Map<string, Proposal[]>();
	for (const proposal of pending) {
		const kind = proposal.kind || 'other';
		const list = byKind.get(kind) ?? [];
		list.push(proposal);
		byKind.set(kind, list);
	}
	const kinds = [
		...KIND_ORDER.filter((kind) => byKind.has(kind)),
		...[...byKind.keys()].filter((kind) => !KIND_ORDER.includes(kind)).sort()
	];
	const sections: KindSection[] = [];
	for (const kind of kinds) {
		const proposals = byKind.get(kind) ?? [];
		if (kind === 'cast') {
			// A candidate has no object behind it: supersede already collapses
			// same-name candidates, and the rows ride flat with no sections.
			const items: ReviewRow[] = proposals.map((proposal) => ({
				id: proposal.id,
				proposal,
				op: null,
				stale: false
			}));
			sections.push({
				kind,
				label: kindSectionLabel(kind),
				hint: kindHint(kind),
				objects: [],
				items,
				ids: distinct(items.map((item) => item.id))
			});
			continue;
		}
		const objects = buildObjects(kind, proposals);
		const items = objects.flatMap((object) => object.items);
		sections.push({
			kind,
			label: kindSectionLabel(kind),
			hint: kindHint(kind),
			objects,
			items,
			ids: distinct(objects.flatMap((object) => object.ids))
		});
	}
	return sections;
}

/**
 * Split a list into the live queue and the derived archive partition (§3.2).
 *
 * Archived-ness is a DERIVED FACT of the fold (a function of the rows' age, not a
 * new log event), so a surface that receives a mixed list still cannot mass-accept
 * an aged row: the archive half is list-local, renders no checkboxes, and a
 * multi-id decide refuses archived ids server-side.
 */
export function partitionArchived(items: Proposal[]): {
	live: Proposal[];
	archived: Proposal[];
} {
	const live: Proposal[] = [];
	const archived: Proposal[] = [];
	for (const item of items) (item.archived ? archived : live).push(item);
	return { live, archived };
}

/** Every proposal id in a tree (the global select-all mask, live rows only). */
export function treeIds(sections: KindSection[]): string[] {
	return distinct(sections.flatMap((section) => section.ids));
}


// --- labels -------------------------------------------------------------------

/** The asset a proposal acts on: its readable target, else the name it proposes. */
export function proposalTarget(proposal: Proposal): string {
	if (proposal.target_name) return proposal.target_name;
	if (proposal.target) return proposal.target;
	const name = proposal.patch.name;
	if (typeof name === 'string' && name.trim()) return name;
	return kindLabel(proposal.kind);
}

/** One line for a row (also the aria-label stem, so the two can never drift). */
export function proposalLabel(proposal: Proposal): string {
	return `${proposal.action} ${proposalTarget(proposal)}`.trim();
}

/** One operation's value as the owner reads it (strings verbatim, JSON pretty). */
export function formatValue(value: unknown): string {
	if (value === null) return 'null';
	if (value === undefined) return '';
	if (typeof value === 'string') return value;
	if (typeof value === 'number' || typeof value === 'boolean') return String(value);
	if (Array.isArray(value) && value.every((entry) => typeof entry === 'string')) {
		return value.join(', ');
	}
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

/**
 * The committed row's line: the API's summary when it has one, else a fold over
 * the decision's own `kind`/`action` (W9.23 §4 added them — the D77 residual).
 */
export function decisionLabel(decision: Decision): string {
	if (decision.applied_summary) return decision.applied_summary;
	const name =
		decision.applied && typeof decision.applied.name === 'string'
			? decision.applied.name
			: decision.pre_image && typeof decision.pre_image.name === 'string'
				? decision.pre_image.name
				: '';
	// A born-parented accept CREATED its book, so the fold must not read "amend".
	if (decision.target_new && name) return `created ${decision.kind || 'book'} '${name}'`;
	if (decision.kind && name) return `${decision.action || 'applied'} ${decision.kind} '${name}'`;
	const scope = [decision.action, decision.kind].filter(Boolean).join(' ');
	if (scope) return `${scope} — proposal ${decision.proposal_id}`.trim();
	return `${decision.decision || 'decision'} on proposal ${decision.proposal_id}`.trim();
}

/** The judgment signal as the owner reads it (`P(propose) 0.72`). */
export function signalLabel(signal: number | null): string {
	return signal === null ? '' : `P(propose) ${signal.toFixed(2)}`;
}


// --- mock mode (VITE_MOCK=1) ---------------------------------------------------
//
// The demo has no engine, so the scripted fixture set (owned by this lane, plan
// §5 "Mock seeding") IS the queue. It is parsed through the SAME normalizers the
// wire read uses — a fixture that skipped them would not exercise the defensive
// parsing the surfaces depend on. Decisions mutate the in-memory copy so the
// accept/undo flow is demonstrable offline.

type Fixtures = typeof import('./proposals.fixtures');

let fixturesModule: Promise<Fixtures> | null = null;
let mockState: Record<string, SessionQueue> | null = null;

/** Lazily load the fixtures so they are code-split out of the production bundle. */
function loadFixtures(): Promise<Fixtures> {
	fixturesModule ??= import('./proposals.fixtures');
	return fixturesModule;
}

async function mockStore(): Promise<Record<string, SessionQueue>> {
	if (mockState) return mockState;
	const { MOCK_REVIEWS } = await loadFixtures();
	mockState = Object.fromEntries(
		Object.entries(MOCK_REVIEWS).map(([sessionId, review]) => [
			sessionId,
			parseSessionQueue({
				pending: review.pending,
				archived: review.archived,
				committed: review.committed
			})
		])
	);
	return mockState;
}

async function mockSessionQueue(sessionId: string): Promise<SessionQueue> {
	const { MOCK_REVIEWS } = await loadFixtures();
	const store = await mockStore();
	// An ad-hoc mock session id (the demo's `createSession`) has no scripted
	// queue: it falls back to the first fixture so the drawer shows the tree
	// rather than an empty pane the reviewer would read as a bug.
	const key = store[sessionId] ? sessionId : Object.keys(MOCK_REVIEWS)[0] ?? '';
	const queue = store[key] ?? { pending: [], archived: [], committed: [] };
	return {
		pending: queue.pending.map((item) => ({ ...item })),
		archived: queue.archived.map((item) => ({ ...item })),
		committed: queue.committed.map((item) => ({ ...item }))
	};
}

async function mockInbox(): Promise<InboxSession[]> {
	const { MOCK_REVIEWS } = await loadFixtures();
	const store = await mockStore();
	return Object.entries(MOCK_REVIEWS).map(([sessionId, review]) => {
		const queue = store[sessionId] ?? { pending: [], archived: [], committed: [] };
		return {
			sessionId,
			title: review.title,
			pending: queue.pending.map((item) => ({ ...item })),
			archived: queue.archived.map((item) => ({ ...item }))
		};
	});
}

async function mockDecide(ids: string[], decision: ProposalDecision): Promise<DecideOutcome> {
	const store = await mockStore();
	const wanted = new Set(ids);
	const decisions: Decision[] = [];
	// The archive partition mirrors the server's decide guard (§3.3): a multi-id
	// body refuses archived ids, a SINGLE-id action on an archived row is legal.
	const mass = ids.length > 1;
	const rejected: RejectedDecision[] = [];
	for (const queue of Object.values(store)) {
		const archived = queue.archived.filter((item) => wanted.has(item.id));
		if (mass) {
			for (const item of archived) {
				rejected.push({
					id: item.id,
					error: 'archived — decide it on its own, not in a batch'
				});
			}
		}
		const gone = [
			...queue.pending.filter((item) => wanted.has(item.id)),
			...(mass ? [] : archived)
		];
		if (gone.length === 0) continue;
		const decided = new Set(gone.map((item) => item.id));
		queue.pending = queue.pending.filter((item) => !decided.has(item.id));
		// A refused archived id is NOT decided, so it stays in the drawer.
		queue.archived = queue.archived.filter((item) => !decided.has(item.id));
		for (const item of gone) {
			const record: Decision = {
				decision_id: `mock-decision-${item.id}`,
				proposal_id: item.id,
				decision,
				kind: item.kind,
				action: item.action,
				target_new: item.target_new,
				pre_image: item.action === 'add' ? null : { ...item.patch },
				applied: decision === 'accept' ? { ...item.patch, name: item.target_name } : null,
				applied_summary: `${decision} ${item.action} ${item.kind} '${item.target_name || item.target || ''}'`.trim()
			};
			decisions.push(record);
			if (decision === 'accept') queue.committed = [record, ...queue.committed];
		}
	}
	return { decisions, rejected, error: '' };
}

async function mockUndo(decisionId: string): Promise<{ decision: Decision } | { error: string }> {
	const store = await mockStore();
	for (const queue of Object.values(store)) {
		const row = queue.committed.find((item) => item.decision_id === decisionId);
		if (!row) continue;
		queue.committed = queue.committed.filter((item) => item.decision_id !== decisionId);
		return { decision: { ...row } };
	}
	return { error: 'unknown decision' };
}
