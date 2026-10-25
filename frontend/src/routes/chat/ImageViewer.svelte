<script lang="ts">
	// Image viewer (lightbox): a full-viewport overlay showing one transcript
	// image at its natural size (capped at 92vw/92vh) instead of the 200–260px
	// thumbnail the transcript itself renders.
	//
	// Stateless: the caller owns `src` and clears it in `onClose`. Escape is owned
	// by the uiLayers surface stack (D51 §2.4) — while this overlay is mounted it
	// is the top surface, so Escape dismisses it (and nothing underneath it), via
	// a registered surface rather than a bespoke window keydown listener.
	import { onMount } from 'svelte';
	import { register } from '#lib/uiLayers';

	let {
		src,
		alt = '',
		onClose
	}: {
		src: string;
		alt?: string;
		onClose: () => void;
	} = $props();

	let closeButton = $state<HTMLButtonElement | null>(null);

	onMount(() => {
		closeButton?.focus();
		return register('image-viewer', () => onClose());
	});
</script>

<div class="viewer" role="dialog" aria-modal="true" aria-label="Image viewer" tabindex="-1">
	<!-- The backdrop is its own element BEHIND the image (z-index 1) and the close
	     button: a click on either of those can never reach it. -->
	<div class="backdrop" role="presentation" onclick={onClose}></div>
	<button
		type="button"
		class="close"
		bind:this={closeButton}
		onclick={onClose}
		aria-label="Close image viewer">✕</button
	>
	<img class="viewer-image" {src} {alt} />
</div>

<style>
	.viewer {
		position: fixed;
		inset: 0;
		z-index: var(--z-drawer-top, 23);
		display: flex;
		align-items: center;
		justify-content: center;
	}
	.backdrop {
		position: absolute;
		inset: 0;
		background: var(--scrim, rgba(0, 0, 0, 0.55));
		cursor: zoom-out;
	}
	.viewer-image {
		position: relative;
		z-index: 1;
		max-width: 92vw;
		max-height: 92vh;
		width: auto;
		height: auto;
		object-fit: contain;
		border-radius: 8px;
		box-shadow: 0 12px 40px var(--shadow, rgba(0, 0, 0, 0.45));
	}
	.close {
		position: fixed;
		z-index: 2;
		top: 0.75rem;
		right: 0.9rem;
		width: 2rem;
		height: 2rem;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 0;
		border: 1px solid var(--line);
		border-radius: 50%;
		background: var(--bg-raised);
		color: var(--text);
		font-size: 1rem;
		line-height: 1;
		cursor: pointer;
	}
</style>
