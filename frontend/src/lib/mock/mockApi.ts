// MOCK API — dev-only fake for `$lib/api`. Enable with `VITE_MOCK=1`; see
// `frontend/.env.example`. It issues no HTTP and has no backend: scripted
// StreamEvents are emitted on timers so `/chat` is fully demonstrable offline.
// Signatures, `StreamOutcome` states and stop semantics mirror the real client.
//
// W9.26 (context management): the seed carries the whole compaction surface so
// the feature is demoable without the backend —
//   * `mock-era-now`   parent era: live context-low row (unsettled Compact
//                      button) + a `kind:"compaction"` child beside a
//                      `kind:"user"` swipe alternate (swipe-group exclusion);
//   * `mock-era-next`  the successor era: inherited rows, the settled row, the
//                      break marker, and post-fork turns (break above notice);
//   * `mock-context-low` the plain / noop / over_budget context-low variants;
//   * `getRecap`       chain-aware summary rows for the notice's expander;
//   * `startCompaction`/`getCompactionJob` the 202-then-poll job (running →
//                      done) so the button path is clickable end to end.
// Two content escapes drive the 409 client paths: a message containing
// `!compacting` answers 409 `{reason:"compacting"}` once (the page retries), and
// `!superseded` answers 409 `{reason:"superseded", id: mock-era-next}` (the page
// rolls back, follows, and replays); `!superseded-hard` keeps refusing from the
// successor too, so the follow limit + replay-failure restore are demoable.

import type {
	CompactionJob,
	CompactionMarker,
	ContextLowRow,
	Projection,
	StreamEvent,
	StreamOutcome,
	StreamTurnHandlers,
	TurnKind
} from '#lib/api';
import { StreamError, MOCK_SEED_SESSIONS } from '#lib/api';

interface MockSession {
	facts: Record<string, string>;
	turns: number;
}

/** One transcript row in a seeded chain (projection item shape). */
interface MockRow {
	id: number;
	seq: number;
	type: 'user' | 'narrative';
	payload: Record<string, unknown>;
}

/** A seeded chain: transcript rows plus the W9.26 projection additions. */
interface MockChain {
	facts: Record<string, string>;
	user_messages: MockRow[];
	narratives: MockRow[];
	context_lows: ContextLowRow[];
	compactions: CompactionMarker[];
	recap: Array<Record<string, unknown>>;
	/** `GET .../branches` payload, when this session has forks. */
	branches?: Record<string, unknown>;
}

const INITIAL_FACTS: Record<string, string> = {
	time: '14:35',
	loc: 'night market',
	weather: 'light rain',
	tone: 'warm',
	inv: ''
};

const TIMES = ['14:35', '15:10', '15:48', '16:20', '17:05'];

const sessions = new Map<string, MockSession>();

// --- W9.26 seed chains ------------------------------------------------------

/** The old era's turns (seqs 1–6) — inherited by the successor era verbatim. */
const OLD_TURNS: MockRow[] = [
	{
		id: 1,
		seq: 1,
		type: 'user',
		payload: { text: 'The rain has not let up since the bridge.', kind: 'in_character' }
	},
	{
		id: 2,
		seq: 2,
		type: 'narrative',
		payload: {
			prose:
				'The innkeeper sets a chipped cup down without being asked and looks past you, at the door, the way people do when they are counting exits.'
		}
	},
	{
		id: 3,
		seq: 3,
		type: 'user',
		payload: { text: 'Ask her about the compass.', kind: 'in_character' }
	},
	{
		id: 4,
		seq: 4,
		type: 'narrative',
		payload: {
			prose:
				'“Brass?” She wipes the counter with her sleeve. “There was a man here with a brass thing. Paid in coin that was still warm.”'
		}
	},
	{ id: 5, seq: 5, type: 'user', payload: { text: 'I take the key.', kind: 'in_character' } },
	{
		id: 6,
		seq: 6,
		type: 'narrative',
		payload: {
			prose:
				'The lock gives with a sound like a held breath released. Behind the door, the corridor runs down further than the inn is tall.'
		}
	}
];

/** The context-low row the parent era emitted at 82% of its usable budget. */
const ERA_NOW_LOW: ContextLowRow = {
	seq: 7,
	session_id: MOCK_SEED_SESSIONS.eraNow,
	payload: {
		event: 'context_low',
		agent: 'narrate',
		ratio: 0.82,
		prompt_tokens: 21400,
		budget_tokens: 26112,
		basis: 'measured'
	}
};

/** The compaction marker the successor era carries (covers the parent's seqs). */
const ERA_NEXT_MARKER: CompactionMarker = {
	seq: 9,
	session_id: MOCK_SEED_SESSIONS.eraNext,
	payload: {
		event: 'compaction',
		source_session: MOCK_SEED_SESSIONS.eraNow,
		summary_ref: [MOCK_SEED_SESSIONS.eraNow, 8],
		covers_seqs: [1, 6],
		trigger: 'manual',
		stats: { calls: 1, prompt_tokens: 18342, model: 'your-model-id' }
	}
};

const SUMMARY_TEXT =
	'The rainbound evening at the Rusty Anchor: the innkeeper knew of a brass compass paid for in still-warm coin. ' +
	'The player took the brass key she slid across the counter; the lock gave, and the corridor behind it runs deeper than the inn is tall.';

/** The `summary` v2 row (recap read only — never in the projection). */
const ERA_RECAP: Array<Record<string, unknown>> = [
	{
		seq: 8,
		text: SUMMARY_TEXT,
		covers_seqs: [1, 6],
		trigger: 'manual',
		stats: { calls: 1, prompt_tokens: 18342, model: 'your-model-id' }
	}
];

const CHAINS = new Map<string, MockChain>();

// Parent era: old turns through the low row, no marker in its OWN chain, so its
// Compact button is live. Its branches carry a user alternate AND a compaction
// continuation (the swipe group must keep only the user one).
CHAINS.set(MOCK_SEED_SESSIONS.eraNow, {
	facts: { time: '17:05', loc: 'the Rusty Anchor', weather: 'light rain', tone: 'wary', inv: '+brass key' },
	user_messages: OLD_TURNS.filter((row) => row.type === 'user'),
	narratives: OLD_TURNS.filter((row) => row.type === 'narrative'),
	context_lows: [ERA_NOW_LOW],
	compactions: [],
	recap: [],
	branches: {
		session_id: MOCK_SEED_SESSIONS.eraNow,
		parent: null,
		siblings: [
			{
				session_id: MOCK_SEED_SESSIONS.eraNow,
				base_seq: 5,
				snippet: 'The lock gives with a sound like a held breath released.',
				is_self: true,
				kind: 'user'
			}
		],
		children: [
			{
				session_id: MOCK_SEED_SESSIONS.swipeAlt,
				base_seq: 5,
				snippet: 'She does not hand over the key. “Ask me what it opens first.”',
				kind: 'user'
			},
			{
				session_id: MOCK_SEED_SESSIONS.eraNext,
				base_seq: 5,
				snippet: '',
				kind: 'compaction'
			}
		]
	}
});

const ERA_NEXT_TURN: MockRow = {
	id: 10,
	seq: 10,
	type: 'user',
	payload: { text: 'The key is warm in my hand.', kind: 'in_character' }
};
const ERA_NEXT_NARRATIVE: MockRow = {
	id: 11,
	seq: 11,
	type: 'narrative',
	payload: {
		prose:
			'The warmth spreads up your wrist as the corridor bends left, then left again, doubling back on itself. Somewhere below, water is moving.'
	}
};
const ERA_SECOND_MARKER: CompactionMarker = {
	seq: 13,
	session_id: MOCK_SEED_SESSIONS.eraFinal,
	payload: {
		event: 'compaction',
		source_session: MOCK_SEED_SESSIONS.eraNext,
		summary_ref: [MOCK_SEED_SESSIONS.eraNext, 12],
		covers_seqs: [1, 11],
		trigger: 'forced_90',
		stats: { calls: 3, prompt_tokens: 24880, model: 'your-model-id' }
	}
};

// Successor era: inherits the parent rows (with the parent's seqs), then the
// marker (one break) and the post-fork turns.
CHAINS.set(MOCK_SEED_SESSIONS.eraNext, {
	facts: { time: '17:12', loc: 'the corridor behind the key door', weather: 'none', tone: 'wary', inv: '+brass key' },
	user_messages: [...OLD_TURNS.filter((row) => row.type === 'user'), ERA_NEXT_TURN],
	narratives: [...OLD_TURNS.filter((row) => row.type === 'narrative'), ERA_NEXT_NARRATIVE],
	// The parent's low row is INHERITED — it renders above the break, settled.
	context_lows: [ERA_NOW_LOW],
	compactions: [ERA_NEXT_MARKER],
	recap: ERA_RECAP
});

// A THIRD era: the chain now carries TWO markers, so the transcript shows two
// breaks and each collapse chevron folds ONLY its own span.
CHAINS.set(MOCK_SEED_SESSIONS.eraFinal, {
	facts: { time: '17:20', loc: 'the corridor behind the key door', weather: 'none', tone: 'wary', inv: '+brass key' },
	user_messages: [
		...OLD_TURNS.filter((row) => row.type === 'user'),
		ERA_NEXT_TURN,
		{ id: 14, seq: 14, type: 'user', payload: { text: 'Follow the water sound.', kind: 'in_character' } }
	],
	narratives: [
		...OLD_TURNS.filter((row) => row.type === 'narrative'),
		ERA_NEXT_NARRATIVE,
		{
			id: 15,
			seq: 15,
			type: 'narrative',
			payload: { prose: 'The corridor ends at a low arch, and beyond it the water is a black mirror holding one lantern of light.' }
		}
	],
	context_lows: [ERA_NOW_LOW],
	compactions: [ERA_NEXT_MARKER, ERA_SECOND_MARKER],
	recap: [
		...ERA_RECAP,
		{
			seq: 12,
			text:
				'The corridor behind the key door bends back on itself and descends; the brass key stays warm. ' +
				'The second era folds the whole first walk into the summary of the first.',
			covers_seqs: [1, 11],
			trigger: 'forced_90',
			absorbs: [MOCK_SEED_SESSIONS.eraNow, 8],
			stats: { calls: 3, prompt_tokens: 24880, model: 'your-model-id' }
		}
	]
});

// A plain single-era chat with all three context-low variants.
CHAINS.set(MOCK_SEED_SESSIONS.contextLow, {
	facts: { time: '14:35', loc: 'night market', weather: 'light rain', tone: 'warm', inv: '' },
	user_messages: [
		{ id: 1, seq: 1, type: 'user', payload: { text: 'Pick up the lantern.', kind: 'in_character' } },
		{ id: 5, seq: 5, type: 'user', payload: { text: 'Ask the price of the map.', kind: 'in_character' } }
	],
	narratives: [
		{
			id: 2,
			seq: 2,
			type: 'narrative',
			payload: { prose: 'The lantern is heavier than it looks and the wick smells of cedar.' }
		},
		{
			id: 6,
			seq: 6,
			type: 'narrative',
			payload: { prose: 'The map seller names a price and then, seeing your face, names a smaller one.' }
		}
	],
	context_lows: [
		{
			seq: 3,
			session_id: MOCK_SEED_SESSIONS.contextLow,
			payload: {
				event: 'context_low',
				agent: 'narrate',
				ratio: 0.83,
				prompt_tokens: 22100,
				budget_tokens: 26624,
				basis: 'measured'
			}
		},
		{
			seq: 7,
			session_id: MOCK_SEED_SESSIONS.contextLow,
			payload: {
				event: 'context_low',
				agent: 'narrate',
				ratio: 0.94,
				prompt_tokens: 25000,
				budget_tokens: 26624,
				basis: 'measured',
				noop: true
			}
		},
		{
			seq: 8,
			session_id: MOCK_SEED_SESSIONS.contextLow,
			payload: {
				event: 'context_low',
				agent: 'narrate',
				ratio: 1.0,
				prompt_tokens: 0,
				budget_tokens: 0,
				basis: 'measured',
				over_budget: true
			}
		}
	],
	compactions: [],
	recap: []
});

// --- W9.26 mock compaction jobs ---------------------------------------------

interface MockJob {
	sessionId: string;
	polls: number;
	status: 'running' | 'done' | 'failed' | 'noop';
	successor: string | null;
}

const jobs = new Map<string, MockJob>();
let jobCounter = 0;
/** Per-session count of simulated "compacting" 409s already served. */
const compactingRefusals = new Map<string, number>();

function delay(ms: number, signal?: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		if (signal?.aborted) {
			reject(abortError());
			return;
		}
		const timer = setTimeout(() => {
			signal?.removeEventListener('abort', onAbort);
			resolve();
		}, ms);
		const onAbort = () => {
			clearTimeout(timer);
			reject(abortError());
		};
		signal?.addEventListener('abort', onAbort, { once: true });
	});
}

function abortError(): Error {
	const error = new Error('aborted');
	error.name = 'AbortError';
	return error;
}

function chunk(text: string, size: number): string[] {
	const parts: string[] = [];
	for (let i = 0; i < text.length; i += size) parts.push(text.slice(i, i + size));
	return parts;
}

/** Scripted events for one turn: reasoning, then prose + a DSL block, then done. */
function buildScript(content: string, kind: TurnKind): StreamEvent[] {
	const events: StreamEvent[] = [
		{ type: 'reasoning', delta: 'Reading the last beat of the scene…' },
		{ type: 'reasoning', delta: ' picking an image, then the prose.' }
	];

	let text: string;
	if (kind === 'ooc') {
		text = `[OOC] Noted — “${content}”. I'll park that outside the fiction for now.`;
	} else {
		const prose = `The rain keeps its slow argument with the shutters. You weigh “${content}” and the room leans in to listen.`;
		// Typing "broken"/"malformed" emits a deliberately unparseable payload so the
		// chat pane's raw-text fallback is demonstrable without the backend.
		const dsl = /broken|malformed/i.test(content)
			? [
					'root = ScenePane("Broken scene", [ghost])',
					'ghost = UnknownComponent("not in the library")'
				].join('\n')
			: [
					'root = ScenePane("Rusty Anchor Tavern — Evening", [meter, beat, artifact])',
					'meter = MeterBar("Tension", 68, 100)',
					'beat = BeatCard("The innkeeper slides a brass key across the counter and does not meet your eyes.")',
					'artifact = ArtifactCard("Cursed Compass", "Quest item", "A tarnished brass compass whose needle points at whoever last lied to you.")'
				].join('\n');
		text = `${prose}\n\`\`\`openui\n${dsl}\n\`\`\`\n`;
	}

	for (const piece of chunk(text, 24)) events.push({ type: 'content', delta: piece });
	events.push({ type: 'done', meta: { mock: true, kind } });
	return events;
}

interface PlayResult {
	status: 'done' | 'stopped' | 'dropped';
	cursor: number;
	meta?: Record<string, unknown>;
}

async function play(
	script: StreamEvent[],
	from: number,
	dropAt: number,
	handlers: StreamTurnHandlers
): Promise<PlayResult> {
	let cursor = from;
	try {
		while (cursor < script.length) {
			await delay(24, handlers.signal);
			if (handlers.signal?.aborted) return { status: 'stopped', cursor };
			if (cursor === dropAt) return { status: 'dropped', cursor: cursor + 1 };

			const event = script[cursor++];
			if (event.type === 'reasoning') handlers.onReasoning?.(event.delta ?? '', event.meta);
			else if (event.type === 'content') handlers.onContent?.(event.delta ?? '', event.meta);
			else if (event.type === 'done') return { status: 'done', cursor, meta: event.meta };
			else if (event.type === 'error') {
				handlers.onError?.(new StreamError(event.delta ?? 'mock error', event.meta), event.meta);
				return { status: 'done', cursor };
			}
		}
		return { status: 'done', cursor, meta: undefined };
	} catch (err) {
		if (handlers.signal?.aborted || (err as { name?: string })?.name === 'AbortError') {
			return { status: 'stopped', cursor };
		}
		throw err;
	}
}

function advanceFacts(sessionId: string): void {
	const session = sessions.get(sessionId);
	if (!session) return;
	session.turns += 1;
	session.facts.time = TIMES[Math.min(session.turns, TIMES.length - 1)];
	session.facts.tone = session.turns % 2 === 1 ? 'warm→wary' : 'warm';
	session.facts.inv = session.turns >= 1 ? '+brass key' : '';
}

/**
 * Append one finished turn's rows to a chain, so the delayed projection refetch
 * (§2.2, ~5 s after `done`) rebuilds the turn exactly like a reload would. The
 * mock has no event log; without this a turn vanished from the mock demo five
 * seconds after it streamed.
 */
function appendTurn(sessionId: string, content: string, kind: TurnKind): void {
	let chain = CHAINS.get(sessionId);
	if (!chain) {
		chain = {
			facts: { ...(sessions.get(sessionId)?.facts ?? INITIAL_FACTS) },
			user_messages: [],
			narratives: [],
			context_lows: [],
			compactions: [],
			recap: []
		};
		CHAINS.set(sessionId, chain);
	}
	const rows = [...chain.user_messages, ...chain.narratives];
	const maxSeq = Math.max(
		0,
		...rows.map((row) => row.seq),
		...chain.context_lows.map((row) => row.seq),
		...chain.compactions.map((row) => row.seq)
	);
	const maxId = Math.max(0, ...rows.map((row) => row.id));
	chain.user_messages.push({
		id: maxId + 1,
		seq: maxSeq + 1,
		type: 'user',
		payload: { text: content, kind }
	});
	chain.narratives.push({
		id: maxId + 2,
		seq: maxSeq + 2,
		type: 'narrative',
		payload: {
			prose:
				kind === 'ooc'
					? `[OOC] Noted — “${content}”.`
					: `The room leans in to listen. You said: “${content}”, and the rain keeps its slow argument with the shutters.`
		}
	});
	chain.facts = { ...(sessions.get(sessionId)?.facts ?? chain.facts) };
}

/** The projection body for one seeded chain (or an empty live-created chat). */
function projectionFor(sessionId: string): Projection {
	const chain = CHAINS.get(sessionId);
	const session = sessions.get(sessionId);
	if (!chain) {
		return {
			narratives: [],
			user_messages: [],
			rolls: [],
			assets: [],
			notices: [],
			context_lows: [],
			compactions: [],
			state: { ...(session?.facts ?? INITIAL_FACTS) }
		};
	}
	const item = (row: MockRow) => ({ id: row.id, seq: row.seq, type: row.type, payload: row.payload });
	return {
		narratives: chain.narratives.map(item),
		user_messages: chain.user_messages.map(item),
		rolls: [],
		assets: [],
		notices: [],
		context_lows: chain.context_lows.map((row) => ({ ...row, payload: { ...row.payload } })),
		compactions: chain.compactions.map((row) => ({ ...row, payload: { ...row.payload } })),
		state: { ...chain.facts }
	};
}

export const mockApi = {
	async createSession(init: Record<string, unknown> = {}): Promise<string> {
		void init;
		const id = `mock-${Math.random().toString(36).slice(2, 8)}`;
		await delay(60);
		sessions.set(id, { facts: { ...INITIAL_FACTS }, turns: 0 });
		return id;
	},

	async getProjection(sessionId: string): Promise<Projection> {
		await delay(40);
		return projectionFor(sessionId);
	},

	/** W9.26: the seeded branch payloads (null = no fork support for this chat). */
	async getBranches(
		sessionId: string,
		selfCut: number | null = null
	): Promise<Record<string, unknown> | null> {
		void selfCut;
		await delay(20);
		return CHAINS.get(sessionId)?.branches ?? null;
	},

	/** W9.26: chain-aware recap rows (the break notice's raw-data expander). */
	async getRecap(sessionId: string): Promise<{ recap: Array<Record<string, unknown>> }> {
		await delay(20);
		return { recap: CHAINS.get(sessionId)?.recap ?? [] };
	},

	/** W9.26: begin a compaction job; `getCompactionJob` finishes it after 2 polls. */
	async startCompaction(
		sessionId: string
	): Promise<{ job_id: string } | { error: string; reason?: string; successorId?: string }> {
		await delay(80);
		jobCounter += 1;
		const jobId = `mock-job-${jobCounter}`;
		const successor =
			sessionId === MOCK_SEED_SESSIONS.eraNext ? null : MOCK_SEED_SESSIONS.eraNext;
		jobs.set(jobId, { sessionId, polls: 0, status: 'running', successor });
		return { job_id: jobId };
	},

	/** W9.26: poll a compaction job (running → done; done carries the successor). */
	async getCompactionJob(sessionId: string, jobId: string): Promise<CompactionJob> {
		await delay(30);
		const job = jobs.get(jobId);
		if (!job) return { job_id: jobId, status: 'failed', error: 'unknown mock job' };
		job.polls += 1;
		if (job.polls >= 2) job.status = job.successor ? 'done' : 'noop';
		const done = job.status === 'done';
		return {
			job_id: jobId,
			status: job.status,
			...(done && job.successor ? { session_id: job.successor } : {}),
			...(done ? { title: 'Rusty Anchor — two eras' } : {}),
			...(job.status === 'noop'
				? { error: 'nothing uncovered — the era is already compacted' }
				: {})
		};
	},

	/** No-op: the caller's AbortController already stops the scripted stream. */
	async stopTurn(_sessionId: string): Promise<void> {},

	async streamTurn(
		sessionId: string,
		content: string,
		kind: TurnKind,
		handlers: StreamTurnHandlers = {}
	): Promise<StreamOutcome> {
		// W9.26 409 demos, mirroring the engine's two structured refusals.
		if (/!compacting/i.test(content)) {
			const seen = compactingRefusals.get(sessionId) ?? 0;
			compactingRefusals.set(sessionId, seen + 1);
			if (seen < 1) {
				const error = new StreamError('compacting', { status: 409, reason: 'compacting' });
				handlers.onError?.(error);
				return { status: 'error', error };
			}
		}
		if (/!superseded-hard/i.test(content)) {
			// The successor refuses too: drives the transitive-follow LIMIT and the
			// replay-failure path (composer restored, error surfaced — no silent loss).
			const error = new StreamError('superseded', {
				status: 409,
				reason: 'superseded',
				id: MOCK_SEED_SESSIONS.eraNext
			});
			handlers.onError?.(error);
			return { status: 'error', error };
		}
		if (
			/!superseded/i.test(content) &&
			sessionId !== MOCK_SEED_SESSIONS.eraNext &&
			sessionId !== MOCK_SEED_SESSIONS.swipeAlt
		) {
			const error = new StreamError('superseded', {
				status: 409,
				reason: 'superseded',
				id: MOCK_SEED_SESSIONS.eraNext
			});
			handlers.onError?.(error);
			return { status: 'error', error };
		}

		const script = buildScript(content, kind);
		// Typing "reconnect" drops the stream once mid-turn so the UI exercises
		// the same one-retry resume path the real client uses.
		const dropAt = /reconnect/i.test(content) && kind === 'in_character' ? Math.max(1, script.length - 3) : -1;
		let cursor = 0;

		for (let attempt = 0; attempt <= 1; attempt++) {
			const result = await play(script, cursor, attempt === 0 ? dropAt : -1, handlers);
			cursor = result.cursor;
			if (result.status === 'stopped') return { status: 'stopped' };
			if (result.status === 'done') {
				advanceFacts(sessionId);
				appendTurn(sessionId, content, kind);
				handlers.onDone?.(result.meta);
				return { status: 'done', meta: result.meta };
			}
			// status 'dropped' -> one resume attempt
		}

		const error = new StreamError('mock stream dropped before done');
		handlers.onError?.(error);
		return { status: 'error', error };
	}
};
