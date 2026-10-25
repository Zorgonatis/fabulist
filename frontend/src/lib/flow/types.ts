// Flow manifest types (W9.2) — the hand-typed TS mirror of the engine's
// `Manifest` JSON schema (`the maintainer's private notes (unshipped)` §2.2), the same
// discipline `lib/stores.ts` uses for the event-log view: one authored shape,
// no codegen.
//
// The manifest is DESCRIPTIVE data (C1): the flow surface only reads it. Every
// field here is optional-tolerant at the edges — the renderer degrades rather
// than throws when the engine omits a key.

/** A node's live activation trail (decision 6). Absent ⇒ `'pending'`. */
export type ActivationState = 'pending' | 'running' | 'done';

/** Per-node activation, keyed by node id. Derived by `activationFromSteps`,
 * consumed by `FlowGraph`; the component maps purely by node id and never
 * inspects stage names (requirement 5). */
export type ActivationMap = Record<string, ActivationState>;

/** Node taxonomy (`Node.kind`). */
export type FlowNodeKind = 'stage' | 'gate' | 'branch' | 'trigger' | 'terminal';

/** What a node actually runs (`Node.runs.kind`). */
export type FlowRunKind = 'model_pass' | 'fixed' | 'engine' | 'hook' | 'embedder';

/** Edge taxonomy. `loop_back` is the bounded regenerate→validate leg. */
export type FlowEdgeKind = 'flow' | 'branch' | 'loop_back' | 'attach';

/** Where a node belongs in the prompt cache contract (C4). */
export type RegionKind = 'stable_prefix' | 'dynamic_tail' | 'neutral';

/** The inspector's per-node-kind I/O contract (plan §4). */
export type InspectorKind = 'model_pass' | 'fixed_stage' | 'gate' | 'terminal';

/** A prompt-editor door, a fixed engine surface, or nothing (requirement 3). */
export type PromptDoor =
	| { door: 'step_editor'; step_id: string }
	| { door: 'fixed'; reason: string }
	| { door: 'none'; reason: string };

/** The agent binding a node runs under. `bound_agent` is the endpoint overlay
 * only — the committed artifact always authors it `null` (plan §2.1). */
export interface FlowNodeAgent {
	interaction: string | null;
	default_agent: string | null;
	bound_agent: string | null;
	source: 'agent_bindings' | 'engine';
}

/** What a node runs, plus the dotted call site for non-registry passes. */
export interface FlowNodeRuns {
	kind: FlowRunKind;
	interaction: string | null;
	call_site: string | null;
	description: string;
}

/** How the inspector resolves this node's inputs/outputs (plan §4). */
export interface FlowNodeInspector {
	kind: InspectorKind;
	/** `agent-calls?agent=` filter, or `null` for engine surfaces. */
	agent_filter: string | null;
	/** system-event names this fixed stage reads/emits. */
	events: string[];
}

/** Free-form authoring notes (wave-1 "static" honesty lives here). */
export interface FlowNodeMeta {
	description: string;
	notes: string[];
}

/** One graph node, recursive via `composition` (R13). */
export interface FlowNode {
	id: string;
	title: string;
	kind: FlowNodeKind;
	runs: FlowNodeRuns;
	agent: FlowNodeAgent | null;
	prompt_surface: PromptDoor;
	inspector: FlowNodeInspector;
	/** Stage names (`turn_stages.name`) that light this node. */
	activation: string[];
	/** W9.8 renders this; wave 1 collapses it to a leaf (non-goal §7). */
	composition: FlowSubGraph | null;
	/** W9.8: declared fan-out (one arm per live member); null when the node does not fan out. */
	fanout?: {
		kind: 'per_member' | string;
		source: string;
		merge: string;
		max_parallel: number;
		/** Issue #56.3: the endpoint overlays the EFFECTIVE on/off state of the
		 *  stage (the code artifact always authors it enabled). */
		enabled?: boolean;
		/** W9.17: the manifest node(s) the fan-out invokes (e.g. `npc_lane`).
		 *  They are templates for N invocations, never stations — the read view
		 *  draws the ACTUAL members instead (see `withLaneArms`). */
		nodes?: string[];
	} | null;
	/** Render hint for a synthetic lane arm (never in the committed manifest). */
	arm?: boolean;
	meta: FlowNodeMeta;
}

/** One region inside a composition (`stable_prefix`/`dynamic_tail`/neutral). */
export interface FlowRegion {
	id: string;
	kind: RegionKind;
	title: string;
	order: number;
}

/** A composition sub-graph: same `Node` shape inside, so nesting is unbounded. */
export interface FlowSubGraph {
	kind: 'composition';
	title: string;
	regions: FlowRegion[];
	entry: string;
	sink: string;
	nodes: FlowNode[];
	edges: FlowEdge[];
}

/** One directed edge; branches and the bounded loop are first-class. */
export interface FlowEdge {
	id: string;
	from: string;
	to: string;
	kind: FlowEdgeKind;
	/** e.g. `verdict == fail`. */
	condition: string | null;
	/** Short chip text (`ok`/`diffs`/`fail`/`re-validate`). */
	label: string | null;
	/** Loop bound (regen hard cap = 2, D49). */
	cap: number | null;
}

/** One graph (graphs[0] is the turn flow; off-turn graphs anchor via
 * `attached_to` and an `attach` edge — R5). */
export interface FlowGraph {
	id: string;
	kind: 'turn' | 'off_turn';
	title: string;
	attached_to: string | null;
	entry: string;
	nodes: FlowNode[];
	edges: FlowEdge[];
}

/** The top-level manifest contract. */
export interface FlowManifest {
	schema_version: 1;
	/** Provenance citations, keyed by role (like the agent-map). */
	generated_from: Record<string, string>;
	/** `turn_stages.name` → node id. DATA, never a component constant. */
	activation_map: Record<string, string>;
	graphs: FlowGraph[];
}

// --- turn topology (W9.5 editor wire shape) --------------------------------
//
// The manifest DESCRIBES the flow; the topology is what the engine EXECUTES
// (`graph.CANONICAL_TOPOLOGY`, seeded into the preset — W9.6). The editor works
// on this shape (R19/R21/R23): nodes are ids + kinds, edges are wiring. A node
// created here is a configured run in the W9.6 graph processor; a node template
// (R21) is a later asset that fills the same slot.

/** One executable node. Titles, prompt surfaces and agents are not topology.
 *
 * `template` (W9.10) is an OPTIONAL reference instead of a canonical id: a node
 * with `template` runs the behaviour its template `implements`, under whatever
 * `id` the owner chose. `data` is node data the engine reads (e.g. the fan-out's
 * `max_lanes`); the template's `config` seeds it. */
export interface TopologyNode {
	id: string;
	kind: FlowNodeKind;
	template?: string | null;
	data?: Record<string, unknown> | null;
}

/** One executable edge (normalized: `kind` defaults to `'flow'` on the wire). */
export interface TopologyEdge {
	from: string;
	to: string;
	kind: FlowEdgeKind;
	/** Branch value the source node routes on (`ok`/`diffs`/`fail`/…) — branches only. */
	condition: string | null;
	/** Loop bound carried by the `fail` edge back into the narrator (D49). */
	cap: number | null;
}

/** The turn topology: the graph the engine builds for every turn. */
export interface Topology {
	nodes: TopologyNode[];
	edges: TopologyEdge[];
}

/** One validator rejection. The engine owns the codes; `lib/flow/editor.ts`
 * mirrors the cheap subset client-side so Save can be disabled early. */
export interface TopologyViolation {
	code: string;
	/** The offending node id, when the violation names one. */
	node?: string | null;
	/** The offending edge, as the editor's derived edge id (`editor.edgeId`). */
	edge?: string | null;
	message: string;
}

// --- inspector inputs (lib-side, decoupled from the chat API client) --------

/**
 * One captured model pass, structurally compatible with `AgentCallRow`
 * (`#lib/api/bts`) but declared here so `lib/flow` carries no chat coupling.
 * Extra capture fields (`reasoning_text`, `finish`, `latency_s`) are optional:
 * older engines omit them.
 */
export interface InspectorCall {
	seq: number;
	ts: string;
	agent: string;
	messages: Array<Record<string, unknown>>;
	response_text: string;
	reasoning_text?: string;
	finish?: string | null;
	latency_s?: number | null;
}

/** One system event a fixed stage / gate reads or emits. */
export interface InspectorEvent {
	name: string;
	ts?: string;
	payload?: unknown;
}

/** One rendered inspector field (a labelled block of text or a message list). */
export interface InspectorField {
	label: string;
	/** Pre-rendered text; already stringified for non-string payloads. */
	value: string;
	kind: 'text' | 'messages';
}

/** The resolved inputs/outputs the inspector renders. */
export interface InspectorView {
	inputs: InspectorField[];
	outputs: InspectorField[];
	/** Honest empty-state copy when the node kind carries no I/O. */
	empty: string;
}
