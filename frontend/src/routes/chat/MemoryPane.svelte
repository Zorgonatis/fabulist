<script lang="ts">
	// Memories pane (Wave 4b H2) — the owner's LIVE RAG view.
	//
	// "Memories (Used this turn)": exactly what the recall stage asked and what
	// came back for the CURRENT turn, per turn. Collapsed by default; expanding
	// reveals the queries + the recall block, and the recall knobs (D25 C2) sit
	// behind their own expansion.
	//
	//   * RAG view  — last turn's `rag` from GET .../inspect/turns (I2):
	//                 {query, queries, recall_text, k, rerank_enabled, query_mode,
	//                  results: [{seq, text, distance}]}
	//   * knobs     — GET/PATCH .../memory-config; `enabled` rides the canonical
	//                 chat_memory event via PATCH .../memory (the memory-config
	//                 route rejects `enabled`, so the pane splits the write).
	//
	// W5C-1: the per-result rows recall actually surfaced (seq/text/distance)
	// now ride the payload as `rag.results`, so the pane renders them instead
	// of the old pending-HB note.
	import { setChatMemory } from '#lib/api';
	import {
		API_BASE,
		MEMORY_CONFIG_DEFAULTS,
		getMemoryConfig,
		patchMemoryConfig
	} from '#lib/api';
	import type { MemoryConfig } from '#lib/api';

	let { sessionId = '' }: { sessionId?: string | null } = $props();

	type RagResult = { seq: number | null; text: string; distance: number | null };
	type RagView = {
		queries: string[];
		recall_text: string;
		k: number;
		rerank_enabled: boolean | null;
		query_mode: string;
		memory_enabled: boolean;
		results: RagResult[];
	};

	let rag = $state<RagView | null>(null);
	let ragLoading = $state(false);
	let config = $state<MemoryConfig>({ ...MEMORY_CONFIG_DEFAULTS });
	let draft = $state<MemoryConfig>({ ...MEMORY_CONFIG_DEFAULTS });
	let expanded = $state(false);
	let knobsOpen = $state(false);
	let saving = $state(false);
	let saved = $state(false);
	let saveError = $state('');

	// Reload on session switch; clear first so a null id empties the pane.
	$effect(() => {
		const id = sessionId;
		rag = null;
		config = { ...MEMORY_CONFIG_DEFAULTS };
		draft = { ...MEMORY_CONFIG_DEFAULTS };
		expanded = false;
		knobsOpen = false;
		saved = false;
		saveError = '';
		if (!id) return;
		void load(id);
	});

	async function load(id: string): Promise<void> {
		ragLoading = true;
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(id)}/inspect/turns`);
			if (res.ok) {
				const body = (await res.json()) as { turns?: Array<{ rag?: Partial<RagView> }> };
				const turns = body.turns ?? [];
				const last = turns.length ? turns[turns.length - 1]?.rag : undefined;
				if (last) {
					rag = {
						queries: Array.isArray(last.queries)
							? last.queries.filter((q): q is string => typeof q === 'string')
							: [],
						recall_text: typeof last.recall_text === 'string' ? last.recall_text : '',
						k: typeof last.k === 'number' ? last.k : MEMORY_CONFIG_DEFAULTS.k,
						rerank_enabled:
							typeof last.rerank_enabled === 'boolean' ? last.rerank_enabled : null,
						query_mode: typeof last.query_mode === 'string' ? last.query_mode : 'message',
						memory_enabled: last.memory_enabled !== false,
						results: (Array.isArray(last.results) ? (last.results as unknown[]) : [])
							.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
							.map((r) => ({
								seq: typeof r.seq === 'number' ? r.seq : null,
								text: typeof r.text === 'string' ? r.text : '',
								distance: typeof r.distance === 'number' ? r.distance : null
							}))
					};
				}
			}
		} catch {
			/* a missing inspector route just leaves the RAG view empty */
		} finally {
			ragLoading = false;
		}
		const cfg = await getMemoryConfig(id);
		config = cfg;
		draft = { ...cfg };
	}

	async function save(): Promise<void> {
		const id = sessionId;
		if (!id || saving) return;
		saving = true;
		saved = false;
		saveError = '';
		try {
			// `enabled` lives on the canonical chat_memory event; the rest is the
			// recall knob snapshot (PATCH rejects `enabled` — extra=forbid).
			if (draft.enabled !== config.enabled) await setChatMemory(id, draft.enabled);
			await patchMemoryConfig(id, {
				k: draft.k,
				block_chars: draft.block_chars,
				rerank_enabled: draft.rerank_enabled,
				query_mode: draft.query_mode
			});
			config = await getMemoryConfig(id);
			draft = { ...config };
			saved = true;
		} catch (err) {
			saveError = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	function reset(): void {
		draft = { ...config };
		saved = false;
		saveError = '';
	}

	const summaryLine = $derived.by(() => {
		if (!rag) return ragLoading ? 'loading…' : 'no turn yet';
		const q = rag.queries.length;
		const recalled = rag.recall_text.trim()
			? `${rag.recall_text.trim().length} chars recalled`
			: 'nothing recalled';
		const rows = rag.results.length ? ` · ${rag.results.length} rows` : '';
		return `${q === 1 ? '1 query' : `${q} queries`} · ${recalled}${rows} · k=${rag.k}`;
	});
</script>

<div class="memory-pane">
	<button
		type="button"
		class="mem-toggle"
		aria-expanded={expanded}
		onclick={() => (expanded = !expanded)}
	>
		<span class="mem-chevron" aria-hidden="true">{expanded ? '▾' : '▸'}</span>
		<span class="mem-title">Memories (Used this turn)</span>
		<span class="mem-summary">{summaryLine}</span>
	</button>

	{#if expanded}
		<section class="mem-section">
			<h4 class="hud-heading">Queries this turn</h4>
			{#if rag && rag.queries.length}
				<ol class="query-list">
					{#each rag.queries as q, i (i)}
						<li class="query-row"><span class="query-idx">{i + 1}</span><span class="query-text">{q}</span></li>
					{/each}
				</ol>
			{:else}
				<p class="hud-note">No query captured for the last turn.</p>
			{/if}
		</section>

		<section class="mem-section">
			<h4 class="hud-heading">Recalled block</h4>
			{#if rag && rag.recall_text.trim()}
				<pre class="recall-block">{rag.recall_text}</pre>
			{:else}
				<p class="hud-note">Nothing was recalled for the last turn.</p>
			{/if}
		</section>

		<section class="mem-section">
			<h4 class="hud-heading">Result rows</h4>
			{#if rag && rag.results.length}
				<ul class="result-list">
					{#each rag.results as row, i (i)}
						<li class="result-row">
							<span class="result-seq">seq {row.seq ?? '—'}</span>
							<span class="result-distance">
								{row.distance === null ? '—' : row.distance.toFixed(3)}
							</span>
							<span class="result-text">{row.text}</span>
						</li>
					{/each}
				</ul>
			{:else}
				<p class="hud-note">
					Nothing was surfaced for the last turn (or it predates result-row capture).
				</p>
			{/if}
		</section>

		<section class="mem-section">
			<button
				type="button"
				class="knobs-toggle"
				aria-expanded={knobsOpen}
				onclick={() => (knobsOpen = !knobsOpen)}
			>
				<span class="mem-chevron" aria-hidden="true">{knobsOpen ? '▾' : '▸'}</span>
				Recall settings
			</button>

			{#if knobsOpen}
				<div class="knobs">
					<label class="knob">
						<input type="checkbox" bind:checked={draft.enabled} />
						<span>Memory on</span>
					</label>
					<label class="knob">
						<span class="knob-label">Final results (k)</span>
						<input type="number" min="1" max="50" bind:value={draft.k} />
					</label>
					<label class="knob">
						<span class="knob-label">Block budget (chars)</span>
						<input type="number" min="100" max="20000" bind:value={draft.block_chars} />
					</label>
					<label class="knob">
						<input type="checkbox" bind:checked={draft.rerank_enabled} />
						<span>Rerank results</span>
					</label>
					<label class="knob">
						<span class="knob-label">Query mode</span>
						<select bind:value={draft.query_mode}>
							<option value="message">raw message</option>
							<option value="message+scene">message + scene</option>
						</select>
					</label>
					<div class="knob-actions">
						<button type="button" class="knob-save" disabled={saving} onclick={() => void save()}>
							{saving ? 'saving…' : 'Save'}
						</button>
						<button type="button" class="knob-reset" disabled={saving} onclick={reset}>Reset</button>
						{#if saved}<span class="knob-ok">saved</span>{/if}
						{#if saveError}<span class="knob-err">{saveError}</span>{/if}
					</div>
				</div>
			{/if}
		</section>
	{/if}
</div>

<style>
	.memory-pane {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.mem-toggle,
	.knobs-toggle {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		width: 100%;
		text-align: left;
		font: inherit;
		font-size: var(--font-ui);
		font-weight: 600;
		color: var(--text);
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.45rem 0.6rem;
		cursor: pointer;
	}
	.mem-toggle:hover,
	.knobs-toggle:hover {
		background: var(--bg-hover);
		border-color: var(--accent-line);
	}
	.knobs-toggle {
		font-weight: 600;
		background: transparent;
	}
	.mem-chevron {
		color: var(--text-dim);
		font-size: var(--font-small);
	}
	.mem-title {
		flex: none;
	}
	.mem-summary {
		font-size: var(--font-micro);
		font-weight: 400;
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}
	.mem-section {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding-left: 0.2rem;
	}
	.query-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.query-row {
		display: flex;
		gap: 0.45rem;
		font-size: var(--font-small);
	}
	.query-idx {
		color: var(--accent-strong);
		font-variant-numeric: tabular-nums;
	}
	.query-text {
		color: var(--text);
		word-break: break-word;
	}
	.result-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.result-row {
		display: grid;
		grid-template-columns: 4.2rem 3.5rem 1fr;
		gap: 0.4rem;
		align-items: baseline;
		font-size: var(--font-micro);
		line-height: 1.4;
	}
	.result-seq {
		color: var(--accent-strong);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.result-distance {
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.result-text {
		color: var(--text-muted);
		word-break: break-word;
	}
	.recall-block {
		margin: 0;
		padding: 0.45rem 0.55rem;
		background: var(--bg-inset);
		border-radius: 6px;
		font-size: var(--font-micro);
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
		max-height: 14rem;
		overflow: auto;
	}
	.knobs {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding: 0.5rem 0.55rem;
		background: var(--bg-inset);
		border-radius: 8px;
	}
	.knob {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: var(--font-small);
	}
	.knob-label {
		flex: 1 1 auto;
		color: var(--text-muted);
	}
	.knob input[type='number'] {
		width: 5.5rem;
	}
	.knob input[type='number'],
	.knob select {
		font: inherit;
		font-size: var(--font-small);
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.15rem 0.35rem;
	}
	.knob-actions {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.15rem;
	}
	.knob-save {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.25rem 0.7rem;
		border: none;
		border-radius: 6px;
		background: var(--accent);
		color: var(--on-accent);
		cursor: pointer;
	}
	.knob-save:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.knob-reset {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.25rem 0.6rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.knob-ok {
		font-size: var(--font-micro);
		color: var(--ok);
	}
	.knob-err {
		font-size: var(--font-micro);
		color: var(--danger-text);
	}
</style>
