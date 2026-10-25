<script lang="ts">
	// One node box in the flow graph (W9.2). Pure presentational: it renders a
	// `FlowNode` plus its derived activation state and reports intent through
	// two callbacks. It opens nothing itself — the prompt-door affordance only
	// emits the step id, so the page (or the future Studio editor) decides what
	// "open" means.

	import type { ActivationState, FlowNode } from './types';

	let {
		node,
		state = 'pending',
		selected = false,
		debug = false,
		onselect,
		onopenstep,
		arm = false,
		laneNote = ''
	}: {
		node: FlowNode;
		/** A fan-out arm (one per live lane member) — compact, no door. */
		arm?: boolean;
		/** Fan-out state caption ("2 lanes · 4 in flight", or why there are none). */
		laneNote?: string;
		/** `pending` (static this wave) / `running` / `done`. */
		state?: ActivationState;
		selected?: boolean;
		/** Session debug toggle — shows whether captures can be inspected. */
		debug?: boolean;
		onselect?: (id: string) => void;
		/** Prompt-door callback: carries the W6 step id, opens nothing. */
		onopenstep?: (stepId: string) => void;
	} = $props();

	/** The kind chip: gates read as `gate`; otherwise the run kind. */
	const kindLabel = $derived.by(() => {
		if (node.kind === 'gate') return 'gate';
		switch (node.runs.kind) {
			case 'model_pass':
				return 'model pass';
			case 'fixed':
				return 'fixed';
			case 'engine':
				return 'engine';
			case 'hook':
				return 'hook';
			case 'embedder':
				return 'embedder';
		}
	});

	/** The agent that actually runs this node (bound preset overlay wins). */
	const agentName = $derived(node.agent?.bound_agent || node.agent?.default_agent || '');
	const agentBound = $derived(!!node.agent?.bound_agent);

	/** Region marker: cache-contract regions this node's composition declares. */
	const regionMarkers = $derived.by(() => {
		const regions = node.composition?.regions ?? [];
		return regions
			.filter((r) => r.kind === 'stable_prefix' || r.kind === 'dynamic_tail')
			.map((r) => ({ id: r.id, kind: r.kind, title: r.title }));
	});

	const stepId = $derived(node.prompt_surface.door === 'step_editor' ? node.prompt_surface.step_id : null);
	/** The engine-owned reason shown when there is no step-editor door. */
	const fixedReason = $derived(
		node.prompt_surface.door === 'step_editor' ? '' : node.prompt_surface.reason
	);
	const noDoor = $derived(node.prompt_surface.door === 'none');
</script>

<div
	class="node" class:arm
	class:selected
	class:running={state === 'running'}
	class:done={state === 'done'}
	class:static={state === 'pending'}
>
	<button
		type="button"
		class="node-main"
		aria-current={selected ? 'true' : undefined}
		title={node.meta.description || node.runs.description}
		onclick={() => onselect?.(node.id)}
	>
		<span class="node-head">
			<span class="title">{node.title}</span>
			<span class="kind" data-kind={node.kind}>{kindLabel}</span>
		</span>
		{#if node.kind !== 'stage'}
			<span class="node-subkind">{node.kind}</span>
		{/if}
		<span class="badges">
			{#if agentName}
				<span class="badge agent" title={agentBound ? 'Active preset binding' : 'Default agent'}>
					{agentBound ? 'bound' : 'agent'}: {agentName}
				</span>
			{/if}
			{#each regionMarkers as region (region.id)}
				<span class="badge region" data-region={region.kind} title={region.title}>
					{region.kind === 'stable_prefix' ? 'prefix' : 'tail'}
				</span>
			{/each}
			{#if node.composition}
				<span class="badge nested" title="Composition — expanded in W9.8">nested</span>
			{/if}
			{#if debug && node.inspector.kind === 'model_pass'}
				<span class="badge capturable" title="Captured calls available with debug on">captures</span>
			{/if}
		</span>
	</button>

	{#if laneNote}
		<p class="lane-note">{laneNote}</p>
	{/if}
	{#if !arm}
	<div class="door">
		{#if stepId}
			<button
				type="button"
				class="door-open"
				title={`Open the step editor for "${stepId}"`}
				onclick={() => onopenstep?.(stepId)}
			>
				edit step
			</button>
		{:else}
			<span class="door-fixed" class:none={noDoor} title={fixedReason}>
				{noDoor ? 'no prompt door' : `fixed — ${fixedReason}`}
			</span>
		{/if}
	</div>
	{/if}
</div>

<style>
	.lane-note {
		margin: 0;
		padding: 0 0.5rem 0.4rem;
		font-size: 0.68rem;
		color: color-mix(in srgb, var(--fg) 65%, transparent);
	}
	.node.arm {
		border-style: dashed;
		border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
		background: color-mix(in srgb, var(--accent) 6%, var(--bg-raised));
	}
	.node.arm .node-head {
		font-size: 0.72rem;
	}
	.node {
		display: flex;
		flex-direction: column;
		height: 100%;
		border: 1px solid var(--line);
		border-radius: 7px;
		background: var(--bg-raised);
		overflow: hidden;
		transition: border-color 0.15s ease, box-shadow 0.15s ease;
	}
	.node.selected {
		border-color: var(--accent);
	}
	.node.running {
		border-color: var(--accent-strong);
		box-shadow: 0 0 0 2px var(--accent-soft);
	}
	.node.done {
		border-color: var(--accent-line);
	}
	.node.static {
		opacity: 0.82;
	}
	.node-main {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.2rem;
		flex: 1;
		width: 100%;
		padding: 0.4rem 0.5rem;
		border: none;
		background: transparent;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}
	.node-main:hover {
		background: var(--bg-hover);
	}
	.node-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.4rem;
		width: 100%;
	}
	.title {
		font-weight: 600;
		font-size: var(--font-small);
		line-height: 1.15;
	}
	.kind {
		flex: 0 0 auto;
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.kind[data-kind='gate'] {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.node-subkind {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.badges {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
		margin-top: 0.1rem;
	}
	.badge {
		padding: 0.05rem 0.35rem;
		border-radius: 999px;
		font-size: var(--font-micro);
		line-height: 1.5;
		border: 1px solid var(--line);
		color: var(--text-muted);
		white-space: nowrap;
	}
	.badge.region {
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.badge.region[data-region='dynamic_tail'] {
		border-color: var(--warn);
		color: var(--warn);
	}
	.badge.nested {
		border-style: dashed;
	}
	.badge.capturable {
		border-color: var(--ok);
		color: var(--ok);
	}
	.door {
		border-top: 1px solid var(--line);
		padding: 0.2rem 0.5rem;
		min-height: 1.4rem;
		display: flex;
		align-items: center;
	}
	.door-open {
		border: none;
		background: var(--accent-soft);
		color: var(--accent-strong);
		border-radius: 4px;
		padding: 0.1rem 0.4rem;
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.door-open:hover {
		background: var(--accent-line);
		color: var(--on-accent);
	}
	.door-fixed {
		font-size: var(--font-micro);
		color: var(--text-dim);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.door-fixed.none {
		font-style: italic;
	}
</style>
