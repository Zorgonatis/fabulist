<script lang="ts">
	// Recap pane (Wave 4b H1): the player-facing view of compaction summaries.
	//
	// Compaction folds old turns into `summary` events (graph/compaction.py);
	// the prompt side already reads them, but the player had no reader (docs/
	// D20-HYDRATION "Wave 4b"). This pane lists them from
	// GET /api/sessions/{sid}/recap in seq order and, on click, jumps the
	// transcript to the first turn the summary covers (same anchor pattern the
	// SessionDrawer search hits use: #history-{event_id}).
	//
	// W9.26: the read is CHAIN-AWARE — it lists every era's summary (absorbed
	// ones too, with `absorbs` naming what they folded in), so the pane shows
	// the whole compaction history, not just the current era's. Each summary v2
	// row also carries its `trigger` and measured `stats` (calls, prompt tokens,
	// serving model); they render as a quiet meta line — the same raw data the
	// transcript's "Compaction done" expander shows.
	//
	// The pane is presentational over the recap read; the parent owns the seq ->
	// transcript-anchor translation (`onJump`) because only the hydrated view
	// knows the event ids behind the seqs.
	import { getRecap } from '#lib/api';
	import type { RecapEntry } from '#lib/api';

	let {
		sessionId = '',
		onJump
	}: { sessionId?: string | null; onJump?: (seq: number) => void } = $props();

	let recap = $state<RecapEntry[]>([]);
	let loading = $state(false);
	let error = $state('');

	// Clear + reload on session switch (mirrors InspectorPane's I4 fix: a null id
	// must empty the pane, not leave the previous chat's rows on screen).
	$effect(() => {
		const id = sessionId;
		recap = [];
		error = '';
		if (!id) return;
		void load(id);
	});

	async function load(id: string): Promise<void> {
		loading = true;
		const result = await getRecap(id);
		loading = false;
		if ('error' in result) {
			error = result.error;
			return;
		}
		recap = result.recap;
	}

	function jump(entry: RecapEntry): void {
		const low = entry.covers_seqs[0];
		if (low === null || low === undefined) return;
		onJump?.(low);
	}

	/** `[lo, hi]` SEQ label for a covered span, or a stated absence.
	 *
	 * W9.26 §2.5: spans are SEQ numbers (never turn indices) — the same
	 * vocabulary the transcript's break row uses, so the two agree. */
	function spanLabel(entry: RecapEntry): string {
		const [low, high] = entry.covers_seqs;
		if (low === null && high === null) return 'range unknown';
		if (low === high || high === null) return `turn ${low}`;
		return `turns ${low}–${high}`;
	}

	/** The measured stats line (absent fields drop out rather than print empty). */
	function statsLabel(entry: RecapEntry): string {
		const parts: string[] = [];
		if (entry.trigger) parts.push(entry.trigger);
		if (entry.stats.model) parts.push(entry.stats.model);
		if (entry.stats.calls !== null) {
			parts.push(`${entry.stats.calls} ${entry.stats.calls === 1 ? 'call' : 'calls'}`);
		}
		if (entry.stats.prompt_tokens !== null) {
			parts.push(`${entry.stats.prompt_tokens.toLocaleString()} prompt tokens`);
		}
		return parts.join(' · ');
	}
</script>

<div class="recap-pane">
	<p class="pane-lead">
		The story so far, one recap per stretch of turns, newest last. Click one to jump to the turns it covers.
	</p>

	{#if error}
		<p class="hud-note hud-note-error">{error}</p>
	{:else if loading}
		<p class="hud-note">loading…</p>
	{:else if recap.length === 0}
		<p class="hud-note">No recaps yet — summaries appear as the story grows.</p>
	{:else}
		<ol class="recap-list">
			{#each recap as entry (entry.seq)}
				<li>
					<button
						type="button"
						class="recap-row"
						title="Jump to {spanLabel(entry)}"
						onclick={() => jump(entry)}
					>
						<span class="recap-span">{spanLabel(entry)}</span>
						<span class="recap-text">{entry.text}</span>
						<span class="recap-meta">
							{#if statsLabel(entry)}<span class="recap-stats">{statsLabel(entry)}</span>{/if}
							{#if entry.absorbs}
								<span class="recap-absorbs" title="This summary folded in an earlier era's summary">
									includes {entry.absorbs[0]} · turn {entry.absorbs[1]}
								</span>
							{/if}
						</span>
						<span class="recap-jump" aria-hidden="true">↦</span>
					</button>
				</li>
			{/each}
		</ol>
	{/if}
</div>

<style>
	.recap-pane {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.pane-lead {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.recap-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.recap-row {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: baseline;
		gap: 0.5rem;
		width: 100%;
		text-align: left;
		font: inherit;
		font-size: var(--font-small);
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		color: var(--text);
		cursor: pointer;
	}
	.recap-row:hover {
		background: var(--bg-hover);
		border-color: var(--accent-line);
	}
	.recap-row:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.recap-span {
		font-size: var(--font-micro);
		font-variant-numeric: tabular-nums;
		letter-spacing: 0.02em;
		color: var(--accent-strong);
		white-space: nowrap;
		padding-top: 0.1rem;
	}
	.recap-text {
		color: var(--text-muted);
		line-height: 1.45;
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}
	.recap-meta {
		grid-column: 2 / 3;
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem 0.6rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.recap-stats,
	.recap-absorbs {
		font-variant-numeric: tabular-nums;
	}
	.recap-absorbs {
		color: var(--accent-strong);
	}
	.recap-jump {
		grid-row: 1 / 3;
		grid-column: 3 / 4;
		align-self: center;
		color: var(--text-dim);
	}
</style>
