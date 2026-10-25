// Fabulist API client - image attachments + world-info import.

import { API_BASE, USE_MOCK } from './shared';

// --- image attachments (W2.8) ------------------------------------------------

export interface AttachmentResult {
	asset_id: string;
	mime: string;
	kind: string;
	caption: string;
}


/** Upload an image for a session; the backend stores the asset and captions it. */
export async function uploadAttachment(
	sessionId: string,
	file: File
): Promise<AttachmentResult | { error: string }> {
	if (USE_MOCK) {
		return {
			asset_id: `mock-${Date.now()}`,
			kind: 'image',
			mime: file.type || 'image/png',
			caption: 'a mocked image caption'
		};
	}
	try {
		const body = new FormData();
		body.append('file', file);
		const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments`, {
			method: 'POST',
			body
		});
		if (!res.ok) {
			const detail = await res.text();
			return { error: `upload failed (${res.status}): ${detail.slice(0, 140)}` };
		}
		const data = (await res.json()) as {
			asset: { asset_id: string; mime: string; kind?: string; caption?: string };
		};
		return {
			asset_id: data.asset.asset_id,
			mime: data.asset.mime,
			kind: data.asset.kind ?? 'image',
			caption: data.asset.caption ?? ''
		};
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}


// --- attachment list (image rehydration) --------------------------------------

export interface AttachmentItem {
	asset_id: string;
	mime: string;
	caption: string;
	seq: number | null;
	data_url?: string;
}


/** List a session's image assets with inline data URLs (chat rehydration). */
export async function listAttachments(
	sessionId: string
): Promise<{ attachments: AttachmentItem[] }> {
	if (USE_MOCK) return { attachments: [] };
	const res = await fetch(
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments`
	);
	if (!res.ok) throw new Error(`attachments failed (${res.status})`);
	return (await res.json()) as { attachments: AttachmentItem[] };
}

// --- world info import (W2.10 WI v1) -------------------------------------------


/** Import an ST world-info JSON (or character_book) into a session. */
export async function importWorldInfo(
	sessionId: string,
	book: unknown
): Promise<{ entries: number } | { error: string }> {
	if (USE_MOCK) return { entries: 0 };
	try {
		const res = await fetch(
			`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/worldinfo`,
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ book })
			}
		);
		if (!res.ok) {
			const detail = await res.text();
			return { error: `world info import failed (${res.status}): ${detail.slice(0, 120)}` };
		}
		return (await res.json()) as { entries: number };
	} catch (err) {
		return { error: err instanceof Error ? err.message : String(err) };
	}
}
