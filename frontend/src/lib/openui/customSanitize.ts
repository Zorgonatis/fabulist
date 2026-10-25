// Custom (raw-HTML escape hatch) sanitize seam — THE one place that decides what
// narrator-authored HTML/CSS may reach a Custom artifact. Mirrors `markdown.ts`
// as the app's other single sanitize seam: one step, one config object, and this
// comment saying that relaxing safety is a one-line change here.
//
// Browser-only. DOMPurify needs a live DOM, so on the server
// (`typeof window === 'undefined'`) `sanitizeCustomHtml` returns '' and the
// caller (Custom.svelte) renders the paper-scrap fallback instead — SSR never
// emits sanitized markup. `scrubCustomCss` is pure string work and runs
// anywhere.
//
// Policy anchors (the maintainer's private notes (unshipped)):
//  - external resources: only `data:` image/font URIs and app asset endpoints
//    (`/api/...`) survive; remote http(s) URLs are stripped.
//  - `<style>` is FORBIDDEN inside `html`. The `css` prop is the whole style
//    channel: it is scrubbed here and injected as a `<style>`'s *textContent*,
//    never parsed as markup.
//  - no scripts, no iframes/objects/embeds, no forms/inputs, no event handlers.

import DOMPurify from 'dompurify';

/** URI schemes/endpoints allowed in `src`/`href` and CSS `url()`: data images/fonts + app assets. */
const ALLOWED_URI_REGEXP = /^(?:data:(?:image|font)\/[a-z0-9.+-]+|\/api\/)/i;

const CUSTOM_HTML_CONFIG = {
	ALLOWED_URI_REGEXP,
	ALLOW_DATA_ATTR: false,
	FORBID_TAGS: [
		'script',
		'style',
		'iframe',
		'object',
		'embed',
		'link',
		'meta',
		'form',
		'input',
		'button'
	],
	ALLOWED_ATTR: [
		'class',
		'style',
		'title',
		'alt',
		'src',
		'href',
		'colspan',
		'rowspan',
		'width',
		'height',
		'align'
	]
};

/** A CSS `url(...)` call, capturing its quote ('' when unquoted) and its target. */
const CSS_URL_CALL_RE = /url\(\s*(['"]?)(.*?)\1\s*\)/gi;
/** An `@import` rule (remote stylesheet fetch) — stripped whole. */
const CSS_IMPORT_RE = /@import\b[^;]*;?/gi;

/**
 * Sanitize narrator HTML for the Custom shadow root.
 *
 * SSR-safe: with no DOM to sanitize (server / pre-hydration) it returns '' so
 * the caller falls back to the escaped paper-scrap. In the browser it runs the
 * single DOMPurify step above.
 */
export function sanitizeCustomHtml(html: string): string {
	if (!html) return '';
	if (typeof window === 'undefined') return '';
	return scrubStyleAttributes(DOMPurify.sanitize(html, CUSTOM_HTML_CONFIG));
}

/**
 * Scrub the `css` prop before it becomes a `<style>`'s textContent.
 *
 * `@import` is stripped entirely (it fetches a remote stylesheet). Every
 * `url(...)` survives only when its target is a `data:` image/font or an
 * `/api/` asset; anything else is stripped to nothing. Pure string work, so it
 * runs on the server too.
 */
export function scrubCustomCss(css: string): string {
	if (!css) return '';
	const withoutImports = css.replace(CSS_IMPORT_RE, '');
	return withoutImports.replace(CSS_URL_CALL_RE, (match, _quote, target: string) =>
		ALLOWED_URI_REGEXP.test(target.trim()) ? match : ''
	);
}

/** An inline `style="…"` attribute whose value can carry `url(...)`. */
const STYLE_ATTR_RE = /style=(["'])(.*?)\1/gi;

/**
 * Inline style attributes survive DOMPurify (`style` is an allowed, expressive
 * attribute), so a remote `url()` inside one would ride through unless it is
 * scrubbed here — under the SAME rule as the css prop. The engine gate rejects
 * any remote URI anywhere in a Custom statement, but a hand-edited log never
 * passed the gate; defense-in-depth means this seam must not depend on the
 * gate having run (the contract custom_policy.py's header states).
 */
function scrubStyleAttributes(html: string): string {
	if (!html || !html.includes('style=')) return html;
	return html.replace(STYLE_ATTR_RE, (_match, quote: string, value: string) => {
		const scrubbed = scrubCustomCss(value);
		return `style=${quote}${scrubbed}${quote}`;
	});
}
