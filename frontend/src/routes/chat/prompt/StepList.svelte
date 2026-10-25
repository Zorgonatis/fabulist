<script lang="ts">
	// The Steps list (W6.4): one row per prompt step — the ordered life of a
	// turn. Each row answers "what is this step, which agent runs it, is it on,
	// how many blocks does it own" at a glance; the row body opens the step.
	//
	// The component is presentational: the drawer owns the draft preset and
	// passes the resolved rows plus three mutation callbacks (select / rebind /
	// toggle). No API calls happen here.

	/** One resolved step row (catalog display meta + draft-preset state). */
	export interface StepRowView {
		id: string;
		/** Plain-language step name (server catalog, local fallback). */
		title: string;
		/** One-line description of what the step does. */
		blurb: string;
		/** Engine-facing timing blurb (tooltip). */
		trigger: string;
		/** The agent that runs when no binding is set. */
		defaultAgent: string;
		/** The active preset's binding for this step ('' = use the default). */
		boundAgent: string;
		/** How many prompt blocks the step's flow carries. */
		blockCount: number;
		/** False when every block in the step is disabled. */
		enabled: boolean;
		/** True while this step's editor is open. */
		selected: boolean;
	}

	let {
		rows,
		agentOptions,
		onSelect,
		onRebind,
		onToggle
	}: {
		rows: StepRowView[];
		/** Agents offered by the inline rebind select. */
		agentOptions: string[];
		onSelect: (id: string) => void;
		onRebind: (id: string, agent: string) => void;
		onToggle: (id: string) => void;
	} = $props();
</script>

<ol class="steps">
	{#each rows as row (row.id)}
		<li class="step" class:selected={row.selected} class:off={!row.enabled}>
			<button
				type="button"
				class="entry"
				aria-current={row.selected ? 'true' : undefined}
				title={row.trigger || row.blurb}
				onclick={() => onSelect(row.id)}
			>
				<span class="title">{row.title}</span>
				<span class="blurb">{row.blurb}</span>
			</button>
			<div class="meta">
				<label class="agent">
					<span class="meta-label">agent</span>
					<select
						aria-label={`${row.title} agent`}
						value={row.boundAgent}
						onchange={(event) => onRebind(row.id, event.currentTarget.value)}
					>
						<option value="">default ({row.defaultAgent || '—'})</option>
						{#each agentOptions as agent (agent)}
							<option value={agent}>{agent}</option>
						{/each}
						{#if row.boundAgent && !agentOptions.includes(row.boundAgent)}
							<option value={row.boundAgent}>{row.boundAgent} (custom)</option>
						{/if}
					</select>
				</label>
				<span class="blocks">{row.blockCount} block{row.blockCount === 1 ? '' : 's'}</span>
				<button
					type="button"
					class="dot"
					class:on={row.enabled}
					aria-pressed={row.enabled}
					title={row.enabled
						? 'On — click to disable every block in this step'
						: 'Off — click to enable this step'}
					onclick={() => onToggle(row.id)}
				>
					<span class="dot-mark" aria-hidden="true"></span>
					{row.enabled ? 'on' : 'off'}
				</button>
			</div>
		</li>
	{:else}
		<li class="empty-row">No steps — the step catalog is unavailable.</li>
	{/each}
</ol>

<style>
	.steps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.step {
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-inset);
		overflow: hidden;
	}
	.step.selected {
		border-color: var(--accent);
	}
	.step.off .title {
		opacity: 0.55;
	}
	.entry {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.15rem;
		width: 100%;
		padding: 0.5rem 0.6rem 0.35rem;
		border: none;
		background: transparent;
		color: inherit;
		text-align: left;
		cursor: pointer;
	}
	.entry:hover {
		background: var(--bg-hover);
	}
	.title {
		font-weight: 600;
		font-size: 0.9rem;
	}
	.blurb {
		font-size: 0.78rem;
		opacity: 0.75;
		line-height: 1.25;
	}
	.meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0 0.6rem 0.45rem;
		font-size: 0.75rem;
	}
	.agent {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}
	.meta-label {
		opacity: 0.6;
	}
	.agent select {
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
		font-size: 0.75rem;
		padding: 0.1rem 0.2rem;
	}
	.blocks {
		opacity: 0.7;
	}
	.dot {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		margin-left: auto;
		background: transparent;
		border: 1px solid var(--line);
		border-radius: 999px;
		color: inherit;
		font-size: 0.7rem;
		padding: 0.1rem 0.5rem 0.1rem 0.35rem;
		cursor: pointer;
	}
	.dot-mark {
		width: 0.5rem;
		height: 0.5rem;
		border-radius: 50%;
		background: var(--line);
	}
	.dot.on .dot-mark {
		background: var(--accent);
	}
	.empty-row {
		padding: 0.5rem;
		opacity: 0.7;
		font-size: 0.8rem;
	}
</style>
