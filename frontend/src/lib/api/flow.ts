// Fabulist API client — flow manifest (W9.2) + turn topology (W9.5).
//
//   GET {API_BASE}/api/flow/manifest  -> FlowManifest (schema_version 1)
//   GET {API_BASE}/api/prompts/topology -> {topology, canonical, violations}
//   PUT {API_BASE}/api/prompts/topology {topology} -> 200 {topology, violations}
//                                                  | 400 {detail, violations}
//
// The manifest DESCRIBES the flow (read-only, C1); the topology is what the
// engine EXECUTES (the preset's `topology`, seeded from
// `graph.CANONICAL_TOPOLOGY` — W9.6). The editor writes only the topology.
//
// `MOCK_FLOW_MANIFEST` is the plan's filled R5 example and `MOCK_TOPOLOGY` is
// the engine's canonical wiring, kept self-contained so the component and the
// chat panel can be built (and `VITE_MOCK=1` demos run) before the backend
// endpoints land. They mirror the `MOCK_PROMPT_STEPS` pattern.

import { API_BASE, StreamError, USE_MOCK, readJson, str } from './shared';
import { cloneTopology, edgeId, validateLocal } from '#lib/flow/editor';
import type {
	FlowEdge,
	FlowEdgeKind,
	FlowGraph,
	FlowManifest,
	FlowNode,
	FlowNodeAgent,
	FlowNodeInspector,
	FlowNodeKind,
	FlowNodeMeta,
	FlowNodeRuns,
	FlowRegion,
	FlowRunKind,
	FlowSubGraph,
	InspectorKind,
	PromptDoor,
	RegionKind,
	Topology,
	TopologyEdge,
	TopologyNode,
	TopologyViolation
} from '#lib/flow/types';

/** Endpoint paths, kept in one place so the orchestrator can rewire them. */
export const FLOW_ENDPOINTS = {
	manifest: () => `${API_BASE}/api/flow/manifest`,
	topology: () => `${API_BASE}/api/prompts/topology`,
	lanes: (sessionId: string) =>
		`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/lanes`
} as const;

/** One lane of the last turn's NPC fan-out (W9.8: draw the ACTUAL members). */
export interface FlowLane {
	member_id: string | null;
	name: string | null;
	intent: string | null;
	ok: boolean;
	/**
	 * Issue #56.1: the member is PLANNED and its lane is still in flight — the
	 * dispatcher recorded the set, the fan-in join has not answered yet. The
	 * drawer draws these arms as running (no intent to show).
	 */
	pending?: boolean;
}

export interface FlowLanes {
	session_id: string;
	lanes: FlowLane[];
	source: string | null;
	/** Effective fan-out width of the STORED topology (issue #56.2). */
	max_parallel: number;
	/** False when the stage is switched off (`data.enabled = false`, #56.3). */
	enabled: boolean;
	/** True while the turn's lanes are planned but not yet answered. */
	in_flight: boolean;
	turn_seq: number | null;
}

const EMPTY_LANES: FlowLanes = {
	session_id: '',
	lanes: [],
	source: null,
	max_parallel: 4,
	enabled: true,
	in_flight: false,
	turn_seq: null
};

/** The last turn's lane set, or an empty set (a session with no fan-out). */
export async function getLanes(sessionId: string | null): Promise<FlowLanes> {
	if (!sessionId) return { ...EMPTY_LANES };
	if (USE_MOCK) {
		return {
			...EMPTY_LANES,
			session_id: sessionId,
			source: 'presence',
			lanes: [
				{ member_id: 'cm_a', name: 'Kael', intent: 'press for the ledger', ok: true },
				{ member_id: 'cm_b', name: 'Mara', intent: 'warn him off', ok: true },
				{ member_id: 'cm_c', name: 'Vess', intent: 'watch the door', ok: true }
			],
			turn_seq: 1
		};
	}
	try {
		const res = await fetch(FLOW_ENDPOINTS.lanes(sessionId), {
			headers: { Accept: 'application/json' }
		});
		if (!res.ok) return { ...EMPTY_LANES, session_id: sessionId };
		const raw = (await readJson(res)) as Record<string, unknown>;
		const lanes = Array.isArray(raw.lanes)
			? (raw.lanes as Array<Record<string, unknown>>).map((row) => ({
					member_id: str(row.member_id) || null,
					name: str(row.name) || null,
					intent: str(row.intent) || null,
					ok: row.ok !== false,
					pending: row.pending === true
				}))
			: [];
		return {
			session_id: sessionId,
			lanes,
			source: str(raw.source) || null,
			max_parallel: Number(raw.max_parallel) || 4,
			enabled: raw.enabled !== false,
			in_flight: raw.in_flight === true,
			turn_seq: typeof raw.turn_seq === 'number' ? raw.turn_seq : null
		};
	} catch {
		return { ...EMPTY_LANES, session_id: sessionId };
	}
}

const NODE_KINDS: FlowNodeKind[] = ['stage', 'gate', 'branch', 'trigger', 'terminal'];
const RUN_KINDS: FlowRunKind[] = ['model_pass', 'fixed', 'engine', 'hook', 'embedder'];
const EDGE_KINDS: FlowEdgeKind[] = ['flow', 'branch', 'loop_back', 'attach'];
const REGION_KINDS: RegionKind[] = ['stable_prefix', 'dynamic_tail', 'neutral'];
const INSPECTOR_KINDS: InspectorKind[] = ['model_pass', 'fixed_stage', 'gate', 'terminal'];

function oneOf<T extends string>(value: unknown, allowed: T[], fallback: T): T {
	return typeof value === 'string' && (allowed as string[]).includes(value)
		? (value as T)
		: fallback;
}

function nullableStr(value: unknown): string | null {
	return typeof value === 'string' ? value : null;
}

function strArray(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function stringMap(value: unknown): Record<string, string> {
	const out: Record<string, string> = {};
	if (value && typeof value === 'object') {
		for (const [key, val] of Object.entries(value as Record<string, unknown>)) {
			if (typeof val === 'string') out[key] = val;
		}
	}
	return out;
}

function normalizeRuns(raw: unknown): FlowNodeRuns {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	return {
		kind: oneOf(o.kind, RUN_KINDS, 'fixed'),
		interaction: nullableStr(o.interaction),
		call_site: nullableStr(o.call_site),
		description: str(o.description)
	};
}

function normalizeAgent(raw: unknown): FlowNodeAgent | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	return {
		interaction: nullableStr(o.interaction),
		default_agent: nullableStr(o.default_agent),
		bound_agent: nullableStr(o.bound_agent),
		source: o.source === 'engine' ? 'engine' : 'agent_bindings'
	};
}

function normalizeDoor(raw: unknown): PromptDoor {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	if (o.door === 'step_editor' && typeof o.step_id === 'string') {
		return { door: 'step_editor', step_id: o.step_id };
	}
	if (o.door === 'none') return { door: 'none', reason: str(o.reason) };
	return { door: 'fixed', reason: str(o.reason) };
}

function normalizeInspector(raw: unknown): FlowNodeInspector {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	return {
		kind: oneOf(o.kind, INSPECTOR_KINDS, 'fixed_stage'),
		agent_filter: nullableStr(o.agent_filter),
		events: strArray(o.events)
	};
}

function normalizeMeta(raw: unknown): FlowNodeMeta {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	return { description: str(o.description), notes: strArray(o.notes) };
}

function normalizeNode(raw: unknown): FlowNode | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	return {
		id,
		title: str(o.title, id),
		kind: oneOf(o.kind, NODE_KINDS, 'stage'),
		runs: normalizeRuns(o.runs),
		agent: normalizeAgent(o.agent),
		prompt_surface: normalizeDoor(o.prompt_surface),
		inspector: normalizeInspector(o.inspector),
		activation: strArray(o.activation),
		composition: normalizeSubGraph(o.composition),
		// W9.8: the fan-out descriptor must survive normalisation, or the
		// renderer has nothing to fan and the arms never appear.
		fanout: normalizeFanout(o.fanout),
		meta: normalizeMeta(o.meta)
	};
}

function normalizeFanout(raw: unknown): FlowNode['fanout'] {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const merge = str(o.merge);
	if (!merge) return null;
	const nodes = Array.isArray(o.nodes)
		? o.nodes.map((id) => str(id)).filter((id) => id.length > 0)
		: undefined;
	return {
		kind: str(o.kind, 'per_member'),
		source: str(o.source, 'cast_presence'),
		merge,
		max_parallel: Number(o.max_parallel) || 4,
		// Issue #56.3: the endpoint overlays the stored topology's on/off state;
		// an absent key reads as enabled (the artifact's own shape).
		enabled: o.enabled !== false,
		// W9.17: the manifest node(s) the fan-out invokes. They are templates
		// for N invocations — the read view draws the ACTUAL members instead.
		...(nodes && nodes.length > 0 ? { nodes } : {})
	};
}

function normalizeRegion(raw: unknown): FlowRegion | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	return {
		id,
		kind: oneOf(o.kind, REGION_KINDS, 'neutral'),
		title: str(o.title, id),
		order: typeof o.order === 'number' && Number.isFinite(o.order) ? o.order : 0
	};
}

function normalizeSubGraph(raw: unknown): FlowSubGraph | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	return {
		kind: 'composition',
		title: str(o.title),
		regions: Array.isArray(o.regions)
			? o.regions.map(normalizeRegion).filter((r): r is FlowRegion => r !== null)
			: [],
		entry: str(o.entry),
		sink: str(o.sink),
		nodes: Array.isArray(o.nodes)
			? o.nodes.map(normalizeNode).filter((n): n is FlowNode => n !== null)
			: [],
		edges: Array.isArray(o.edges)
			? o.edges.map(normalizeEdge).filter((e): e is FlowEdge => e !== null)
			: []
	};
}

function normalizeEdge(raw: unknown): FlowEdge | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	return {
		id,
		from: str(o.from),
		to: str(o.to),
		kind: oneOf(o.kind, EDGE_KINDS, 'flow'),
		condition: nullableStr(o.condition),
		label: nullableStr(o.label),
		cap: typeof o.cap === 'number' && Number.isFinite(o.cap) ? o.cap : null
	};
}

function normalizeGraph(raw: unknown): FlowGraph | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	return {
		id,
		kind: o.kind === 'off_turn' ? 'off_turn' : 'turn',
		title: str(o.title, id),
		attached_to: nullableStr(o.attached_to),
		entry: str(o.entry),
		nodes: Array.isArray(o.nodes)
			? o.nodes.map(normalizeNode).filter((n): n is FlowNode => n !== null)
			: [],
		edges: Array.isArray(o.edges)
			? o.edges.map(normalizeEdge).filter((e): e is FlowEdge => e !== null)
			: []
	};
}

/** Coerce an endpoint payload into a `FlowManifest`; malformed keys degrade. */
export function normalizeFlowManifest(raw: unknown): FlowManifest {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	return {
		schema_version: 1,
		generated_from: stringMap(o.generated_from),
		activation_map: stringMap(o.activation_map),
		graphs: Array.isArray(o.graphs)
			? o.graphs.map(normalizeGraph).filter((g): g is FlowGraph => g !== null)
			: []
	};
}

/** Read the topology manifest (`GET /api/flow/manifest`). */
export async function getFlowManifest(): Promise<FlowManifest> {
	if (USE_MOCK) return structuredClone(MOCK_FLOW_MANIFEST);

	const res = await fetch(FLOW_ENDPOINTS.manifest(), { headers: { Accept: 'application/json' } });
	if (!res.ok) {
		throw new StreamError(`getFlowManifest failed: ${res.status} ${res.statusText}`);
	}
	return normalizeFlowManifest(await readJson(res));
}

/** The plan's filled R5 example (§2.3), structurally valid against `types.ts`. */
export const MOCK_FLOW_MANIFEST: FlowManifest = {
	schema_version: 1,
	generated_from: {
		node_set: 'fabulist.flow.manifest.TURN_NODES',
		off_turn_flows: 'fabulist.flow.manifest.OFF_TURN_FLOWS',
		stage_names: 'fabulist.graph.nodes.STAGE_NAMES',
		step_catalog: 'fabulist.prompts.catalog.INTERACTION_CATALOG',
		bindings: 'fabulist.prompts.agent_sets.DEFAULT_BINDINGS',
		flows: 'fabulist.prompts.modules.DEFAULT_FLOWS'
	},
	activation_map: {
		plan: 'planner',
		lanes: 'npc_lanes',
		narrate: 'narrate',
		dsl: 'validate',
		critic: 'critic',
		world_state: 'bts',
		env_hud: 'env_hud',
		attribution: 'attribution'
	},
	graphs: [
		{
			id: 'turn_flow',
			kind: 'turn',
			title: 'Turn flow',
			attached_to: null,
			entry: 'pre_turn',
			nodes: [
				{
					id: 'pre_turn',
					title: 'Pre-turn',
					kind: 'stage',
					runs: {
						kind: 'fixed',
						interaction: null,
						call_site: 'fabulist.graph.nodes.make_pre_turn_node',
						description:
							'Reads what you just did, moves the clock on, and gathers the facts the narrator needs.'
					},
					agent: null,
					prompt_surface: { door: 'fixed', reason: 'engine-owned rules clock/classifier' },
					inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: { description: 'Engine step — no model call, nothing to edit here.', notes: [] }
				},
				{
					// W9.20: the planner is the plan junction's arm, BEFORE the lanes and
					// the narration (it used to sit post-commit in this example).
					id: 'planner',
					title: 'Planner',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'planner',
						call_site: null,
						description:
							'One steering pass: the story strings, the scene date/time, weather and the facts the plan holds established.'
					},
					agent: {
						interaction: 'planner',
						default_agent: 'narrator',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'planner' },
					inspector: {
						kind: 'model_pass',
						agent_filter: 'planner',
						events: ['plan', 'plan_refresh_failed']
					},
					activation: ['plan'],
					composition: null,
					meta: {
						description:
							"W9.20: the plan junction's arm — runs BEFORE the lanes and the narration, only when the preset's cadence says a plan is due (turn 1 is mandatory), never per turn.",
						notes: []
					}
				},
				{
					id: 'npc_lanes',
					title: 'NPC lanes',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'npc',
						call_site: null,
						description: "Every cast member in the scene works out what they want and acts on it — four at a time."
					},
					agent: {
						interaction: 'npc',
						// W9.25: the lanes are narrative-voice work — they default to the
						// NARRATION task (narrator-class on any legacy config), never the
						// extractor's cost class.
						default_agent: 'completion.narration',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'npc' },
					inspector: { kind: 'model_pass', agent_filter: 'npc', events: [] },
					activation: ['lanes'],
					composition: null,
					fanout: {
						kind: 'per_member',
						source: 'cast_presence',
						merge: 'narrate',
						max_parallel: 4,
						nodes: ['npc_lane']
					},
					meta: {
						description:
							'One lane per cast member in the scene, merged back into the narration. With no cast there are no lanes and the turn runs as usual.',
						notes: []
					}
				},
				{
					// The manifest names this row's kind `fanout`; the client taxonomy has
					// no such kind and `normalizeNode` folds it to `stage` (the fan-out is
					// declared on `npc_lanes.fanout`, and arms are drawn from the live cast).
					id: 'npc_lane',
					title: 'NPC lane',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'npc',
						call_site: null,
						description: "One cast member's intent, narration and lines."
					},
					agent: {
						interaction: 'npc',
						// W9.25: the lanes are narrative-voice work — they default to the
						// NARRATION task (narrator-class on any legacy config), never the
						// extractor's cost class.
						default_agent: 'completion.narration',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'npc' },
					inspector: { kind: 'model_pass', agent_filter: 'npc', events: ['npc_lane'] },
					activation: [],
					composition: null,
					fanout: null,
					meta: {
						description:
							'One lane. Lanes are written down in cast order, not in the order they happen to finish.',
						notes: []
					}
				},
				{
					id: 'narrate',
					title: 'Narrator turn',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'narrate',
						call_site: null,
						description: 'Streams the prose and the panel markup.'
					},
					agent: {
						interaction: 'narrate',
						default_agent: 'narrator',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'narrate' },
					inspector: { kind: 'model_pass', agent_filter: 'narrator', events: [] },
					activation: ['narrate'],
					composition: {
						kind: 'composition',
						title: 'Narrator prompt assembly',
						regions: [
							{
								id: 'stable_prefix',
								kind: 'stable_prefix',
								title: 'Stable prefix (cache contract)',
								order: 0
							},
							{ id: 'dynamic_tail', kind: 'dynamic_tail', title: 'Dynamic tail', order: 1 }
						],
						entry: 'narrator_system',
						sink: 'assembled_prompt',
						nodes: [
							{
								id: 'narrator_system',
								title: 'System prompt',
								kind: 'trigger',
								runs: {
									kind: 'fixed',
									interaction: null,
									call_site: null,
									description: 'The narrator\'s standing instructions — sent identically every turn.'
								},
								agent: null,
								prompt_surface: { door: 'step_editor', step_id: 'narrate' },
								inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
								activation: [],
								composition: null,
								meta: { description: 'Fixed start — the same every turn', notes: [] }
							},
							{
								id: 'library',
								title: 'Component library',
								kind: 'trigger',
								runs: {
									kind: 'fixed',
									interaction: null,
									call_site: null,
									description: 'What the narrator is told about the visual panels it may draw.'
								},
								agent: null,
								prompt_surface: { door: 'step_editor', step_id: 'narrate' },
								inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
								activation: [],
								composition: null,
								meta: { description: 'Fixed start — the same every turn', notes: [] }
							},
							{
								id: 'facts',
								title: 'Injected facts',
								kind: 'trigger',
								runs: {
									kind: 'fixed',
									interaction: null,
									call_site: null,
									description: 'What the world currently records.'
								},
								agent: null,
								prompt_surface: { door: 'fixed', reason: 'engine-injected facts' },
								inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
								activation: [],
								composition: null,
								meta: { description: 'Changes every turn', notes: [] }
							},
							{
								id: 'recalled',
								title: 'Recalled memories',
								kind: 'trigger',
								runs: {
									kind: 'fixed',
									interaction: null,
									call_site: null,
									description: 'Memories and lore pulled in for this turn.'
								},
								agent: null,
								prompt_surface: { door: 'fixed', reason: 'engine recall seam' },
								inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
								activation: [],
								composition: null,
								meta: { description: 'Changes every turn', notes: [] }
							},
							{
								id: 'assembled_prompt',
								title: 'Assembled prompt',
								kind: 'terminal',
								runs: {
									kind: 'fixed',
									interaction: null,
									call_site: null,
									description: 'Everything the narrator receives, in order.'
								},
								agent: null,
								prompt_surface: { door: 'none', reason: 'composition output' },
								inspector: { kind: 'terminal', agent_filter: null, events: [] },
								activation: [],
								composition: null,
								meta: {
									description: 'The same steps, drawn as a plain line.',
									notes: []
								}
							}
						],
						edges: [
							{
								id: 'e_ns_g',
								from: 'narrator_system',
								to: 'assembled_prompt',
								kind: 'flow',
								condition: null,
								label: null,
								cap: null
							},
							{
								id: 'e_lib_g',
								from: 'library',
								to: 'assembled_prompt',
								kind: 'flow',
								condition: null,
								label: null,
								cap: null
							},
							{
								id: 'e_facts_g',
								from: 'facts',
								to: 'assembled_prompt',
								kind: 'flow',
								condition: null,
								label: null,
								cap: null
							},
							{
								id: 'e_recall_g',
								from: 'recalled',
								to: 'assembled_prompt',
								kind: 'flow',
								condition: null,
								label: null,
								cap: null
							}
						]
					},
					meta: {
						description: 'The in-character pass: one draft streams, and the checks repair it in place if needed.',
						notes: []
					}
				},
				{
					id: 'validate',
					title: 'Validate',
					kind: 'gate',
					runs: {
						kind: 'fixed',
						interaction: null,
						call_site: 'fabulist.graph.nodes.validate_dsl',
						description: 'Checks the panel markup and rebuilds it if it is malformed.'
					},
					agent: null,
					prompt_surface: { door: 'fixed', reason: 'deterministic gate + resynthesizer' },
					inspector: { kind: 'gate', agent_filter: null, events: ['validator_flags'] },
					activation: ['dsl'],
					composition: null,
					meta: { description: 'A rule-based check — no model call.', notes: [] }
				},
				{
					id: 'decision_gate',
					title: 'Decision gate',
					kind: 'gate',
					runs: {
						kind: 'model_pass',
						interaction: null,
						call_site: 'fabulist.graph.nodes._run_turn_gate',
						description: 'Asks the yes/no checks — the anti-slop list and the movement question — 30 at a time.'
					},
					agent: {
						interaction: null,
						default_agent: null,
						bound_agent: null,
						source: 'engine'
					},
					prompt_surface: { door: 'fixed', reason: 'decision client, not a W6 step' },
					inspector: { kind: 'gate', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: {
						description: 'Advisory flags only; no stage frame today. Static in wave 1.',
						notes: []
					}
				},
				{
					id: 'critic',
					title: 'Critic',
					kind: 'gate',
					runs: {
						kind: 'model_pass',
						interaction: 'critic',
						call_site: null,
						description: 'Reviews the reply: either fine, or the issues it found. The turn is kept either way.'
					},
					agent: {
						interaction: 'critic',
						default_agent: 'extractor',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'critic' },
					inspector: { kind: 'gate', agent_filter: 'extractor', events: ['critic_verdict'] },
					activation: ['critic'],
					composition: null,
					meta: {
						description: 'A failing verdict still keeps the turn — repairs happen inside the narrator step, and the verdict is recorded so you can see what it objected to.',
						notes: []
					}
				},
				{
					id: 'bts',
					title: 'World tracker (BTS)',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'bts',
						call_site: null,
						description: 'Records what changed in the world.'
					},
					agent: {
						interaction: 'bts',
						default_agent: 'extractor',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'bts' },
					inspector: {
						kind: 'model_pass',
						agent_filter: 'extractor',
						events: ['world_state', 'world_state_failed']
					},
					activation: ['world_state'],
					composition: null,
					meta: { description: 'The tracker: it writes the state you see in the side panel.', notes: [] }
				},
				{
					id: 'env_hud',
					title: 'Env HUD',
					kind: 'stage',
					runs: {
						kind: 'engine',
						interaction: null,
						call_site: 'fabulist.state.env_hud.env_hud_statement',
						description: 'Writes the scene\'s status strip (weather, time, place).'
					},
					agent: null,
					prompt_surface: { door: 'fixed', reason: 'engine-emitted HUD, never narrator' },
					inspector: {
						kind: 'fixed_stage',
						agent_filter: null,
						events: ['env_hud', 'env_hud_failed']
					},
					activation: ['env_hud'],
					composition: null,
					meta: { description: '', notes: [] }
				},
				{
					id: 'attribution',
					title: 'Attributing speakers',
					kind: 'stage',
					runs: {
						kind: 'engine',
						interaction: null,
						call_site: 'fabulist.state.attribution.parse_attribution',
						description:
							'Works out who says each quoted line from the colour the narrator wrapped it in — no model call.'
					},
					agent: null,
					prompt_surface: {
						door: 'none',
						reason: "parses the narrator's colour markup; no prompt of its own"
					},
					inspector: {
						kind: 'fixed_stage',
						agent_filter: null,
						events: ['attribution', 'attribution_failed']
					},
					activation: ['attribution'],
					composition: null,
					meta: { description: '', notes: [] }
				},
				{
					id: 'done',
					title: 'Done',
					kind: 'terminal',
					runs: {
						kind: 'engine',
						interaction: null,
						call_site: null,
						description: 'The turn is finished; clean-up runs after this.'
					},
					agent: null,
					prompt_surface: { door: 'none', reason: 'terminal' },
					inspector: { kind: 'terminal', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				}
			],
			edges: [
				{
					id: 'e1',
					from: 'pre_turn',
					to: 'planner',
					kind: 'branch',
					condition: 'plan due',
					label: 'due',
					cap: null
				},
				{
					id: 'e1a',
					from: 'pre_turn',
					to: 'npc_lanes',
					kind: 'branch',
					condition: 'no plan due',
					label: 'skip',
					cap: null
				},
				{
					id: 'e1e',
					from: 'planner',
					to: 'npc_lanes',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					// The manifest's kind is `fanout`; `normalizeEdge` folds it to `flow`
					// (the client edge taxonomy has no fan-out kind), so it is authored
					// here in the shape the renderer actually receives.
					id: 'e1b',
					from: 'npc_lanes',
					to: 'npc_lane',
					kind: 'flow',
					condition: null,
					label: 'per member',
					cap: null
				},
				{
					id: 'e1c',
					from: 'npc_lanes',
					to: 'narrate',
					kind: 'flow',
					condition: null,
					label: 'no cast',
					cap: null
				},
				{
					id: 'e1d',
					from: 'npc_lane',
					to: 'narrate',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e2',
					from: 'narrate',
					to: 'validate',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e3',
					from: 'validate',
					to: 'decision_gate',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e4',
					from: 'decision_gate',
					to: 'critic',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e5',
					from: 'critic',
					to: 'bts',
					kind: 'branch',
					condition: 'verdict == ok',
					label: 'ok',
					cap: null
				},
				{
					id: 'e6',
					from: 'critic',
					to: 'bts',
					kind: 'branch',
					condition: 'verdict == diffs',
					label: 'diffs',
					cap: null
				},
				{
					id: 'e10',
					from: 'bts',
					to: 'env_hud',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e11',
					from: 'env_hud',
					to: 'attribution',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				},
				{
					id: 'e12',
					from: 'attribution',
					to: 'done',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				}
			]
		},
		{
			id: 'recall',
			kind: 'off_turn',
			title: 'Pre-turn recall',
			attached_to: 'pre_turn',
			entry: 'recall_embed',
			nodes: [
				{
					id: 'recall_embed',
					title: 'Recall embed',
					kind: 'trigger',
					runs: {
						kind: 'embedder',
						interaction: null,
						call_site: 'fabulist.memory.embedder.embed_texts',
						description: 'Turns your message into a search and pulls the closest earlier turns back in.'
					},
					agent: {
						interaction: null,
						default_agent: null,
						bound_agent: null,
						source: 'engine'
					},
					prompt_surface: { door: 'fixed', reason: 'memory seam, not a W6 step' },
					inspector: { kind: 'fixed_stage', agent_filter: null, events: ['recall_queries'] },
					activation: [],
					composition: null,
					meta: { description: 'Runs alongside the turn, not inside it.', notes: [] }
				}
			],
			edges: [
				{
					id: 'oa1',
					from: 'recall_embed',
					to: 'pre_turn',
					kind: 'attach',
					condition: null,
					label: 'recall block',
					cap: null
				}
			]
		},
		{
			id: 'imagegen',
			kind: 'off_turn',
			title: 'Image generation',
			attached_to: 'narrate',
			entry: 'dsl_statement',
			nodes: [
				{
					id: 'dsl_statement',
					title: 'Narrative DSL',
					kind: 'trigger',
					runs: {
						kind: 'fixed',
						interaction: null,
						call_site: null,
						description: 'A panel in the narrator\'s reply that asks for an image.'
					},
					agent: null,
					prompt_surface: { door: 'none', reason: 'narrator output, not an authored step' },
					inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				},
				{
					id: 'imagegen_call',
					title: 'Image generation',
					kind: 'stage',
					runs: {
						kind: 'hook',
						interaction: null,
						call_site: 'fabulist.api.imagegen_api',
						description: 'Render the DSL-requested image.'
					},
					agent: {
						interaction: null,
						default_agent: 'imagegen',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'fixed', reason: 'engine hook, no W6 step' },
					inspector: { kind: 'fixed_stage', agent_filter: 'imagegen', events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				}
			],
			edges: [
				{
					id: 'ob1',
					from: 'dsl_statement',
					to: 'imagegen_call',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				}
			]
		},
		{
			id: 'compaction',
			kind: 'off_turn',
			title: 'Compaction',
			attached_to: 'bts',
			entry: 'budget_check',
			nodes: [
				{
					id: 'budget_check',
					title: 'Budget check',
					kind: 'trigger',
					runs: {
						kind: 'engine',
						interaction: null,
						call_site: 'fabulist.graph.compaction.Compactor.maybe_compact',
						description: 'Fire-and-forget post-turn hook when history exceeds budget.'
					},
					agent: null,
					prompt_surface: { door: 'fixed', reason: 'post-turn hook' },
					inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: {
						description: "Runs AFTER 'done'; never on the turn stream (D42).",
						notes: []
					}
				},
				{
					id: 'summarize',
					title: 'Summarize',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'summarize',
						call_site: null,
						description: 'Compact long history into a summary.'
					},
					agent: {
						interaction: 'summarize',
						default_agent: 'summarizer',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'summarize' },
					inspector: { kind: 'model_pass', agent_filter: 'summarizer', events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				}
			],
			edges: [
				{
					id: 'oc1',
					from: 'budget_check',
					to: 'summarize',
					kind: 'flow',
					condition: 'over_budget',
					label: null,
					cap: null
				}
			]
		},
		{
			id: 'assistant_hooks',
			kind: 'off_turn',
			title: 'Assistant hooks',
			attached_to: 'done',
			entry: 'event',
			nodes: [
				{
					id: 'event',
					title: 'Event',
					kind: 'trigger',
					runs: {
						kind: 'engine',
						interaction: null,
						call_site: null,
						description: 'A per-turn assistant hook or a manual consult.'
					},
					agent: null,
					prompt_surface: { door: 'fixed', reason: 'event trigger' },
					inspector: { kind: 'fixed_stage', agent_filter: null, events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				},
				{
					id: 'assistant_consult',
					title: 'Assistant consult',
					kind: 'stage',
					runs: {
						kind: 'model_pass',
						interaction: 'assistant',
						call_site: null,
						description: "A side assistant's reply."
					},
					agent: {
						interaction: 'assistant',
						default_agent: 'narrator',
						bound_agent: null,
						source: 'agent_bindings'
					},
					prompt_surface: { door: 'step_editor', step_id: 'assistant' },
					inspector: { kind: 'model_pass', agent_filter: 'narrator', events: [] },
					activation: [],
					composition: null,
					meta: { description: '', notes: [] }
				}
			],
			edges: [
				{
					id: 'od1',
					from: 'event',
					to: 'assistant_consult',
					kind: 'flow',
					condition: null,
					label: null,
					cap: null
				}
			]
		}
	]
};

// --- turn topology (W9.5) ---------------------------------------------------
//
// The topology is the executable half of the flow surface: node ids + kinds and
// the wiring between them. `canonical` rides along on the GET so the editor's
// palette can offer the engine's own node set (R20/R21) instead of a hard-coded
// copy; `violations` on both verbs is the engine's validator speaking.

/** GET/PUT envelope for `/api/prompts/topology`. */
export interface TopologyResponse {
	topology: Topology;
	/** The code-authored wiring (`graph.CANONICAL_TOPOLOGY`) — the palette source. */
	canonical: Topology;
	violations: TopologyViolation[];
}

/**
 * An edge plus the one extra wire key this client must not lose.
 *
 * `fanout: true` marks a `Send` edge (the NPC-lane fan-out). It is not in the
 * typed edge shape (`#lib/flow/types`) because the editor does not draw it, but
 * it IS topology data: the engine's off switch
 * (`fabulist.graph.contracts.prune_disabled_fanout`) can only remove a stage
 * whose fan-out edge still says so. A read/save round trip that dropped the
 * marker therefore made every later "lanes off" save refused — so the marker
 * rides through this client untouched (issue #56).
 */
type WireTopologyEdge = TopologyEdge & { fanout?: boolean };

/** One edge as the wire spells it (`kind`/`condition`/`cap` are optional). */
const wireEdge = (from: string, to: string, fanout = false): TopologyEdge => {
	const edge = { from, to, kind: 'flow', condition: null, cap: null } as WireTopologyEdge;
	if (fanout) edge.fanout = true;
	return edge;
};

const wireBranch = (from: string, to: string, condition: string, cap: number | null = null): TopologyEdge => ({
	from,
	to,
	kind: 'branch',
	condition,
	cap
});

/**
 * The engine's canonical turn topology (`src/fabulist/graph/graph.py`
 * `CANONICAL_TOPOLOGY`, W9.6): the linear spine plus the critic's verdict
 * fan-out. The critic's fail->regenerate loop is RETIRED (W9.24 W3): repairs
 * happen inside the narrate node's agent loop; a legacy fail verdict logs and keeps.
 * NOT in the topology: it lives in code (`nodes.REGEN_CAP`), so it is absent
 * here too — an edge-level `cap` is optional wire data, not a canonical fact.
 */
export const MOCK_TOPOLOGY: Topology = {
	// Mirrors graph.CANONICAL_TOPOLOGY (W9.20): the planner junction on
	// pre_turn, the Send fan-out (W9.17) and the conditional critic (W9.19).
	nodes: [
		{ id: 'pre_turn', kind: 'stage' },
		{ id: 'planner', kind: 'stage' },
		{ id: 'npc_lanes', kind: 'stage', data: { max_lanes: 4 } },
		{ id: 'npc_lane', kind: 'stage' },
		{ id: 'narrate', kind: 'stage' },
		{ id: 'commit', kind: 'stage' },
		{ id: 'validate', kind: 'gate' },
		{ id: 'decision_gate', kind: 'gate' },
		{ id: 'critic', kind: 'gate' },
		{ id: 'bts', kind: 'stage' },
		{ id: 'env_hud', kind: 'stage' },
		{ id: 'attribution', kind: 'stage' },
		{ id: 'done', kind: 'terminal' }
	],
	edges: [
		wireBranch('pre_turn', 'planner', 'plan'),
		wireBranch('pre_turn', 'npc_lanes', 'skip'),
		wireEdge('planner', 'npc_lanes'),
		wireEdge('npc_lanes', 'npc_lane', true),
		wireEdge('npc_lanes', 'narrate'),
		wireEdge('npc_lane', 'narrate'),
		wireEdge('narrate', 'commit'),
		wireEdge('commit', 'validate'),
		wireEdge('validate', 'decision_gate'),
		wireBranch('decision_gate', 'critic', 'critic'),
		wireBranch('decision_gate', 'bts', 'planner'),
		wireBranch('critic', 'bts', 'ok'),
		wireBranch('critic', 'bts', 'diffs'),
		wireBranch('critic', 'narrate', 'fail'),
		wireEdge('bts', 'env_hud'),
		wireEdge('env_hud', 'attribution'),
		wireEdge('attribution', 'done')
	]
};

/** Mock mode's stored topology: PUT then GET round-trips inside one session. */
let mockTopology: Topology = cloneTopology(MOCK_TOPOLOGY);

function normalizeTopologyNode(raw: unknown): TopologyNode | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const id = str(o.id);
	if (!id) return null;
	const node: TopologyNode = { id, kind: oneOf(o.kind, NODE_KINDS, 'stage') };
	// W9.10: a template REFERENCE survives the round trip (an owner-created
	// node carries it; a canonical node does not), as does its node data.
	const template = str(o.template);
	if (template) node.template = template;
	if (o.data && typeof o.data === 'object' && !Array.isArray(o.data)) {
		node.data = o.data as Record<string, unknown>;
	}
	return node;
}

function normalizeTopologyEdge(raw: unknown): TopologyEdge | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	const from = str(o.from);
	const to = str(o.to);
	if (!from || !to) return null;
	const edge: WireTopologyEdge = {
		from,
		to,
		kind: oneOf(o.kind, EDGE_KINDS, 'flow'),
		condition: nullableStr(o.condition),
		cap: typeof o.cap === 'number' && Number.isFinite(o.cap) ? o.cap : null
	};
	// The fan-out marker is data the editor never edits but the engine reads
	// (see `WireTopologyEdge`): keep it on the round trip.
	if (o.fanout === true) edge.fanout = true;
	return edge;
}

/** Coerce an endpoint payload into a `Topology`; malformed entries are dropped. */
export function normalizeTopology(raw: unknown): Topology {
	const o = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
	return {
		nodes: Array.isArray(o.nodes)
			? o.nodes.map(normalizeTopologyNode).filter((n): n is TopologyNode => n !== null)
			: [],
		edges: Array.isArray(o.edges)
			? o.edges.map(normalizeTopologyEdge).filter((e): e is TopologyEdge => e !== null)
			: []
	};
}

function normalizeViolation(raw: unknown): TopologyViolation | null {
	if (!raw || typeof raw !== 'object') return null;
	const o = raw as Record<string, unknown>;
	// The engine may name an edge by its endpoints or by a derived id.
	const edgeObject = o.edge && typeof o.edge === 'object' ? normalizeTopologyEdge(o.edge) : null;
	const code = str(o.code, 'invalid_topology');
	return {
		code,
		node: nullableStr(o.node),
		edge: typeof o.edge === 'string' ? o.edge : edgeObject ? edgeId(edgeObject) : null,
		message: str(o.message, code)
	};
}

function normalizeViolations(raw: unknown): TopologyViolation[] {
	if (!Array.isArray(raw)) return [];
	return raw.map(normalizeViolation).filter((v): v is TopologyViolation => v !== null);
}

function normalizeTopologyResponse(raw: Record<string, unknown>): TopologyResponse {
	const topology = normalizeTopology(raw.topology);
	return {
		topology,
		canonical: raw.canonical ? normalizeTopology(raw.canonical) : topology,
		violations: normalizeViolations(raw.violations)
	};
}

/** The wire body: only the fields the engine reads (edge ids are client-derived). */
function wireTopology(topology: Topology): Record<string, unknown> {
	return {
		nodes: topology.nodes.map((node) => ({
			id: node.id,
			kind: node.kind,
			...(node.template ? { template: node.template } : {}),
			...(node.data ? { data: node.data } : {})
		})),
		edges: topology.edges.map((edge) => ({
			from: edge.from,
			to: edge.to,
			kind: edge.kind,
			...(edge.condition ? { condition: edge.condition } : {}),
			...(typeof edge.cap === 'number' ? { cap: edge.cap } : {}),
			...(edge as WireTopologyEdge).fanout ? { fanout: true } : {}
		}))
	};
}

/** Read the executable topology plus the canonical wiring and any violations. */
export async function getTopology(): Promise<TopologyResponse> {
	if (USE_MOCK) {
		return {
			topology: cloneTopology(mockTopology),
			canonical: cloneTopology(MOCK_TOPOLOGY),
			violations: []
		};
	}
	const res = await fetch(FLOW_ENDPOINTS.topology(), { headers: { Accept: 'application/json' } });
	if (!res.ok) throw await topologyFailure(res, 'getTopology');
	return normalizeTopologyResponse(await readJson(res));
}

/**
 * Store the whole topology. The response is what the engine actually kept, so
 * the pane re-seeds its draft from it; a 400 throws a `StreamError` whose
 * `meta.violations` carries the engine's rejections (see `violationsFromError`).
 */
export async function putTopology(topology: Topology): Promise<TopologyResponse> {
	const body = { topology: wireTopology(topology) };
	if (USE_MOCK) {
		// Mock mode still validates: the pane's 400 path is exercised without an engine.
		const violations = validateLocal(topology);
		if (violations.length) {
			throw new StreamError(violations[0].message, { status: 400, violations });
		}
		mockTopology = cloneTopology(topology);
		return {
			topology: cloneTopology(mockTopology),
			canonical: cloneTopology(MOCK_TOPOLOGY),
			violations: []
		};
	}
	const res = await fetch(FLOW_ENDPOINTS.topology(), {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body)
	});
	if (!res.ok) throw await topologyFailure(res, 'putTopology');
	return normalizeTopologyResponse(await readJson(res));
}

async function topologyFailure(res: Response, call: string): Promise<StreamError> {
	const body = await readJson(res);
	const violations = normalizeViolations(body.violations);
	const detail = typeof body.detail === 'string' ? body.detail : '';
	const reason =
		detail || violations[0]?.message || `${call} failed: ${res.status} ${res.statusText}`;
	return new StreamError(reason, { status: res.status, violations });
}

/** The violations a rejected save carried (empty for any other failure). */
export function violationsFromError(err: unknown): TopologyViolation[] {
	if (!(err instanceof StreamError)) return [];
	const raw = err.meta?.violations;
	return Array.isArray(raw) ? (raw as TopologyViolation[]) : [];
}
