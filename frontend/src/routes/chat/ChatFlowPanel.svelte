<script lang="ts">
	// Chat-only wrapper around the reusable flow renderer (W9.2, lane C).
	//
	// This is the one place the flow surface is allowed to know about chat. It:
	//   * loads the topology manifest (`GET /api/flow/manifest`, mock-aware);
	//   * keeps re-reading the last turn's lane set (`GET .../lanes`) as the turn
	//     advances, so the fan-out arms land on the turn that produced them;
	//   * derives the live activation trail from the app's turn trace stores
	//     (`turnSteps` + `processingStage`) — the same pair the step strip reads;
	//   * reads the session's agent-debug toggle (captures only exist for turns
	//     that ran with it on);
	//   * resolves the SELECTED node's captured model calls, scoped to the
	//     current turn (the last user-message boundary);
	//   * renders `<FlowGraph>` + `<NodeInspector>` and forwards the prompt door.
	//
	// `#lib/flow/*` stays chat-free: every prop this file passes is plain data.
	import { API_BASE } from '#lib/api';
	import { getAgentCalls } from '#lib/api/bts';
	import { getFlowManifest, getLanes, getTopology, putTopology, violationsFromError } from '#lib/api/flow';
	import type { FlowLanes } from '#lib/api/flow';
	import { activationFromSteps } from '#lib/flow/activation';
	import { cloneTopology } from '#lib/flow/editor';
	import FlowGraph from '#lib/flow/FlowGraph.svelte';
	import NodeInspector from '#lib/flow/NodeInspector.svelte';
	import type { FlowManifest, FlowNode, InspectorCall, Topology, TopologyNode } from '#lib/flow/types';
	import { processingStage, sessionId, turnSteps, type TurnStep } from '#lib/stores';

	let {
		onopenstep
	}: {
		/** Prompt-door callback: carries the W6 step id (the page opens it). */
		onopenstep?: (stepId: string) => void;
	} = $props();

	let manifest = $state<FlowManifest | null>(null);
	/** W9.8: the last turn's ACTUAL lane set — the arms drawn per member. */
	let lanes = $state<FlowLanes | null>(null);
	let manifestError = $state('');
	let manifestLoading = $state(true);
	let selectedId = $state<string | null>(null);
	/** Session debug toggle (`GET .../agent-debug`); captures need it on. */
	let debug = $state(false);
	let calls = $state<InspectorCall[]>([]);
	let callsLoading = $state(false);
	let panel = $state<HTMLElement | null>(null);

	// --- the lane config (issues #56.2/#56.3) ---------------------------------
	//
	// The fan-out's width and its on/off switch are TOPOLOGY data (the running
	// graph is compiled from it, ADR-009), so they are edited by writing the
	// stored topology through the validated door — `PUT /api/prompts/topology`
	// refuses an edit the builder could not compile, with the violation named.
	// The control is deliberately small: one width select and one switch, on the
	// node the manifest declares as the fan-out.
	/** The stored topology the config writes back (null until read). */
	let topologyDraft = $state<Topology | null>(null);
	let laneBusy = $state(false);
	let laneNote = $state('');

	const laneNodeId = $derived(
		manifest?.graphs.flatMap((graph) => graph.nodes).find((node) => node.fanout)?.id ?? null
	);
	/** The fan-out's topology row: the manifest's id, else the canonical one. */
	const laneNode = $derived(findFanNode(topologyDraft, laneNodeId));
	const laneEnabled = $derived(laneData("enabled") !== false);
	const laneWidth = $derived(Number(laneData("max_lanes")) || 4);

	function findFanNode(topo: Topology | null, hint: string | null): TopologyNode | null {
		if (!topo) return null;
		const byHint = hint ? topo.nodes.find((node) => node.id === hint) : undefined;
		return byHint ?? topo.nodes.find((node) => node.id === 'npc_lanes') ?? null;
	}

	function laneData(key: string): unknown {
		const data = laneNode?.data as Record<string, unknown> | undefined;
		return data ? data[key] : undefined;
	}

	async function loadTopology(): Promise<void> {
		try {
			const body = await getTopology();
			topologyDraft = body.topology;
		} catch {
			// The surface is optional: a failed read just hides the config row.
			topologyDraft = null;
		}
	}

	/**
	 * Write ONE lane setting back to the default preset's topology.
	 *
	 * The whole topology goes back (the door takes the wiring, not a patch), so a
	 * rejected edit can never leave a half-written node behind: a 400 means the
	 * log still carries the previous, compilable topology.
	 */
	async function saveLaneSetting(patch: { max_lanes?: number; enabled?: boolean }): Promise<void> {
		if (!topologyDraft || !laneNode) return;
		laneBusy = true;
		laneNote = '';
		const next = cloneTopology(topologyDraft);
		const target = next.nodes.find((node) => node.id === laneNode.id);
		if (!target) {
			laneBusy = false;
			return;
		}
		target.data = { ...(target.data ?? {}), ...patch };
		try {
			const body = await putTopology(next);
			topologyDraft = body.topology;
			laneNote = 'saved — applies to the next turn';
			// The manifest's fan-out descriptor is the endpoint's overlay of this
			// very setting, so re-read it: the board's note follows the save.
			await loadManifest();
			await readLanes();
		} catch (err) {
			const violations = violationsFromError(err);
			laneNote = violations.length
				? `${violations[0].code}: ${violations[0].message}`
				: err instanceof Error
					? err.message
					: 'topology refused';
		} finally {
			laneBusy = false;
		}
	}

	async function loadManifest(): Promise<void> {
		manifestLoading = true;
		manifestError = '';
		try {
			manifest = await getFlowManifest();
		} catch (err) {
			manifestError = err instanceof Error ? err.message : 'turn flow unavailable';
		} finally {
			manifestLoading = false;
		}
	}

	// The topology (incl. the preset's `bound_agent` overlay) is session-constant,
	// so it is read once; the Reload control re-reads it by hand.
	$effect(() => {
		void loadManifest();
	});

	$effect(() => {
		void loadTopology();
	});

	// --- the lane set (W9.8) --------------------------------------------------
	//
	// The lane set is NOT session-constant: it is a fact of the CURRENT turn,
	// folded from the log (the turn's ``npc_lanes_plan`` row, then the
	// ``npc_lane`` rows and the ``npc_lanes`` summary the fan-in join writes).
	// It is therefore re-read whenever the turn trace moves — and, issue #56.1,
	// also on a short interval WHILE the lanes are in flight: the plan is
	// recorded at dispatch, which emits no stage frame of its own, so a
	// frame-driven read alone would not notice the members arriving until the
	// join (the panel used to show the PREVIOUS turn's set for the whole turn).
	//
	// Svelte 5: read the `$store` values directly — `get(store)` inside an effect
	// does not track. `$turnSteps` advances per stage frame and `$processingStage`
	// moves with it; either change re-reads the set. The seq guard drops a slow
	// response that a newer frame (or a session switch) has already superseded.
	let lanesSeq = 0;

	async function readLanes(): Promise<void> {
		const sid = $sessionId;
		if (!sid) {
			lanes = null;
			return;
		}
		const seq = (lanesSeq += 1);
		const next = await getLanes(sid);
		if (seq !== lanesSeq) return; // superseded by a newer frame or session
		lanes = next;
	}

	$effect(() => {
		const sid = $sessionId;
		void $turnSteps.length;
		void $processingStage;
		if (!sid) {
			lanes = null;
			return;
		}
		void readLanes();
	});

	/** How often an in-flight turn's lane set is re-read (issue #56.1). */
	const LANE_POLL_MS = 1000;
	const lanesInFlight = $derived(lanes?.in_flight === true);
	$effect(() => {
		// Poll while a TURN is running (the plan row is written at dispatch, which
		// emits no stage frame of its own) and stop the moment it is not: an
		// aborted turn leaves its plan behind, and a poll keyed on `in_flight`
		// alone would then run forever against an idle session.
		if (!running) return;
		const timer = setInterval(() => void readLanes(), LANE_POLL_MS);
		return () => clearInterval(timer);
	});

	// --- live activation ------------------------------------------------------
	// The full trace yields the done trail (matching the persisted `turn_stages`
	// rehydration); the current frame is authoritative for the node it names.
	const activation = $derived(
		manifest ? activationFromSteps(manifest, $turnSteps, $processingStage) : {}
	);

	const runningId = $derived(
		Object.entries(activation).find(([, state]) => state === 'running')?.[0] ?? null
	);

	// Keep the running node on screen as stages arrive. The board scrolls along
	// its ACTIVE axis: a wide pane draws the flow left→right, so the running node
	// leaves the viewport SIDEWAYS while the vertical scroll is already at the top.
	// `FlowGraph` publishes the axis it drew (`data-flow-orientation`), and centring
	// on the other axis is a no-op — so a vertical board keeps the old behaviour.
	// `lastScrolled` keeps this from fighting a manual scroll between stage
	// boundaries.
	let lastScrolled: string | null = null;
	$effect(() => {
		const id = runningId;
		if (!id || id === lastScrolled) return;
		lastScrolled = id;
		requestAnimationFrame(() => {
			const node = panel?.querySelector('.node.running');
			if (!node) return;
			const axis = panel
				?.querySelector('[data-flow-orientation]')
				?.getAttribute('data-flow-orientation');
			node.scrollIntoView(
				axis === 'horizontal'
					? { block: 'nearest', inline: 'center' }
					: { block: 'center', inline: 'nearest' }
			);
		});
	});

	const nodes = $derived(manifest ? manifest.graphs.flatMap((graph) => graph.nodes) : []);
	const selectedNode = $derived(nodes.find((node) => node.id === selectedId) ?? null);

	function selectNode(id: string): void {
		selectedId = id;
	}

	// --- session debug toggle -------------------------------------------------
	$effect(() => {
		const sid = $sessionId;
		debug = false;
		if (!sid) return;
		void loadDebug(sid);
	});

	async function loadDebug(sid: string): Promise<void> {
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sid)}/agent-debug`, {
				headers: { Accept: 'application/json' }
			});
			if (!res.ok) return;
			const body = (await res.json()) as { enabled?: unknown };
			debug = body.enabled === true;
		} catch {
			// Optional surface — a failed read just leaves the chip off.
		}
	}

	// --- per-node captures (turn-scoped) -------------------------------------
	/**
	 * Slack subtracted from the turn's first stage frame when scoping captures.
	 *
	 * The trace stamps a step with its CLOSING frame timestamp (the fold
	 * overwrites the opened step's `ts` with the `done` frame's), and a capture is
	 * appended a few hundred microseconds BEFORE its own closing frame — so the
	 * boundary sits just ahead of the turn's first capture. The previous turn is a
	 * minute or more earlier, so this cannot reach back into it.
	 */
	const TURN_TS_SLACK_MS = 3_000;

	/**
	 * The current turn's start boundary in epoch ms, or `null` when no trace has
	 * been recorded (the last user-message boundary as far as the client can see:
	 * its first engine stage frame).
	 */
	function turnStartMs(steps: readonly TurnStep[]): number | null {
		const boundary = steps.length ? steps[0].ts : ($processingStage?.ts ?? '');
		if (!boundary) return null;
		const parsed = Date.parse(boundary);
		return Number.isFinite(parsed) ? parsed - TURN_TS_SLACK_MS : null;
	}

	/**
	 * The captures belonging to one node, newest last.
	 *
	 * The join key is the node's `runs.interaction` (the capture's `agent` field
	 * is the INTERACTION id: `narrate`, `bts`, `critic`) — the manifest's
	 * `inspector.agent_filter` names the bound model agent (`narrator`,
	 * `extractor`), which is NOT what `agent-calls?agent=` filters on. The
	 * declared filter is only a last-ditch fallback.
	 */
	function callsForNode(
		node: FlowNode,
		all: InspectorCall[],
		startMs: number | null
	): InspectorCall[] {
		const pool = startMs === null ? all : all.filter((call) => Date.parse(call.ts) >= startMs);
		const key = node.runs.interaction ?? '';
		if (key) {
			const exact = pool.filter((call) => call.agent === key);
			if (exact.length) return exact;
			const related = pool.filter((call) => call.agent.startsWith(`${key}_`));
			if (related.length) return related;
		}
		const filter = node.inspector.agent_filter;
		return filter ? pool.filter((call) => call.agent === filter) : [];
	}

	// Re-resolve whenever the selection moves OR the turn trace advances: a stage
	// boundary can land the capture the selected node needs, so an inspector kept
	// open through a live turn fills in without a second click.
	let callsSeq = 0;
	$effect(() => {
		const node = selectedNode;
		const sid = $sessionId;
		const steps = $turnSteps;
		if (!node || !sid || !debug || node.inspector.kind === 'terminal') {
			callsSeq += 1;
			calls = [];
			callsLoading = false;
			return;
		}
		const startMs = turnStartMs(steps);
		const seq = (callsSeq += 1);
		callsLoading = true;
		void (async () => {
			const all = await getAgentCalls(sid);
			if (seq !== callsSeq) return; // a newer selection/turn superseded this read
			calls = callsForNode(node, all, startMs);
			callsLoading = false;
		})();
	});

	const running = $derived(runningId !== null);
</script>

<div class="flow-panel" data-flow-panel="true" bind:this={panel}>
	<header class="panel-head">
		<span class="panel-title">Turn flow</span>
		<span class="chip" class:live={running} data-flow-status={running ? 'running' : 'idle'}>
			{running ? 'running' : 'idle'}
		</span>
		<span class="chip" class:on={debug} data-flow-debug={debug ? 'on' : 'off'}>
			debug {debug ? 'on' : 'off'}
		</span>
		<button type="button" class="panel-reload" onclick={() => void loadManifest()}>
			Reload
		</button>
	</header>

	{#if manifestLoading && !manifest}
		<p class="panel-note">Loading the turn flow…</p>
	{:else if manifestError}
		<p class="panel-note error" role="alert">The turn flow is unavailable: {manifestError}</p>
	{:else if manifest}
		{#if laneNode}
			<div class="lane-config" data-lane-config="true">
				<span class="lane-title">NPC lanes</span>
				<label class="lane-switch" title="Switch lanes off: the narrator writes this scene alone">
					<input
						type="checkbox"
						checked={laneEnabled}
						disabled={laneBusy}
						data-lane-enabled={laneEnabled ? 'on' : 'off'}
						onchange={(event) =>
							void saveLaneSetting({ enabled: event.currentTarget.checked })}
					/>
					<span class="lane-state">{laneEnabled ? 'on' : 'off'}</span>
				</label>
				<label class="lane-width" title="How many lanes may run at once (every lane still runs)">
					<span>max in flight</span>
					<select
						value={String(laneWidth)}
						disabled={laneBusy || !laneEnabled}
						data-lane-width={laneWidth}
						onchange={(event) =>
							void saveLaneSetting({ max_lanes: Number(event.currentTarget.value) })}
					>
						{#each [1, 2, 3, 4, 5, 6, 7, 8] as width (width)}
							<option value={String(width)}>{width}</option>
						{/each}
					</select>
				</label>
				{#if laneNote}
					<span class="lane-note" class:error={laneNote.includes(':')} role="status">
						{laneNote}
					</span>
				{/if}
			</div>
		{/if}
		<NodeInspector node={selectedNode} {calls} {debug} />
		{#if callsLoading}
			<p class="panel-note">Reading captures for this turn…</p>
		{/if}
		<FlowGraph
			{manifest}
			{activation}
			{debug}
			{selectedId}
			lanes={lanes?.lanes ?? []}
			laneWidth={lanes?.max_parallel ?? laneWidth}
			lanesEnabled={lanes?.enabled ?? laneEnabled}
			inFlight={lanes?.in_flight === true}
			onselect={selectNode}
			{onopenstep}
		/>
	{:else}
		<p class="panel-note">No turn flow to show.</p>
	{/if}
</div>

<style>
	.flow-panel {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		padding: 0.6rem 1rem 0.8rem;
	}
	.panel-head {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 0.5rem;
	}
	.panel-title {
		margin-right: auto;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
	}
	.chip {
		padding: 0.05rem 0.4rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.chip.live {
		border-color: var(--accent-strong);
		color: var(--accent-strong);
	}
	.chip.on {
		border-color: var(--ok);
		color: var(--ok);
	}
	.panel-reload {
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.panel-reload:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.panel-note {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.panel-note.error {
		color: var(--danger-text);
	}
	/* --- the lane config (issues #56.2/#56.3) ------------------------------ */
	.lane-config {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.5rem 0.75rem;
		padding: 0.3rem 0.5rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.lane-title {
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
	}
	.lane-switch,
	.lane-width {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}
	.lane-switch input {
		margin: 0;
		accent-color: var(--accent-strong);
	}
	.lane-state {
		min-width: 1.6rem;
	}
	.lane-width select {
		padding: 0 0.2rem;
		border: 1px solid var(--line);
		border-radius: 4px;
		background: transparent;
		color: var(--text);
		font: inherit;
	}
	.lane-note {
		color: var(--text-dim);
	}
	.lane-note.error {
		color: var(--danger-text);
	}
</style>
