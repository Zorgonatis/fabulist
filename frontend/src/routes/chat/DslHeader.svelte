<script lang="ts">
	// Pinned DSL header (W3B): the top strip of the reply area. It replaces the
	// absolutely-positioned StatusBar pill, which floated in a zero-height slot
	// over the composer and obscured the AssistantPanel's bar.
	//
	// Contents
	//   - Scene title: parsed from the newest narrator reply's DSL via lane A's
	//     `sceneTitleOf` seam (`#lib/openui/library`). The root `ScenePane(title,
	//     …)` line is emitted LAST by prompt contract, so a title belongs at the
	//     top of the reply rather than as a card at the bottom of it.
	//   - Turn progress: the running step's label + a derived percentage.
	//   - Click summary: the ordered step trace (`turnSteps`) with validator notes.
	//   - Flow drop-down (W9.2): a second control hosting `ChatFlowPanel` — the
	//     live turn graph + per-node inspector.
	//
	// This strip is a NORMAL layout row (no absolute positioning, no zero-height
	// tricks), so it can never cover the assistants bar above the composer. Both
	// drop-downs are absolutely positioned panels below it.
	import * as library from '#lib/openui/library';
	import type { ProcessingStage, TranscriptEntry, TurnStep } from '#lib/stores';
	import ChatFlowPanel from './ChatFlowPanel.svelte';

	let {
		entries = [],
		running = false,
		stage = null,
		steps = [],
		onopenstep
	}: {
		/** The transcript; the newest narrator entry supplies the scene DSL. */
		entries?: TranscriptEntry[];
		/** True while a turn is in flight (the composer's `streaming`). */
		running?: boolean;
		/** The last `stage` frame of that turn, or `null` before the first one. */
		stage?: ProcessingStage | null;
		/** The turn's ordered step trace (persists after `done`). */
		steps?: TurnStep[];
		/** Forwarded to `ChatFlowPanel`: a flow node's prompt-door click. */
		onopenstep?: (stepId: string) => void;
	} = $props();

	/**
	 * The scene-title seam. Lane A owns `sceneTitleOf` in `#lib/openui/library`;
	 * until that export lands this reads it defensively and degrades to `null`,
	 * so check/build stay green either way. The export name is held in a variable
	 * so the bundler does not statically bind a symbol that does not exist yet
	 * (which would emit an IMPORT_IS_UNDEFINED build warning). A malformed DSL
	 * payload is swallowed: it must never break the header.
	 */
	const SCENE_TITLE_EXPORT = 'sceneTitleOf';
	function sceneTitleOf(dsl: string): string | null {
		if (!dsl) return null;
		const fn = (library as unknown as Record<string, unknown>)[SCENE_TITLE_EXPORT];
		if (typeof fn !== 'function') return null;
		try {
			return (fn as (text: string) => string | null)(dsl);
		} catch {
			return null;
		}
	}

	/** Step count assumed before the first frame arrives (nodes.py `_STAGE_TOTAL`). */
	const PRE_FRAME_TOTAL = 6;
	/** Shown when a turn is running but no stage frame has arrived yet. */
	const PREPARING_LABEL = 'Preparing the turn';

	let open = $state(false);
	let flowOpen = $state(false);

	/** The newest narrator reply's DSL text, or `''` when none exists. */
	const lastDsl = $derived.by(() => {
		for (let i = entries.length - 1; i >= 0; i--) {
			if (entries[i].role === 'narrator') return entries[i].dsl ?? '';
		}
		return '';
	});
	const sceneTitle = $derived(sceneTitleOf(lastDsl));

	function clampPercent(value: number): number {
		if (!Number.isFinite(value)) return 0;
		return Math.min(100, Math.max(0, Math.round(value)));
	}

	/** Equal slices per step; a started-but-unfinished step counts half. */
	function stepPercent(frame: ProcessingStage | null): number {
		const total = frame && frame.total !== null && frame.total > 0 ? frame.total : PRE_FRAME_TOTAL;
		const raw = frame && frame.index !== null ? frame.index : 1;
		const index = raw >= 1 ? raw : 1;
		const completed = frame?.state === 'done' ? 1 : 0.5;
		return clampPercent((100 * (index - 1 + completed)) / total);
	}

	const percent = $derived(stepPercent(stage));
	const label = $derived(stage?.label || (running ? PREPARING_LABEL : ''));
	// Issue #22: the strip renders ONLY while a turn runs.
	//
	// It used to earn its 23px on any idle chat that had a scene title or a step
	// trace, which meant a permanent row above the transcript whose title was a
	// THIRD printing of time/place (the HUD in the thread head and the state
	// pane's rows were the other two). The HUD is now the owner of the scene's
	// facts, so the strip's scene title is the running turn's heading and nothing
	// else. An open drop-down pins the strip: an inspection is never yanked out
	// from under the reader mid-read.
	const visible = $derived(running || open || flowOpen);
</script>

{#if visible}
	<div class="dsl-header" data-dsl-header="true">
		<button
			type="button"
			class="dsl-header-btn"
			aria-expanded={open}
			title="Show the steps this turn ran"
			onclick={() => {
				flowOpen = false; // one drop-down at a time
				open = !open;
			}}
		>
			{#if sceneTitle}
				<span class="dsl-scene">{sceneTitle}</span>
			{/if}
			{#if running}
				<span class="dsl-progress">
					<span class="dsl-label">{label || 'Working'}</span>
					<span class="dsl-track" aria-hidden="true">
						<span class="dsl-fill" style="width: {percent}%"></span>
					</span>
					<span class="dsl-percent">{percent}%</span>
				</span>
			{/if}
			<span class="dsl-caret" aria-hidden="true">▾</span>
		</button>

		<!-- W9.2: the second drop-down — the live turn graph + node inspector. -->
		<button
			type="button"
			class="dsl-flow-btn"
			aria-label="Flow — this turn's steps and their inputs"
			aria-expanded={flowOpen}
			title="Show the steps this turn ran and what each one received"
			onclick={() => {
				open = false; // one drop-down at a time
				flowOpen = !flowOpen;
			}}
		>
			<span class="dsl-flow-label">Flow</span>
			<span class="dsl-caret" aria-hidden="true">▾</span>
		</button>

		{#if flowOpen}
			<div class="dsl-flow" data-flow-dropdown="true" aria-label="Flow panel">
				<ChatFlowPanel {onopenstep} />
			</div>
		{/if}

		{#if open}
			<div class="dsl-steps" data-step-trace="true" aria-label="Turn steps">
				{#if steps.length}
					<ol class="dsl-step-list">
						{#each steps as step, i (i)}
							<li class="dsl-step" class:running={step.state === 'start'}>
								<span class="dsl-step-label">{step.label || step.name}</span>
								{#if step.notes.length}
									<ul class="dsl-notes">
										{#each step.notes as note}<li>{note}</li>{/each}
									</ul>
								{/if}
							</li>
						{/each}
					</ol>
				{:else}
					<p class="dsl-empty">No steps recorded for this turn yet.</p>
				{/if}
			</div>
		{/if}
	</div>
{/if}

<style>
	/* A normal flex row at the top of the reply area — never absolute, so it
	   cannot overlap the assistants bar or the composer below the transcript. */
	.dsl-header {
		position: relative;
		flex: 0 0 auto;
		display: flex;
		align-items: center;
		border-bottom: 1px solid var(--line);
		background: var(--bg-raised);
	}
	.dsl-header-btn {
		display: flex;
		align-items: center;
		gap: 0.55rem;
		/* Grows to fill the strip. */
		flex: 1 1 auto;
		min-width: 0;
		padding: 0.22rem 1rem;
		border: 0;
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1.4;
		text-align: left;
		cursor: pointer;
	}
	.dsl-header-btn:hover {
		background: var(--bg-hover);
	}
	/* The second control: a compact caret button, same strip, its own panel. */
	.dsl-flow-btn {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex: 0 0 auto;
		padding: 0.22rem 1rem 0.22rem 0.6rem;
		border: 0;
		border-left: 1px solid var(--line);
		background: transparent;
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1.4;
		cursor: pointer;
	}
	.dsl-flow-btn:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.dsl-flow-btn[aria-expanded='true'] {
		color: var(--accent-strong);
	}
	.dsl-flow-label {
		white-space: nowrap;
	}
	.dsl-scene {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--text);
		font-weight: 600;
	}
	.dsl-progress {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		margin-left: auto;
		white-space: nowrap;
	}
	.dsl-label {
		white-space: nowrap;
	}
	.dsl-track {
		display: block;
		width: 3rem;
		height: 3px;
		border-radius: 999px;
		background: var(--line-strong);
		overflow: hidden;
	}
	.dsl-fill {
		display: block;
		height: 100%;
		border-radius: inherit;
		background: var(--accent);
		transition: width 150ms ease-out;
	}
	.dsl-percent {
		min-width: 2.4em;
		text-align: right;
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}
	.dsl-caret {
		color: var(--text-dim);
	}
	/* The turn board scrolls on its long axis (horizontal by default, vertical
	   in a tall pane — FlowGraph picks by host aspect): the panel owns the
	   scroll, capped short of the composer so the drop-down never traps the
	   input row under it. */
	.dsl-flow {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		z-index: var(--z-dock);
		max-height: min(64vh, 38rem);
		overflow: auto;
		border-bottom: 1px solid var(--line);
		background: var(--bg-raised);
		box-shadow: 0 8px 20px rgb(0 0 0 / 25%);
	}
	/* The summary floats while open; the strip itself stays a layout row. */
	.dsl-steps {
		position: absolute;
		top: 100%;
		left: 0;
		right: 0;
		z-index: var(--z-dock);
		max-height: 16rem;
		overflow: auto;
		padding: 0.4rem 1rem 0.55rem;
		border-bottom: 1px solid var(--line);
		background: var(--bg-raised);
		box-shadow: 0 8px 20px rgb(0 0 0 / 25%);
	}
	.dsl-step-list {
		margin: 0;
		padding-left: 1.1rem;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.dsl-step {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.dsl-step.running {
		color: var(--text);
	}
	.dsl-step-label {
		color: inherit;
	}
	.dsl-notes {
		margin: 0.15rem 0 0;
		padding-left: 1rem;
		list-style: disc;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.dsl-empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
</style>
