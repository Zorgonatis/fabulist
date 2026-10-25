// Fabulist API client — global search (owner ruling 2026-10-13, item 8).
//
// Global search has NO active-chat scope: it searches every VISIBLE session and
// every definition (personas / worlds / books). The chat-scoped semantic search
// lives in its own top-bar control (`#lib/api/revisions` `searchSession`).
// Read-only: the backend derives both halves from the event log and appends
// nothing (D9).
//
//   GET {API_BASE}/api/search/global?q=...
//     -> {sessions: [{session_id, title, snippet, score}], definitions: [{kind, id, name}]}

import { API_BASE } from './shared';

/** Definition kinds the global search can return (mirrors the studio tabs). */
export type GlobalDefinitionKind = 'persona' | 'world' | 'book';

/** A session the query matched, semantically (snippet + nearest distance) or
 *  by title (snippet = title, score = null). */
export interface GlobalSessionHit {
	session_id: string;
	title: string;
	snippet: string;
	score: number | null;
}

/** A persona / world / book whose name matched the query. */
export interface GlobalDefinitionHit {
	kind: GlobalDefinitionKind;
	id: string;
	name: string;
}

export interface GlobalSearchResult {
	sessions: GlobalSessionHit[];
	definitions: GlobalDefinitionHit[];
}

export async function globalSearch(
	q: string
): Promise<GlobalSearchResult | { error: string }> {
	try {
		const res = await fetch(`${API_BASE}/api/search/global?q=${encodeURIComponent(q)}`);
		if (!res.ok) return { error: `search failed (${res.status})` };
		return (await res.json()) as GlobalSearchResult;
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}
