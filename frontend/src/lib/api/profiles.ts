// Fabulist API client - experience profiles (D33).

import { MOCK_ASSISTANTS } from './assistants';
import { MOCK_PERSONAS } from './personas';
import { mockDefaultPreset, mockPromptPresets } from './prompts';
import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';
import { mockBooks, mockWorlds } from './worlds';

/** The world half of an envelope: the world wire + its inlined cast + books. */
export interface ProfileWorldBundle {
	world: Record<string, unknown> | null;
	cast: Array<Record<string, unknown>>;
	books: Array<Record<string, unknown>>;
}


/**
 * A v1 experience-profile envelope. Loose-typed on purpose: the definitions
 * ride through verbatim (D4 forward-compat), so only the envelope's own frame
 * is spelled out here.
 */
export interface ProfileEnvelope {
	format: string;
	format_version: number;
	name: string;
	exported_at?: string;
	world: ProfileWorldBundle | null;
	preset: Record<string, unknown> | null;
	/** Present only when the exporter was asked to include the roster. */
	assistants?: Array<Record<string, unknown>>;
	[key: string]: unknown;
}


/**
 * Legacy alias: pre-rename profile exports carried the roster under "sidekicks".
 * The backend importer still accepts it; nothing here writes or displays that key.
 */
const LEGACY_PROFILE_ASSISTANT_KEY = 'sidekicks';


/** The roster list an envelope carries, new spelling first (legacy fallback). */
export function envelopeAssistantRoster(
	envelope: ProfileEnvelope
): Array<Record<string, unknown>> | undefined {
	if (Array.isArray(envelope.assistants)) return envelope.assistants;
	const legacy = envelope[LEGACY_PROFILE_ASSISTANT_KEY];
	return Array.isArray(legacy) ? (legacy as Array<Record<string, unknown>>) : undefined;
}


/** One `{kind, id, name?, count?}` row in the import result. */
export interface ImportRow {
	kind: string;
	id: string;
	name?: string;
	count?: number;
}


/**
 * One import warning. Heterogeneous — discriminate on `type`:
 *  - `agent_binding`: a binding named an unknown local agent and fell back.
 *  - `preset_id`: a preset claiming the default id was imported under a new id.
 */
export interface ProfileWarning {
	type: string;
	interaction?: string;
	wanted?: string;
	fell_back_to?: string | null;
	assigned?: string;
	reason?: string;
	[key: string]: unknown;
}


/** Response of `POST /api/profiles/import`. */
export interface ImportResult {
	imported: ImportRow[];
	skipped: ImportRow[];
	warnings: ProfileWarning[];
	/** Present when a profile PNG body carried an image the target stored (D33/G). */
	image_asset?: string;
}


/** Options for `exportProfile` (mirrors the export query string). */
export interface ProfileExportParams {
	/** Preset id to bundle (required by the backend). */
	preset: string;
	/** World id to bundle; omitted/null is a preset + assistants profile. */
	world?: string | null;
	/** Include the assistant roster (the export toggle, default ON in the UI). */
	assistants?: boolean;
	/** Profile name; omitted -> the world's, else the preset's. */
	name?: string | null;
}


/** Result of a triggered profile download. */
export interface ProfileExportResult {
	/** The download filename (from Content-Disposition, else the slug rule). */
	filename: string;
}


/** Profile endpoint paths (kept in one place the orchestrator can rewire). */
export const PROFILE_ENDPOINTS = {
	export: (params: ProfileExportParams): string => {
		const query = new URLSearchParams();
		query.set('preset', params.preset);
		if (params.world) query.set('world', params.world);
		if (params.assistants) query.set('assistants', '1');
		if (params.name && params.name.trim()) query.set('name', params.name.trim());
		return `${API_BASE}/api/profiles/export?${query.toString()}`;
	},
	import: (): string => `${API_BASE}/api/profiles/import`
} as const;


/** ASCII slug of `text` (mirrors the backend importer's `_slug`). */
function profileSlug(text: string): string {
	const dashed = [...text]
		.map((c) => (/[A-Za-z0-9]/.test(c) && c.charCodeAt(0) < 128 ? c : '-'))
		.join('');
	return dashed.split('-').filter(Boolean).join('-').toLowerCase() || 'profile';
}


/** `<slug>.profile.json` (mirrors the backend's `_filename`). */
function profileFilename(name: string): string {
	const slug = [...name]
		.map((c) => (/[A-Za-z0-9_-]/.test(c) && c.charCodeAt(0) < 128 ? c : '-'))
		.join('')
		.replace(/^[-_]+/, '')
		.replace(/[-_]+$/, '');
	return `${slug || 'profile'}.profile.json`;
}


/** Extract the quoted download name from a Content-Disposition header. */
function filenameFromHeader(header: string | null): string | null {
	if (!header) return null;
	const match = /filename="?([^";]+)"?/i.exec(header);
	return match ? match[1].trim() : null;
}


/** Trigger a browser download of `blob` as `filename` (anchor + object URL). */
function downloadBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	anchor.click();
	URL.revokeObjectURL(url);
}


/**
 * Build a plausible mock envelope from the mock catalog (VITE_MOCK=1). The
 * shape matches the live exporter: a world bundle (world + inlined cast +
 * books) when a world is named, else `world: null`; assistants only when asked.
 */
function mockProfileEnvelope(params: ProfileExportParams): ProfileEnvelope {
	const world = params.world ? (mockWorlds.find((w) => w.world_id === params.world) ?? null) : null;
	const cast = world ? MOCK_PERSONAS.filter((p) => world.cast.includes(p.persona_id)) : [];
	const books = world ? mockBooks.filter((b) => world.book_ids.includes(b.book_id)) : [];
	const preset = mockPromptPresets.find((p) => p.preset_id === params.preset) ?? mockDefaultPreset();
	const envelope: ProfileEnvelope = {
		format: 'fabulist-profile',
		format_version: 1,
		name: params.name?.trim() || world?.name || preset.name || 'profile',
		exported_at: new Date().toISOString(),
		world: world
			? {
					world: { ...world },
					cast: cast.map((p) => ({ ...p })),
					books: books.map((b) => ({ ...b }))
				}
			: null,
		preset: { ...preset }
	};
	if (params.assistants) envelope.assistants = MOCK_ASSISTANTS.map((a) => ({ ...a }));
	return envelope;
}


/**
 * Mock import result (VITE_MOCK=1): a two-entity imported list, one skipped
 * entity, and one unknown-agent fallback warning (the shape a real import of
 * the mock envelope produces).
 */
function mockImportProfile(envelope: ProfileEnvelope): ImportResult {
	const worldName = str(envelope.world?.world?.name) || envelope.name || 'Imported world';
	const presetName = str(envelope.preset?.name) || 'Imported preset';
	const firstBook = envelope.world?.books?.[0];
	const bookName = str(firstBook?.name) || 'Existing book';
	return {
		imported: [
			{
				kind: 'world',
				id: str(envelope.world?.world?.world_id) || `world-${profileSlug(envelope.name)}`,
				name: worldName
			},
			{
				kind: 'preset',
				id: `default-${profileSlug(envelope.name)}`,
				name: presetName
			}
		],
		skipped: [
			{
				kind: 'book',
				id: str(firstBook?.book_id) || 'book-existing',
				name: bookName
			}
		],
		warnings: [
			{
				type: 'agent_binding',
				interaction: 'narrator',
				wanted: 'mega-narrator',
				fell_back_to: 'narrator'
			}
		]
	};
}


function normalizeImportRow(raw: unknown): ImportRow | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const kind = str(obj.kind);
	const id = str(obj.id);
	if (!kind && !id) return null;
	const row: ImportRow = { kind, id };
	if (typeof obj.name === 'string') row.name = obj.name;
	if (typeof obj.count === 'number') row.count = obj.count;
	return row;
}


function normalizeProfileWarning(raw: unknown): ProfileWarning | null {
	if (!raw || typeof raw !== 'object') return null;
	const obj = raw as Record<string, unknown>;
	const type = str(obj.type);
	if (!type) return null;
	return { ...obj, type } as ProfileWarning;
}


/** Tolerantly normalize an import result (dropping malformed rows). */
function normalizeImportResult(body: Record<string, unknown>): ImportResult {
	const rows = (value: unknown): ImportRow[] =>
		Array.isArray(value)
			? value.map((item) => normalizeImportRow(item)).filter((row): row is ImportRow => row !== null)
			: [];
	const warnings = Array.isArray(body.warnings)
		? body.warnings
				.map((item) => normalizeProfileWarning(item))
				.filter((warning): warning is ProfileWarning => warning !== null)
		: [];
	return { imported: rows(body.imported), skipped: rows(body.skipped), warnings };
}


/**
 * Export a profile and trigger its download (`GET /api/profiles/export`).
 *
 * `preset` is required; `world` + `assistants` are opt-in (an envelope without
 * assistants never touches the local roster on import). Resolves with the
 * download filename; a 404 (unknown world/preset) throws a `StreamError` with
 * the backend's precise reason.
 */
export async function exportProfile(params: ProfileExportParams): Promise<ProfileExportResult> {
	if (USE_MOCK) {
		const envelope = mockProfileEnvelope(params);
		const filename = profileFilename(envelope.name);
		downloadBlob(
			new Blob([JSON.stringify(envelope, null, 2)], { type: 'application/json' }),
			filename
		);
		return { filename };
	}

	const res = await fetch(PROFILE_ENDPOINTS.export(params), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `exportProfile failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	const blob = await res.blob();
	let name = params.name?.trim() ?? '';
	try {
		const parsed = JSON.parse(await blob.text()) as ProfileEnvelope;
		if (typeof parsed?.name === 'string' && parsed.name) name = parsed.name;
	} catch {
		// Non-JSON body: fall back to the name param / slug rule below.
	}
	const filename =
		filenameFromHeader(res.headers.get('content-disposition')) ?? profileFilename(name);
	downloadBlob(blob, filename);
	return { filename };
}


/**
 * Import a profile envelope (`POST /api/profiles/import`).
 *
 * An envelope carrying `assistants: []` clears the local roster; an envelope
 * WITHOUT the key leaves it untouched (absent != empty). Throws a `StreamError`
 * with the backend's precise reason on a malformed envelope (422).
 */
export async function importProfile(envelope: ProfileEnvelope): Promise<ImportResult> {
	if (USE_MOCK) return mockImportProfile(envelope);

	const res = await fetch(PROFILE_ENDPOINTS.import(), {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(envelope)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `importProfile failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	return normalizeImportResult(await readJson(res));
}


/**
 * Import a profile **PNG** (`POST /api/profiles/import`, `image/png`).
 *
 * A profile PNG's body IS the profile's assigned image, so the backend stores
 * those bytes as a definition asset on the target log and repoints the assigned
 * layer at the local ref. Posting only the decoded envelope as JSON (what
 * {@link importProfile} does) carries just the exporting log's content address —
 * a dangling ref, so the artwork would be lost and a re-export would 404.
 *
 * Shares the response handling and error wording with {@link importProfile}.
 */
export async function importProfilePng(
	data: ArrayBuffer | Uint8Array
): Promise<ImportResult> {
	if (USE_MOCK) {
		throw new StreamError('PNG profile import needs the engine — the image bytes live in the log');
	}
	const res = await fetch(PROFILE_ENDPOINTS.import(), {
		method: 'POST',
		headers: { 'Content-Type': 'image/png', Accept: 'application/json' },
		body: data as BodyInit
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `importProfile failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	return normalizeImportResult(await readJson(res));
}


// --- Wave R5: connections editor (definitions surface) -----------------------
//
// The connections document (configs/connections.json) maps a connection NAME to
// an endpoint/model/key/sampler entry and binds logical agents to those names.
// There was a read/patch API but no UI; this lane adds the editor surface. All
// paths are absolute `${API_BASE}` (AGENTS.md gotcha: a relative `/api/...`
// 404s behind vite and gets swallowed by a cosmetic catch).
//
//   GET    /api/connections                 -> {connections: {name: entry}, agents}
//   POST   /api/connections/{name}          -> {connection: entry}          (201)
//   PATCH  /api/connections/{name}          -> {connection: entry, reloaded} (no base_url)
//   DELETE /api/connections/{name}          -> {deleted, reloaded}          (409 agent-bound)
//   GET    /api/connections/{name}/models?base_url=&api_key_env=
//                                            -> {models: [id], base_url, url, api_key_set}
//
// Key storage: only `api_key_env` (an env-var NAME) is persisted; the secret is
// never returned. `api_key_set` reports whether the named env var resolves now.
// A literal key is not supported by the registry (see the backend seam note).
