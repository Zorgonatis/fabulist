// Fabulist API client - behind-the-scenes world state + speaker attribution.

import { API_BASE, USE_MOCK, readJson } from './shared';

/** One `agent_call` system-event row (`GET .../agent-calls`). */
export interface AgentCallRow {
	seq: number;
	ts: string;
	agent: string;
	messages: Array<Record<string, unknown>>;
	response_text: string;
	/** W9.3: passthroughs for the flow node inspector (absent on older captures). */
	reasoning_text?: string;
	finish?: string | null;
	latency_s?: number;
	meta?: Record<string, unknown>;
}


/** One attributed spoken line (`{speaker, quote}`, quote WITHOUT the marks). */
export interface AttributedQuote {
	speaker: string;
	/** Canonical member id (W7), when the engine resolved the speaker through
	 *  the roster. Absent on the free-form fallback path, where only the display
	 *  `speaker` name is known. */
	speaker_id?: string;
	quote: string;
}


/** One attribution agent call: its log seq + the parsed quotes for that turn. */
export interface AttributionRow {
	seq: number;
	attributions: AttributedQuote[];
}


/** One relationship metric (trust / suspicion / friend / ...). */
export interface BtsMetric {
	name: string;
	value: number;
	reason?: string;
}


/** A character's venue (``@venue > area``). */
export interface BtsVenue {
	venue: string;
	area?: string;
}


/** One plot thread row. */
export interface BtsThread {
	status: string;
	name: string;
	detail: string;
}


/** One off-screen character row. */
export interface BtsOffScreen {
	name: string;
	location?: string;
	activity?: string;
	mood?: string;
	note?: string;
}


/** Nested per-character state: ``{category: {key: value}}``. */
export type BtsState = Record<string, Record<string, Record<string, string>>>;


/**
 * A parsed (or checkpoint+delta folded) BTS world state — the same shape the
 * engine's `system`/`world_state` event carries, so the dashboard renders it
 * directly.
 */
export interface BtsWorldState {
	kind: string;
	state: BtsState;
	venue: Record<string, BtsVenue>;
	off_screen: BtsOffScreen[];
	threads: BtsThread[];
	tension: { level?: number; irony?: string };
	deltas: Record<string, string>;
	raw: string;
}


function normalizeAgentCall(raw: unknown): AgentCallRow | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	if (typeof obj.seq !== 'number') return null;
	const messages = Array.isArray(obj.messages)
		? obj.messages.filter(
				(m): m is Record<string, unknown> => !!m && typeof m === 'object'
			)
		: [];
	return {
		seq: obj.seq,
		ts: typeof obj.ts === 'string' ? obj.ts : '',
		agent: typeof obj.agent === 'string' ? obj.agent : '',
		messages,
		response_text: typeof obj.response_text === 'string' ? obj.response_text : '',
		reasoning_text: typeof obj.reasoning_text === 'string' ? obj.reasoning_text : undefined,
		finish: typeof obj.finish === 'string' ? obj.finish : null,
		latency_s: typeof obj.latency_s === 'number' ? obj.latency_s : undefined,
		meta: rawMeta(obj)
	};
}

function rawMeta(obj: Record<string, unknown>): Record<string, unknown> | undefined {
	const meta = obj.meta;
	if (!meta || typeof meta !== 'object' || Array.isArray(meta)) return undefined;
	return meta as Record<string, unknown>;
}


/**
 * Read a session's captured agent calls (`GET .../agent-calls`), seq-ordered.
 * Returns `[]` on any failure (captures are an optional audit surface).
 */
export async function getAgentCalls(sessionId: string, agent?: string): Promise<AgentCallRow[]> {
	if (USE_MOCK) return [];
	try {
		const qs = agent ? `?agent=${encodeURIComponent(agent)}` : '';
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/agent-calls${qs}`,
			{ headers: { Accept: 'application/json' } }
		);
		if (!res.ok) return [];
		const body = await readJson(res);
		const raw = Array.isArray(body.calls) ? body.calls : [];
		return raw
			.map(normalizeAgentCall)
			.filter((row): row is AgentCallRow => row !== null)
			.sort((a, b) => a.seq - b.seq);
	} catch {
		return [];
	}
}


const BTS_BLOCK_RE = /\[BTS(\s*\|\s*CP)?\s*\]([\s\S]*?)\[\/BTS\]/i;

const BTS_HEADER_RE = /^\s*::\s*(.+?)\s*::\s*(\[RE-ENTRY\])?\s*$/;

const BTS_VENUE_RE = /@\s*([A-Za-z0-9_'’\- ]+?)(?:\s*>\s*([A-Za-z0-9_'’\- ]+?))?\s*(?:[|(]|$)/;

const BTS_ENV_KEYS = new Set(['loc', 'location', 'weather', 'tone']);

const BTS_THREAD_STATUS: Record<string, string> = {
	'+': 'new',
	'~': 'active',
	'=': 'paused',
	'-': 'resolved'
};

const BTS_CATEGORY_KEYS: Array<[string, readonly string[]]> = [
	[
		'physical',
		[
			'hp', 'health', 'wound', 'wounds', 'injury', 'injuries', 'status',
			'fatigue', 'energy', 'hygiene', 'hunger', 'satiety', 'thirst', 'pose',
			'position', 'comfort', 'condition_physical'
		]
	],
	[
		'emotional',
		['mood', 'stress', 'focus', 'clarity', 'fear', 'anxiety', 'arousal', 'moodlets', 'moodlet', 'interest']
	],
	[
		'appearance',
		['outfit', 'outfit_cat', 'appearance', 'clothing', 'hair', 'makeup', 'scent', 'condition', 'wearing', 'accessories']
	],
	[
		'inventory',
		['carrying', 'inventory', 'inv', 'currency', 'gold', 'items', 'equipment', 'weapon', 'consumables']
	],
	[
		'stats',
		['str', 'dex', 'con', 'int', 'wis', 'cha', 'skill', 'skills', 'level', 'xp', 'mana', 'stamina', 'ability', 'abilities', 'status_effects']
	],
	[
		'narrative',
		['thread', 'threads', 'quest', 'flag', 'flags', 'tension', 'irony', 'goal', 'scene', 'arc', 'foreshadow']
	]
];


function btsCategoryFor(key: string): string {
	const lowered = key.trim().toLowerCase();
	if (lowered.startsWith('rel') || key.includes('→') || key.includes('->')) return 'relationships';
	for (const [category, keys] of BTS_CATEGORY_KEYS) {
		if (keys.includes(lowered)) return category;
	}
	return 'other';
}


function splitBtsAttrs(line: string): string[] {
	const parts: string[] = [];
	for (const rawPart of line.split('|')) {
		const chunk = rawPart.trim();
		if (!chunk) continue;
		const isInv = /^[+~=-]\s*\S/.test(chunk);
		if (chunk.includes(':') || parts.length === 0 || isInv) parts.push(chunk);
		else parts[parts.length - 1] = `${parts[parts.length - 1]} | ${chunk}`;
	}
	return parts;
}


function btsKeyValue(chunk: string): [string, string] | null {
	const idx = chunk.indexOf(':');
	if (idx === -1) return null;
	const key = chunk.slice(0, idx).trim();
	const value = chunk.slice(idx + 1).trim();
	return key ? [key, value] : null;
}


/** Parse ``+a | ~b(40%) | =c | -d`` into structured thread rows. */
export function parseBtsThreads(text: string): BtsThread[] {
	const rows: BtsThread[] = [];
	for (const chunk of text.split('|')) {
		const token = chunk.trim();
		if (!token) continue;
		const match = /^\s*([+~=-])\s*([A-Za-z0-9_'’\- ]+?)\s*(?:\((.*?)\))?\s*$/.exec(token);
		if (!match) continue;
		rows.push({
			status: BTS_THREAD_STATUS[match[1]] ?? 'active',
			name: match[2].trim(),
			detail: (match[3] ?? '').trim()
		});
	}
	return rows;
}


function parseBtsOffScreen(line: string): BtsOffScreen | null {
	const parts = line.split('|').map((part) => part.trim());
	if (!parts[0]) return null;
	const entry: BtsOffScreen = { name: parts[0] };
	for (const part of parts.slice(1)) {
		const venue = BTS_VENUE_RE.exec(part);
		if (venue && !entry.location) {
			entry.location = venue[1].trim();
			const paren = /\(([^)]*)\)/.exec(part);
			if (paren) entry.activity = paren[1].trim();
		} else if (/^mood:/i.test(part)) {
			entry.mood = part.slice(part.indexOf(':') + 1).trim();
		} else if (part && !entry.note) {
			entry.note = part;
		}
	}
	return entry;
}


function collectBtsVenues(body: string): Record<string, BtsVenue> {
	const venue: Record<string, BtsVenue> = {};
	let current: string | null = null;
	for (const rawLine of body.split('\n')) {
		const line = rawLine.trim();
		const header = BTS_HEADER_RE.exec(line);
		if (header) {
			const name = header[1].trim();
			current = name.toUpperCase() === 'OFF' ? null : name;
			continue;
		}
		if (current === null) continue;
		const found = BTS_VENUE_RE.exec(line);
		if (!found) continue;
		const place = found[1].trim();
		if (!place) continue;
		const row: BtsVenue = { venue: place };
		if (found[2]) row.area = found[2].trim();
		venue[current] = row;
	}
	return venue;
}


function applyBtsTension(line: string, tension: { level?: number; irony?: string }): void {
	const match = /tension\s*:\s*([0-9]+(?:\s*→\s*[0-9]+)?)\s*(?:\/\s*10)?/i.exec(line);
	if (match) {
		const tail = match[1].split('→').pop() ?? match[1];
		const level = Number.parseInt(tail.trim(), 10);
		if (!Number.isNaN(level)) tension.level = level;
	}
	const irony = /irony\s*:\s*(.+)/i.exec(line);
	if (irony) tension.irony = irony[1].trim();
}


/**
 * Parse one BTS notation block into a structured world state (mirrors
 * `fabulist.state.bts.parse_bts`). Returns `null` when no block is present.
 *
 * One deliberate divergence from the Python parser: environment lines
 * (`weather:`/`tone:`/`loc:`) written after `:: OFF ::` are treated as
 * turn-level (skipped) rather than mis-read as off-screen characters, and a
 * bare `+name | detail` in the OFF section is a plot thread. This keeps the
 * dashboard's off-screen list truthful against real model drift.
 */
export function parseBtsNotation(text: string): BtsWorldState | null {
	const block = BTS_BLOCK_RE.exec(text);
	if (!block) return null;
	const kind = block[1] ? 'checkpoint' : 'delta';
	const body = block[2];
	const state: BtsState = {};
	const off_screen: BtsOffScreen[] = [];
	const threads: BtsThread[] = [];
	const tension: { level?: number; irony?: string } = {};
	let current: string | null = null;
	let inOff = false;

	for (const rawLine of body.split('\n')) {
		const line = rawLine.trim();
		if (!line) continue;
		const header = BTS_HEADER_RE.exec(line);
		if (header) {
			const name = header[1].trim();
			if (name.toUpperCase() === 'OFF') {
				current = null;
				inOff = true;
			} else {
				current = name;
				inOff = false;
				if (!state[name]) state[name] = {};
			}
			continue;
		}
		const lowered = line.toLowerCase();
		if (lowered.startsWith('threads:')) {
			threads.push(...parseBtsThreads(line.slice(line.indexOf(':') + 1)));
			continue;
		}
		if (lowered.startsWith('tension:')) {
			applyBtsTension(line, tension);
			continue;
		}
		if (/^(weather|tone|loc|location|time)\s*:/i.test(line)) continue;
		if (inOff) {
			const bare = /^\s*([+~=-])\s*([A-Za-z0-9_'’\-]+)\s*\|\s*(.+)$/.exec(line);
			if (bare) {
				threads.push({
					status: BTS_THREAD_STATUS[bare[1]] ?? 'active',
					name: bare[2],
					detail: bare[3].trim()
				});
				continue;
			}
			const entry = parseBtsOffScreen(line);
			if (entry) off_screen.push(entry);
			continue;
		}
		if (current === null) continue;
		for (const chunk of splitBtsAttrs(line)) {
			const kv = btsKeyValue(chunk);
			if (kv === null) {
				const bare = /^\s*([+~=-])\s*([A-Za-z0-9_'’\-]+)\s*\|\s*(.+)$/.exec(chunk);
				if (bare) {
					threads.push({
						status: BTS_THREAD_STATUS[bare[1]] ?? 'active',
						name: bare[2],
						detail: bare[3].trim()
					});
				}
				continue;
			}
			const key = kv[0];
			if (BTS_ENV_KEYS.has(key.toLowerCase())) continue;
			const category = btsCategoryFor(key);
			if (!state[current][category]) state[current][category] = {};
			state[current][category][key] = kv[1];
		}
	}

	return {
		kind,
		state,
		venue: collectBtsVenues(body),
		off_screen,
		threads,
		tension,
		deltas: {},
		raw: block[0]
	};
}


const BTS_METRIC_RE =
	/^\s*([A-Za-z][\w' \-]*?)\s+(-?\d+)\s*(?:→\s*(-?\d+))?\s*(?:\(([^)]*)\))?\s*$/;


/** Parse a relationship value (``trust -15 | suspicion 70 (deceit)``) to metrics. */
export function parseRelationshipMetrics(raw: string): BtsMetric[] {
	const out: BtsMetric[] = [];
	for (const part of raw.split('|')) {
		const match = BTS_METRIC_RE.exec(part.trim());
		if (!match) continue;
		const after = match[3];
		out.push({
			name: match[1].trim(),
			value: Number.parseInt(after ?? match[2], 10),
			reason: match[4] ? match[4].trim() : undefined
		});
	}
	return out;
}


function mergeRelValue(prior: string | undefined, next: string): string {
	if (!prior) return next;
	const metrics = parseRelationshipMetrics(prior);
	for (const part of next.split('|')) {
		const match = BTS_METRIC_RE.exec(part.trim());
		if (!match) continue;
		const name = match[1].trim();
		const value = Number.parseInt(match[3] ?? match[2], 10);
		const reason = match[4] ? match[4].trim() : undefined;
		const existing = metrics.find((m) => m.name.toLowerCase() === name.toLowerCase());
		if (existing) {
			existing.value = value;
			existing.reason = reason;
		} else {
			metrics.push({ name, value, reason });
		}
	}
	return metrics
		.map((m) => `${m.name} ${m.value}${m.reason ? ` (${m.reason})` : ''}`)
		.join(' | ');
}


function isRelKey(key: string): boolean {
	return /^rel/i.test(key) || key.includes('→') || key.includes('->');
}


function cloneBtsState(state: BtsState): BtsState {
	return JSON.parse(JSON.stringify(state)) as BtsState;
}


function mergeBtsOffScreen(prior: BtsOffScreen[], next: BtsOffScreen[]): BtsOffScreen[] {
	const out = prior.map((entry) => ({ ...entry }));
	for (const entry of next) {
		if (!entry.name) continue;
		if (/^(weather|tone|loc|location|time)\s*:/i.test(entry.name)) continue;
		if (/^[+~=-]/.test(entry.name)) continue;
		const idx = out.findIndex((row) => row.name === entry.name);
		if (idx >= 0) out[idx] = { ...entry };
		else out.push({ ...entry });
	}
	return out;
}


function mergeBtsThreads(prior: BtsThread[], next: BtsThread[]): BtsThread[] {
	const out = prior.map((thread) => ({ ...thread }));
	for (const thread of next) {
		const idx = out.findIndex((row) => row.name === thread.name);
		if (idx >= 0) out[idx] = { ...thread };
		else out.push({ ...thread });
	}
	return out;
}


/**
 * Fold a checkpoint/delta stream into the current world state. A checkpoint
 * replaces the accumulated state wholesale; a delta deep-merges per character /
 * category / key (relationship values merge their metrics so trust survives a
 * suspicion-only update).
 */
export function foldBtsWorldState(
	acc: BtsWorldState | null,
	next: BtsWorldState
): BtsWorldState {
	if (!acc || next.kind === 'checkpoint') {
		return {
			kind: next.kind,
			state: cloneBtsState(next.state),
			venue: { ...next.venue },
			off_screen: mergeBtsOffScreen([], next.off_screen),
			threads: mergeBtsThreads([], next.threads),
			tension: { ...next.tension },
			deltas: { ...next.deltas },
			raw: next.raw
		};
	}
	const state = cloneBtsState(acc.state);
	for (const [character, categories] of Object.entries(next.state)) {
		if (!state[character]) state[character] = {};
		for (const [category, attrs] of Object.entries(categories)) {
			if (!state[character][category]) state[character][category] = {};
			for (const [key, value] of Object.entries(attrs)) {
				state[character][category][key] = isRelKey(key)
					? mergeRelValue(state[character][category][key], value)
					: value;
			}
		}
	}
	return {
		kind: acc.kind,
		state,
		venue: { ...acc.venue, ...next.venue },
		off_screen: mergeBtsOffScreen(acc.off_screen, next.off_screen),
		threads: mergeBtsThreads(acc.threads, next.threads),
		tension: { ...acc.tension, ...next.tension },
		deltas: { ...acc.deltas, ...next.deltas },
		raw: next.raw
	};
}


/** One raw `system`/`world_state` event row from GET .../world-state. */
export interface WorldStateEventRow {
	seq: number;
	ts?: string;
	kind?: string;
	state?: BtsState;
	venue?: Record<string, BtsVenue>;
	off_screen?: BtsOffScreen[];
	threads?: BtsThread[];
	tension?: { level?: number; irony?: string };
	raw?: string;
}


/** Project a raw world_state event row into the dashboard's `BtsWorldState`. */
function worldStateFromEvent(row: WorldStateEventRow): BtsWorldState {
	return {
		kind: typeof row.kind === 'string' ? row.kind : 'delta',
		state: row.state && typeof row.state === 'object' ? row.state : {},
		venue: row.venue && typeof row.venue === 'object' ? row.venue : {},
		off_screen: Array.isArray(row.off_screen) ? row.off_screen : [],
		threads: Array.isArray(row.threads) ? row.threads : [],
		tension: row.tension && typeof row.tension === 'object' ? row.tension : {},
		deltas: {},
		raw: typeof row.raw === 'string' ? row.raw : ''
	};
}


function meaningfulWorldState(acc: BtsWorldState | null): BtsWorldState | null {
	if (!acc) return null;
	const meaningful =
		Object.keys(acc.state).length > 0 ||
		acc.threads.length > 0 ||
		acc.off_screen.length > 0 ||
		Object.keys(acc.venue).length > 0 ||
		acc.tension.level !== undefined;
	return meaningful ? acc : null;
}


/**
 * Legacy fallback: reconstruct world state from the raw `bts` agent captures.
 * Only used when the world-state endpoint is absent (older engine) or errors.
 */
async function latestWorldStateFromCaptures(sessionId: string): Promise<BtsWorldState | null> {
	const rows = await getAgentCalls(sessionId);
	const btsRows = rows.filter((row) => /\[BTS/i.test(row.response_text));
	let acc: BtsWorldState | null = null;
	for (const row of btsRows) {
		const parsed = parseBtsNotation(row.response_text);
		if (parsed) acc = foldBtsWorldState(acc, parsed);
	}
	return meaningfulWorldState(acc);
}


/**
 * Read a session's latest world state from the engine's persisted
 * `system`/`world_state` events (GET .../world-state?history=1). Events are
 * folded seq-ordered so a delta turn merges onto the last checkpoint. Falls
 * back to reconstructing from `bts` agent captures ONLY when the endpoint is
 * missing (older engine) or errors — a 200 with no state is authoritative.
 */
export async function latestWorldState(sessionId: string): Promise<BtsWorldState | null> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/world-state?history=1`,
			{ headers: { Accept: 'application/json' } }
		);
		if (res.ok) {
			const body = await readJson(res);
			const history = Array.isArray(body.history)
				? (body.history as WorldStateEventRow[])
				: body.latest
					? [body.latest as WorldStateEventRow]
					: [];
			let acc: BtsWorldState | null = null;
			for (const row of history) acc = foldBtsWorldState(acc, worldStateFromEvent(row));
			return meaningfulWorldState(acc);
		}
	} catch {
		// endpoint absent/errored -> capture fallback
	}
	return latestWorldStateFromCaptures(sessionId);
}


/**
 * Append one nested BTS override (`POST .../state/nested`, D45).
 *
 * The BTS tracker lanes fold from the `system`/`world_state` lane, which the
 * flat `state-delta` write never touches, so a correction rides this door
 * instead. `path` addresses the folded world-state object directly:
 * `["state", <name>, <category>, <key>]` for a cast/relationship/emotional/
 * inventory leaf, `["venue", <name>, "venue"]`, `["threads", "<index>",
 * "status"]` for a list lane, `["tension", "level"]`. The engine folds, applies
 * `{path, value}`, and appends the result as a non-checkpoint
 * `world_state`/`bts_override` row the dashboard folds last. Returns `true` on
 * success; a rejection (unknown session, malformed/out-of-range path) returns
 * `false` and writes nothing, so the caller simply does not refetch.
 */
export async function writeNestedState(
	sessionId: string,
	path: string[],
	value: string
): Promise<boolean> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/state/nested`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ path, value })
			}
		);
		return res.ok;
	} catch {
		return false;
	}
}


// --- W9.29 §8: the identity-stable entity registry -------------------------
//
// The BTS tracker no longer reports a whole payload per turn: it changes
// identity-stable objects (locations / meters / threads / relationships /
// items) through validated write actions, and `GET .../state` serves the folded
// registry beside the flat state. These two calls are the sidebar's read and the
// ✎ write door (the engine runs the SAME action validation the tracker's tools
// run, so a rejection is the readable error the model would have seen).

/** One registry location: its canonical path, note and current occupants. */
export interface RegistryLocation {
	id: string;
	name: string;
	/** Root-first `parent > child` path (the HUD's `loc`). */
	path: string;
	parent_id: string | null;
	note: string;
	/** Set only on `loc_off_screen`: where the off-screen crowd is. */
	off_screen_ref: string | null;
	/** Being keys (`cm_…` / `player`) assigned here. */
	occupants: string[];
}

/** One meter or trait row (a trait is the same object with a text value). */
export interface RegistryAttribute {
	id: string;
	/** Whose state it is: a member id, `player`, or `scene`. */
	host: string;
	/** Toward whom (`null` for a self/scene row; required for relational rows). */
	subject: string | null;
	key: string;
	value: number | string;
	/** The value before the most recent change (absent until a second set). */
	prior?: number | string;
	/** Seq of the event that changed it. */
	changed_seq?: number;
}

/** One prose edge between two beings (numbers live on meters, never here). */
export interface RegistryRelationship {
	id: string;
	from: string;
	to: string;
	name: string;
	note: string;
}

/** One plot thread with its status vocabulary. */
export interface RegistryThread {
	id: string;
	name: string;
	status: string;
	detail: string;
	note: string;
}

/** One inventory item and who holds it. */
export interface RegistryItem {
	id: string;
	name: string;
	holder: string;
	note: string;
}

/** The legacy `world_state` fold, read-side shim for pre-regime sessions. */
export interface RegistryLegacy {
	state: BtsState;
	venue: Record<string, BtsVenue>;
	off_screen: BtsOffScreen[];
	threads: BtsThread[];
	tension: { level?: number; irony?: string };
	flat: Record<string, string>;
}

/** The folded registry as `GET .../state` serves it. */
export interface EntityRegistry {
	locations: RegistryLocation[];
	/**
	 * `being key -> display name` for the raw occupant keys (W9.31 §8.1).
	 *
	 * A pure fold of the cast roster plus the session persona, so it may be
	 * SPARSE (a session whose roster was never materialized) — read it as
	 * `beings[key] ?? key`. Absent on an engine older than W9.31.
	 */
	beings?: Record<string, string>;
	assignments: Record<string, string>;
	off_screen_ref: string | null;
	meters: RegistryAttribute[];
	traits: RegistryAttribute[];
	relationships: RegistryRelationship[];
	threads: RegistryThread[];
	items: RegistryItem[];
	legacy: RegistryLegacy;
}

/** One node of the rendered location tree. */
export interface LocationNode {
	location: RegistryLocation;
	/** 0 for a root, +1 per parent hop (the pane indents by this). */
	depth: number;
	/** Rooms whose `parent_id` is this location, in wire order. */
	children: LocationNode[];
}

/**
 * Group the flat location list into the tree its `parent_id` links describe.
 *
 * Roots are the locations with no parent OR whose parent is not in the list (an
 * orphan is promoted to a root, so NOTHING is dropped). Wire order is preserved
 * within a level, so the tree reads the way the engine's fold ordered it. A
 * parent cycle — which the dumb store allows — leaves nodes unreachable from any
 * root; those are promoted too (the first in wire order becomes the root of its
 * ring), because a drop would hide a live location from the pane.
 */
export function buildLocationTree(locations: RegistryLocation[]): LocationNode[] {
	const nodes = new Map<string, LocationNode>();
	for (const location of locations) {
		nodes.set(location.id, { location, depth: 0, children: [] });
	}
	const roots: LocationNode[] = [];
	for (const node of nodes.values()) {
		const parentId = node.location.parent_id;
		const parent = parentId ? nodes.get(parentId) : undefined;
		if (parent && parent !== node) {
			parent.children.push(node);
		} else {
			roots.push(node);
		}
	}
	// Depth is assigned by walking down from the roots; `seen` also stops a
	// parent cycle from recursing forever.
	const seen = new Set<string>();
	const assignDepth = (node: LocationNode, depth: number): void => {
		if (seen.has(node.location.id)) return;
		seen.add(node.location.id);
		node.depth = depth;
		for (const child of node.children) assignDepth(child, depth + 1);
	};
	for (const root of roots) assignDepth(root, 0);
	for (const node of nodes.values()) {
		if (seen.has(node.location.id)) continue;
		// Detach the promoted node from its cycle parent so it renders once, at
		// depth 0, with the rest of the ring under it.
		const parentId = node.location.parent_id;
		const parent = parentId ? nodes.get(parentId) : undefined;
		const index = parent ? parent.children.indexOf(node) : -1;
		if (parent && index >= 0) parent.children.splice(index, 1);
		roots.push(node);
		assignDepth(node, 0);
	}
	return roots;
}

/**
 * Read a session's folded entity registry (`GET .../state` -> `registry`).
 *
 * Returns `null` on any failure (an older engine, no session): the sidebar then
 * keeps its legacy world-state path, which is the read it always had.
 */
export async function getEntityRegistry(sessionId: string): Promise<EntityRegistry | null> {
	try {
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/state`, {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return null;
		const body = await readJson(res);
		const registry = body.registry;
		if (!registry || typeof registry !== 'object') return null;
		return registry as EntityRegistry;
	} catch {
		return null;
	}
}

// --- W9.32 §2: the session tree (the state pane's one read) -----------------
//
// `GET .../tree` is the engine's ONE render-ready projection of a session
// (`fabulist.state.session_tree.session_tree`, a PURE read): the flat registry
// flattened for display, one branch per thing the pane renders, in this order.
// The branch fields are the ones a human reads — occupants, holders, meter
// hosts and condition hosts are DISPLAY names, so the pane never resolves a
// `cm_…` key to a person. Beside those names every ADDRESSABLE node carries its
// registry id (`loc_…`/`itm_…`/`th_…`/`rel_…`/`cd_…`/`eco_…`), which is what the
// `update_object` write door takes — the pane shows that id only while the
// node's own ✎ is open. The scene's values carry no id: they are catalog keys
// (`set_attr` on the reserved `scene` host), and the scene's rows are served in
// the `scene` branch ONLY (`meters` holds every non-scene attribute).

/** The tree's clock: the folded instant, who moved it, the open proposal. */
export interface TreeClock {
	time: string;
	date: string;
	day: string | null;
	/** Actor of the newest `state-delta` row that CHANGED `time`. */
	last_writer: string | null;
	/** The newest accepted-model `clock_proposal` audit row (W9.32 §3.2). */
	proposal: {
		time: string;
		date: string;
		day: string | number | null;
		evidence: string;
		advanced_minutes: number | null;
	} | null;
	/** True only when the folded instant equals the proposal's. */
	applied: boolean;
}

/** One condition (`cd_…`) on a being or on the scene, by display name. */
export interface TreeCondition {
	/** The `cd_…` id — what `set_condition`/`update_object` address. */
	id: string;
	host: string | null;
	key: string;
	severity: string;
	source: string;
	/** `YYYY-MM-DD HH:MM` instant, or `null` for a permanent condition. */
	expires_at: string | null;
	/** The fold's own expiry verdict against the current clock. */
	expired: boolean;
}

/** What the room is doing: the scene's traits, tension, conditions. */
export interface TreeScene {
	weather: string;
	tone: string;
	/** The D45 manual pin when set, else the model's scene trait. */
	forecast: { value: string; pinned: boolean };
	irony: string;
	genre: string;
	tension: number | null;
	conditions: TreeCondition[];
}

/** One location: its own name, its parent's NAME, path, note and occupants. */
export interface TreePlace {
	/** The `loc_…` id — what `update_object`/`retire_object` address. */
	id: string;
	name: string;
	/** The parent's name (`null` at a root) — names only, never an id. */
	parent: string | null;
	/** Root-first `parent > child` path (the HUD's `loc`). */
	path: string;
	note: string;
	/** Occupants by display name. */
	occupants: string[];
}

/** The location tree plus the off-screen bucket (not a room — an absence). */
export interface TreePlaces {
	tree: TreePlace[];
	/**
	 * The absence bucket. It carries NO id on purpose: the reserved off-screen
	 * literal is always live and never retired, so there is nothing to address.
	 */
	off_screen: { name: string; at: string | null; occupants: string[] };
}

/** One item and who holds it (display name; `null` when unheld). */
export interface TreeItem {
	/** The `itm_…` id — what `update_object` addresses. */
	id: string;
	name: string;
	holder: string | null;
	note: string;
}

/** One plot thread with its status vocabulary and detail line. */
export interface TreeThread {
	/** The `th_…` id — what `update_object` addresses. */
	id: string;
	name: string;
	status: string;
	detail: string;
	note: string;
}

/** One prose edge between two beings (numbers live on meters, never here). */
export interface TreeRelation {
	/** The `rel_…` id — what `update_object` addresses. */
	id: string;
	from: string | null;
	to: string | null;
	/** Prose the writer gave, or the default `from→to` rebuilt from names. */
	label: string;
	note: string;
}

/** One meter or trait row: a numeric reading or a text trait. */
export interface TreeMeterRow {
	key: string;
	value: number | string;
	/** Toward whom, for a relational row (display name). */
	subject?: string;
}

/**
 * One host's rows, split into NUMBERS (meters) and TEXT (traits).
 *
 * Only NON-scene hosts appear here: the scene's own rows are the `scene`
 * branch's (weather/tone/forecast/irony/genre/tension), so the same reading is
 * never served in two places.
 */
export interface TreeMeterGroup {
	/** The host by display name. */
	host: string;
	numbers: TreeMeterRow[];
	traits: TreeMeterRow[];
}

/** The session's one purse (`null` until an economy is established). */
export interface TreeEconomy {
	/** The `eco_…` id — what `update_object`/`retire_object` address. */
	id: string;
	denomination: string;
	balance: number;
	note: string;
}

/** One being's body: how they stand, what they wear, what shows on them. */
export interface TreeBody {
	pose: string;
	/** Clothing layers, outermost first. */
	clothing: string[];
	surface: string;
	note: string;
}

/** A being's live layer — shared verbatim by the cast nodes and the Player. */
export interface TreeLive {
	/** The location path; `null` when the registry has no assignment. */
	location: string | null;
	off_screen: boolean;
	/** Registry-first presence: in-scene is object equality with the player. */
	presence: 'in_scene' | 'elsewhere' | 'off_screen' | 'unknown';
	meters: { numbers: TreeMeterRow[]; traits: TreeMeterRow[] };
	conditions: TreeCondition[];
	body: TreeBody | null;
	/** Held item names. */
	items: string[];
}

/** A cast member's roster facts — nothing derived from their live state. */
export interface TreeParticipation {
	/** The ONE identity field the tree carries: the real, joinable `cm_…` id. */
	member_id: string;
	display: string;
	aliases: string[];
	provenance: string;
	source_id: string | null;
	status: string;
	tier: string;
	promoted_persona_id: string | null;
}

/** The card TEXT the session was seeded with (`null`: no card). */
export interface TreeDefinition {
	name: string;
	description: string;
	personality: string;
	scenario: string;
}

/** One cast node: the three separable layers. */
export interface TreeCastNode {
	participation: TreeParticipation;
	definition: TreeDefinition | null;
	live: TreeLive;
}

/** The player: the reserved identity key plus the same live layer as a member. */
export interface TreePlayer {
	identity: { key: string; display: string };
	live: TreeLive;
	persona_ref?: string;
}

/** The whole session as the tree serves it (top-level keys ARE render order). */
export interface SessionTree {
	clock: TreeClock;
	scene: TreeScene;
	places: TreePlaces;
	items: TreeItem[];
	threads: TreeThread[];
	relations: TreeRelation[];
	meters: TreeMeterGroup[];
	conditions: TreeCondition[];
	economy: TreeEconomy | null;
	cast: TreeCastNode[];
	Player: TreePlayer;
}

/** An empty live layer: what a being the registry has not seen looks like. */
const EMPTY_LIVE: TreeLive = {
	location: null,
	off_screen: false,
	presence: 'unknown',
	meters: { numbers: [], traits: [] },
	conditions: [],
	body: null,
	items: []
};

/**
 * Fill in a missing branch so the pane can render a partial tree.
 *
 * The engine's projection always serves every branch, so this only guards a
 * 200 whose body is thinner than promised (an older engine): a defaulted array
 * renders as an empty section, where an absent one would crash the `{#each}`.
 */
function normalizeTree(raw: Record<string, unknown>): SessionTree {
	const tree = raw as Partial<SessionTree>;
	return {
		clock: tree.clock ?? {
			time: '',
			date: '',
			day: null,
			last_writer: null,
			proposal: null,
			applied: false
		},
		scene: tree.scene ?? {
			weather: '',
			tone: '',
			forecast: { value: '', pinned: false },
			irony: '',
			genre: '',
			tension: null,
			conditions: []
		},
		places: tree.places ?? { tree: [], off_screen: { name: '', at: null, occupants: [] } },
		items: tree.items ?? [],
		threads: tree.threads ?? [],
		relations: tree.relations ?? [],
		meters: tree.meters ?? [],
		conditions: tree.conditions ?? [],
		economy: tree.economy ?? null,
		cast: tree.cast ?? [],
		Player: tree.Player ?? { identity: { key: 'player', display: 'Player' }, live: EMPTY_LIVE }
	};
}

/**
 * The MOCK demo's world (there is no engine to ask): one row per section, so
 * `VITE_MOCK=1` still shows the pane's branch stack instead of empty sections.
 *
 * `clock.time`/`scene.tone` are deliberately EMPTY: the mock stream advances
 * exactly those two facts per turn, and the pane reads the tree first with the
 * flat fold beneath, so leaving them blank keeps the demo's clock and tone
 * moving instead of freezing on a stand-in value.
 */
function mockEntityTree(): SessionTree {
	return {
		clock: {
			time: '',
			date: '',
			day: null,
			last_writer: null,
			proposal: null,
			applied: false
		},
		scene: {
			weather: 'light rain',
			tone: '',
			forecast: { value: '', pinned: false },
			irony: 'everyone is being very polite',
			genre: '',
			tension: 6,
			conditions: []
		},
		places: {
			tree: [
				{
					id: 'loc_nightmarket',
					name: 'night market',
					parent: null,
					path: 'night market',
					note: 'stalls and paper lanterns',
					occupants: ['Yara', 'Player (Sam)']
				},
				{
					id: 'loc_noodlestall',
					name: 'noodle stall',
					parent: 'night market',
					path: 'night market > noodle stall',
					note: '',
					occupants: []
				}
			],
			off_screen: { name: 'off screen', at: 'the tram depot', occupants: ['Iris'] }
		},
		items: [{ id: 'itm_brasskey', name: 'brass key', holder: 'Yara', note: 'warm to the touch' }],
		threads: [
			{
				id: 'th_ledger',
				name: 'the missing ledger',
				status: 'active',
				detail: 'Yara has not said who took it',
				note: ''
			}
		],
		relations: [
			{ id: 'rel_yara_player', from: 'Yara', to: 'Player (Sam)', label: '', note: 'owes him a favour' }
		],
		// No scene group: the scene's own rows are the `scene` branch's.
		meters: [
			{
				host: 'Yara',
				numbers: [{ key: 'trust', value: 12, subject: 'Player (Sam)' }],
				traits: [{ key: 'mood', value: 'wary' }]
			}
		],
		conditions: [
			{
				id: 'cd_cutpalm',
				host: 'Yara',
				key: 'cut palm',
				severity: 'minor',
				source: 'the broken glass',
				expires_at: null,
				expired: false
			}
		],
		economy: { id: 'eco_marks', denomination: 'marks', balance: 34, note: '' },
		cast: [
			{
				participation: {
					member_id: 'cm_yara',
					display: 'Yara',
					aliases: [],
					provenance: 'card',
					source_id: null,
					status: 'active',
					tier: 'speaking',
					promoted_persona_id: null
				},
				definition: null,
				live: {
					location: 'night market',
					off_screen: false,
					presence: 'in_scene',
					meters: { numbers: [], traits: [] },
					conditions: [],
					body: {
						pose: 'standing',
						clothing: ['rain-soaked coat'],
						surface: 'damp hair',
						note: ''
					},
					items: ['brass key']
				}
			}
		],
		Player: {
			identity: { key: 'player', display: 'Player (Sam)' },
			live: {
				location: 'night market',
				off_screen: false,
				presence: 'in_scene',
				meters: { numbers: [], traits: [] },
				conditions: [],
				body: { pose: 'hands in pockets', clothing: [], surface: '', note: '' },
				items: []
			}
		}
	};
}

/**
 * Read the session tree (`GET .../tree` -> `tree`, W9.32 §2).
 *
 * Returns `null` on any failure (no session, an engine without the route): the
 * pane then falls back to the flat 5-key fold rows it always had. A mock build
 * answers a small stand-in tree so `VITE_MOCK=1` still renders every section.
 */
export async function getEntityTree(sessionId: string): Promise<SessionTree | null> {
	if (USE_MOCK) return mockEntityTree();
	try {
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/tree`, {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return null;
		const body = await readJson(res);
		const tree = body.tree;
		if (!tree || typeof tree !== 'object' || Array.isArray(tree)) return null;
		return normalizeTree(tree as Record<string, unknown>);
	} catch {
		return null;
	}
}

/** The result of one registry write action through the ✎ door. */
export interface EntityOpResult {
	ok: boolean;
	/** The engine's readable rejection text (the same one the model corrects from). */
	error?: string;
	/** The minted/cited id on an accepted action. */
	id?: string;
}

/**
 * Run ONE registry write action as the user (`POST .../entities`, W9.29 §8).
 *
 * `op` names a tracker action (`set_attr`, `move_being`, `update_object`, ...)
 * and `args` is that action's own argument object, so the owner's edit is
 * validated by the exact code path the BTS loop uses. A rejection answers `400`
 * with the readable reason and writes nothing; this helper surfaces it as
 * `{ok: false, error}` so the row never looks saved when it was not.
 */
export async function writeEntityOp(
	sessionId: string,
	op: string,
	args: Record<string, unknown>
): Promise<EntityOpResult> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/entities`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ op, args })
			}
		);
		const body = await readJson(res);
		if (!res.ok) {
			const detail = (body as Record<string, unknown>).detail;
			const reason =
				detail && typeof detail === 'object' && typeof (detail as Record<string, unknown>).reason === 'string'
					? ((detail as Record<string, unknown>).reason as string)
					: typeof detail === 'string'
						? detail
						: 'the action was refused';
			return { ok: false, error: reason };
		}
		const accepted = body as Record<string, unknown>;
		return {
			ok: accepted.ok !== false,
			...(typeof accepted.id === 'string' ? { id: accepted.id } : {})
		};
	} catch {
		return { ok: false, error: 'the engine did not answer' };
	}
}


function isAttributionCall(row: AgentCallRow): boolean {
	if (row.agent === 'attribution') return true;
	const system = row.messages[0]?.content;
	return typeof system === 'string' && /report who speaks which line/i.test(system);
}


/** Parse a raw attribution model reply (a JSON array) into `{speaker, quote}`. */
export function parseAttributionText(text: string): AttributedQuote[] {
	const start = text.indexOf('[');
	const end = text.lastIndexOf(']');
	if (start === -1 || end <= start) return [];
	let data: unknown;
	try {
		data = JSON.parse(text.slice(start, end + 1));
	} catch {
		return [];
	}
	if (!Array.isArray(data)) return [];
	const out: AttributedQuote[] = [];
	for (const entry of data) {
		if (!entry || typeof entry !== 'object') continue;
		const obj = entry as Record<string, unknown>;
		const speaker = typeof obj.speaker === 'string' ? obj.speaker.trim() : '';
		const speaker_id =
			typeof obj.speaker_id === 'string' ? obj.speaker_id.trim() : '';
		const quote = typeof obj.quote === 'string' ? obj.quote : '';
		if (speaker && quote.trim())
			out.push(speaker_id ? { speaker, speaker_id, quote } : { speaker, quote });
	}
	return out;
}


/**
 * Legacy fallback: per-turn attributions from the `attribution` agent captures.
 * Only used when the attributions endpoint is absent (older engine) or errors.
 */
async function getAttributionsFromCaptures(sessionId: string): Promise<AttributionRow[]> {
	const rows = await getAgentCalls(sessionId);
	const out: AttributionRow[] = [];
	for (const row of rows) {
		if (!isAttributionCall(row)) continue;
		out.push({ seq: row.seq, attributions: parseAttributionText(row.response_text) });
	}
	return out.sort((a, b) => a.seq - b.seq);
}


/**
 * Read a session's per-turn speaker attributions from the engine's persisted
 * `system`/`attribution` events (GET .../attributions), seq-ordered. Falls back
 * to the `attribution` agent captures ONLY when the endpoint is missing (older
 * engine) or errors. Rows with an empty array are kept (they still pin the turn).
 */
export async function getAttributions(sessionId: string): Promise<AttributionRow[]> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attributions`,
			{ headers: { Accept: 'application/json' } }
		);
		if (res.ok) {
			const body = await readJson(res);
			const rows = Array.isArray(body.attributions) ? body.attributions : [];
			return (rows as AttributionRow[])
				.map((row) => ({
					seq: typeof row.seq === 'number' ? row.seq : 0,
					attributions: Array.isArray(row.attributions) ? row.attributions : []
				}))
				.sort((a, b) => a.seq - b.seq);
		}
	} catch {
		// endpoint absent/errored -> capture fallback
	}
	return getAttributionsFromCaptures(sessionId);
}

// --- Wave B: card Opening block + session module overrides (D29/D30) ---------
//
// Two read/write seams for parsed-but-dead V2 card data:
//
//   GET  {API_BASE}/api/sessions/{id}/opening          -> OpeningInfo
//   POST {API_BASE}/api/sessions/{id}/greeting         body {"index": n}
//        -> {"session_id", "index", "event": "greeting_selected"} (append-only)
//   GET  {API_BASE}/api/sessions/{id}/session-modules  -> {"modules": SessionModule[]}
//   PUT  {API_BASE}/api/sessions/{id}/session-modules/{module_id}
//        body <SessionModule> -> the appended module
//
// The greeting is the card's `first_mes` + `alternate_greetings`; the selection
// is a latest-wins `greeting_selected` event. Session modules are the card's
// present `system_prompt` / `post_history_instructions` / `mes_example`, seeded
// at import and spliced into the narrate flow at their anchors.
//
// The background engine serves `opening`/`greeting` from the cards router; the
// `session-modules` endpoints (and the splice call-site) are orchestrator-owned
// — rewire `SESSION_OVERRIDE_ENDPOINTS` if the live paths differ. Every function
// carries a `USE_MOCK` branch so the mock demo stays alive.


/** The result of one manual reconciliation pass (`POST .../reconcile`). */
export interface ReconcileResult {
	/** The drift rows the pass filed, in belief order (already in the log). */
	flags: Array<Record<string, unknown>>;
	/** How many beliefs the model stated (0 when nothing could be checked). */
	checked: number;
	/** Why nothing was checked — the engine's soft failure, never an HTTP error. */
	error?: string;
}

/**
 * Run the W9.29 §12 reconciliation pass NOW (`POST .../reconcile`).
 *
 * The pass otherwise only rides the BTS cadence (every tenth committed turn).
 * This door drives the same off-turn pass: the model states what it believes,
 * the engine files one `reconciliation_drift` notice per disagreement, and the
 * registry is never written. A failure is a SOFT answer — `200` with
 * `{flags: [], checked: 0, error}` — so the caller renders one line either way;
 * only an unknown session (404) or an unreachable engine is an error here.
 */
export async function checkReconcileNow(sessionId: string): Promise<ReconcileResult> {
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/reconcile`,
			{ method: 'POST' }
		);
		const body = await readJson(res);
		if (!res.ok) {
			const detail = body.detail;
			return {
				flags: [],
				checked: 0,
				error: typeof detail === 'string' ? detail : 'the engine did not run the check'
			};
		}
		const flags = Array.isArray(body.flags) ? (body.flags as Array<Record<string, unknown>>) : [];
		const checked = typeof body.checked === 'number' ? body.checked : 0;
		return {
			flags,
			checked,
			...(typeof body.error === 'string' ? { error: body.error } : {})
		};
	} catch {
		return { flags: [], checked: 0, error: 'the engine did not answer' };
	}
}
