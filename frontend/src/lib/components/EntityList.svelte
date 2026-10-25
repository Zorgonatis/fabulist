<script lang="ts" generics="T">
	// Selectable entity list (D39 / W9.1).
	//
	// A labelled list plus an optional "new" affordance and a per-row action
	// slot. It renders NO per-type form and knows nothing about any object type:
	// callers pass the accessors (`getId` / `getLabel` / `getMeta`) and receive
	// the chosen item through `onSelect` / the `rowActions` snippet.
	import type { Snippet } from 'svelte';

	type Props = {
		items: readonly T[];
		/** Id of the selected row (highlight only; selection is owned by the caller). */
		selectedId?: string | null;
		getId: (item: T) => string;
		getLabel: (item: T) => string;
		/** Optional secondary line under the label. */
		getMeta?: (item: T) => string;
		onSelect: (item: T) => void;
		/** Optional create affordance; omitted hides the "New" button. */
		onNew?: () => void;
		/** Empty-state copy. */
		empty?: string;
		/** Per-row controls, rendered after the selectable main button. */
		rowActions?: Snippet<[T]>;
	};

	let {
		items,
		selectedId = null,
		getId,
		getLabel,
		getMeta,
		onSelect,
		onNew,
		empty = 'Nothing here yet.',
		rowActions
	}: Props = $props();
</script>

<div class="list-head">
	<span class="count">{items.length}</span>
	{#if onNew}
		<button type="button" class="new" onclick={onNew}>+ New</button>
	{/if}
</div>

{#if items.length === 0}
	<p class="hint">{empty}</p>
{:else}
	<ul class="rows">
		{#each items as item (`entity-${getId(item)}`)}
			<li class="row" class:selected={getId(item) === selectedId}>
				<button
					type="button"
					class="row-main"
					aria-current={getId(item) === selectedId ? 'true' : undefined}
					onclick={() => onSelect(item)}
				>
					<span class="row-label">{getLabel(item)}</span>
					{#if getMeta}
						<span class="row-meta">{getMeta(item)}</span>
					{/if}
				</button>
				{#if rowActions}
					<div class="row-actions">{@render rowActions(item)}</div>
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.list-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.count {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.hint {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.row {
		display: flex;
		align-items: stretch;
		gap: 0.3rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		padding: 0.35rem 0.45rem;
	}
	.row.selected {
		border-color: var(--accent-line);
		background: var(--accent-soft);
	}
	.row-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.15rem;
		border: none;
		background: none;
		color: var(--text);
		text-align: left;
		padding: 0.15rem 0.2rem;
		border-radius: 6px;
		cursor: pointer;
	}
	.row-main:hover {
		background: var(--bg-hover);
	}
	.row-label {
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.row-meta {
		font-size: var(--font-micro);
		color: var(--text-dim);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.row-actions {
		flex: none;
		display: flex;
		align-items: center;
		gap: 0.25rem;
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
	}
	.new {
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
		padding: 0.35rem 0.7rem;
	}
	.new:hover {
		background: var(--accent);
		color: var(--on-accent);
	}
</style>
