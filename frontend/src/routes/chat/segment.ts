// Split a narrator stream into prose and OpenUI DSL.
//
// v0.5 — statement extraction, not range capture. Two forms are recognised:
//   1. an explicit fence:  ```openui ... ```
//   2. unfenced component statements: `id = Component(...)`, extracted
//      individually (balanced parens, may span lines and interleave with
//      prose). Prose is NEVER swallowed by a DSL region — the v0
//      "DSL runs to the end" heuristic ate real prose when narrators
//      emitted panels before or between paragraphs.
// v0.7 — fence bodies are split per STATEMENT (mirroring the backend
// `_fence_segments`), so every dsl segment is a single statement on both
// paths. The chat pane mounts one renderer per statement at its prose anchor.
//
// Recomputed on every content delta, so it works while the stream is partial
// (an unbalanced statement is treated as in-progress DSL).
//
// v0.6 — `segments`: the same extraction, but kept in DOCUMENT ORDER so the
// chat pane can interleave panels with the prose they anchor to instead of
// stacking every panel after the whole prose block. `prose` / `dsl` /
// `dslStarted` are unchanged (byte-for-byte) — callers that only need the
// flat channels keep working.
// v0.8 — the recognized component names are DERIVED from the render registry
// itself (`library.components`), longest-name-first, mirroring the backend's
// `component_names_longest_first()`. The previous hand list had drifted — it
// stranded Monitor/Phone/SMS/PostIt/SocialPost/Custom statements as raw prose
// during a live stream (they drew after a refresh, when the backend segmenter
// — which derives from the same spec — supplied the segments).

import { library } from '#lib/openui/library';
import type { NarratorSegment } from '#lib/stores';

export interface Segmented {
	prose: string;
	dsl: string;
	/** True once a DSL region has been detected (used to show the DSL block). */
	dslStarted: boolean;
	/** Ordered prose/DSL pieces (document order); empty when nothing parsed yet. */
	segments: NarratorSegment[];
}

const OPEN_FENCE = /```(?:openui|ui)\b[ \t]*\r?\n/;
// Derived from the render registry, never a hand list: a hand-maintained copy
// drifts the moment a component is registered (v0.8 live defect). Longest-first
// (then codepoint order) mirrors the backend `component_names_longest_first()`
// so `Artifact` cannot win the alternation over `ArtifactCard`.
const COMPONENTS = Object.keys(library.components).sort(
	(a, b) => b.length - a.length || (a < b ? -1 : a > b ? 1 : 0)
);
// Assignment is OPTIONAL: a live narrator emitted bare calls
// (`DialogueSpotlight("Hobbs", …)`) and every such line fell into prose, so
// the panel never rendered (owner report 2026-10-17). The parser still needs
// an identifier, which DslFallback supplies at mount.
const STMT = new RegExp(
	`^\\s*(?:[A-Za-z_]\\w*\\s*=\\s*)?(?:${COMPONENTS.join('|')})\\s*\\(`
);

function parens(line: string): number {
	let depth = 0;
	for (const ch of line) {
		if (ch === '(') depth++;
		else if (ch === ')') depth--;
	}
	return depth;
}

/** Ordered prose/DSL runs over statement-bearing text (no fences).
 *
 * Consecutive prose lines share one run; each balanced-paren component
 * statement is its own run. Shared by the unfenced path and by fence bodies
 * (where every run is coerced to `dsl`) so both segmenters agree with the
 * backend's `segment_narrative` — one dsl segment PER STATEMENT. */
function runsOf(text: string): Array<{ kind: 'prose' | 'dsl'; lines: string[] }> {
	const lines = text.split(/\r?\n/);
	const runs: Array<{ kind: 'prose' | 'dsl'; lines: string[] }> = [];
	let i = 0;
	while (i < lines.length) {
		if (STMT.test(untick(lines[i]))) {
			const stmt: string[] = [];
			let depth = 0;
			while (i < lines.length) {
				const inner = untick(lines[i]);
				stmt.push(inner);
				depth += parens(inner);
				i++;
				if (depth <= 0) break; // statement closed (or in-progress at stream end)
			}
			runs.push({ kind: 'dsl', lines: stmt });
		} else {
			const proseRun: string[] = [];
			while (i < lines.length && !STMT.test(untick(lines[i]))) {
				proseRun.push(lines[i]);
				i++;
			}
			runs.push({ kind: 'prose', lines: proseRun });
		}
	}
	return runs;
}

/** Collapse runs of blank lines and trim a prose fragment for display. */
function cleanProse(text: string): string {
	return text.replace(/\n{3,}/g, '\n\n').trim();
}

function untick(line: string): string {
	// Markdown inline-code statements (`card = ObjectCard(...)`) extract
	// cleanly — live defect: the narrator copies the doc's formatting.
	const t = line.trim();
	if (t.length >= 2 && t.startsWith('`') && t.endsWith('`')) {
		const inner = t.replace(/^`+|`+$/g, '').trim();
		if (!inner.includes('`')) return inner;
	}
	return line;
}

export function splitProseAndDsl(raw: string): Segmented {
	const fence = OPEN_FENCE.exec(raw);
	if (fence) {
		// Flat channels keep their v0.5 meaning: prose is everything before the
		// first fence; dsl is the body between the first opener and the last
		// closer (a second fence, if any, stays inside the dsl text).
		const prose = raw.slice(0, fence.index).trimEnd();
		const rest = raw.slice(fence.index + fence[0].length);
		const close = rest.lastIndexOf('```');
		const dsl = (close === -1 ? rest : rest.slice(0, close)).trim();

		// Segments stay in document order: one dsl segment per STATEMENT inside
		// each fence (mirroring the backend `_fence_segments` — non-statement text
		// stays dsl too), with the prose around and between fences kept as prose
		// segments. Per-statement granularity is what lets the chat pane mount one
		// renderer per statement at its prose anchor; it also keeps validator flag
		// `segment_index`es aligned with the backend segmenter.
		const segments: NarratorSegment[] = [];
		let pos = 0;
		for (;;) {
			const open = OPEN_FENCE.exec(raw.slice(pos));
			if (!open) break;
			const openIdx = pos + open.index;
			const lead = cleanProse(raw.slice(pos, openIdx));
			if (lead) segments.push({ kind: 'prose', text: lead });
			const bodyStart = openIdx + open[0].length;
			const closeIdx = raw.indexOf('```', bodyStart);
			const bodyEnd = closeIdx === -1 ? raw.length : closeIdx;
			const body = raw.slice(bodyStart, bodyEnd).trim();
			if (body) {
				const inner = runsOf(body)
				.map((run) => ({ text: run.lines.join('\n').trim() }))
				.filter((run) => run.text.length > 0);
				if (inner.length === 0) inner.push({ text: body });
				for (const run of inner) segments.push({ kind: 'dsl', text: run.text });
			}
			if (closeIdx === -1) {
				pos = raw.length;
				break;
			}
			pos = closeIdx + 3;
		}
		const tail = cleanProse(raw.slice(pos));
		if (tail) segments.push({ kind: 'prose', text: tail });
		return { prose, dsl, dslStarted: true, segments };
	}

	// Group the stream into ordered runs: consecutive prose lines share one
	// run; each balanced-paren component statement is its own run. Prose and
	// DSL runs are then flattened exactly the way v0.5 did. Statement lines
	// wholly wrapped in markdown code backticks are unticked first (live
	// defect: the narrator copies the doc's example formatting).
	const runs = runsOf(raw);

	const proseOut: string[] = [];
	for (const run of runs) if (run.kind === 'prose') proseOut.push(...run.lines);
	const dslOut = runs.filter((run) => run.kind === 'dsl').map((run) => run.lines.join('\n'));

	const prose = proseOut.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd();
	const dsl = dslOut.join('\n').trim();

	const segments: NarratorSegment[] = [];
	for (const run of runs) {
		const text =
			run.kind === 'prose'
				? run.lines.join('\n').replace(/\n{3,}/g, '\n\n').trim()
				: run.lines.join('\n').trim();
		if (text) segments.push({ kind: run.kind, text });
	}

	return { prose, dsl, dslStarted: dslOut.length > 0, segments };
}
