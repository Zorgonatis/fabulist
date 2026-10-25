// Mock proposal fixtures (`VITE_MOCK=1`) — the scripted queue the review tree is
// demonstrated and checked against with no engine running (W9.23 §3.5/Q5).
//
// Why a separate module instead of inline constants: the proposal fold is the
// widest surface in the wave, and the fixture set has to be READ and EDITED as
// data (add a row, age one out, mark an op stale) rather than as code. The module
// is loaded through a DYNAMIC import from `proposals.ts` so it stays out of the
// production bundle, exactly like `#lib/mock/mockApi`.
//
// The rows are RAW WIRE SHAPES: `proposals.ts` runs them through the same
// defensive normalizers the real read uses, so a fixture that drifts from the
// contract fails the same way a server row would instead of quietly passing.
//
// What the set must contain (plan §3.5, Q5): multi-kind, multi-object, op-typed
// rows (an entry ADD, an entry MODIFY old -> new, an entry DELETE, an
// `extensions.<key>` modify), a STALE op row, a LEGACY whole-value amend row, an
// aged/archived set, cast rows, a committed set carrying kind/action — plus the
// historian status fixture so the drawer's run line renders offline too.
//
// W9.30 §7 W3 adds the two — three — shapes the epic's surfaces must render
// without an engine: a BORN-PARENTED books row (`target` is a book NAME that does
// not exist yet, `target_new: true`, entry-add `ops`) and an ordinary lane book
// ADD (`patch.name` + `patch.entries`), both of which CREATE their book on
// accept, plus a PROMOTION row (an ordinary `actor` `add` whose `origin` is
// `promotion` and whose `patch` is the full deterministic persona card, the name
// already collision-suffixed).

/** One raw `Proposal` as the API serves it (unparsed on purpose — see the header). */
export type RawProposal = Record<string, unknown>;

/** One raw `Decision` as the API serves it. */
export type RawDecision = Record<string, unknown>;

/** One session's scripted read payload (`GET /api/proposals?session=`). */
export interface MockReview {
	title: string;
	pending: RawProposal[];
	archived: RawProposal[];
	committed: RawDecision[];
}

const SESSION_LIVE = 'mock-night-market';
const SESSION_ARCHIVE = 'mock-academy';

const MILLIE_BEFORE =
	'A tide-reader who keeps the harbour’s almanac and does not volunteer what she reads in it.';
const MILLIE_AFTER = `${MILLIE_BEFORE} Since the night the compass broke, she has started leaving one lamp lit.`;

const MINSTER_BEFORE =
	'A hanging town of grey stone and salt wind, its lower streets flooded twice a day.';
const MINSTER_AFTER = `${MINSTER_BEFORE} The night market now runs past curfew, tolerated rather than permitted.`;

const ENTRY_TIDE_MODIFIED_BEFORE = {
	comment: 'Tide tables',
	keys: ['tide', 'almanac'],
	content: 'The almanac lists the year’s highest tides in a hand nobody has claimed.'
};
const ENTRY_TIDE_MODIFIED_AFTER = {
	comment: 'Tide tables',
	keys: ['tide', 'almanac', 'flood'],
	content:
		'The almanac lists the year’s highest tides in a hand nobody has claimed. Two entries are written over, and the ink is younger than the rest.'
};
const ENTRY_CURFEW_BEFORE = {
	comment: 'Curfew',
	keys: ['curfew', 'watch'],
	content: 'The watch rings the curfew bell at the ninth hour.'
};

/** The two fixture sessions, keyed by session id (the inbox's unit). */
export const MOCK_REVIEWS: Record<string, MockReview> = {
	[SESSION_LIVE]: {
		title: 'Night market — the brass key',
		pending: [
			{
				id: 'p-world-minster',
				kind: 'world',
				action: 'amend',
				target: 'w-minster',
				target_name: 'Minster-by-the-Sea',
				patch: {},
				ops: [
					{ op: 'modify', path: 'description', old: MINSTER_BEFORE, new: MINSTER_AFTER },
					{
						op: 'add',
						path: 'extensions.curfew_note',
						new: 'The night market runs past curfew — tolerated, not permitted.'
					}
				],
				origin: 'bts',
				signal: 0.72,
				evidence: 'Two scenes put the market past the ninth-hour bell.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				id: 'p-actor-millie',
				kind: 'actor',
				action: 'amend',
				target: 'a-millie',
				target_name: 'Millie',
				patch: {},
				ops: [
					{ op: 'modify', path: 'description', old: MILLIE_BEFORE, new: MILLIE_AFTER },
					{ op: 'modify', path: 'extensions.mood', old: 'wary', new: 'warm, watchful' },
					{
						op: 'delete',
						path: 'personality',
						old: 'Terse. Answers questions with better questions.'
					}
				],
				origin: 'bts',
				signal: 0.87,
				evidence: 'She now leaves a lamp burning — the card says nothing about it.',
				supersedes: ['p-actor-millie-old'],
				archived: false,
				age_turns: 1,
				// The `extensions.mood` op was filed against a value the card no
				// longer holds: the accept refuses THAT path, the row stays live.
				stale_paths: ['extensions.mood']
			},
			{
				id: 'p-actor-fen',
				kind: 'actor',
				action: 'amend',
				target: 'a-fen',
				target_name: 'Fen',
				patch: {},
				ops: [
					{
						op: 'modify',
						path: 'scenario',
						old: 'Fen keeps the harbour office and the harbour’s secrets.',
						new: 'Fen keeps the harbour office, the harbour’s secrets, and one brass key that is not on any hook.'
					}
				],
				origin: 'bts',
				signal: 0.64,
				evidence: 'The key was handed over on screen and never explained.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				id: 'p-book-tides',
				kind: 'book',
				action: 'amend',
				target: 'b-tides',
				target_name: 'Tides & Omens',
				patch: {},
				ops: [
					{
						op: 'add',
						path: 'entries.7c1d9f02',
						new: {
							comment: 'Younger ink',
							keys: ['ink', 'almanac'],
							content:
								'Two entries in the almanac are written in younger ink. Nobody will say by whom.'
						}
					},
					{
						op: 'modify',
						path: 'entries.3b8a55e1',
						old: ENTRY_TIDE_MODIFIED_BEFORE,
						new: ENTRY_TIDE_MODIFIED_AFTER
					},
					{
						op: 'delete',
						path: 'entries.9d0c4a17',
						old: ENTRY_CURFEW_BEFORE
					}
				],
				origin: 'bts',
				signal: 0.55,
				evidence: 'The almanac was read twice on screen; both readings add a key.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				id: 'p-actor-brann',
				kind: 'actor',
				action: 'add',
				target: null,
				target_name: null,
				patch: {
					name: 'Brann',
					description: 'Runs the fish stall at the end of the market row; counts everything twice.',
					personality: 'Cheerful, exacting.',
					scenario: 'Brann has been at the market longer than anyone will admit.'
				},
				ops: null,
				origin: 'bts',
				signal: 0.61,
				evidence: 'Named three times; no card exists.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				// A pre-R2 whole-value amend, hydrated at read: one whole-field
				// modify row per patch key, `legacy: true`, `old` from the current
				// fold. It carries no anchored value, so it is never stale-tracked.
				id: 'p-actor-legacy',
				kind: 'actor',
				action: 'amend',
				target: 'a-millie',
				target_name: 'Millie',
				patch: { personality: 'Terse. Answers questions with better questions.' },
				ops: [
					{
						op: 'modify',
						path: 'personality',
						old: 'Terse. Answers questions with better questions.',
						new: 'Terse, until the lamp is lit.',
						legacy: true
					}
				],
				origin: 'promotion',
				signal: null,
				evidence: 'Filed before amendments carried operations.',
				supersedes: [],
				archived: false,
				age_turns: 2,
				stale_paths: []
			},
			{
				// W9.30 §6 — BORN-PARENTED: the target is a book NAME the world does
				// not hold, so accepting CREATES it and applies these entry adds in
				// one step. Its own object (`t:<name>`), the `new` chip, and the
				// create hint in place of any stale badge.
				id: 'p-book-lantern-parented',
				kind: 'book',
				action: 'amend',
				target: 'The Lantern Registry',
				target_name: 'The Lantern Registry',
				target_new: true,
				patch: {},
				ops: [
					{
						op: 'add',
						path: 'entries.4f21ab90',
						new: {
							keys: ['lamplighters', 'registry'],
							content:
								'The lamplighters keep a registry of every lamp that has gone out without a wick being spent.',
							comment: 'The registry'
						}
					},
					{
						op: 'add',
						path: 'entries.1c93de47',
						new: {
							keys: ['the brass key'],
							content:
								'One entry is a lamp with no address, marked only with a brass key pressed into the page.',
							comment: 'Lamp with no address'
						}
					}
				],
				origin: 'bts',
				signal: 0.68,
				evidence: 'The lamp registry was read on screen; no book of that name exists yet.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				// W9.30 §6 — an ordinary LANE BOOK ADD: equally a create (the accept
				// attaches it), so it renders the same affordance as the parented row.
				id: 'p-book-harbour-notices',
				kind: 'book',
				action: 'add',
				target: null,
				target_name: null,
				patch: {
					name: 'Harbour Notices',
					entries: [
						{
							keys: ['notices', 'mooring'],
							content:
								'Notices are nailed to the harbour post, and a mooring is only held while its notice stands.',
							comment: 'The harbour post'
						},
						{
							keys: ['brass key'],
							content: 'A brass key was mentioned in one notice and never claimed.',
							comment: 'Unclaimed key'
						}
					]
				},
				ops: null,
				origin: 'bts',
				signal: 0.61,
				evidence: 'Two notices were read aloud; the harbour keeps no book for them.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				// W9.30 §6 — a PROMOTION row: an ordinary `actor` ADD (the accept is the
				// one that already exists) whose card the cast ladder assembled from the
				// cast member Odile. The name is the FINAL, collision-suffixed one
				// (`Odile` → `Odile1`), so the owner sees what will be created before
				// accepting. Authored fields arrive empty — the engine writes only what
				// play established.
				id: 'p-actor-promotion-odile',
				kind: 'actor',
				action: 'add',
				target: null,
				target_name: null,
				patch: {
					name: 'Odile1',
					description:
						'Also known as the lamp-keeper.\nTowards Brann: keeps his lamp lit, and no coin changes hands for it.\nHolds the brass key.\nPromoted from chat "Night market — the brass key" at turn 12.',
					personality: '',
					scenario: '',
					first_mes: '',
					book_ids: [],
					alternate_greetings: [],
					image_asset: null,
					extensions: { cast_aliases: ['the lamp-keeper'] },
					provenance: {
						origin: 'promotion',
						member_id: 'cm_4b1d0c7a9e22',
						session_id: SESSION_LIVE,
						at_seq: 41
					}
				},
				ops: null,
				origin: 'promotion',
				signal: null,
				evidence:
					'cast member Odile has 3 tracked facts: relationship Brann, item the brass key, alias the lamp-keeper',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				id: 'p-cast-odile',
				kind: 'cast',
				action: 'add',
				target: null,
				target_name: null,
				patch: {
					name: 'Odile',
					aliases: ['the lamp-keeper'],
					origin: 'bts'
				},
				ops: null,
				origin: 'bts',
				signal: 0.58,
				evidence: 'Addressed by name across two turns; not on the roster.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			},
			{
				id: 'p-cast-brann',
				kind: 'cast',
				action: 'add',
				target: null,
				target_name: null,
				patch: { name: 'Brann', aliases: ['the fishmonger'], origin: 'bts' },
				ops: null,
				origin: 'bts',
				signal: null,
				evidence: 'Referred to twice without a card.',
				supersedes: [],
				archived: false,
				age_turns: 1,
				stale_paths: []
			}
		],
		archived: [
			{
				id: 'p-world-minster-aged',
				kind: 'world',
				action: 'amend',
				target: 'w-minster',
				target_name: 'Minster-by-the-Sea',
				patch: {},
				ops: [
					{
						op: 'modify',
						path: 'scenario',
						old: 'The town keeps its own hours.',
						new: 'The town keeps its own hours, and the tide keeps the town.'
					}
				],
				origin: 'bts',
				signal: 0.49,
				evidence: 'Filed sixteen turns ago and never reviewed.',
				supersedes: [],
				archived: true,
				age_turns: 16,
				stale_paths: []
			},
			{
				id: 'p-actor-fen-aged',
				kind: 'actor',
				action: 'amend',
				target: 'a-fen',
				target_name: 'Fen',
				patch: {},
				ops: [
					{
						op: 'modify',
						path: 'description',
						old: 'Keeps the harbour office.',
						new: 'Keeps the harbour office and the keys that go with it.'
					}
				],
				origin: 'bts',
				signal: 0.52,
				evidence: 'Filed fourteen turns ago.',
				supersedes: [],
				archived: true,
				age_turns: 14,
				stale_paths: []
			}
		],
		committed: [
			{
				decision_id: 'd-accept-1',
				proposal_id: 'p-actor-millie-prev',
				decision: 'accept',
				kind: 'actor',
				action: 'amend',
				pre_image: { name: 'Millie', description: MILLIE_BEFORE },
				applied: { name: 'Millie', description: MILLIE_AFTER },
				applied_summary: "Amended actor 'Millie'"
			},
			{
				decision_id: 'd-accept-2',
				proposal_id: 'p-cast-brann-prev',
				decision: 'accept',
				kind: 'cast',
				action: 'add',
				pre_image: null,
				applied: { name: 'Odile' },
				applied_summary: "Added to the cast 'Odile'"
			}
		]
	},
	[SESSION_ARCHIVE]: {
		title: 'Return to the academy',
		pending: [],
		archived: [
			{
				id: 'p-academy-wren',
				kind: 'actor',
				action: 'amend',
				target: 'a-wren',
				target_name: 'Wren',
				patch: {},
				ops: [
					{
						op: 'add',
						path: 'extensions.house',
						new: 'Aviary'
					},
					{
						op: 'delete',
						path: 'personality',
						old: 'Guarded to the point of rudeness.'
					}
				],
				origin: 'bts',
				signal: 0.44,
				evidence: 'Filed nineteen turns ago.',
				supersedes: [],
				archived: true,
				age_turns: 19,
				stale_paths: []
			}
		],
		committed: []
	}
};

/**
 * The historian status fixture, coded to the PINNED wire of §3.6.7:
 *
 *   GET /api/sessions/{sid}/historian
 *     -> {running, last_run: {status, trigger, turn_seq, gate, filed, dropped,
 *                             failed_lanes, ts} | null, committed_turns, cadence, due_in}
 *
 * `last_run` is `null` on a session the historian has never touched (a fresh
 * chat), and `due_in` is the turns remaining before the next automatic run.
 * `running: false` is the resting state; the run-now button disables on `true`.
 */
export interface MockHistorianStatus {
	running: boolean;
	last_run: {
		status: string;
		trigger: string;
		turn_seq: number;
		gate: string;
		filed: number;
		dropped: number;
		failed_lanes: { lane: string; reason: string }[];
		ts: string;
	} | null;
	committed_turns: number;
	cadence: number;
	due_in: number;
}

/** One scripted historian status per fixture session. */
export const MOCK_HISTORIAN_STATUS: Record<string, MockHistorianStatus> = {
	[SESSION_LIVE]: {
		running: false,
		last_run: {
			status: 'finished',
			trigger: 'cadence',
			turn_seq: 10,
			gate: 'proceeded',
			filed: 8,
			dropped: 2,
			failed_lanes: [],
			ts: '2026-10-19T20:14:00Z'
		},
		committed_turns: 13,
		cadence: 10,
		due_in: 7
	},
	[SESSION_ARCHIVE]: {
		running: false,
		last_run: null,
		committed_turns: 19,
		cadence: 10,
		due_in: 0
	}
};

/** The status a session the fixtures do not name reads (never `undefined`). */
export const MOCK_HISTORIAN_DEFAULT: MockHistorianStatus = {
	running: false,
	last_run: null,
	committed_turns: 0,
	cadence: 10,
	due_in: 10
};

/**
 * The historian status fixture for one session (never `undefined`).
 *
 * The status is C2's client (`historian.ts`) to fetch; this lane owns only the
 * FIXTURE, so mock mode renders the drawer's run line with no engine — the
 * accessor is what the client's mock branch calls, keeping the pinned shape in
 * one place instead of letting each consumer invent its own stub.
 */
export function historianStatusFixture(sessionId: string): MockHistorianStatus {
	return MOCK_HISTORIAN_STATUS[sessionId] ?? MOCK_HISTORIAN_DEFAULT;
}
