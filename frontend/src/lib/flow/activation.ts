// Live activation derivation (W9.2, decision 6).
//
// Pure library helper (no Svelte, no chat coupling) so the chat panel AND the
// later Studio home (R11) derive the trail identically. The stage-name → node
// mapping is DATA read from `manifest.activation_map` — never a hardcoded
// constant in a component (requirement 5).

import type { ActivationMap, FlowManifest } from './types';

/** The minimal trace shape the helper needs (structurally compatible with the
 * chat store's `TurnStep` and `ProcessingStage`). */
export interface ActivationStep {
	/** Node/stage name (`narrate`, `dsl`, `critic`, …). */
	name: string;
	/** `start` while the step runs, `done` once its boundary closed. */
	state: 'start' | 'done';
}

/** The current frame shape (`ProcessingStage`); `stage` is the stage name. */
export interface ActivationStage {
	stage: string;
	state: 'start' | 'done';
}

/**
 * Fold the ordered turn trace plus the current frame into a per-node trail.
 *
 * The full trace yields the done trail (matching the persisted `turn_stages`
 * rehydration on reload); the current `processingStage` is authoritative for
 * the node it names. Nodes absent from the map stay absent ⇒ the renderer
 * shows them `pending` (static, honest — wave 1 adds no engine emission).
 */
export function activationFromSteps(
	manifest: FlowManifest,
	steps: readonly ActivationStep[],
	stage: ActivationStage | null
): ActivationMap {
	const map = manifest.activation_map ?? {};
	const out: ActivationMap = {};

	// Chronological fold: a later `start` after a `done` (regen re-emits the
	// critic slot) correctly reads back as `running`.
	for (const step of steps) {
		const id = map[step.name];
		if (!id) continue;
		out[id] = step.state === 'done' ? 'done' : 'running';
	}

	if (stage) {
		const id = map[stage.stage];
		if (id) out[id] = stage.state === 'done' ? 'done' : 'running';
	}

	return out;
}
