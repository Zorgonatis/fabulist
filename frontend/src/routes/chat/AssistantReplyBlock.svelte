<script lang="ts">
	// One assistant reply, rendered as a distinct aside inside the assistant
	// panel's history section. Presentational only — the panel owns the API call
	// and the store writes.
	import type { AssistantReplyEntry } from '#lib/stores';

	let { reply }: { reply: AssistantReplyEntry } = $props();
</script>

<article class="assistant-reply" class:error={!!reply.error} aria-label="Assistant reply">
	<header>
		<span class="tag">ASSISTANT</span>
		<span class="name">{reply.assistantName || reply.assistant}</span>
		<span class="agent">{reply.agent}</span>
	</header>
	{#if reply.user_msg}
		<p class="prompt">you → {reply.user_msg}</p>
	{/if}
	{#if reply.pending}
		<p class="text pending">consulting…</p>
	{:else if reply.error}
		<p class="text error-text">{reply.error}</p>
	{:else}
		<p class="text">{reply.text}</p>
	{/if}
</article>

<style>
	.assistant-reply {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		border: 1px solid var(--line);
		border-left: 3px solid var(--assistant-accent);
		border-radius: 10px;
		background: var(--bg-raised);
		padding: 0.5rem 0.7rem;
		max-width: 46rem;
	}
	.assistant-reply.error {
		border-color: var(--line);
		border-left-color: var(--danger);
		background: var(--danger-soft);
	}
	header {
		display: flex;
		align-items: baseline;
		gap: 0.45rem;
	}
	.tag {
		font-size: 0.58rem;
		font-weight: 700;
		letter-spacing: 0.1em;
		color: var(--bg);
		background: var(--assistant-accent);
		border-radius: 4px;
		padding: 0.08rem 0.35rem;
	}
	.name {
		font-size: 0.78rem;
		font-weight: 600;
		color: var(--text);
	}
	.agent {
		margin-left: auto;
		font-size: 0.62rem;
		color: var(--text-muted);
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	}
	.prompt {
		margin: 0;
		font-size: 0.68rem;
		color: var(--text-muted);
		font-style: italic;
	}
	.text {
		margin: 0;
		white-space: pre-wrap;
		line-height: 1.5;
		font-size: 0.86rem;
		color: var(--text);
	}
	.text.pending {
		color: var(--text-muted);
	}
	.text.error-text {
		color: var(--danger-text);
	}
</style>
