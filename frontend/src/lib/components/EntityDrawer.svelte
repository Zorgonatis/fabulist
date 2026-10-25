<script lang="ts">
	// Shared right-rail drawer shell (D39 / W9.1).
	//
	// Every management drawer re-implements the same scrim + fixed panel + header +
	// close. This primitive owns that shell and the dialog semantics with it: the
	// scrim click-to-close, `role="dialog" aria-modal="true"`, the Escape handler
	// and the z-index band. Callers own their own list/form markup through the
	// `headerActions` / `body` / `footer` snippets — no per-type logic lives here.
	//
	// Escape is owned by the `lib/uiLayers` surface stack (D51 §2.4): the drawer
	// registers while it is mounted (a caller mounts it only while open), so a
	// single stack decides which coexisting surface closes — the topmost — instead
	// of every open drawer reacting to one Escape. Consumers need no changes.
	//
	// `headerActions` is the snippet the D39 import/export row rides in.
	import { onMount } from 'svelte';
	import type { Snippet } from 'svelte';
	import { register } from '#lib/uiLayers';

	/** Distinguishes concurrent drawers (ids must be unique on the uiLayers stack). */
	let nextLayerId = 0;

	type Props = {
		/** Header title text (also the drawer's default accessible name). */
		title: string;
		/** `aria-label` override; defaults to `title`. */
		ariaLabel?: string;
		onClose: () => void;
		/** Panel width (any CSS length); defaults to the 26rem rail width. */
		width?: string;
		/** Controls rendered beside the title. */
		headerActions?: Snippet;
		body: Snippet;
		footer?: Snippet;
	};

	let { title, ariaLabel, onClose, width = '26rem', headerActions, body, footer }: Props =
		$props();

	const label = $derived(ariaLabel ?? title);

	const layerId = `drawer:${nextLayerId++}`;

	onMount(() => {
		// `onClose` is read at call time so a caller that swaps the handler keeps
		// the current one; unregister runs when the drawer unmounts (i.e. closes).
		return register(layerId, () => onClose());
	});
</script>

<div class="scrim" role="presentation" onclick={onClose}></div>

<div class="entity-drawer" role="dialog" aria-modal="true" aria-label={label} style="width: {width}">
	<header>
		<h2>{title}</h2>
		<div class="header-actions">
			{#if headerActions}{@render headerActions()}{/if}
			<button type="button" class="close" onclick={onClose} aria-label="Close {label}">×</button>
		</div>
	</header>

	<div class="body">
		{@render body()}
	</div>

	{#if footer}
		<footer>
			{@render footer()}
		</footer>
	{/if}
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 22;
	}
	.entity-drawer {
		position: fixed;
		top: 0;
		right: 0;
		height: 100vh;
		max-width: 96vw;
		z-index: 23;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border-left: 1px solid var(--line);
		box-shadow: -8px 0 24px var(--shadow);
		color: var(--text);
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.85rem 1rem;
		border-bottom: 1px solid var(--line);
	}
	h2 {
		margin: 0;
		font-size: var(--font-lg);
	}
	.header-actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	.close {
		font: inherit;
		font-size: 1.1rem;
		line-height: 1;
		width: 1.6rem;
		height: 1.6rem;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.close:hover {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0.75rem 1rem 1.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
	}
	footer {
		flex: none;
		padding: 0.6rem 1rem;
		border-top: 1px solid var(--line);
		display: flex;
		justify-content: flex-end;
		gap: 0.4rem;
	}
</style>
