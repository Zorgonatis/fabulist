// Appearance → DOM (issue #12 + #13, ADR-027).
//
// Three writes, and nothing else:
//
//   1. `document.documentElement[data-theme] = "<preset>"` — the preset block in
//      `app.css` (`[data-theme='oled']` / `[data-theme='light']` /
//      `[data-theme='savilen']`) retunes the token set. `dark` is the `:root`
//      default, so an absent/unknown value still renders the shipped palette;
//      the attribute is set anyway so the DOM says which preset is live.
//
//   2. ONE `<style id="fabulist-user-css">` appended LAST in `<head>` — the
//      reader's pasted stylesheet. Last in head is what makes the cascade
//      `base -> preset -> user CSS`; it is re-appended on every apply because
//      Vite/SvelteKit can inject their own <style> chunks later and would
//      otherwise land after it.
//
//   3. `document.documentElement[data-wallpaper]` + ONE inline custom property,
//      `--wallpaper-image: url("…")` — the reader's image behind the prose
//      (issue #13). The ATTRIBUTE turns on the `:root[data-wallpaper]` layer in
//      `app.css` (the scrim + the re-solved token ladder); the PROPERTY carries
//      the URL, because a URL is data and does not belong in the stylesheet.
//      Removing a wallpaper removes BOTH: the attribute so the ladder returns to
//      the shipped one, the property so no dangling URL stays on <html>.
//
//   4. `document.documentElement[data-align] = "left" | "center" | "right"` —
//      issue #41's reading-column position. The attribute is the whole DOM half:
//      `routes/chat/TranscriptList.svelte` moves the `.transcript > *` measure's
//      inline margins per value (`center` is the default and the shipped look),
//      so the transcript markup does not change and no layout is restructured.
//      Like the preset, the attribute is set even for `center` so the DOM says
//      which mode is live.
//
// The last step of the CASCADE is NOT here: the session's tone tokens are inline
// custom properties on `<html>`, set by `routes/chat/+page.svelte` from
// `GET /api/sessions/{id}/theme`. Inline beats every stylesheet rule without
// `!important`, so the fiction's palette outranks the reader's furniture (owner
// ruling D-D). This module must never try to lift user CSS above that: no
// `!important` is added, and the style element is not moved after
// `svelte:head` injections by force. `tests/test_appearance_api.py` pins the
// contract from the source side.
//
// One interaction with that fourth step is deliberate: the wallpaper layer
// declares `--bg-warmth: 0%` (the owner's ruling), and the ladder it ships does
// not read the token any more — so even where a session tone re-sets warmth
// inline afterwards (it outranks the stylesheet, by design), the composites stay
// on their measured numbers. Nothing here fights the tone; the layer simply
// stops depending on it.

import { getAppearance, type AppearanceState, type WallpaperInfo } from '#lib/api/appearance';

/** The id of the (single) user-CSS style element. Tests and the docs pin this. */
export const USER_CSS_STYLE_ID = 'fabulist-user-css';

/** The attribute the preset blocks in `app.css` key off. */
export const PRESET_ATTR = 'data-theme';

/** The attribute that turns on the wallpaper layer (`:root[data-wallpaper]`). */
export const WALLPAPER_ATTR = 'data-wallpaper';

/** The attribute the reading-column alignment rules (TranscriptList) key off. */
export const ALIGN_ATTR = 'data-align';

/** The inline custom property that carries the image URL (`url("…")`). */
export const WALLPAPER_VAR = '--wallpaper-image';

/** Apply the preset (ids come from the backend catalog; unknown ids are harmless). */
export function applyPreset(preset: string): void {
	if (typeof document === 'undefined') return;
	document.documentElement.setAttribute(PRESET_ATTR, preset);
}

/**
 * Install the pasted stylesheet as the single, LAST `<style>` in `<head>`.
 *
 * An empty string REMOVES the element entirely — so "clear the box" returns the
 * page to the preset with no residue. The backend has already reduced the CSS to
 * the guard's allowlist; this function does no filtering of its own (one place
 * decides, so the drawer's "what was dropped" report is always the truth).
 */
export function applyUserCss(css: string): void {
	if (typeof document === 'undefined') return;
	const head = document.head;
	const existing = head.querySelector<HTMLStyleElement>(`style#${USER_CSS_STYLE_ID}`);
	if (!css.trim()) {
		existing?.remove();
		return;
	}
	const style = existing ?? document.createElement('style');
	style.id = USER_CSS_STYLE_ID;
	style.dataset.owner = 'appearance';
	style.textContent = css;
	// Re-append: an existing element moves to the end, a new one lands last.
	head.appendChild(style);
}

/**
 * Apply (or clear) the wallpaper: the attribute AND the inline image URL.
 *
 * `null` / a missing URL removes both, which is what makes "remove" return the
 * install to the shipped cascade instead of leaving a half-applied layer behind.
 * The URL is written as a quoted `url("…")` because the value is round-tripped
 * through CSSOM and an unquoted URL containing `)` or whitespace would break the
 * rule; the backend's name is generated, but the cache-busting `?v=` is not ours
 * to trust.
 */
export function applyWallpaper(wallpaper: WallpaperInfo | null | undefined): void {
	if (typeof document === 'undefined') return;
	const root = document.documentElement;
	const url = wallpaper?.url ?? '';
	if (!url) {
		root.removeAttribute(WALLPAPER_ATTR);
		root.style.removeProperty(WALLPAPER_VAR);
		return;
	}
	root.setAttribute(WALLPAPER_ATTR, '');
	root.style.setProperty(WALLPAPER_VAR, `url("${url}")`);
}

/**
 * Apply the reading-column alignment (issue #41).
 *
 * One attribute on `<html>`; `TranscriptList.svelte` owns the variants of the
 * measure's inline margin. An unknown value is written anyway and lands on the
 * centered base rule (there is no rule to match), so a hand-edited document can
 * never leave the column unpositioned.
 */
export function applyAlign(align: string): void {
	if (typeof document === 'undefined') return;
	document.documentElement.setAttribute(ALIGN_ATTR, align || 'center');
}

/** Apply a whole appearance state (preset, then the override, then the image). */
export function applyAppearance(
	state: Pick<AppearanceState, 'preset' | 'custom_css' | 'wallpaper' | 'align'>
): void {
	applyPreset(state.preset);
	applyUserCss(state.custom_css);
	applyWallpaper(state.wallpaper);
	applyAlign(state.align);
}

/**
 * Load the install's appearance and apply it. Called once from the root layout.
 *
 * Failure is silent by design: the appearance is furniture, and a UI that
 * shouted because a theme read failed would be worse than the shipped default.
 * The defaults are exactly what `:root` already renders, so "failed" and
 * "shipped" look the same.
 */
export async function initAppearance(): Promise<AppearanceState | null> {
	try {
		const state = await getAppearance();
		applyAppearance(state);
		return state;
	} catch {
		return null;
	}
}
