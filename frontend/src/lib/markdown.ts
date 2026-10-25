// Full markdown rendering for non-DSL transcript prose.
//
// One parse (marked, GFM, breaks ON) followed by ONE sanitize step (DOMPurify).
//
// `breaks: true` maps a single newline to `<br>`. That is the chat-semantics
// choice: users type single newlines inside a message, and the transcript sinks
// (`.prose`, `.opening-text`) no longer use `white-space: pre-wrap` — without
// `breaks` those newlines would collapse into spaces. Block spacing is now
// owned by CSS margins instead of literal `\n` text nodes + pre-wrap.
//
// Both steps run only in the browser: DOMPurify needs a DOM, so on the server we
// fall back to escaped plain text. The transcript is client-loaded (its store
// starts empty), so SSR never renders markdown output anyway.

import DOMPurify from 'dompurify';
import { marked } from 'marked';

/** HTML-escape a plain string — the server/SSR fallback (no DOM to sanitize). */
function escapeHtml(text: string): string {
	return text
		.replace(/&/g, '&amp;')
		.replace(/</g, '&lt;')
		.replace(/>/g, '&gt;')
		.replace(/"/g, '&quot;')
		.replace(/'/g, '&#39;');
}

/**
 * Sanitize parsed markdown HTML.
 *
 * THE ONE SEAM: this is the single place to change if the model is ever allowed
 * to write HTML directly (e.g. a future setting passing ALLOWED_TAGS to
 * DOMPurify, or returning `html` unchanged). Nothing else in the app assumes
 * this output is sanitized, so relaxing it here is the whole change.
 */
function sanitizeHtml(html: string): string {
	return DOMPurify.sanitize(html);
}

/**
 * Render `text` as markdown HTML for a `{@html …}` sink.
 *
 * Empty input renders nothing (plain-when-empty). `marked.parse` is given
 * `async: false`, so it returns a string synchronously.
 */
export function renderMarkdown(text: string): string {
	if (!text) return '';
	if (typeof window === 'undefined') return escapeHtml(text);
	const html = marked.parse(text, { gfm: true, breaks: true, async: false });
	return sanitizeHtml(html);
}
