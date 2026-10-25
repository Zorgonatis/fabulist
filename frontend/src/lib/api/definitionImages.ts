// Fabulist API client - persona / world definition images (T4a).
//
// A definition carries a sessionless `image_asset` (a content-addressed id on
// the reserved media anchor). These routes serve that picture: `POST` stores raw
// bytes and re-appends the definition with the new id (latest-wins, D9); `GET`
// streams the stored bytes. Definitions are edited with NO chat open
// (non-negotiable 7), so both calls are session-free — no session id anywhere.
//
//   POST {API_BASE}/api/personas/{id}/image  raw image/* bytes -> {persona, image_asset, mime}
//   GET  {API_BASE}/api/personas/{id}/image  -> image bytes | 404
//   POST {API_BASE}/api/worlds/{id}/image    raw image/* bytes -> {world, image_asset, mime}
//   GET  {API_BASE}/api/worlds/{id}/image    -> image bytes | 404
//
// A world's cast members are persona ids, so a cast member's picture is the
// persona's own (`fetchDefinitionImage('persona', castId)`) — worlds keep no
// separate per-cast store.

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson } from './shared';

/** Definition kinds that carry an image route (personas + worlds). */
export type DefinitionKind = 'persona' | 'world';

/** Image endpoint per kind, kept here so the orchestrator can rewire them. */
const IMAGE_ENDPOINTS: Record<DefinitionKind, (id: string) => string> = {
	persona: (id) => `${API_BASE}/api/personas/${encodeURIComponent(id)}/image`,
	world: (id) => `${API_BASE}/api/worlds/${encodeURIComponent(id)}/image`
};

/** What a successful upload recorded (`POST .../image`). */
export interface DefinitionImageUpload {
	/** Content-addressed asset id now on the definition's `image_asset`. */
	image_asset: string;
	mime: string;
}

/**
 * Fetch a definition's image as an object URL, or `null` when it has none.
 *
 * A `404` (unknown definition, or no image attached) is the ordinary "no image"
 * answer, not an error; any other failure throws. The caller OWNS the returned
 * URL and must `URL.revokeObjectURL` it when it replaces or drops the thumbnail.
 */
export async function fetchDefinitionImage(
	kind: DefinitionKind,
	id: string
): Promise<string | null> {
	if (USE_MOCK) return null;

	const res = await fetch(IMAGE_ENDPOINTS[kind](id));
	if (res.status === 404) return null;
	if (!res.ok) {
		throw new StreamError(`fetchDefinitionImage failed: ${res.status} ${res.statusText}`);
	}
	const blob = await res.blob();
	if (!blob.size) return null;
	return URL.createObjectURL(blob);
}

/**
 * Attach (or replace) a definition's image (`POST .../image`).
 *
 * Sends the raw blob with its own mime — the backend accepts `image/*` bytes up
 * to 10 MiB. Resolves to the stored asset id so the caller can refresh.
 */
export async function attachDefinitionImage(
	kind: DefinitionKind,
	id: string,
	blob: Blob
): Promise<DefinitionImageUpload> {
	if (USE_MOCK) {
		return { image_asset: `mock-${Date.now()}`, mime: blob.type || 'image/png' };
	}

	const res = await fetch(IMAGE_ENDPOINTS[kind](id), {
		method: 'POST',
		headers: { 'Content-Type': blob.type || 'image/png' },
		body: blob
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(detail.reason || `attachDefinitionImage failed: ${res.status}`, {
			...detail,
			status: res.status
		});
	}
	const body = await readJson(res);
	const asset = typeof body.image_asset === 'string' ? body.image_asset : '';
	const mime = typeof body.mime === 'string' ? body.mime : blob.type || 'image/png';
	return { image_asset: asset, mime };
}

/**
 * Up-to-two uppercase initials for a circular-avatar fallback.
 *
 * Drawn from the first letters of the first two whitespace-separated words
 * (`"Lyra Voss" -> "LV"`, `"Lyra" -> "L"`); `"?"` when the name is blank.
 */
export function definitionInitials(name: string): string {
	const words = name.trim().split(/\s+/).filter(Boolean);
	if (words.length === 0) return '?';
	const letters = words.slice(0, 2).map((word) => word[0] ?? '');
	return letters.join('').toUpperCase();
}
