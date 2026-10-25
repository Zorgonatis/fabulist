<script lang="ts">
	// `/` is a convenience route: send the user to the chat screen.
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';

	onMount(() => {
		void goto('/chat', { replaceState: true });
	});
</script>

<svelte:head>
	<title>Fabulist</title>
</svelte:head>

<p class="redirect">Redirecting to <a href="/chat">/chat</a>…</p>

<style>
	.redirect {
		font-family: system-ui, sans-serif;
		padding: 2rem;
		color: var(--text-muted);
	}
</style>
