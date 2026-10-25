// Fabulist API client - narrator toggle settings + pinned default persona.

import { MOCK_PERSONAS } from './personas';
import { ENDPOINTS, StreamError, USE_MOCK, readErrorDetail, readJson } from './shared';
import type { FeatureDefinition, SettingsCatalog, ToggleDefinition } from './types';

/** Mock-mode toggle state (mirrors the backend's `settings_changed` fold). */
const mockToggles = new Map<string, boolean>();

/** Mock-mode engine FEATURE flags (issue #39: Anti Injection, default OFF). */
const mockFeatures = new Map<string, boolean>();

/** Mock feature catalog mirroring `fabulist.api.settings.FEATURE_FLAGS`. */
const MOCK_FEATURES: Omit<FeatureDefinition, 'enabled'>[] = [
	{ id: 'comfy.enabled', name: 'Image generation (ComfyUI)' },
	{ id: 'anti_injection', name: 'Anti Injection' }
];


/** Mock-mode pinned default persona (mirrors `default_persona_changed`). */
let mockDefaultPersonaId: string | null = null;

let mockDefaultPersonaLabel: string | null = null;


/** Static mock catalog mirroring `fabulist.prompts.parts` (mock mode only). */
const MOCK_CATALOG: Omit<ToggleDefinition, 'enabled'>[] = [
	{ id: 'director', label: 'Director', description: '', protected: true, core: true, tier: 1, priority: 100 },
	{ id: 'perspective', label: 'Perspective', description: '', protected: false, core: false, tier: 1, priority: 60 },
	{ id: 'no_user_control', label: 'User agency — never violate', description: '', protected: true, core: true, tier: 1, priority: 100 },
	{ id: 'npc_cognition', label: 'NPC autonomy and knowledge bounds', description: '', protected: false, core: false, tier: 1, priority: 55 },
	{ id: 'stop_and_pass', label: 'Stop at the choice point', description: '', protected: true, core: true, tier: 1, priority: 100 },
	{ id: 'length_control', label: 'Length and pacing', description: '', protected: false, core: false, tier: 2, priority: 50 },
	{ id: 'no_parroting', label: 'No parroting', description: '', protected: true, core: true, tier: 2, priority: 100 },
	{ id: 'anti_slop', label: 'Prose style', description: '', protected: true, core: true, tier: 3, priority: 100 },
	{ id: 'panel_placement', label: 'Panel placement', description: '', protected: true, core: false, tier: 4, priority: 95 },
	{ id: 'visual_panels', label: 'Visual panels', description: '', protected: false, core: false, tier: 4, priority: 40 },
	{ id: 'world_facts', label: 'World facts', description: '', protected: false, core: false, tier: 0, priority: 70 },
	{ id: 'hard_prohibition', label: 'Hard prohibition', description: '', protected: false, core: false, tier: 0, priority: 80 },
	{ id: 'ooc', label: 'Out of character (OOC)', description: '', protected: false, core: false, tier: 0, priority: 65 }
];


function mockSettingsCatalog(): SettingsCatalog {
	const toggles: Record<string, ToggleDefinition> = {};
	const order: string[] = [];
	const protectedIds: string[] = [];
	const coreIds: string[] = [];
	for (const entry of MOCK_CATALOG) {
		toggles[entry.id] = { ...entry, enabled: mockToggles.get(entry.id) ?? true };
		order.push(entry.id);
		if (entry.protected) protectedIds.push(entry.id);
		if (entry.core) coreIds.push(entry.id);
	}
	const features: Record<string, FeatureDefinition> = {};
	for (const entry of MOCK_FEATURES) {
		features[entry.id] = { ...entry, enabled: mockFeatures.get(entry.id) ?? false };
	}
	return {
		toggles,
		features,
		default_persona_id: mockDefaultPersonaId,
		default_persona_label: mockDefaultPersonaLabel,
		meta: {
			order,
			count: order.length,
			protected: protectedIds,
			core: coreIds,
			updated_at: null,
			source: 'mock'
		}
	};
}


/** Read the folded toggle catalog (W2.11). */
export async function getSettings(): Promise<SettingsCatalog> {
	if (USE_MOCK) return mockSettingsCatalog();

	const res = await fetch(ENDPOINTS.settings(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`getSettings failed: ${res.status} ${res.statusText}`);
	}
	return normalizeSettings(await readJson(res));
}


/**
 * Persist toggle changes. The backend validates atomically and returns the new
 * catalog; a 422 carries the refusal reason for the UI to surface.
 */
export async function patchSettings(
	toggles: Record<string, boolean>
): Promise<SettingsCatalog> {
	if (USE_MOCK) {
		for (const [id, value] of Object.entries(toggles)) mockToggles.set(id, value);
		return mockSettingsCatalog();
	}

	const res = await fetch(ENDPOINTS.settings(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ toggles })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `patchSettings failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	return normalizeSettings(await readJson(res));
}


/**
 * Persist engine FEATURE flags (`PATCH {features}` — issue #39's Anti Injection
 * toggle). The backend validates ids atomically and returns the new catalog; a
 * 404 names the unknown id. The engine reads the folded value PER PROMPT BUILD,
 * so an accepted write applies to the next turn without a restart.
 */
export async function patchFeatures(
	features: Record<string, boolean>
): Promise<SettingsCatalog> {
	if (USE_MOCK) {
		for (const [id, value] of Object.entries(features)) mockFeatures.set(id, value);
		return mockSettingsCatalog();
	}

	const res = await fetch(ENDPOINTS.settings(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ features })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `patchFeatures failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	return normalizeSettings(await readJson(res));
}


function normalizeSettings(raw: Record<string, unknown>): SettingsCatalog {
	const rawToggles = raw.toggles && typeof raw.toggles === 'object' ? raw.toggles : {};
	const toggles: Record<string, ToggleDefinition> = {};
	for (const [id, value] of Object.entries(rawToggles as Record<string, unknown>)) {
		if (!value || typeof value !== 'object') continue;
		const t = value as Record<string, unknown>;
		toggles[id] = {
			id: typeof t.id === 'string' ? t.id : id,
			label: typeof t.label === 'string' ? t.label : id,
			description: typeof t.description === 'string' ? t.description : '',
			enabled: t.enabled !== false,
			protected: t.protected === true,
			core: t.core === true,
			tier: typeof t.tier === 'number' ? t.tier : 0,
			priority: typeof t.priority === 'number' ? t.priority : 0
		};
	}
	const rawFeatures = raw.features && typeof raw.features === 'object' ? raw.features : {};
	const features: Record<string, FeatureDefinition> = {};
	for (const [id, value] of Object.entries(rawFeatures as Record<string, unknown>)) {
		if (!value || typeof value !== 'object') continue;
		const f = value as Record<string, unknown>;
		features[id] = {
			id: typeof f.id === 'string' ? f.id : id,
			name: typeof f.name === 'string' ? f.name : id,
			enabled: f.enabled === true
		};
	}
	const meta = raw.meta && typeof raw.meta === 'object' ? (raw.meta as SettingsCatalog['meta']) : {};
	return {
		toggles,
		features,
		default_persona_id:
			typeof raw.default_persona_id === 'string' ? raw.default_persona_id : null,
		default_persona_label:
			typeof raw.default_persona_label === 'string' ? raw.default_persona_label : null,
		meta
	};
}


/**
 * Pin (or clear) the global default persona (`PATCH /api/settings`).
 *
 * `personaId = null` sends an explicit `default_persona_id: null` (unpin).
 * Returns the folded catalog so the caller can refresh its view; a rejected
 * id surfaces the server's reason (unknown persona -> 404).
 */
export async function patchDefaultPersona(
	personaId: string | null
): Promise<SettingsCatalog> {
	if (USE_MOCK) {
		mockDefaultPersonaId = personaId;
		mockDefaultPersonaLabel = personaId
			? (MOCK_PERSONAS.find((p) => p.persona_id === personaId)?.name ?? null)
			: null;
		return mockSettingsCatalog();
	}

	const res = await fetch(ENDPOINTS.settings(), {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ default_persona_id: personaId })
	});
	if (!res.ok) {
		const detail = await readErrorDetail(res);
		throw new StreamError(
			detail.reason || `patchDefaultPersona failed: ${res.status} ${res.statusText}`,
			{ ...detail, status: res.status }
		);
	}
	return normalizeSettings(await readJson(res));
}
