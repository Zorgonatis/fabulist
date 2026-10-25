// OpenUI avatar resolver — the shared name→image seam (artifact components canon).
//
// WHY THIS EXISTS: artifact components (Phone / SMS / SocialPost …) render
// avatars for NAMED senders, but a narrator DSL statement carries only names —
// never image bytes (non-negotiable 3: the narrator emits prose + DSL only).
// The engine's definition images (persona/world artwork, api/definitionImages)
// are the single source of truth, so this module bridges names to stored art.
//
// DESIGN (front-loaded, like every canon module):
//   1. ONE store: `avatarIndex` — Map<displayName, objectURL | null>.
//      `null`            = known cast member with NO image → initials emblem.
//      key absent        = not a cast member at all (invention, "Unknown
//                          Number") → initials emblem; the distinction is kept
//                          so a future caller can tell "imageless cast member"
//                          from "never hydrated" / "invented name".
//   2. Reactivity follows the project rule (AGENTS.md): read the STORE directly
//      in markup — `$avatarIndex.get(name) ?? initialsFor(name)`. There is
//      deliberately NO `avatarFor(name)` helper: it would read the store via
//      `get()` and `get(store)` inside a `$derived` does NOT track — the helper
//      would be a reactivity trap. The reactive idiom is the only idiom.
//   3. Object URLs from `fetchDefinitionImage` are OWNED by this module: a
//      replaced entry revokes its predecessor; nothing else revokes them.
//   4. Failures are quiet by design: a persona without artwork (404) or a
//      failed fetch resolves its name to `null` — an artifact pane must never
//      fail on a missing avatar (fallback ladder: image → initials emblem).
//   5. Mock mode (`VITE_MOCK=1`, the dev-artifacts harness): hydration is a
//      no-op and every name renders its initials emblem — the harness needs no
//      engine. `fetchDefinitionImage` already returns null under USE_MOCK; the
//      early return skips the personas list call too.
//
// WIRING (orchestrator, Wave 2): the chat page calls
// `hydrateAvatars(world.cast)` once per active world (and on world switch);
// components never hydrate — they only read.

import { writable } from 'svelte/store';
import { fetchDefinitionImage } from '../api/definitionImages';
import { listPersonas } from '../api/personas';
import { USE_MOCK } from '../api/shared';

/** name → artwork object URL (or null = imageless). Subscribe in markup. */
export const avatarIndex = writable<ReadonlyMap<string, string | null>>(new Map());

/**
 * Deterministic initials emblem for any display name.
 *
 * "Lyra" → "L", "Unknown Number" → "UN", "marcus vane" → "MV": first grapheme
 * of the first word plus the first grapheme of the last word when there are
 * two or more words, upper-cased. Pure; safe for SSR (no DOM access).
 */
export function initialsFor(name: string): string {
	const words = name.trim().split(/\s+/).filter(Boolean);
	if (words.length === 0) return '?';
	if (words.length === 1) {
		return [...words[0]][0]?.toUpperCase() ?? '?';
	}
	const first = [...words[0]][0] ?? '';
	const last = [...words[words.length - 1]][0] ?? '';
	return (first + last).toUpperCase() || '?';
}

/**
 * Hydrate name→image for the given cast persona ids (additive, idempotent).
 *
 * Resolves each id to its persona name (one `listPersonas` call — definitions
 * are a small, sessionless list), then fetches artwork in parallel. Names not
 * in the cast are left untouched; imageless personas map to null; entries from
 * a previous hydration are replaced (revoking the old object URL). Under
 * `USE_MOCK` this is a no-op — every name falls back to initials.
 */
export async function hydrateAvatars(personaIds: string[]): Promise<void> {
	if (USE_MOCK) return;
	const wanted = new Set(personaIds);
	if (wanted.size === 0) return;
	let personas;
	try {
		personas = (await listPersonas()).filter((p) => wanted.has(p.persona_id));
	} catch {
		return; // quiet by design (4): the pane falls back to initials
	}
	const entries = await Promise.all(
		personas.map(async (p): Promise<[string, string | null]> => {
			try {
				return [p.name, await fetchDefinitionImage('persona', p.persona_id)];
			} catch {
				return [p.name, null]; // quiet by design (4)
			}
		})
	);
	avatarIndex.update((prev) => {
		const next = new Map(prev);
		for (const [name, url] of entries) {
			const old = next.get(name);
			if (typeof old === 'string') URL.revokeObjectURL(old);
			next.set(name, url);
		}
		return next;
	});
}
