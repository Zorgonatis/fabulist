// Appearance API client (issue #12 + #13, ADR-027) — preset, CSS override, wallpaper.
//
// The backend surface (src/fabulist/api/appearance_api.py):
//
//   GET    {API_BASE}/api/appearance  -> {preset, custom_css, wallpaper, fit,
//                                        align, alignments[], fits,
//                                        wallpaper_limits, presets[], guard{}}
//   PATCH  {API_BASE}/api/appearance  {"preset"?, "custom_css"?, "fit"?, "align"?}
//        -> the same body plus `dropped[]`: every declaration the guard refused
//   POST   {API_BASE}/api/appearance/wallpaper   multipart `file` -> the same body
//   GET    {API_BASE}/api/appearance/wallpaper   the image bytes
//   DELETE {API_BASE}/api/appearance/wallpaper   -> the same body
//
// This is USER state (data/appearance.json + data/appearance/wallpaper.<ext>),
// not the event log, and it is NOT session-scoped: one appearance per install,
// whether or not a chat is open.
//
// `wallpaper.url` is a PATH on the engine; this module resolves it against
// API_BASE and appends the version (the file's mtime), so a re-upload of the same
// format gets a new URL and the immutable cache header is honest.
//
// The precedence the UI implements (owner ruling D-D):
//   base (app.css :root) -> preset (data-theme) -> user CSS -> session tone
// and the wallpaper layer (issue #13) is a fourth, orthogonal step that only
// re-solves tokens under `<html data-wallpaper>`.
// See `#lib/theme/apply` for the DOM half and `app.css` for the tokens.
//
// ENDPOINTS lives here rather than in `api/shared` because this module is
// self-contained (the same reason `api/hud` and `api/connections` carry their
// own thin clients): one caller, one surface.

import { API_BASE, USE_MOCK, readErrorDetail } from './shared';

/** One selectable preset (light / dark / oled / savilen). */
export interface AppearancePreset {
	id: string;
	label: string;
	color_scheme: 'light' | 'dark';
}

/** One reading-column position (issue #41: left / center / right). */
export interface AppearanceAlignment {
	id: string;
	label: string;
}

/** What a pasted stylesheet may and may not do (enforced by the backend guard). */
export interface AppearanceGuard {
	/** Property allowlist — everything else is dropped on write. */
	allowed: string[];
	/** The must-not families, keyed by family with a human description. */
	blocked: Record<string, string>;
	/** Custom-property prefixes a paste may not declare (--z-*, --font-*, …). */
	reserved_custom_properties: string[];
	/** The readable-type floor a pasted font-size must clear (14). */
	min_font_px: number;
	max_custom_css_chars: number;
	/** The one sentence the drawer shows about tone vs. pasted CSS (!important). */
	important_note: string;
}

/** The set wallpaper, as the drawer and `apply.ts` need it. */
export interface WallpaperInfo {
	/** The stored file's name (`wallpaper.jpg`) — for the "what is set" line. */
	name: string;
	/** Absolute URL, already resolved against API_BASE and versioned. */
	url: string;
	/** The file's mtime, used as the cache key (`?v=`). */
	version: string;
	mime: string;
	format: string;
	bytes: number;
	width: number;
	height: number;
	/** How it meets the viewport — `cover` (the only mode this issue ships). */
	fit: string;
}

/** The upload caps the drawer states up front (they come from the backend). */
export interface WallpaperLimits {
	max_bytes: number;
	max_bytes_label: string;
	max_edge: number;
	formats: Array<{ label: string; mime: string }>;
	fits: string[];
	default_fit: string;
}

/** The current appearance plus everything the drawer needs to render itself. */
export interface AppearanceState {
	preset: string;
	custom_css: string;
	/** The image behind the prose — `null` is the shipped default (no wallpaper). */
	wallpaper: WallpaperInfo | null;
	fit: string;
	/** Where the reading column sits (`left` / `center` / `right`, issue #41). */
	align: string;
	/** The alignment catalog, in left → center → right order. */
	alignments: AppearanceAlignment[];
	fits: string[];
	wallpaper_limits: WallpaperLimits;
	presets: AppearancePreset[];
	guard: AppearanceGuard;
}

/** One declaration the guard refused, with the reason to show the reader. */
export interface DroppedDeclaration {
	selector: string;
	declaration: string;
	reason: string;
}

export interface AppearancePatchResult extends AppearanceState {
	dropped: DroppedDeclaration[];
}

export const APPEARANCE_ENDPOINTS = {
	appearance: () => `${API_BASE}/api/appearance`,
	wallpaper: () => `${API_BASE}/api/appearance/wallpaper`
} as const;

export const DEFAULT_PRESET = 'dark';

/** The shipped alignment: centered is today's reading column (issue #22). */
export const DEFAULT_ALIGN = 'center';

/**
 * Presentation metadata for the theme selector (issue #37 owner ruling).
 *
 * The order is DERIVED, never hand-maintained: greyscale themes first, white →
 * black by descending page lightness, then coloured themes in RGB order by
 * ascending primary hue (the backend's `fabulist.theme.appearance.PRESET_META`
 * carries the same table; this mirror exists only for the offline/mock catalog).
 */
const PRESET_ORDER: Record<string, { greyscale: boolean; lightness: number; hue: number }> = {
	light: { greyscale: true, lightness: 96.1, hue: 0 },
	dark: { greyscale: true, lightness: 7, hue: 0 },
	oled: { greyscale: true, lightness: 0, hue: 0 },
	savilen: { greyscale: false, lightness: 0, hue: 175.2 }
};

/** Sort key for the selector; unknown ids land after every known theme. */
function presetOrderKey(preset: AppearancePreset): [number, number, number] {
	const meta = PRESET_ORDER[preset.id];
	if (!meta) return [2, 0, 0];
	return meta.greyscale ? [0, -meta.lightness, 0] : [1, meta.hue, 0];
}

/** Order a catalog by the ruled theme order (greyscale white→black, then hue). */
function orderPresets(presets: AppearancePreset[]): AppearancePreset[] {
	return [...presets].sort((a, b) => {
		const left = presetOrderKey(a);
		const right = presetOrderKey(b);
		return left[0] - right[0] || left[1] - right[1] || left[2] - right[2];
	});
}

/** Preset catalog used when the engine is unreachable or in mock mode. */
const FALLBACK_PRESETS: AppearancePreset[] = orderPresets([
	{ id: 'light', label: 'Light', color_scheme: 'light' },
	{ id: 'dark', label: 'Dark', color_scheme: 'dark' },
	{ id: 'oled', label: 'OLED', color_scheme: 'dark' },
	{ id: 'savilen', label: 'Savilen', color_scheme: 'dark' }
]);

/** Alignment catalog used when the engine is unreachable or in mock mode. */
const FALLBACK_ALIGNMENTS: AppearanceAlignment[] = [
	{ id: 'left', label: 'Left' },
	{ id: 'center', label: 'Center' },
	{ id: 'right', label: 'Right' }
];

/** Guard rules the drawer renders before any response has arrived (mock/offline). */
const FALLBACK_GUARD: AppearanceGuard = {
	allowed: [],
	blocked: {},
	reserved_custom_properties: ['--z-', '--font-', '--reading-'],
	min_font_px: 14,
	max_custom_css_chars: 20000,
	important_note:
		'The scene’s tone colours are applied inline and outrank this box — add !important to a declaration if you want to pin that colour regardless.'
};

/** The caps the drawer states before a read lands (mirror of the backend's). */
const FALLBACK_LIMITS: WallpaperLimits = {
	max_bytes: 8 * 1024 * 1024,
	max_bytes_label: '8 MB',
	max_edge: 4096,
	formats: [
		{ label: 'PNG', mime: 'image/png' },
		{ label: 'JPEG', mime: 'image/jpeg' },
		{ label: 'GIF', mime: 'image/gif' },
		{ label: 'WebP', mime: 'image/webp' }
	],
	fits: ['cover'],
	default_fit: 'cover'
};

/** Mock-mode appearance (states are per-install, so one module-level value). */
let mockPreset = DEFAULT_PRESET;
let mockCss = '';
let mockWallpaper: WallpaperInfo | null = null;
let mockAlign = DEFAULT_ALIGN;

function mockState(): AppearanceState {
	return {
		preset: mockPreset,
		custom_css: mockCss,
		wallpaper: mockWallpaper,
		fit: mockWallpaper?.fit ?? FALLBACK_LIMITS.default_fit,
		align: mockAlign,
		alignments: FALLBACK_ALIGNMENTS,
		fits: FALLBACK_LIMITS.fits,
		wallpaper_limits: FALLBACK_LIMITS,
		presets: FALLBACK_PRESETS,
		guard: FALLBACK_GUARD
	};
}

/** Resolve the engine's wallpaper path into a cacheable absolute URL. */
function wallpaperUrl(path: string, version: string): string {
	const base = `${API_BASE}${path}`;
	return version ? `${base}?v=${encodeURIComponent(version)}` : base;
}

function normalizeWallpaper(raw: unknown): WallpaperInfo | null {
	if (!raw || typeof raw !== 'object') return null;
	const entry = raw as Record<string, unknown>;
	const path = typeof entry.url === 'string' ? entry.url : '';
	if (!path) return null;
	const version = typeof entry.version === 'string' ? entry.version : '';
	const num = (value: unknown): number => (typeof value === 'number' ? value : 0);
	return {
		name: typeof entry.name === 'string' ? entry.name : '',
		url: wallpaperUrl(path, version),
		version,
		mime: typeof entry.mime === 'string' ? entry.mime : '',
		format: typeof entry.format === 'string' ? entry.format : '',
		bytes: num(entry.bytes),
		width: num(entry.width),
		height: num(entry.height),
		fit: typeof entry.fit === 'string' && entry.fit ? entry.fit : FALLBACK_LIMITS.default_fit
	};
}

function normalizeLimits(raw: Record<string, unknown>): WallpaperLimits {
	const limits = raw.wallpaper_limits;
	if (!limits || typeof limits !== 'object') return FALLBACK_LIMITS;
	const entry = limits as Record<string, unknown>;
	const formats = Array.isArray(entry.formats)
		? entry.formats
				.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
				.map((item) => ({
					label: typeof item.label === 'string' ? item.label : '',
					mime: typeof item.mime === 'string' ? item.mime : ''
				}))
				.filter((item) => item.label && item.mime)
		: FALLBACK_LIMITS.formats;
	const fits = Array.isArray(entry.fits)
		? entry.fits.filter((item): item is string => typeof item === 'string')
		: FALLBACK_LIMITS.fits;
	return {
		max_bytes:
			typeof entry.max_bytes === 'number' ? entry.max_bytes : FALLBACK_LIMITS.max_bytes,
		max_bytes_label:
			typeof entry.max_bytes_label === 'string'
				? entry.max_bytes_label
				: FALLBACK_LIMITS.max_bytes_label,
		max_edge: typeof entry.max_edge === 'number' ? entry.max_edge : FALLBACK_LIMITS.max_edge,
		formats: formats.length ? formats : FALLBACK_LIMITS.formats,
		fits: fits.length ? fits : FALLBACK_LIMITS.fits,
		default_fit:
			typeof entry.default_fit === 'string' && entry.default_fit
				? entry.default_fit
				: FALLBACK_LIMITS.default_fit
	};
}

function normalize(raw: Record<string, unknown>): AppearanceState {
	const presets: AppearancePreset[] = Array.isArray(raw.presets)
		? raw.presets
				.filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
				.map((entry) => ({
					id: typeof entry.id === 'string' ? entry.id : String(entry.id ?? ''),
					label: typeof entry.label === 'string' ? entry.label : String(entry.id ?? ''),
					color_scheme: entry.color_scheme === 'light' ? 'light' : 'dark'
				}))
		: FALLBACK_PRESETS;
	const guardRaw =
		raw.guard && typeof raw.guard === 'object' ? (raw.guard as Record<string, unknown>) : {};
	const limits = normalizeLimits(raw);
	const wallpaper = normalizeWallpaper(raw.wallpaper);
	return {
		preset: typeof raw.preset === 'string' && raw.preset ? raw.preset : DEFAULT_PRESET,
		custom_css: typeof raw.custom_css === 'string' ? raw.custom_css : '',
		wallpaper,
		fit:
			typeof raw.fit === 'string' && raw.fit
				? raw.fit
				: (wallpaper?.fit ?? limits.default_fit),
		align: typeof raw.align === 'string' && raw.align ? raw.align : DEFAULT_ALIGN,
		alignments: Array.isArray(raw.alignments)
			? raw.alignments
					.filter(
						(entry): entry is Record<string, unknown> =>
							!!entry && typeof entry === 'object'
					)
					.map((entry) => ({
						id: typeof entry.id === 'string' ? entry.id : String(entry.id ?? ''),
						label:
							typeof entry.label === 'string' ? entry.label : String(entry.id ?? '')
					}))
			: FALLBACK_ALIGNMENTS,
		fits: limits.fits,
		wallpaper_limits: limits,
		presets: presets.length ? orderPresets(presets) : FALLBACK_PRESETS,
		guard: {
			allowed: Array.isArray(guardRaw.allowed)
				? guardRaw.allowed.filter((p): p is string => typeof p === 'string')
				: FALLBACK_GUARD.allowed,
			blocked:
				guardRaw.blocked && typeof guardRaw.blocked === 'object'
					? (guardRaw.blocked as Record<string, string>)
					: FALLBACK_GUARD.blocked,
			reserved_custom_properties: Array.isArray(guardRaw.reserved_custom_properties)
				? guardRaw.reserved_custom_properties.filter((p): p is string => typeof p === 'string')
				: FALLBACK_GUARD.reserved_custom_properties,
			min_font_px:
				typeof guardRaw.min_font_px === 'number'
					? guardRaw.min_font_px
					: FALLBACK_GUARD.min_font_px,
			max_custom_css_chars:
				typeof guardRaw.max_custom_css_chars === 'number'
					? guardRaw.max_custom_css_chars
					: FALLBACK_GUARD.max_custom_css_chars,
			important_note:
				typeof guardRaw.important_note === 'string'
					? guardRaw.important_note
					: FALLBACK_GUARD.important_note
		}
	};
}

/** Read the install's appearance (never session-scoped). */
export async function getAppearance(): Promise<AppearanceState> {
	if (USE_MOCK) return mockState();
	const res = await fetch(APPEARANCE_ENDPOINTS.appearance(), {
		headers: { Accept: 'application/json' }
	});
	if (!res.ok) {
		throw new Error(`getAppearance failed: ${res.status} ${res.statusText}`);
	}
	return normalize((await res.json()) as Record<string, unknown>);
}

/**
 * Persist the preset and/or the pasted stylesheet.
 *
 * Only the keys passed are written (`custom_css: ''` clears the override), so the
 * drawer can retune the preset without re-sending the stylesheet, and the
 * alignment can move without touching either. The response
 * carries `dropped[]` — the guard's refusals — which the drawer lists; a rejected
 * write (unknown preset / oversized CSS) surfaces the server's reason.
 */
export async function patchAppearance(patch: {
	preset?: string;
	custom_css?: string;
	fit?: string;
	align?: string;
}): Promise<AppearancePatchResult> {
	if (USE_MOCK) {
		if (patch.preset !== undefined) mockPreset = patch.preset;
		if (patch.custom_css !== undefined) mockCss = patch.custom_css;
		if (patch.fit !== undefined && mockWallpaper) mockWallpaper = { ...mockWallpaper, fit: patch.fit };
		if (patch.align !== undefined) mockAlign = patch.align;
		return { ...mockState(), dropped: [] };
	}
	const res = await fetch(APPEARANCE_ENDPOINTS.appearance(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(patch)
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new Error(detail.reason || `patchAppearance failed: ${res.status}`);
	}
	const raw = (await res.json()) as Record<string, unknown>;
	const dropped = Array.isArray(raw.dropped)
		? raw.dropped
				.filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
				.map((entry) => ({
					selector: typeof entry.selector === 'string' ? entry.selector : '',
					declaration: typeof entry.declaration === 'string' ? entry.declaration : '',
					reason: typeof entry.reason === 'string' ? entry.reason : ''
				}))
		: [];
	return { ...normalize(raw), dropped };
}

/**
 * Upload the reader's wallpaper (multipart `file`), replacing any previous one.
 *
 * The backend does the validating — format, 8 MB, 4096px an edge — and answers a
 * 422 whose ``detail`` is the sentence the drawer shows; this function does not
 * pre-check, so there is exactly one place that decides what an acceptable image
 * is (the same rule the guard follows for pasted CSS).
 */
export async function uploadWallpaper(file: File): Promise<AppearanceState> {
	if (USE_MOCK) {
		mockWallpaper = {
			name: file.name,
			url: typeof URL !== 'undefined' && URL.createObjectURL ? URL.createObjectURL(file) : '',
			version: 'mock',
			mime: file.type || 'image/png',
			format: 'Mock',
			bytes: file.size,
			width: 0,
			height: 0,
			fit: FALLBACK_LIMITS.default_fit
		};
		return mockState();
	}
	const body = new FormData();
	body.append('file', file, file.name);
	const res = await fetch(APPEARANCE_ENDPOINTS.wallpaper(), { method: 'POST', body });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new Error(detail.reason || `uploadWallpaper failed: ${res.status}`);
	}
	return normalize((await res.json()) as Record<string, unknown>);
}

/** Remove the wallpaper (the shipped default is "none"); answers the new state. */
export async function removeWallpaper(): Promise<AppearanceState> {
	if (USE_MOCK) {
		mockWallpaper = null;
		return mockState();
	}
	const res = await fetch(APPEARANCE_ENDPOINTS.wallpaper(), { method: 'DELETE' });
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new Error(detail.reason || `removeWallpaper failed: ${res.status}`);
	}
	return normalize((await res.json()) as Record<string, unknown>);
}
