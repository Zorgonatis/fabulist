<script lang="ts">
	// Inline per-node inspector (W9.3, decision 5). Presentational: the caller
	// resolves and passes the captured calls + system events for the selected
	// node (turn-scoped), and this component maps them to inputs/outputs by the
	// node's declared `inspector.kind` (plan §4). No fetching here — the chat
	// wrapper owns the requests.
	//
	// Captures only exist for turns that ran with the session debug toggle on,
	// so when `debug` is off the inspector shows the enable affordance.
	//
	// A node that declares a `composition` is itself a graph (R13/W9.8): the
	// drill-down renders under the header as `<CompositionView>` — structural, always
	// meaningful, so it sits above the turn-scoped captures. Both hosts (the chat
	// panel and the Studio graph home) show it with no chat coupling.

	import CompositionView from './CompositionView.svelte';
	import type { FlowNode, InspectorCall, InspectorEvent, InspectorField, InspectorView } from './types';

	let {
		node,
		calls = [],
		events = [],
		debug = false,
		onselect
	}: {
		node: FlowNode | null;
		/** Captured model passes for this node's agent, turn-scoped. */
		calls?: InspectorCall[];
		/** System events for this node (fixed stages / gates). */
		events?: InspectorEvent[];
		/** Session debug toggle (captures exist only when it was on). */
		debug?: boolean;
		/** Reported with a composition module id — view-only today. */
		onselect?: (id: string) => void;
	} = $props();

	function stringify(value: unknown): string {
		if (value === null || value === undefined) return '';
		if (typeof value === 'string') return value;
		try {
			return JSON.stringify(value, null, 2);
		} catch {
			return String(value);
		}
	}

	/** Render a capture's message list as `[role] content` blocks. */
	function formatMessages(messages: Array<Record<string, unknown>>): string {
		return messages
			.map((message) => {
				const role = typeof message.role === 'string' ? message.role : '?';
				const content = message.content;
				const body =
					typeof content === 'string' ? content : stringify(content ?? message);
				return `[${role}] ${body}`;
			})
			.join('\n\n');
	}

	function view(node: FlowNode | null): InspectorView {
		if (!node) return { inputs: [], outputs: [], empty: '' };
		const kind = node.inspector.kind;
		const declared = node.inspector.events ?? [];
		const latest = calls.length ? calls[calls.length - 1] : null;

		if (kind === 'model_pass') {
			const inputs: InspectorField[] = [];
			const outputs: InspectorField[] = [];
			if (latest) {
				if (latest.messages.length) {
					inputs.push({
						label: `messages (${latest.messages.length})`,
						value: formatMessages(latest.messages),
						kind: 'messages'
					});
				}
				if (latest.response_text) {
					outputs.push({ label: 'response', value: latest.response_text, kind: 'text' });
				}
				if (latest.reasoning_text) {
					outputs.push({ label: 'reasoning', value: latest.reasoning_text, kind: 'text' });
				}
				const meta: string[] = [];
				if (latest.finish) meta.push(`finish=${latest.finish}`);
				if (typeof latest.latency_s === 'number') meta.push(`latency=${latest.latency_s}s`);
				if (meta.length) {
					outputs.push({ label: 'meta', value: meta.join('  '), kind: 'text' });
				}
			}
			return {
				inputs,
				outputs,
				empty: latest ? '' : 'No captured call for this turn.'
			};
		}

		if (kind === 'gate') {
			const observed = events.filter((event) => !declared.length || declared.includes(event.name));
			const inputs: InspectorField[] = [];
			const outputs: InspectorField[] = [];
			// Actionable payload: the verdict event if present, else the model call.
			const verdictEvent = observed.length ? observed[observed.length - 1] : null;
			if (verdictEvent) {
				inputs.push({
					label: verdictEvent.name,
					value: stringify(verdictEvent.payload),
					kind: 'text'
				});
			} else if (latest) {
				inputs.push({
					label: `messages (${latest.messages.length})`,
					value: formatMessages(latest.messages),
					kind: 'messages'
				});
			}
			const decision = verdictEvent ? stringify(verdictEvent.payload) : latest?.response_text ?? '';
			if (decision) outputs.push({ label: 'decision', value: decision, kind: 'text' });
			return {
				inputs,
				outputs,
				empty: decision || inputs.length ? '' : 'No decision payload for this turn.'
			};
		}

		if (kind === 'fixed_stage') {
			const inputs: InspectorField[] = [
				{
					label: 'trigger events',
					value: declared.length ? declared.join(', ') : 'none declared',
					kind: 'text'
				}
			];
			const outputs: InspectorField[] = events.map((event) => ({
				label: event.ts ? `${event.name} @ ${event.ts}` : event.name,
				value: stringify(event.payload),
				kind: 'text'
			}));
			return {
				inputs,
				outputs,
				empty: outputs.length ? '' : 'No engine events recorded for this step this turn.'
			};
		}

		return { inputs: [], outputs: [], empty: 'Terminal node — no inputs or outputs.' };
	}

	const resolved = $derived(view(node));
</script>

<div class="inspector">
	{#if node}
		<header class="head">
			<h4 class="node-title">{node.title}</h4>
			<span class="kind">{node.inspector.kind}</span>
			{#if node.inspector.agent_filter}
				<span class="agent">agent: {node.inspector.agent_filter}</span>
			{/if}
		</header>

		{#if node.composition}
			<section class="composition-block">
				<h5 class="io-title">Inside this node</h5>
				<CompositionView composition={node.composition} {onselect} />
			</section>
		{/if}

		{#if !debug}
			<p class="debug-banner">
				Turn on session debug to see inputs/outputs — captures only exist for turns that ran with it on.
			</p>
		{/if}

		<section class="io">
			<h5 class="io-title">Inputs</h5>
			{#if resolved.inputs.length}
				{#each resolved.inputs as field}
					<div class="field">
						<span class="field-label">{field.label}</span>
						<pre class="field-value" class:mono={field.kind === 'messages'}>{field.value}</pre>
					</div>
				{/each}
			{:else}
				<p class="none">—</p>
			{/if}
		</section>

		<section class="io">
			<h5 class="io-title">Outputs</h5>
			{#if resolved.outputs.length}
				{#each resolved.outputs as field}
					<div class="field">
						<span class="field-label">{field.label}</span>
						<pre class="field-value" class:mono={field.kind === 'messages'}>{field.value}</pre>
					</div>
				{/each}
			{:else}
				<p class="none">—</p>
			{/if}
		</section>

		{#if resolved.empty}
			<p class="empty">{resolved.empty}</p>
		{/if}
	{:else}
		<p class="empty">Select a node to inspect its inputs and outputs.</p>
	{/if}
</div>

<style>
	.inspector {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-panel);
		padding: 0.6rem 0.7rem;
	}
	.head {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	.node-title {
		margin: 0;
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.kind {
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.agent {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.debug-banner {
		margin: 0;
		padding: 0.35rem 0.45rem;
		border-radius: 6px;
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-size: var(--font-micro);
		line-height: 1.35;
	}
	.io {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.io-title {
		margin: 0;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.field-label {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.field-value {
		margin: 0;
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-inset);
		color: var(--text);
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: var(--font-micro);
		line-height: 1.4;
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 14rem;
		overflow: auto;
	}
	.field-value.mono {
		font-family: var(--font-mono, ui-monospace, monospace);
	}
	.none {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.composition-block {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding-bottom: 0.35rem;
		border-bottom: 1px solid var(--line);
	}
</style>
