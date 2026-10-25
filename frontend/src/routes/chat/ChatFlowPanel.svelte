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
	import { getFlowManifest, getLanes } from '#lib/api/flow';
	import type { FlowLanes } from '#lib/api/flow';
	import { activationFromSteps } from '#lib/flow/activation';
	import FlowGraph from '#lib/flow/FlowGraph.svelte';
	import NodeInspector from '#lib/flow/NodeInspector.svelte';
	import type { FlowManifest, FlowNode, InspectorCall } from '#lib/flow/types';
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

	async function loadManifest(): Promise<void> {
		manifestLoading = true;
		manifestError = '';
		try {
			manifest = await getFlowManifest();
		} catch (err) {
			manifestError = err instanceof Error ? err.message : 'flow manifest unavailable';
		} finally {
			manifestLoading = false;
		}
	}

	// The topology (incl. the preset's `bound_agent` overlay) is session-constant,
	// so it is read once; the Reload control re-reads it by hand.
	$effect(() => {
		void loadManifest();
	});

	// --- the lane set (W9.8) --------------------------------------------------
	//
	// The lane set is NOT session-constant: it is a fact of the LAST turn, folded
	// from the log (`npc_lane` rows + their `npc_lanes` summary). It is therefore
	// re-read whenever the turn trace moves — a stage frame is exactly the moment
	// the fan-in join can have written the rows. Reading it once at mount (as this
	// panel used to) pinned the empty set of a panel opened before or during the
	// turn, so the arms never appeared for the turn that produced them.
	//
	// Svelte 5: read the `$store` values directly — `get(store)` inside an effect
	// does not track. `$turnSteps` advances per stage frame and `$processingStage`
	// moves with it; either change re-reads the set. The seq guard drops a slow
	// response that a newer frame (or a session switch) has already superseded.
	let lanesSeq = 0;
	$effect(() => {
		const sid = $sessionId;
		void $turnSteps.length;
		void $processingStage;
		if (!sid) {
			lanes = null;
			return;
		}
		const seq = (lanesSeq += 1);
		void (async () => {
			const next = await getLanes(sid);
			if (seq !== lanesSeq) return; // superseded by a newer frame or session
			lanes = next;
		})();
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
			Reload manifest
		</button>
	</header>

	{#if manifestLoading && !manifest}
		<p class="panel-note">Loading the flow manifest…</p>
	{:else if manifestError}
		<p class="panel-note error" role="alert">Flow manifest unavailable: {manifestError}</p>
	{:else if manifest}
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
			laneWidth={lanes?.max_parallel ?? 4}
			onselect={selectNode}
			{onopenstep}
		/>
	{:else}
		<p class="panel-note">No flow manifest.</p>
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
</style>
