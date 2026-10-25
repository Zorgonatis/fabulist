// Session-scoped latest env HUD statement store (D38 diagram wave, Lane 2a).
//
// Deliberately dumb: it holds the most recent statement read for a session so
// the HUD bar can render it, and nothing else. ChatHud owns the fetch and
// parse; this store is the shared cache. Keeping `sessionId` beside the
// statement lets a consumer ignore a stale statement from a previous session.

import { writable } from 'svelte/store';

export interface HudState {
	/** The session the statement belongs to, or `null` when empty. */
	sessionId: string | null;
	/** The raw `hud = EnvTracker(...)` statement, or `null` when none. */
	statement: string | null;
}

const EMPTY: HudState = { sessionId: null, statement: null };

export const hud = writable<HudState>(EMPTY);

/** Record the latest statement read for `sessionId`. */
export function setHud(sessionId: string, statement: string): void {
	hud.set({ sessionId, statement });
}

/** Drop the cached statement (no session, unreadable, or unrecognised). */
export function clearHud(): void {
	hud.set(EMPTY);
}
