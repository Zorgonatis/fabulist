// Inspector live-refresh signal (2026-10-15 batch, W1b).
//
// The Inspector pane reads GET /inspect/turns + GET /agent-calls only on session
// change and on its manual refresh button, so a model call that runs between
// turns (image-gen "Prompt Enhance", a caption) stays invisible until the user
// reloads by hand. The refresh contract (owner-approved 2026-10-15):
//
//   * read again at every turn end (streaming true -> false), and
//   * read again after each UI-invoked model call.
//
// Both producers are outside the pane, so they bump this shared counter and the
// pane watches it. A monotonic counter (not a boolean) means every bump is seen,
// even when two land in one tick.
//
// Plain Svelte store (like `#lib/stores`) so a component auto-subscribes with
// `$inspectorRefresh`; `get()` inside an `$effect` would not register.
import { writable } from 'svelte/store';

/** Monotonic refresh counter. Watch it to re-read inspector data. */
export const inspectorRefresh = writable(0);

/** Signal the Inspector to re-read. Call at turn end and after a UI model call. */
export function bumpInspectorRefresh(): void {
	inspectorRefresh.update((n) => n + 1);
}
