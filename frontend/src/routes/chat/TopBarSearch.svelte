<script lang="ts">
	// Top-bar chat search (owner ruling 2026-10-13, item 8): the chat-SCOPED
	// semantic search relocated out of the rail. It queries the EXISTING
	// session-scoped endpoint (`searchSession` -> `POST
	// /api/sessions/{id}/search`) for the ACTIVE session, and each hit jumps to
	// its transcript entry via the `#history-{event_id}` anchor the recap jump
	// already uses.
	//
	// The control mirrors the header's shell-btn pattern (icon-only, its
	// accessible name on `aria-label`); the results popover opens below it.
	import { searchSession } from '#lib/api/revisions';
	import type { SearchHit } from '#lib/api/revisions';

	let {
		sessionId
	}: {
		/** The active session id; `null` disables the control. */
		sessionId: string | null;
	} = $props();

	let open = $state(false);
	let query = $state('');
	let hits = $state<SearchHit[]>([]);
	let busy = $state(false);
	let error = $state('');
	let searched = $state(false);

	function reset(): void {
		query = '';
		hits = [];
		error = '';
		busy = false;
		searched = false;
	}

	function toggle(): void {
		if (!sessionId) return;
		open = !open;
		if (!open) reset();
	}

	async function run(): Promise<void> {
		const sid = sessionId;
		const q = query.trim();
		if (!sid || !q) return;
		busy = true;
		const result = await searchSession(sid, q);
		// Drop a response for a superseded query or a session that changed.
		if (sessionId !== sid || query.trim() !== q) return;
		busy = false;
		searched = true;
		if ('error' in result) {
			error = result.error;
			hits = [];
		} else {
			error = '';
			hits = result.results;
		}
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter') void run();
		else if (event.key === 'Escape') {
			open = false;
			reset();
		}
	}

	function jump(hit: SearchHit): void {
		if (hit.event_id == null) return;
		document
			.getElementById(`history-${hit.event_id}`)
			?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}
</script>

<div class="top-search">
	<button
		type="button"
		class="shell-btn"
		class:active={open}
		disabled={!sessionId}
		title={sessionId ? 'Search this chat' : 'Open a chat to search it'}
		aria-label="Search this chat"
		onclick={toggle}
	>
		<span class="shell-glyph" aria-hidden="true">⌕</span>
	</button>

	{#if open}
		<div class="ts-popover">
			<input
				class="ts-input"
				type="search"
				placeholder="Search this chat…"
				aria-label="Search this chat"
				bind:value={query}
				onkeydown={onKeydown}
			/>
			{#if busy}<span class="ts-note">searching…</span>{/if}
			{#if error}<p class="ts-note">{error}</p>{/if}
			{#if !busy && searched && !error}
				{#if hits.length === 0}
					<p class="ts-note">No matches</p>
				{:else}
					<div class="ts-hits">
						{#each hits as hit, hitIndex (`ts-hit:${hitIndex}`)}
							<button type="button" class="ts-hit" onclick={() => jump(hit)}>
								<span class="ts-kind">{hit.kind}</span>
								<span class="ts-text">{hit.text.slice(0, 120)}</span>
							</button>
						{/each}
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</div>

<style>
	.top-search {
		position: relative;
		display: flex;
		align-items: center;
	}
	.ts-popover {
		position: absolute;
		top: calc(100% + 0.4rem);
		right: 0;
		z-index: 30;
		width: min(24rem, 70vw);
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding: 0.5rem;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 8px;
		box-shadow: 0 6px 24px rgb(0 0 0 / 35%);
	}
	.ts-input {
		background: var(--bg-inset, var(--bg));
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.4rem 0.6rem;
		font-size: var(--font-ui);
	}
	.ts-note {
		color: var(--text-muted);
		font-size: var(--font-small);
		margin: 0;
	}
	.ts-hits {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		max-height: 40vh;
		overflow: auto;
	}
	.ts-hit {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		width: 100%;
		text-align: left;
		background: transparent;
		color: var(--text);
		border: none;
		border-radius: 6px;
		padding: 0.3rem 0.4rem;
		cursor: pointer;
		font-size: var(--font-small);
	}
	.ts-hit:hover {
		background: var(--bg-hover);
	}
	.ts-kind {
		flex: none;
		color: var(--accent);
		text-transform: uppercase;
		font-size: var(--font-micro);
		letter-spacing: 0.04em;
	}
	.ts-text {
		color: var(--text-muted);
		min-width: 0;
	}
</style>
