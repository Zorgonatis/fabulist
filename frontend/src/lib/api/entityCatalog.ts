// Fabulist API client - the owner-editable BTS attribute catalog (W9.29 §4).
//
// The tracker's attribute universe is an ASSET, not engine constants: every meter
// (`hostility`, `tension`, ...) and trait (`mood`, `weather`, ...) the BTS write
// actions may set is a row here, with the range a meter enforces (requirement 3:
// "meters are objects with identity, and their attributes and ranges come from a
// studio-editable catalog"). The engine reads the same asset
// (`fabulist.state.entity_catalog.catalog_for`) from one reserved-anchor
// `system` event, so no chat/session is involved and a save applies to the very
// next BTS pass (non-negotiable 7: asset CRUD works with no chat open).
//
// Wire contract (`GET/PUT /api/prompts/entity-catalog`, backend W4):
//
//   GET {API_BASE}/api/prompts/entity-catalog
//     -> {rows: [CatalogRow]}
//        The EFFECTIVE catalog: the stored asset, or the built-in code seed
//        (`DEFAULT_ROWS` below) while the owner has saved nothing.
//   PUT {API_BASE}/api/prompts/entity-catalog   body {rows: [CatalogRow]}
//     -> {rows: [CatalogRow]}; 400 {"detail": "<message>"} on a validation failure.
//
// The PUT is REPLACE semantics (the whole catalog, latest wins - the decision
// bank's contract), which is why the row helpers below are pure set operations
// over one array: the pane edits a draft and saves it in ONE request.
//
// Validation mirrors `fabulist.state.entity_catalog.validate_rows` exactly, so
// the pane reports the same problem the server would, at the same place:
//  · `key` a strict slug (`[a-z][a-z0-9_]*`), unique across the catalog - it is
//    the handle a tool call cites;
//  · `label`/`description` non-empty;
//  · `type` `meter` or `trait`, `host` `relational` / `self` / `scene`;
//  · a METER carries `range` = two ascending integers; a TRAIT carries none.

import { API_BASE, StreamError, USE_MOCK, readErrorDetail, readJson, str } from './shared';

/** A row's type: a range-checked number, or free text. */
export type CatalogRowType = 'meter' | 'trait';

/**
 * Whose value the row is: `relational` needs a subject (a meter toward another
 * being), `self` is the being's own state, `scene` belongs to the room/scene.
 */
export type CatalogRowHost = 'relational' | 'self' | 'scene';

/** One catalog row (the plan §4 shape == the wire shape == the stored shape). */
export interface CatalogRow {
	key: string;
	label: string;
	type: CatalogRowType;
	host: CatalogRowHost;
	/** Meters only: `[low, high]`, ascending. Absent on a trait. */
	range?: [number, number];
	description: string;
}

/** Endpoint path (one place, so a rewire touches one line). */
export const ENTITY_CATALOG_ENDPOINT = (): string => `${API_BASE}/api/prompts/entity-catalog`;

/**
 * The built-in seed, verbatim from `entity_catalog.DEFAULT_ROWS` (plan §4).
 * Served by GET while the asset is empty; the first save stores it as the
 * owner's own. Temperament: deliberately small - the catalog is the owner's
 * surface, not the engine's opinion (outfit/pose/appearance are not seeded).
 */
export const DEFAULT_ENTITY_CATALOG_ROWS: readonly CatalogRow[] = [
	{ key: 'hostility', label: 'Hostility', type: 'meter', host: 'relational', range: [0, 100], description: 'Open animosity toward the subject.' },
	{ key: 'trust', label: 'Trust', type: 'meter', host: 'relational', range: [0, 100], description: 'Willingness to believe the subject means them no harm.' },
	{ key: 'affection', label: 'Affection', type: 'meter', host: 'relational', range: [0, 100], description: 'Warmth and liking felt toward the subject.' },
	{ key: 'fear', label: 'Fear', type: 'meter', host: 'relational', range: [0, 100], description: 'Dread of the subject; high fear reads as flight or freezing.' },
	{ key: 'respect', label: 'Respect', type: 'meter', host: 'relational', range: [0, 100], description: "How much they credit the subject's competence and standing." },
	{ key: 'stress', label: 'Stress', type: 'meter', host: 'self', range: [0, 100], description: 'Pressure they are carrying right now.' },
	{ key: 'arousal', label: 'Arousal', type: 'meter', host: 'self', range: [0, 100], description: 'Physical charge currently running.' },
	{ key: 'energy', label: 'Energy', type: 'meter', host: 'self', range: [0, 100], description: 'How much they have left to spend.' },
	{ key: 'tension', label: 'Tension', type: 'meter', host: 'scene', range: [0, 10], description: 'How close the scene is to breaking.' },
	{ key: 'mood', label: 'Mood', type: 'trait', host: 'self', description: 'Their prevailing emotional colour this beat.' },
	{ key: 'weather', label: 'Weather', type: 'trait', host: 'scene', description: 'What the sky is doing where the scene is.' },
	{ key: 'tone', label: 'Tone', type: 'trait', host: 'scene', description: 'The register the prose is playing in.' },
	{ key: 'forecast', label: 'Forecast', type: 'trait', host: 'scene', description: 'Where the weather is heading.' },
	{ key: 'irony', label: 'Irony', type: 'trait', host: 'scene', description: 'How much the prose means the opposite of what it says.' }
];

/** A fresh, independently mutable copy (the `range` array is duplicated too). */
export function cloneCatalogRows(rows: readonly CatalogRow[]): CatalogRow[] {
	return rows.map((row) => (row.range ? { ...row, range: [...row.range] as [number, number] } : { ...row }));
}

/** Mock-mode asset; `null` = the asset is empty (GET serves the seed). */
let mockRows: CatalogRow[] | null = null;

/** Coerce one raw wire row, or null when it has no usable key. */
function normalizeRow(entry: unknown): CatalogRow | null {
	if (!entry || typeof entry !== 'object') return null;
	const e = entry as Record<string, unknown>;
	const key = str(e.key).trim();
	if (!key) return null;
	const type: CatalogRowType = e.type === 'trait' ? 'trait' : 'meter';
	const host: CatalogRowHost = e.host === 'self' || e.host === 'scene' ? e.host : 'relational';
	const row: CatalogRow = {
		key,
		label: str(e.label).trim() || key,
		type,
		host,
		description: str(e.description).trim()
	};
	const bounds = Array.isArray(e.range) ? e.range : null;
	if (type === 'meter' && bounds && bounds.length === 2) {
		const low = Number(bounds[0]);
		const high = Number(bounds[1]);
		if (Number.isFinite(low) && Number.isFinite(high)) row.range = [low, high];
	}
	return row;
}

/** Normalize the `{rows}` envelope. */
export function normalizeEntityCatalog(raw: Record<string, unknown>): CatalogRow[] {
	const source = Array.isArray(raw.rows) ? raw.rows : [];
	const rows: CatalogRow[] = [];
	for (const entry of source) {
		const row = normalizeRow(entry);
		if (row) rows.push(row);
	}
	return rows;
}

/**
 * The catalog's validation rules, in one place: the pane checks them before a PUT
 * (inline feedback) and the mock throws the same 400 the real endpoint would.
 * Mirrors `entity_catalog.validate_rows` — including the rule that a metered row
 * MUST carry an ascending range and a trait must carry none.
 */
export function catalogSetError(rows: readonly CatalogRow[]): string | null {
	const seen = new Set<string>();
	for (const [index, row] of rows.entries()) {
		const where = `rows[${index}]`;
		const key = row.key.trim();
		if (!/^[a-z][a-z0-9_]*$/.test(key)) {
			return `${where}: 'key' must be a lowercase slug (letters, digits, '_'), e.g. 'hostility'.`;
		}
		if (seen.has(key)) return `${where}: duplicate key '${key}'.`;
		seen.add(key);
		if (!row.label.trim()) return `${where}: enter a label.`;
		if (!row.description.trim()) return `${where}: enter a one-line description.`;
		if (row.type !== 'meter' && row.type !== 'trait') return `${where}: unknown type '${row.type}'.`;
		if (row.host !== 'relational' && row.host !== 'self' && row.host !== 'scene') {
			return `${where}: unknown host '${row.host}'.`;
		}
		if (row.type === 'meter') {
			const range = row.range;
			if (!range || range.length !== 2) {
				return `${where}: a meter needs a 'range' of two integers, e.g. [0, 100].`;
			}
			const [low, high] = range;
			if (!Number.isInteger(low) || !Number.isInteger(high) || low >= high) {
				return `${where}: 'range' must be two ascending integers, e.g. [0, 100].`;
			}
		} else if (row.range) {
			return `${where}: a trait carries no 'range' — free text has nothing to clamp.`;
		}
	}
	return null;
}

/**
 * LIST -> the effective catalog. A `null`/absent `range` on a meter row is legal
 * on the wire only in the sense that the server refuses it on the next PUT; the
 * normalizer keeps what it was given so the pane can show the problem.
 */
export async function getEntityCatalog(): Promise<CatalogRow[]> {
	if (USE_MOCK) {
		return cloneCatalogRows(mockRows ?? DEFAULT_ENTITY_CATALOG_ROWS);
	}
	const res = await fetch(ENTITY_CATALOG_ENDPOINT(), { headers: { Accept: 'application/json' } });
	if (!res.ok) throw await failure(res, 'getEntityCatalog');
	return normalizeEntityCatalog(await readJson(res));
}

/**
 * REPLACE -> PUT the whole catalog (the only write the endpoint has). The
 * response is what the server actually kept, so the pane re-seeds its draft from
 * it rather than from the request.
 */
export async function putEntityCatalog(rows: readonly CatalogRow[]): Promise<CatalogRow[]> {
	const body = { rows: cloneCatalogRows(rows) };
	if (USE_MOCK) {
		const problem = catalogSetError(body.rows);
		if (problem) throw new StreamError(problem, { reason: problem, status: 400 });
		mockRows = body.rows;
		return cloneCatalogRows(mockRows);
	}
	const res = await fetch(ENTITY_CATALOG_ENDPOINT(), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await failure(res, 'putEntityCatalog');
	return normalizeEntityCatalog(await readJson(res));
}

// --- the row set operations (ADD / EDIT / DELETE) ----------------------------
//
// Each returns a NEW array: the catalog is written whole, so a row edit is a set
// edit and the save is the single PUT. Keeping them here (not in the pane) means
// the next host — a popout, a script — can reuse the same semantics.

/** ADD one row (appended: catalog order is the order the palette renders). */
export function addCatalogRow(rows: readonly CatalogRow[], row: CatalogRow): CatalogRow[] {
	return [...cloneCatalogRows(rows), { ...row, range: row.range ? [...row.range] as [number, number] : undefined }];
}

/** EDIT one row in place, keyed by the row's CURRENT key (a rename is an edit). */
export function updateCatalogRow(
	rows: readonly CatalogRow[],
	currentKey: string,
	row: CatalogRow
): CatalogRow[] {
	return cloneCatalogRows(rows).map((existing) => (existing.key === currentKey ? { ...row } : existing));
}

/** DELETE one row by key. */
export function deleteCatalogRow(rows: readonly CatalogRow[], key: string): CatalogRow[] {
	return cloneCatalogRows(rows).filter((row) => row.key !== key);
}

/** One endpoint failure as the pane displays it (the API's own reason). */
async function failure(res: Response, call: string): Promise<StreamError> {
	const detail = await readErrorDetail(res);
	return new StreamError(detail.reason || `${call} failed: ${res.status} ${res.statusText}`, {
		...detail,
		status: res.status
	});
}
