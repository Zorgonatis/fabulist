<script lang="ts">
	// Swipe affordance for a reply with siblings (W2.2, v1: last reply only).
	//
	// Presentational: the parent owns the sibling list and the API calls. `‹`
	// and `›` step between already-loaded siblings (the page hydrates the chosen
	// one from the events endpoint); the retry chip forks at the last user
	// message and streams a fresh reply into the new branch.
	//
	// Issue #81: `branch` is nullable, and null is the FAILED-TURN case. A turn
	// that stored no reply has no siblings to step, so it renders the ↻ alone —
	// the fork needs no group data, and a group the page did load belongs to an
	// earlier pivot (#80 re-reads the projection, so the cut is still right).
	//
	// W9.26 §2.6: `branch.continuations` are the compaction-era children. They
	// are SHOWN here (labelled, with a "compacted" tag) and never swiped: an era
	// is a continuation of the history, not an alternate reply, so it must not
	// join the `index / count` group nor be a target of `‹`/`›`. The DTO keeps
	// them off `siblings` for exactly that reason (a swipe across eras would
	// silently move the reader into a different log).
	import type { BranchInfo } from '#lib/api';

	let {
		branch = null,
		busy = false,
		onPrev,
		onNext,
		onRetry
	}: {
		/** The loaded sibling group (only when it describes THIS row's turn).
		 *  `null` = no group data: the ↻ still applies (a failed turn has no
		 *  alternates to step through), the `‹ n/m ›` block does not. */
		branch?: BranchInfo | null;
		busy?: boolean;
		onPrev: () => void;
		onNext: () => void;
		onRetry: () => void;
	} = $props();

	/** Message used when the era has not taken a turn yet (no snippet to show). */
	const ERA_LABEL = 'compacted continuation';

	const continuations = $derived(branch?.continuations ?? []);
	// A missing group is a group of ONE: nothing to step through, and the ↻ is
	// the only control (issue #81 follow-up — a failed turn has no alternates).
	const groupIndex = $derived(branch?.index ?? 1);
	const groupCount = $derived(branch?.count ?? 1);
	const atFirst = $derived(groupIndex <= 1);
	const atLast = $derived(groupIndex >= groupCount);
	const disabled = $derived(busy || groupCount <= 1);
</script>

<div class="branch-controls" aria-label="Reply alternates">
	{#if groupCount > 1}
		<button
			type="button"
			class="arrow"
			title="Previous reply"
			aria-label="Previous reply"
			disabled={disabled || atFirst}
			onclick={onPrev}
		>
			‹
		</button>
		<span class="count" title="Alternate replies for this turn">
			<span class="gylph" aria-hidden="true">⑂</span>
			{groupIndex} / {groupCount}
		</span>
		<button
			type="button"
			class="arrow"
			title="Next reply"
			aria-label="Next reply"
			disabled={disabled || atLast}
			onclick={onNext}
		>
			›
		</button>
	{/if}
	<button
		type="button"
		class="retry"
		title="Generate another reply (forks a new branch)"
		aria-label="Generate another reply"
		disabled={busy}
		onclick={onRetry}
	>
		↻
	</button>
	{#if continuations.length}
		<span class="sep" aria-hidden="true">│</span>
		{#each continuations as continuation (`era:${continuation.sessionId}`)}
			<!-- A CONTINUATION, not an alternative: no arrow reaches it and it has
			     none of its own. Kept inert on purpose — opening it is the drawer's
			     job (W9.32 grouping), not a swipe chip's. -->
			<span
				class="era"
				role="note"
				title={`Compacted — this chat continues in a newer era (${continuation.sessionId})`}
				data-session-id={continuation.sessionId}
			>
				<span class="era-mark" aria-hidden="true">⇥</span>
				<span class="era-text">{continuation.reply || ERA_LABEL}</span>
				<span class="era-tag">compacted</span>
			</span>
		{/each}
	{/if}
</div>

<style>
	.branch-controls {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.25rem;
		margin-top: 0.15rem;
		color: var(--text-muted);
	}
	.arrow,
	.retry {
		font: inherit;
		line-height: 1;
		display: grid;
		place-items: center;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.arrow {
		font-size: 1rem;
		width: 1.4rem;
		height: 1.4rem;
	}
	.retry {
		font-size: 0.85rem;
		width: 1.4rem;
		height: 1.4rem;
	}
	.arrow:hover:not(:disabled),
	.retry:hover:not(:disabled) {
		background: var(--accent-soft);
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.arrow:disabled,
	.retry:disabled {
		opacity: 0.4;
		cursor: default;
	}
	.count {
		font-size: 0.7rem;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
		padding: 0 0.15rem;
	}
	.gylph {
		color: var(--accent-strong);
		margin-right: 0.15rem;
	}
	/* Era chip (W9.26 §2.6): dashed, italic, inert — visibly NOT a swipe control
	   and not a fork chip. Its "compacted" tag is the same pill the drawer's
	   superseded row carries, so one lifecycle state reads the same everywhere. */
	.sep {
		color: var(--line);
		font-size: 0.7rem;
		margin: 0 0.1rem;
	}
	.era {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		min-width: 0;
		max-width: 20rem;
		padding: 0.05rem 0.35rem;
		border: 1px dashed var(--accent-line);
		border-radius: 999px;
		background: transparent;
		color: var(--text-muted);
		font-size: 0.68rem;
		font-style: italic;
		cursor: default;
	}
	.era-mark {
		flex: none;
		color: var(--accent-strong);
		font-style: normal;
	}
	.era-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.era-tag {
		flex: none;
		padding: 0 0.25rem;
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-size: 0.6rem;
		font-style: normal;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
</style>
