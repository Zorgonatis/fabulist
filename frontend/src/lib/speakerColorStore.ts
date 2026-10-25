// Live speaker-colour registry counter (bug fix: recolouring a cast member did
// not repaint dialogue that was already rendered).
//
// The engine owns the registry (PLAN D9): PATCH .../speaker_colors appends a
// latest-wins override and returns the refreshed rows. `TranscriptList` reads
// the registry only when the transcript *shape* changes (hydration / turn end),
// so a pure colour edit left already-rendered quote spans stale until the next
// turn or a reload. `CastPane` writes the confirmed rows here and bumps
// `version`; `TranscriptList` watches `version` and re-reads the registry.
//
// Plain Svelte store (like `#lib/stores`) so a component auto-subscribes with
// `$speakerColors` and reads the version directly in an `$effect` — `get()`
// would not register the dependency.

import { writable } from 'svelte/store';

import type { SpeakerColor } from '#lib/api';

/** One colour-registry row plus the canonical ``member_id`` it binds to (W7).
 *
 * The engine keys speaker colours by the immutable member id, so a rename keeps
 * the hue; ``name`` is a mutable display snapshot. Rows read from an engine that
 * predates canonical ids (or written before W7) carry no ``member_id`` — there
 * ``name`` is the only key available. */
export interface RegistryRow extends SpeakerColor {
	member_id: string | null;
}

export interface SpeakerColorState {
	/** Monotonic counter, bumped once per confirmed registry write. */
	version: number;
	/** Session the published rows belong to (guards reuse across chats). */
	sessionId: string | null;
	/** The confirmed registry rows, first-assignment order. */
	colors: RegistryRow[];
}

export const speakerColors = writable<SpeakerColorState>({
	version: 0,
	sessionId: null,
	colors: []
});

/** Record the registry a PATCH confirmed, bumping the version. */
export function bumpSpeakerColors(sessionId: string, colors: RegistryRow[]): void {
	speakerColors.update((state) => ({ version: state.version + 1, sessionId, colors }));
}
