// Display-time defence for the assistant panel's HISTORY rows.
//
// The event log is append-only (D9/D10): `assistant_reply` rows logged BEFORE
// the engine split the card out of the reply text still hold the raw
// `asst = AssistantCard(...)` statement inside `text`. The transcript never
// shows them (its segmenter strips component statements), but the panel's
// history renders `reply.text` verbatim, so legacy rows displayed raw DSL.
// The log is never rewritten — this helper strips the statement at DISPLAY
// time instead.
//
// The rules mirror the engine splitter (`fabulist.assistants.panel`
// `split_assistant_reply` + `_clean_prose`) so a row reads the same whether it
// was sanitized engine-side at capture or display-side here:
//   * fences are transparent (`\`\`\`openui` markers never hide a statement);
//   * a bare `asst = AssistantCard(` call is located anywhere in the prose;
//   * the call closes on its matching `)`, quote/escape-aware and nesting-aware
//     for `()`/`[]`/`{}`, and may span lines;
//   * ALL located spans are removed (a reply with two statements leaks neither);
//   * an unclosed call consumes to the end of the text;
//   * a dangling trailing `asst =` fragment (truncated emission) is dropped;
//   * fence markers left behind are removed, runs of blank lines collapse to a
//     single blank line, and the result is trimmed.

/** Locates an `asst = AssistantCard(` call inside a reply. */
const CARD_CALL = /\basst\s*=\s*AssistantCard\s*\(/g;

/** A trailing, half-emitted `asst =` fragment (truncated statement). */
const DANGLING_ASST = /\basst\s*=\s*[A-Za-z]*\s*\(?\s*$/;

/** Backtick fence markers (``` / ```openui) — stripped like the engine does. */
const FENCE = /```[A-Za-z0-9_+-]*/g;

/**
 * Index of the `)` matching the `(` at `openIndex`, or `-1` when unclosed.
 * Quote- and escape-aware (a paren inside a string literal never counts) and
 * nesting-aware for `()`/`[]`/`{}`.
 */
function closeIndex(text: string, openIndex: number): number {
	let depth = 0;
	let inString = false;
	let escape = false;
	for (let i = openIndex; i < text.length; i++) {
		const ch = text[i];
		if (inString) {
			if (escape) escape = false;
			else if (ch === '\\') escape = true;
			else if (ch === '"') inString = false;
			continue;
		}
		if (ch === '"') inString = true;
		else if (ch === '(' || ch === '[' || ch === '{') depth++;
		else if (ch === ')' || ch === ']' || ch === '}') {
			depth--;
			if (depth === 0) return i;
		}
	}
	return -1;
}

/** Collapse the whitespace a lifted statement leaves (engine `_clean_prose`). */
function cleanProse(text: string): string {
	const cleaned: string[] = [];
	let blank = false;
	for (const rawLine of text.replace(FENCE, '').split('\n')) {
		const line = rawLine.replace(/\s+$/, '');
		if (line.trim()) {
			cleaned.push(line);
			blank = false;
		} else if (!blank) {
			cleaned.push('');
			blank = true;
		}
	}
	return cleaned.join('\n').trim();
}

/**
 * Strip every `asst = AssistantCard(...)` span from `text`, leaving prose only.
 *
 * Returns the input unchanged when it holds no statement, so a clean reply is
 * byte-identical. Apply at display time to legacy log rows — never to the log.
 */
export function stripAssistantStatement(text: string): string {
	if (typeof text !== 'string' || !text.trim()) return text;

	const spans: Array<[number, number]> = [];
	CARD_CALL.lastIndex = 0;
	let match: RegExpExecArray | null;
	while ((match = CARD_CALL.exec(text)) !== null) {
		const close = closeIndex(text, match.index + match[0].length - 1);
		spans.push([match.index, close === -1 ? text.length : close + 1]);
	}
	if (!spans.length) {
		const dangling = DANGLING_ASST.exec(text);
		if (!dangling) return text;
		return cleanProse(text.slice(0, dangling.index));
	}

	let out = '';
	let cursor = 0;
	for (const [start, end] of spans) {
		if (start < cursor) continue; // consumed by an earlier (unclosed) span
		out += text.slice(cursor, start);
		cursor = end;
	}
	out += text.slice(cursor);
	return cleanProse(out);
}
