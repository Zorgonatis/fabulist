<script lang="ts">
	// Studio graph home (W9.4, R11: "the graph becomes the Studio's home") — and
	// the topology EDITOR (W9.5, R19/R21/R23).
	//
	// A PROPS-ONLY studio pane (`StudioPaneProps`): it renders the SAME
	// `FlowGraph` the in-chat panel does, over the same manifest endpoint, so the
	// studio is a second host of one renderer — never a second copy of the graph.
	//
	// Two modes, one pane:
	//  · read-only (default) renders the MANIFEST: the engine's description of
	//    what runs, agent bindings, prompt doors, off-turn graphs.
	//  · edit renders the TOPOLOGY: `GET /api/prompts/topology` → mutations in a
	//    local draft → `PUT` on Save. The manifest is a description; the topology
	//    is what the graph processor EXECUTES, which is why the editor writes one
	//    and never the other.
	//
	// What it deliberately does NOT do (and why):
	//  · No live activation. Activation is a per-TURN trail derived from the chat
	//    stores; with no turn in flight in the studio it stays empty, so every
	//    node renders static (`FlowGraph` does that on an empty map).
	//  · No capture fetching. Debug captures are SESSION state (`agent-debug` +
	//    `agent-calls`, scoped to the current turn), and the studio is a
	//    definition surface with no session by contract (AGENTS.md NN7). The
	//    inspector therefore renders the shared `NodeInspector` with `debug`
	//    off and points at chat for inputs/outputs.
	//  · Node templates (W9.10/R21) are the palette's source: the edit palette
	//    lists the LIBRARY grouped by klass (`GET /api/node-templates`) and a
	//    click writes a topology node row referencing the template, so the graph
	//    processor executes the behaviour the template implements. Only when that
	//    fetch fails does the palette fall back to the canonical node set (the
	//    same rows derived locally), so the editor still works offline.
	//  · The prompt door is a LINK, not a copy: a node's step chip bubbles its W6
	//    step id up through `onopenstep`, and the host page opens the drawer
	//    (same contract as `ChatFlowPanel`).
	//
	// `focusId` / `startNew` / `requestSeq` are part of the pane contract and are
	// inert here: a graph pane creates nothing and has no row to focus. They are
	// destructured anyway so the contract is visible at the acceptance site.
	import { errorMessage } from '#lib/api/shared';
	import {
		MOCK_TOPOLOGY,
		getFlowManifest,
		getLanes,
		getTopology,
		putTopology,
		violationsFromError
	} from '#lib/api/flow';
	import {
		NODE_TEMPLATE_KLASSES,
		behaviourMap,
		kindForKlass,
		listNodeTemplates,
		type NodeTemplate,
		type NodeTemplateKlass,
		type NodeTemplateLibrary
	} from '#lib/api/node_templates';
	import { addEdge, addNode, cloneTopology, validateLocal } from '#lib/flow/editor';
	import FlowGraph from '#lib/flow/FlowGraph.svelte';
	import NodeInspector from '#lib/flow/NodeInspector.svelte';
	import type {
		FlowManifest,
		FlowNodeKind,
		Topology,
		TopologyViolation
	} from '#lib/flow/types';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onopenstep,
		sessionId = null
	}: {
		/** Inert: the graph has no focusable row (see the header note). */
		focusId?: string | null;
		/** Inert: nothing is created from the graph. */
		startNew?: boolean;
		/** Inert: no `focusId` to re-apply. */
		requestSeq?: number | null;
		/** Prompt-door callback: carries the W6 step id (the page opens it). */
		onopenstep?: (stepId: string) => void;
		/** The open chat: lets the read-only board draw the ACTUAL lane arms. */
		sessionId?: string | null;
	} = $props();

	let manifest = $state<FlowManifest | null>(null);
	/** W9.8: the open chat's ACTUAL lane set — the fan arms, as in chat. */
	let lanes = $state<{ lanes: { member_id: string | null; name: string | null; intent: string | null; ok: boolean }[]; max_parallel: number } | null>(null);
	let manifestError = $state('');
	let manifestLoading = $state(true);
	let selectedId = $state<string | null>(null);

	// --- topology editor state ---------------------------------------------
	let editing = $state(false);
	let topologyBusy = $state(false);
	let topologyError = $state('');
	let saveError = $state('');
	let saveNote = $state('');
	/** The engine's rejections after a refused PUT (empty otherwise). */
	let serverViolations = $state<TopologyViolation[]>([]);
	/** The engine's canonical wiring — the palette source (never edited). */
	let canonical = $state<Topology | null>(null);
	/** What the server last returned (the Revert target). */
	let saved = $state<Topology | null>(null);
	/** What the editor is working on. */
	let draft = $state<Topology | null>(null);

	let newNodeId = $state('');
	/** The node-template library (null = fetch failed -> canonical fallback). */
	let library = $state<NodeTemplateLibrary | null>(null);
	/** The template the next "Add" instantiates ('' = pick one). */
	let newNodeTemplate = $state('');
	// W9.5: fan-out / rewire by connecting two declared nodes (an edge may be
	// added but never duplicated; branches carry a condition).
	let edgeFrom = $state('');
	let edgeTo = $state('');
	let edgeKind = $state<'flow' | 'branch'>('flow');
	let edgeCondition = $state('');

	const canonicalNodes = $derived((canonical ?? MOCK_TOPOLOGY).nodes);
	const canonicalNodeIds = $derived(canonicalNodes.map((node) => node.id));

	/**
	 * The palette's templates. The API library when it loaded; otherwise the
	 * canonical node set turned into equivalent rows (`implements` = its own id,
	 * klass from the contract vocabulary) — the palette's pre-library source, so
	 * an offline load still offers every runnable node.
	 */
	const paletteTemplates = $derived<NodeTemplate[]>(
		library && library.templates.length
			? library.templates
			: canonicalNodes.map((node) => ({
					id: `canonical:${node.id}`,
					name: node.id,
					klass: (node.kind === 'terminal'
						? 'terminal'
						: node.kind === 'gate'
							? 'gate'
							: 'model_pass') as NodeTemplateKlass,
					implements: node.id,
					interaction: null,
					step_id: null,
					config: {},
					description: '',
					builtin: true
				}))
	);
	const paletteKlasses = $derived<NodeTemplateKlass[]>(
		library && library.klasses.length ? library.klasses : [...NODE_TEMPLATE_KLASSES]
	);
	/** The palette row the next "Add" instantiates (undefined = nothing picked). */
	const selectedTemplate = $derived(
		paletteTemplates.find((template) => template.id === newNodeTemplate)
	);
	/** The template → implements map the local validator resolves node ids with. */
	const paletteBehaviours = $derived(behaviourMap({ templates: paletteTemplates, klasses: paletteKlasses }));

	const localViolations = $derived(
		draft ? validateLocal(draft, canonicalNodeIds, paletteBehaviours) : []
	);
	/** Server findings win: they are authoritative and name the same problems. */
	const violations = $derived(serverViolations.length ? serverViolations : localViolations);
	const dirty = $derived(!!draft && !!saved && JSON.stringify(draft) !== JSON.stringify(saved));
	const canSave = $derived(editing && dirty && localViolations.length === 0 && !topologyBusy);

	const missingCanonical = $derived.by(() => {
		const current = draft;
		if (!current) return [];
		return canonicalNodes.filter((node) => !current.nodes.some((kept) => kept.id === node.id));
	});

	async function loadManifest(): Promise<void> {
		manifestLoading = true;
		manifestError = '';
		try {
			manifest = await getFlowManifest();
			lanes = await getLanes(sessionId);
		} catch (err) {
			manifestError = err instanceof Error ? err.message : 'flow manifest unavailable';
		} finally {
			manifestLoading = false;
		}
	}

	// The topology is session-constant (the endpoint overlays the active preset's
	// bound agent), so it is read once per pane mount; Reload re-reads by hand
	// after a definition/preset change made in another tab.
	$effect(() => {
		void loadManifest();
	});

	const nodes = $derived(manifest ? manifest.graphs.flatMap((graph) => graph.nodes) : []);
	const selectedNode = $derived(nodes.find((node) => node.id === selectedId) ?? null);

	function selectNode(id: string): void {
		selectedId = id;
	}

	// --- editable topology --------------------------------------------------

	async function enterEdit(): Promise<void> {
		topologyBusy = true;
		topologyError = '';
		saveError = '';
		saveNote = '';
		serverViolations = [];
		try {
			const [response, templates] = await Promise.all([
				getTopology(),
				listNodeTemplates()
			]);
			library = templates;
			canonical = response.canonical;
			saved = cloneTopology(response.topology);
			draft = cloneTopology(response.topology);
			serverViolations = response.violations;
			editing = true;
		} catch (err) {
			topologyError = errorMessage(err);
		} finally {
			topologyBusy = false;
		}
	}

	function doneEditing(): void {
		editing = false;
		draft = null;
		saved = null;
		serverViolations = [];
		saveError = '';
		saveNote = '';
	}

	function revert(): void {
		if (!saved) return;
		draft = cloneTopology(saved);
		serverViolations = [];
		saveError = '';
		saveNote = '';
	}

	async function save(): Promise<void> {
		const current = draft;
		if (!current) return;
		topologyBusy = true;
		saveError = '';
		saveNote = '';
		try {
			const response = await putTopology(current);
			saved = cloneTopology(response.topology);
			draft = cloneTopology(response.topology);
			canonical = response.canonical;
			serverViolations = response.violations;
			saveNote = 'Saved. The engine builds the graph from this topology on its next turn.';
		} catch (err) {
			saveError = errorMessage(err);
			serverViolations = violationsFromError(err);
		} finally {
			topologyBusy = false;
		}
	}

	function applyChange(next: Topology): void {
		draft = next;
		// The local checks are the ones that matter for what is on screen now.
		serverViolations = [];
		saveError = '';
		saveNote = '';
	}

	function connectNodes(): void {
		const current = draft;
		if (!current || !edgeFrom || !edgeTo) return;
		const edge: Record<string, unknown> = { from: edgeFrom, to: edgeTo, kind: edgeKind };
		if (edgeKind === 'branch') {
			const condition = edgeCondition.trim();
			if (!condition) {
				saveError = 'a branch edge needs a condition (e.g. verdict == fail)';
				return;
			}
			edge.condition = condition;
			edge.label = condition.replace(/^verdict\s*==\s*/, '');
			edge.cap = 2;
		}
		const next = addEdge(current, edge as never);
		if (next === current) {
			saveError = 'that edge already exists, is a self-edge, or names an unknown node';
			return;
		}
		saveError = '';
		applyChange(next);
		edgeFrom = '';
		edgeTo = '';
		edgeCondition = '';
	}

	function addById(id: string, kind: FlowNodeKind, template?: NodeTemplate): void {
		const current = draft;
		if (!current) return;
		// The canonical re-add buttons are a RESET convenience: they write a plain
		// canonical row (no template), which the engine still executes by id.
		const next = addNode(
			current,
			{
				id,
				kind,
				...(template ? { template: template.id, data: { ...template.config } } : {})
			},
			canonicalNodeIds
		);
		if (next === current) return; // empty or already-declared id
		applyChange(next);
	}

	/** Add one node FROM a template: the row references it (W9.10). */
	function addFromTemplate(template: NodeTemplate): void {
		const id = newNodeId.trim() || defaultNodeId(template);
		const before = draft;
		if (!before) return;
		addById(id, kindForKlass(template.klass), template);
		if (draft !== before) {
			newNodeId = '';
			saveError = '';
		} else {
			saveError = `\"${id}\" is already declared \u2014 choose another id`;
		}
	}

	/** A first guess at a unique node id for a template (`narrate`, `narrate_2`, …). */
	function defaultNodeId(template: NodeTemplate): string {
		const base = template.implements || 'node';
		const declared = (draft?.nodes ?? []).map((node) => node.id);
		if (!declared.includes(base)) return base;
		for (let n = 2; n < 100; n += 1) {
			const candidate = `${base}_${n}`;
			if (!declared.includes(candidate)) return candidate;
		}
		return `${base}_new`;
	}

	function pickTemplate(id: string): void {
		newNodeTemplate = id;
		saveError = '';
	}

	function templatesOf(klass: NodeTemplateKlass): NodeTemplate[] {
		return paletteTemplates.filter((template) => template.klass === klass);
	}

	function submitNewNode(event: SubmitEvent): void {
		event.preventDefault();
		const template = selectedTemplate;
		if (!template) {
			saveError = 'pick a node template to add';
			return;
		}
		addFromTemplate(template);
	}
</script>

<div class="graph-home" data-graph-home="true">
	<header class="home-head">
		<span class="home-title">Turn flow</span>
		<span class="chip">no live turn</span>
		<span class="chip">debug: chat only</span>
		{#if editing}
			<button
				type="button"
				class="home-action primary"
				disabled={!canSave}
				title={localViolations.length
					? 'Fix the violations below first'
					: dirty
						? 'Store the topology'
						: 'Nothing to save'}
				onclick={() => void save()}
			>
				{topologyBusy ? 'Saving…' : 'Save'}
			</button>
			<button
				type="button"
				class="home-action"
				disabled={!dirty || topologyBusy}
				onclick={revert}
			>
				Revert
			</button>
			<button type="button" class="home-action" disabled={topologyBusy} onclick={doneEditing}>
				Done
			</button>
		{:else}
			<button
				type="button"
				class="home-action"
				disabled={topologyBusy}
				title="Edit the executable topology"
				onclick={() => void enterEdit()}
			>
				{topologyBusy ? 'Loading…' : 'Edit'}
			</button>
			<button type="button" class="home-reload" onclick={() => void loadManifest()}>
				Reload manifest
			</button>
		{/if}
	</header>

	{#if topologyError}
		<p class="home-note error" role="alert">Topology unavailable: {topologyError}</p>
	{/if}

	{#if editing && draft}
		<p class="home-note">
			<strong>topology = execution order</strong> — the engine builds and runs this graph; the
			manifest (the descriptive view with prompt doors and agents) is what you see with editing off.
		</p>

		<div class="palette">
			<span class="palette-title">Add node</span>
			{#each missingCanonical as node (node.id)}
				<button
					type="button"
					class="palette-btn"
					title={`Re-add the canonical "${node.id}" node`}
					onclick={() => addById(node.id, node.kind)}
				>
					+ {node.id}<span class="palette-kind">{node.kind}</span>
				</button>
			{/each}
			{#each paletteKlasses as klass (klass)}
				{@const rows = templatesOf(klass)}
				{#if rows.length}
					<div class="palette-group" data-template-klass={klass}>
						<span class="palette-kind">{klass}</span>
						{#each rows as template (template.id)}
							<button
								type="button"
								class="palette-btn"
								class:selected={newNodeTemplate === template.id}
								aria-pressed={newNodeTemplate === template.id}
								title={template.description || `runs ${template.implements}`}
								onclick={() => pickTemplate(template.id)}
							>
								{template.name}<span class="palette-kind">{template.implements}</span>
							</button>
						{/each}
					</div>
				{/if}
			{/each}
			<form class="palette-new" onsubmit={submitNewNode}>
				<input
					class="palette-input"
					bind:value={newNodeId}
					placeholder="new node id (optional)"
					aria-label="New node id"
					spellcheck="false"
					title="The id the node runs under. It executes the behaviour the chosen template implements, so any unused id works."
				/>
				<button type="submit" class="palette-btn" disabled={!selectedTemplate}>Add</button>
			</form>
			<form class="palette" onsubmit={(event) => { event.preventDefault(); connectNodes(); }}>
				<select class="palette-select" bind:value={edgeFrom} aria-label="Edge from">
					<option value="">from…</option>
					{#each draft?.nodes ?? [] as node (node.id)}
						<option value={node.id}>{node.id}</option>
					{/each}
				</select>
				<select class="palette-select" bind:value={edgeTo} aria-label="Edge to">
					<option value="">to…</option>
					{#each draft?.nodes ?? [] as node (node.id)}
						<option value={node.id}>{node.id}</option>
					{/each}
				</select>
				<select class="palette-select" bind:value={edgeKind} aria-label="Edge kind">
					<option value="flow">flow</option>
					<option value="branch">branch</option>
				</select>
				{#if edgeKind === 'branch'}
					<input class="palette-input" bind:value={edgeCondition} placeholder="verdict == ok" aria-label="Edge condition" spellcheck="false" />
				{/if}
				<button type="submit" class="palette-btn" disabled={!edgeFrom || !edgeTo}>Connect</button>
			</form>
		</div>

		{#if violations.length}
			<ul class="violations" role="status">
				{#each violations as violation}
					<li>
						<code>{violation.code}</code>
						{violation.message}
					</li>
				{/each}
			</ul>
		{:else}
			<p class="home-note">No violations.</p>
		{/if}
		{#if saveError}
			<p class="home-note error" role="alert">{saveError}</p>
		{/if}
		{#if saveNote}
			<p class="home-note">{saveNote}</p>
		{/if}

		<NodeInspector node={selectedNode} debug={false} />
		<FlowGraph
			{manifest}
			activation={{}}
			debug={false}
			{editing}
			topology={draft}
			onchange={applyChange}
			{selectedId}
			onselect={selectNode}
			{onopenstep}
		/>
	{:else if manifestLoading && !manifest}
		<p class="home-note">Loading the flow manifest…</p>
	{:else if manifestError}
		<p class="home-note error" role="alert">Flow manifest unavailable: {manifestError}</p>
	{:else if manifest}
		{#if selectedNode}
			<p class="home-note">
				Inputs and outputs come from debug captures, which are per-session — open Flow in chat
				during a turn to inspect them.
			</p>
		{/if}
		<p class="home-note">
			This is the <strong>manifest</strong> — what the engine describes and runs. Edit turns the
			pane into the <strong>topology</strong> editor (execution order; the engine runs that).
		</p>
		<NodeInspector node={selectedNode} debug={false} />
		<FlowGraph
			{manifest}
			activation={{}}
			debug={false}
			{selectedId}
			lanes={lanes?.lanes ?? []}
			laneWidth={lanes?.max_parallel ?? 4}
			onselect={selectNode}
			{onopenstep}
		/>
	{:else}
		<p class="home-note">No flow manifest.</p>
	{/if}
</div>

<style>
	.graph-home {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.home-head {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	.home-title {
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
		white-space: nowrap;
	}
	.home-reload,
	.home-action {
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		cursor: pointer;
		white-space: nowrap;
	}
	.home-reload:hover,
	.home-action:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--text);
	}
	.home-action.primary {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.home-action.primary:hover:not(:disabled) {
		background: var(--accent-line);
		color: var(--on-accent);
	}
	.home-action:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.palette {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.3rem;
		padding: 0.35rem;
		border: 1px dashed var(--line);
		border-radius: 7px;
	}
	.palette-title {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
		margin-right: 0.2rem;
	}
	.palette-new {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		margin-left: 0.3rem;
	}
	.palette-group {
		display: inline-flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.25rem;
		padding-left: 0.3rem;
		border-left: 1px solid var(--line);
	}
	.palette-btn.selected {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.palette-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.1rem 0.4rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.palette-btn:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--text);
	}
	.palette-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.palette-kind {
		color: var(--text-dim);
	}
	.palette-input,
	.palette-select {
		padding: 0.1rem 0.35rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: var(--bg-inset);
		color: var(--text);
		font: inherit;
		font-size: var(--font-micro);
	}
	.palette-input {
		width: 8rem;
	}
	.violations {
		margin: 0;
		padding-left: 1.1rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
	.violations code {
		font-size: var(--font-micro);
		padding: 0.02rem 0.3rem;
		border: 1px solid var(--line);
		border-radius: 4px;
		color: var(--text-muted);
		margin-right: 0.25rem;
	}
	.home-note {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
		line-height: 1.4;
	}
	.home-note.error {
		color: var(--danger-text);
	}
</style>
