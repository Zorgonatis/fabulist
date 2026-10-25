<script lang="ts">
	// A single narration beat shown to the player.
	// OpenUI Lang call: BeatCard(text).
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	let { props }: ComponentRenderProps<{ text: string }> = $props();
</script>

<p class="beat-card" data-openui="BeatCard">{props.text}</p>

<style>
	.beat-card {
		margin: 0;
		font-size: 0.85rem;
		line-height: 1.5;
		font-style: italic;
		color: inherit;
		white-space: pre-line;
	}
</style>
