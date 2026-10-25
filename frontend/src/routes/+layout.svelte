<script lang="ts">
	import '../app.css';
	import { onMount } from 'svelte';
	import { initAppearance } from '#lib/theme/apply';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	// Appearance (issue #12): load the install's preset + pasted CSS once, before
	// the first chat mounts. It runs here rather than in the chat page because the
	// appearance is per-INSTALL user state, not session state — every route (the
	// studio, the component gallery) reads the same tokens. A failed read leaves
	// the shipped defaults (`:root` = dark) in place, which is what the app would
	// paint anyway.
	onMount(() => {
		void initAppearance();
	});
</script>

{@render children()}
