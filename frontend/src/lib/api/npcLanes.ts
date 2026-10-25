// NPC lanes configuration (issue #56) — the ONE client behind every lane control.
//
// The owner ruling: lanes are configured where NPCs are managed (the cast pane's
// quick options) and on a studio page, not only beside a running turn. All of
// those surfaces edit the SAME thing — topology data on the fan-out row
// (`npc_lanes.data.enabled` switches lanes off, `npc_lanes.data.max_lanes` is
// how many run at once). There is no lane-settings endpoint and there should not
// be one: the running graph is compiled from the topology (ADR-009), so the
// validated topology door (`PUT /api/prompts/topology`) is the only write that
// can be honoured, and a save it refuses comes back with the engine's own
// reason (a violation, named).
//
// `getTopology` / `putTopology` / `violationsFromError` are reused from
// `./flow` — this module knows only which row carries the settings, how to read
// them, and how to publish the stored value so two open surfaces never disagree.
import { writable } from 'svelte/store';
import { cloneTopology } from '#lib/flow/editor';
import type { Topology, TopologyNode } from '#lib/flow/types';
import { getTopology, putTopology, violationsFromError } from './flow';

/** The canonical fan-out row the lane settings live on (`graph.CANONICAL_TOPOLOGY`). */
export const LANE_NODE_ID = 'npc_lanes';
/** Width the engine uses when the row does not set one (`nodes.py` falls back to 4). */
export const LANE_DEFAULT_MAX = 4;
/** The widths the owner may pick (a lane pool of 1..8). */
export const LANE_MIN_MAX = 1;
export const LANE_MAX_MAX = 8;
/** The select's options, in order. */
export const LANE_WIDTHS: readonly number[] = [1, 2, 3, 4, 5, 6, 7, 8];

/** What the stored topology says about the lanes, as the owner reads it. */
export interface LaneConfig {
	/** False once the lane row's data switches the whole stage off. */
	enabled: boolean;
	/** How many lanes may run at once. */
	maxLanes: number;
}

/**
 * The values `data.enabled` may carry that read as OFF.
 *
 * Mirrors `fabulist.graph.contracts.stage_disabled`: a hand-edited preset can
 * hand back `false`, `"false"`, `0` or a word, and an absent key is ON.
 */
const DISABLED_WORDS = new Set(['false', '0', 'off', 'no', 'disabled']);

function readEnabled(data: Record<string, unknown>): boolean {
	const value = 'enabled' in data ? data.enabled : true;
	if (typeof value === 'string') return !DISABLED_WORDS.has(value.trim().toLowerCase());
	return Boolean(value);
}

/** The lane row of a topology, or `null` when the flow has no lane step at all. */
export function findLaneNode(topology: Topology | null): TopologyNode | null {
	if (!topology) return null;
	return topology.nodes.find((node) => node.id === LANE_NODE_ID) ?? null;
}

/**
 * What a topology says about the lanes, or `null` when it carries no lane step
 * (a from-scratch graph without the fan-out — nothing to switch, nothing to
 * widen). A width outside the owner's 1..8 range (a hand-edited preset) is shown
 * at the nearest end of it; the next pick writes a legal one.
 */
export function laneConfigOf(topology: Topology | null): LaneConfig | null {
	const node = findLaneNode(topology);
	if (!node) return null;
	const data = (node.data ?? {}) as Record<string, unknown>;
	const raw = Number(data.max_lanes);
	const width = Number.isFinite(raw) && raw > 0 ? Math.round(raw) : LANE_DEFAULT_MAX;
	return {
		enabled: readEnabled(data),
		maxLanes: Math.min(LANE_MAX_MAX, Math.max(LANE_MIN_MAX, width))
	};
}

/**
 * The stored lane config, shared by every open surface.
 *
 * Both the cast pane and the studio page write through the same door, so a save
 * in one must show up in the other without waiting for a reload: whatever the
 * door answers (a read, or the stored topology a save echoed back) lands here,
 * and each surface renders this value. `null` = the flow carries no lane step.
 */
export const laneConfigStore = writable<LaneConfig | null>(null);

/** Read the stored lane config and publish it. Throws when the engine is unreachable. */
export async function readLaneConfig(): Promise<LaneConfig | null> {
	const body = await getTopology();
	const config = laneConfigOf(body.topology);
	laneConfigStore.set(config);
	return config;
}

/**
 * Write one lane setting to the stored topology and return what the engine kept.
 *
 * The base is read FRESH inside the write (a read-modify-write): a width pick in
 * the cast pane can never resurrect a stale copy of the wiring that the studio
 * page, the graph editor or the flow panel changed a moment ago. The whole
 * topology goes back because that is what the door takes, so a refused save
 * leaves the previous, compilable topology in place.
 */
export async function writeLaneConfig(patch: {
	enabled?: boolean;
	maxLanes?: number;
}): Promise<LaneConfig> {
	const body = await getTopology();
	const next = cloneTopology(body.topology);
	const target = next.nodes.find((node) => node.id === LANE_NODE_ID);
	if (!target) {
		throw new Error('This flow has no NPC lane step to configure.');
	}
	target.data = {
		...(target.data ?? {}),
		...(patch.enabled === undefined ? {} : { enabled: patch.enabled }),
		...(patch.maxLanes === undefined ? {} : { max_lanes: patch.maxLanes })
	};
	const saved = await putTopology(next);
	const config = laneConfigOf(saved.topology);
	if (!config) {
		throw new Error('This flow has no NPC lane step to configure.');
	}
	laneConfigStore.set(config);
	return config;
}

/**
 * The sentence to show when a save was refused.
 *
 * A refused write carries the door's own words — the validator's violation
 * message, which names exactly what about the wiring it could not honour. Any
 * other failure (no engine, a dropped connection) gets a plain sentence.
 */
export function laneRefusal(err: unknown): string {
	const violations = violationsFromError(err);
	if (violations.length > 0) return violations[0].message;
	return err instanceof TypeError
		? 'The engine is not answering, so nothing changed.'
		: 'The engine did not accept that change.';
}
