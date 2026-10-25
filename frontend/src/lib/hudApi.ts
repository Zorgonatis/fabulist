// Env HUD read client (D38 diagram wave, Lane 2a).
//
// Engine contract (src/fabulist/api/hud_api.py, Lane 1f):
//
//   GET {API_BASE}/api/sessions/{id}/hud -> {"statement": "hud = EnvTracker(...)"}
//
// The statement is an engine-assembled OpenUI DSL call with five positional
// string args (time, date, loc, weather, forecast). This module only reads it;
// parsing + rendering live in ChatHud/EnvTracker. It mirrors `#lib/api`'s
// established import path for `API_BASE` (`frontend/src/lib/api.ts`).

import { API_BASE } from '#lib/api';

export const hudEndpoint = (sessionId: string): string =>
	`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/hud`;

/**
 * Latest HUD statement for a session, or `null` when it cannot be read.
 *
 * `null` is the tolerant "render nothing" signal: an unknown/expired session
 * (404), an unreachable engine, or a missing/non-string `statement` all degrade
 * to no HUD rather than an error surface. The caller decides what to show.
 */
export async function getHud(sessionId: string): Promise<string | null> {
	let res: Response;
	try {
		res = await fetch(hudEndpoint(sessionId), { headers: { Accept: 'application/json' } });
	} catch {
		return null;
	}
	if (!res.ok) return null;
	try {
		const body = (await res.json()) as { statement?: unknown };
		return typeof body.statement === 'string' ? body.statement : null;
	} catch {
		return null;
	}
}
