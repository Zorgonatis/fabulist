// Shared user-persona state (#94) — the catalog, the pinned default and the
// per-chat assignment seam.
//
// The composer chip and the rail block mount the SAME component
// (`routes/chat/PersonaControl.svelte`), and both read this ONE store, so the
// two surfaces cannot disagree about the three assignment states:
//
//   * OWN       — the chat's own `chat_persona` assignment;
//   * INHERITED — no own assignment and no explicit clear, so the pinned global
//                 default is in effect (`persona_for_chat` falls back to it);
//   * EXPLICIT NONE — a chat that was cleared stays clear even while a pin
//                 exists (the owner ruling: clear = no persona).
//
// Reads ride the existing clients: `GET /api/personas` (the catalog) and
// `GET /api/settings` (the pin). Writes ride the SAME seams the rail's row
// picker used: `POST/DELETE /api/sessions/{id}/persona` for an assignment and
// `PATCH /api/settings {default_persona_id}` for the pin. No new endpoint, no
// server change.
//
// A write lands here BEFORE it lands on the wire's read path: the host's
// session list is refreshed from a prop callback, and the optimistic override
// below bridges the gap so a click is visible on both surfaces immediately.
// The override drops itself once the refreshed list carries the same value.

import { get, writable } from 'svelte/store';

import type { Persona } from '#lib/api';
import { assignChatPersona, getSettings, listPersonas, patchDefaultPersona } from '#lib/api';

/** One chat's optimistic assignment, written through the same seam as the server. */
export interface UserPersonaOverride {
	/** The assigned persona id, or `null` for no persona. */
	personaId: string | null;
	/** True for an EXPLICIT "no persona" clear, false for a real assignment. */
	cleared: boolean;
}

/** The state both persona surfaces render from. */
export interface UserPersonaState {
	/** The persona catalog (same rows the rail's Personas layer lists). */
	personas: Persona[];
	/** The pinned global default (`settings.default_persona_id`), or null. */
	defaultPersonaId: string | null;
	/** The label the settings read folds for the pin, or null. */
	defaultPersonaLabel: string | null;
	/** session id -> the assignment just written (dropped once the list agrees). */
	overrides: Record<string, UserPersonaOverride>;
	/** True once the first catalog read settled (success or failure). */
	loaded: boolean;
	/** The last catalog read's failure, `''` when it succeeded. */
	error: string;
}

const EMPTY: UserPersonaState = {
	personas: [],
	defaultPersonaId: null,
	defaultPersonaLabel: null,
	overrides: {},
	loaded: false,
	error: ''
};

/** The one shared store both persona surfaces subscribe to. */
export const userPersona = writable<UserPersonaState>({ ...EMPTY, overrides: {} });

/** A catalog read in flight; concurrent mounts share it instead of double-fetching. */
let inflight: Promise<void> | null = null;

function message(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

/**
 * Read the persona catalog + the pinned default.
 *
 * Concurrent callers share one request pair. `force` re-reads after a studio
 * edit — a definition created or renamed there must appear on both surfaces
 * without a page reload. A failed read keeps whatever was loaded and records
 * the reason (the controls stay usable; the next host refresh retries).
 */
export function loadUserPersona(force = false): Promise<void> {
	const state = get(userPersona);
	if (inflight) return inflight;
	if (state.loaded && !force) return Promise.resolve();
	inflight = Promise.all([listPersonas(), getSettings()])
		.then(([personas, settings]) => {
			userPersona.update((current) => ({
				...current,
				personas,
				defaultPersonaId: settings.default_persona_id ?? null,
				defaultPersonaLabel: settings.default_persona_label ?? null,
				loaded: true,
				error: ''
			}));
		})
		.catch((err: unknown) => {
			userPersona.update((current) => ({ ...current, loaded: true, error: message(err) }));
		})
		.finally(() => {
			inflight = null;
		});
	return inflight;
}

/**
 * Assign (or explicitly clear) a chat's user persona.
 *
 * `null` sends `DELETE` — an explicit "no persona" that suppresses the pinned
 * default, exactly what the rail's row picker means by "No persona". Throws on
 * failure so the caller can say why and the state stays on the server's value.
 */
export async function assignUserPersona(
	sessionId: string,
	personaId: string | null
): Promise<void> {
	await assignChatPersona(sessionId, personaId);
	userPersona.update((state) => ({
		...state,
		overrides: {
			...state.overrides,
			[sessionId]: { personaId, cleared: personaId === null }
		}
	}));
}

/**
 * Pin (or clear) the default persona new chats inherit.
 *
 * `null` unpins. The folded catalog the PATCH echoes is the new truth, so the
 * store takes its `default_persona_*` pair rather than guessing a label.
 */
export async function pinUserPersona(personaId: string | null): Promise<void> {
	const catalog = await patchDefaultPersona(personaId);
	userPersona.update((state) => ({
		...state,
		defaultPersonaId: catalog.default_persona_id ?? null,
		defaultPersonaLabel: catalog.default_persona_label ?? null
	}));
}

/** Drop one optimistic override once the host's list carries the same value. */
export function dropUserPersonaOverride(sessionId: string): void {
	const state = get(userPersona);
	if (!(sessionId in state.overrides)) return;
	const overrides = { ...state.overrides };
	delete overrides[sessionId];
	userPersona.set({ ...state, overrides });
}
