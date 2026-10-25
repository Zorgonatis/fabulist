// Fabulist API client - the owner-curated decision-query asset (R25). ---------
//
// The per-turn decision gate asks this bank (18 validated anti-slop phrase
// questions + the narrative-movement question today, `src/fabulist/decision.py`)
// and feeds each fired answer to the critic as a precise issue. R25 makes the
// bank an ASSET: the phrase list is freely curatable and custom questions need no
// code change.
//
// Wire contract (`GET/PUT /api/decision-queries`, backend lane R25-A):
//
//   GET {API_BASE}/api/decision-queries
//     -> {questions: [DecisionQuestion], seeded: bool}
//        `seeded` is true while the asset is EMPTY and the built-in default is
//        served; the first PUT makes the set explicit (`seeded: false`).
//   PUT {API_BASE}/api/decision-queries   body {questions: [DecisionQuestion]}
//     -> the same envelope; 400 {"detail": "<message>"} on a validation failure.
//
// Field names are the engine's own decision-call names (`type`, `instructions`,
// `criteria`) so the backend builds its question map without a rename layer. Three
// asset-only fields ride along: `source` (provenance — the pane groups by it),
// `phrase` (a slop row is BOUND to its phrase: `id` is `slugify(phrase)` and the
// instruction is generated from it, matching `decision.slop_questions()`), and
// `section` (which GATE consumes the row — W9.22 §4a: the per-turn critic gate owns
// rows without one, `proposal` is the proposal stage's two questions). `section` is
// preserved by `normalizeQuestion` on purpose: the bank is written back WHOLE, so a
// pane save that dropped it would silently re-route the proposal rows into the
// per-turn ask (backend `decision_queries.questions_for_section`).
//
// Criteria rule that mirrors the engine (`decision_queries.validate_questions`):
// `noul` criteria are OPTIONAL (the validated slop questions carry none —
// "does the text contain X" needs no rubric); when present, BOTH "true" and
// "false" must be non-empty. `choice` needs at least one label with a non-empty
// description.

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

export type DecisionQuestionType = 'noul' | 'choice';

/** Provenance: a row bound to an anti-slop phrase, or an owner-written question. */
export type DecisionQuestionSource = 'slop' | 'custom';

/**
 * Which answer counts as the defect: `true_is_defect` fires when
 * `P(true) >= threshold` (slop-style, "does the text contain X"), while
 * `false_is_defect` fires when `P(true) < threshold` (movement-style, "does the
 * turn advance").
 */
export type DecisionPolarity = 'true_is_defect' | 'false_is_defect';

/**
 * The GATE that consumes a row (backend `decision_queries.PER_TURN_SECTION` /
 * `PROPOSAL_SECTION`). Absent means the per-turn critic gate, which is what every
 * row written before W9.22 is (no migration).
 */
export type DecisionQuestionSection = 'slop' | 'proposal';

/** One question in the asset. */
export interface DecisionQuestion {
	id: string;
	source: DecisionQuestionSource;
	type: DecisionQuestionType;
	instructions: string;
	criteria: Record<string, string>;
	/** Fire point, 0..1. */
	threshold: number;
	enabled: boolean;
	polarity: DecisionPolarity;
	/** Slop rows only: the phrase text; `id` is `slugifyPhrase(phrase)`. */
	phrase?: string;
	/**
	 * Which gate consumes the row; absent = the per-turn critic gate. The bank is
	 * PUT whole, so this must survive a load/save round trip.
	 */
	section?: DecisionQuestionSection;
}

/** The asset envelope both endpoints return. */
export interface DecisionQueryCatalog {
	questions: DecisionQuestion[];
	seeded: boolean;
}

/** Endpoint paths (kept in one place so the orchestrator can rewire them). */
export const DECISION_QUERY_ENDPOINTS = {
	asset: (): string => `${API_BASE}/api/decision-queries`
} as const;

/** Fallback fire point for a row that arrives without a usable threshold. */
export const DEFAULT_THRESHOLD = 0.5;

/** The 18-phrase bank, verbatim from `decision.SLOP_PHRASES` (phrase half). */
export const SLOP_PHRASES: readonly string[] = [
	'shivers down her spine',
	'the air was thick with',
	'breath catching',
	'torn between',
	'the world narrowed',
	'predatory grin',
	'analytical gaze',
	'barely a whisper',
	'pregnant pause',
	'the silence stretched',
	'for a long moment',
	"didn't look back",
	'economical movements',
	'stark contrast',
	'the night is young',
	'playful challenge',
	'everything else faded',
	'a soft blur'
];

/**
 * A stable question id for a phrase: lowercase, non-alphanumerics -> `_`
 * (mirrors `decision.slugify` verbatim, so ids match the engine's answers).
 */
export function slugifyPhrase(phrase: string): string {
	return phrase
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, '_')
		.replace(/^_+|_+$/g, '');
}

/**
 * The validated slop-question wording (verbatim from
 * `decision.slop_questions()`); rewording invalidates the measured probabilities.
 */
export function slopInstructions(phrase: string): string {
	return `Does the text contain the exact phrase "${phrase}" or a trivial variant of it?`;
}

/** The narrative-movement question, verbatim from `decision.MOVEMENT_QUESTION`. */
export const MOVEMENT_QUESTION: Readonly<DecisionQuestion> = {
	id: 'movement',
	source: 'custom',
	type: 'noul',
	instructions: 'Does this turn drive the narrative forward?',
	criteria: {
		true: 'Something changes: new information surfaces, a consequence lands, a decision point opens, or the situation materially shifts.',
		false: 'The turn only restates what is known, replays an emotion, or holds the same standoff — nothing changes.'
	},
	threshold: DEFAULT_THRESHOLD,
	enabled: true,
	polarity: 'false_is_defect'
};

/**
 * The built-in default set: one `noul` per slop phrase (defect when the phrase is
 * present) plus the movement question (defect when the turn does NOT advance).
 * Mirrors `decision.turn_gate_questions()`.
 */
export function seededDecisionQueries(): DecisionQuestion[] {
	const slop = SLOP_PHRASES.map((phrase) => ({
		id: slugifyPhrase(phrase),
		source: 'slop' as const,
		type: 'noul' as const,
		instructions: slopInstructions(phrase),
		criteria: {},
		threshold: DEFAULT_THRESHOLD,
		enabled: true,
		polarity: 'true_is_defect' as const,
		phrase
	}));
	return [...slop, { ...MOVEMENT_QUESTION, criteria: { ...MOVEMENT_QUESTION.criteria } }];
}

/** Mock-mode asset; `null` = the asset is empty (GET serves the seeded default). */
let mockQuestions: DecisionQuestion[] | null = null;

/** One threshold, coerced into 0..1 with the engine default as fallback. */
function thresholdOf(value: unknown, fallback = DEFAULT_THRESHOLD): number {
	return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
		? value
		: fallback;
}

/** Coerce one raw wire question, or null when it has no usable id. */
function normalizeQuestion(entry: unknown): DecisionQuestion | null {
	if (!entry || typeof entry !== 'object') return null;
	const e = entry as Record<string, unknown>;
	const id = str(e.id).trim();
	if (!id) return null;
	const source: DecisionQuestionSource = e.source === 'slop' ? 'slop' : 'custom';
	const criteria: Record<string, string> = {};
	if (e.criteria && typeof e.criteria === 'object' && !Array.isArray(e.criteria)) {
		for (const [key, value] of Object.entries(e.criteria as Record<string, unknown>)) {
			if (typeof value === 'string') criteria[key] = value;
		}
	}
	const question: DecisionQuestion = {
		id,
		source,
		type: e.type === 'choice' ? 'choice' : 'noul',
		instructions: str(e.instructions),
		criteria,
		threshold: thresholdOf(e.threshold),
		enabled: e.enabled !== false,
		polarity: e.polarity === 'false_is_defect' ? 'false_is_defect' : 'true_is_defect'
	};
	if (source === 'slop') question.phrase = str(e.phrase);
	// Preserve the gate binding (W9.22 §4a). An unknown value is dropped exactly
	// like any other unknown key — the write door refuses it server-side anyway.
	if (e.section === 'slop' || e.section === 'proposal') question.section = e.section;
	return question;
}

/** Normalize the `{questions, seeded}` envelope. */
export function normalizeDecisionQueryCatalog(raw: Record<string, unknown>): DecisionQueryCatalog {
	const source = Array.isArray(raw.questions) ? raw.questions : [];
	const questions: DecisionQuestion[] = [];
	for (const entry of source) {
		const question = normalizeQuestion(entry);
		if (question) questions.push(question);
	}
	return { questions, seeded: raw.seeded === true };
}

function cloneQuestions(questions: readonly DecisionQuestion[]): DecisionQuestion[] {
	return questions.map((question) => ({
		...question,
		criteria: { ...question.criteria }
	}));
}

/**
 * The asset's validation rules, in one place: the pane checks them before a PUT
 * (inline feedback) and the mock throws the same 400 the real endpoint would.
 * Mirrors `fabulist.decision_queries.validate_questions` exactly — in
 * particular a noul row carries EITHER both rubric sides OR none (`{}`), which
 * is what keeps the seeded slop payload byte-identical to the measured one.
 */
export function questionSetError(questions: readonly DecisionQuestion[]): string | null {
	const seen = new Set<string>();
	for (const [index, question] of questions.entries()) {
		const id = question.id.trim();
		const label = id ? `“${id}”` : `question ${index + 1}`;
		if (!id) return `${label}: the id is empty — it is the name the critic's flag carries.`;
		if (seen.has(id)) return `${label}: duplicate id — every question needs its own id.`;
		seen.add(id);
		if (question.source === 'slop') {
			const phrase = (question.phrase ?? '').trim();
			if (!phrase) return `${label}: enter the anti-slop phrase.`;
			const derived = slugifyPhrase(phrase);
			if (derived !== id) return `${label}: a phrase row's id must be the phrase slug “${derived}”.`;
		}
		if (!question.instructions.trim()) return `${label}: enter the instruction.`;
		if (question.type === 'choice') {
			const options = Object.entries(question.criteria);
			if (options.length === 0) return `${label}: a choice question needs at least one option.`;
			for (const [option, description] of options) {
				if (!option.trim()) return `${label}: option labels must be non-empty.`;
				if (!description.trim()) return `${label}: the “${option}” option needs a description.`;
			}
		} else if (Object.keys(question.criteria).length > 0) {
			for (const side of ['true', 'false']) {
				if (!(question.criteria[side] ?? '').trim()) {
					return `${label}: a noul rubric needs both “true” and “false” text — or none at all.`;
				}
			}
		}
		if (!Number.isFinite(question.threshold) || question.threshold < 0 || question.threshold > 1) {
			return `${label}: the threshold must be between 0 and 1.`;
		}
	}
	return null;
}

/** One endpoint failure as the pane displays it (the API's own reason). */
async function failure(res: Response, call: string): Promise<StreamError> {
	const detail = await readErrorDetail(res);
	return new StreamError(detail.reason || `${call} failed: ${res.status} ${res.statusText}`, {
		...detail,
		status: res.status
	});
}

/** GET the effective asset (the seeded default while the asset is empty). */
export async function getDecisionQueries(): Promise<DecisionQueryCatalog> {
	if (USE_MOCK) {
		return mockQuestions
			? { questions: cloneQuestions(mockQuestions), seeded: false }
			: { questions: seededDecisionQueries(), seeded: true };
	}
	const res = await fetch(DECISION_QUERY_ENDPOINTS.asset(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) throw await failure(res, 'getDecisionQueries');
	return normalizeDecisionQueryCatalog(await readJson(res));
}

/**
 * PUT the whole asset. The response is the stored set (`seeded: false`), so the
 * pane re-seeds its draft from what the server actually kept.
 */
export async function putDecisionQueries(
	questions: readonly DecisionQuestion[]
): Promise<DecisionQueryCatalog> {
	const body = { questions: cloneQuestions(questions) };
	if (USE_MOCK) {
		const problem = questionSetError(body.questions);
		if (problem) throw new StreamError(problem, { reason: problem, status: 400 });
		mockQuestions = body.questions;
		return { questions: cloneQuestions(mockQuestions), seeded: false };
	}
	const res = await fetch(DECISION_QUERY_ENDPOINTS.asset(), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await failure(res, 'putDecisionQueries');
	return normalizeDecisionQueryCatalog(await readJson(res));
}
