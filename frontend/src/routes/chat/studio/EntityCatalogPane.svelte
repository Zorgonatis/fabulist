<script lang="ts">
	// Entity-catalog pane (W9.29 §4) -------------------------------------------
	//
	// The owner's surface for the BTS attribute universe: every meter (a
	// range-checked number) and trait (free text) the tracker's `set_attr` may
	// set, each with the range a meter enforces. It is PROPS-ONLY
	// (`StudioPaneProps`) — no page store, no page fetch — so the studio shell can
	// re-host it in a popout later (D51 §2.3), and it works with NO chat open
	// (non-negotiable 7: definitions are offline-editable).
	//
	// The asset is written WHOLE (replace semantics), so the pane edits a draft
	// and saves it in one PUT; add/edit/delete are the row-set operations from
	// `#lib/api/entityCatalog`. The API is the authority: its 400 reason is shown
	// verbatim, and `catalogSetError` mirrors it for inline feedback.
	import { onMount } from 'svelte';
	import {
		addCatalogRow,
		catalogSetError,
		cloneCatalogRows,
		deleteCatalogRow,
		getEntityCatalog,
		putEntityCatalog,
		type CatalogRow,
		type CatalogRowHost,
		type CatalogRowType
	} from '#lib/api/entityCatalog';

	let {
		onChanged,
		onDirty
	}: {
		focusId?: string | null;
		startNew?: boolean;
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
	} = $props();

	let drafts = $state<CatalogRow[]>([]);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	/** JSON snapshot of the loaded/saved draft set; drift means unsaved edits. */
	let baseline = $state('');

	const dirty = $derived(baseline !== '' && JSON.stringify(drafts) !== baseline);
	const meterRows = $derived(drafts.filter((row) => row.type === 'meter'));
	const traitRows = $derived(drafts.filter((row) => row.type === 'trait'));

	$effect(() => {
		onDirty?.(dirty);
	});

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	function replaceFromCatalog(rows: readonly CatalogRow[]): void {
		drafts = cloneCatalogRows(rows);
		baseline = JSON.stringify(drafts);
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try {
			replaceFromCatalog(await getEntityCatalog());
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	/** Throw the draft away and go back to the last loaded/saved catalog. */
	function discard(): void {
		drafts = JSON.parse(baseline) as CatalogRow[];
		error = '';
	}

	async function save(): Promise<void> {
		if (busy) return;
		const problem = catalogSetError(drafts);
		if (problem) {
			error = problem;
			return;
		}
		busy = true;
		error = '';
		try {
			replaceFromCatalog(await putEntityCatalog(drafts));
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	// --- row set edits ---------------------------------------------------------

	/** First unused `attribute_N` key for a new row. */
	function nextKey(): string {
		const used = new Set(drafts.map((row) => row.key));
		let index = 1;
		while (used.has(`attribute_${index}`)) index += 1;
		return `attribute_${index}`;
	}

	function addRow(): void {
		drafts = addCatalogRow(drafts, {
			key: nextKey(),
			label: '',
			type: 'meter',
			host: 'self',
			range: [0, 100],
			description: ''
		});
	}

	/**
	 * Remove one row. Keyed on the row's KEY (the set's identity): a draft that
	 * carries a duplicated key — invalid, and flagged by validation — removes both,
	 * which is visible and recoverable via Discard.
	 */
	function removeRow(row: CatalogRow): void {
		drafts = deleteCatalogRow(drafts, row.key);
	}

	/** Sanitize a key as it is typed (`[a-z0-9_]`), then re-seat the caret. */
	function onKeyInput(row: CatalogRow, input: HTMLInputElement): void {
		row.key = input.value.toLowerCase().replace(/[^a-z0-9_]+/g, '_');
		input.value = row.key;
	}

	/**
	 * Switch type. The row's SHAPE changes with it: a meter gains the default
	 * bounds, a trait loses them (a trait carrying a range is refused at the door).
	 * That is a deliberate, visible reset, not a silent merge.
	 */
	function setType(row: CatalogRow, type: CatalogRowType): void {
		if (row.type === type) return;
		row.type = type;
		if (type === 'meter') row.range = row.range ?? [0, 100];
		else delete row.range;
	}

	/** One bound edit; an emptied field becomes NaN, which validation reports. */
	function setBound(row: CatalogRow, side: 0 | 1, value: string): void {
		const current: [number, number] = row.range ?? [0, 100];
		const next: [number, number] = [current[0], current[1]];
		next[side] = Number(value);
		row.range = next;
	}
</script>

<div class="catalog-pane">
	<header class="pane-head">
		<h2 class="pane-title">Entity catalog</h2>
		<span class="pane-meta">{meterRows.length} meters · {traitRows.length} traits</span>
	</header>

	<p class="hint">
		The attributes the BTS tracker may set. A meter is a number inside its range; a trait is free
		text. The tracker receives this list every turn, so a key added here is settable on the very
		next turn — and a range here is what its <code>set_attr</code> calls are clamped to.
	</p>
	<p class="hint">
		<strong>relational</strong> is a value one being holds toward another (a subject is
		required); <strong>self</strong> is their own state; <strong>scene</strong> belongs to the
		room. Nothing here is protected — every row can be removed.
	</p>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading the entity catalog…</p>
	{:else}
		{#if drafts.length === 0}
			<p class="hint">
				The catalog is empty — the tracker may set no attribute at all. Add a meter to start.
			</p>
		{:else}
			<ul class="rows">
				{#each drafts as row, index (index)}
					<li class="row" class:trait={row.type === 'trait'}>
						<div class="row-head">
							<label class="field-inline">
								<span>key</span>
								<input
									class="key"
									value={row.key}
									oninput={(event) => onKeyInput(row, event.currentTarget)}
								/>
							</label>
							<label class="field-inline">
								<span>label</span>
								<input value={row.label} oninput={(event) => (row.label = event.currentTarget.value)} />
							</label>
							<label class="field-inline">
								<span>type</span>
								<select
									value={row.type}
									onchange={(event) =>
										setType(row, event.currentTarget.value as CatalogRowType)}
								>
									<option value="meter">meter (number)</option>
									<option value="trait">trait (text)</option>
								</select>
							</label>
							<label class="field-inline">
								<span>host</span>
								<select
									value={row.host}
									onchange={(event) =>
										(row.host = event.currentTarget.value as CatalogRowHost)}
								>
									<option value="relational">relational</option>
									<option value="self">self</option>
									<option value="scene">scene</option>
								</select>
							</label>
							{#if row.type === 'meter'}
								<label class="field-inline">
									<span>range</span>
									<input
										class="bound"
										type="number"
										value={row.range?.[0] ?? 0}
										oninput={(event) => setBound(row, 0, event.currentTarget.value)}
									/>
									<span>..</span>
									<input
										class="bound"
										type="number"
										value={row.range?.[1] ?? 100}
										oninput={(event) => setBound(row, 1, event.currentTarget.value)}
									/>
								</label>
							{/if}
							<span class="spacer"></span>
							<button type="button" class="danger" onclick={() => removeRow(row)}>
								Remove
							</button>
						</div>
						<input
							class="description"
							value={row.description}
							placeholder="One line: what this value means to the tracker"
							oninput={(event) => (row.description = event.currentTarget.value)}
						/>
					</li>
				{/each}
			</ul>
		{/if}
		<button type="button" class="ghost add" onclick={addRow}>+ Add attribute</button>
	{/if}

	<div class="actions">
		<span class="spacer"></span>
		<button type="button" class="ghost" onclick={discard} disabled={busy || !dirty}>
			Discard changes
		</button>
		<button type="button" class="primary" onclick={() => void save()} disabled={busy || !dirty}>
			{busy ? 'Saving…' : 'Save catalog'}
		</button>
	</div>
</div>

<style>
	.catalog-pane {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		min-height: 0;
	}
	.pane-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.pane-title {
		margin: 0;
		font-size: var(--font-ui);
	}
	.pane-meta {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
	}
	.row {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.45rem 0.5rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.row.trait {
		border-style: dashed;
	}
	.row-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.field-inline {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}
	.field-inline > span {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.key {
		width: 8rem;
		font-family: var(--font-mono, monospace);
		font-size: var(--font-small);
	}
	.bound {
		width: 3.6rem;
	}
	.description {
		font-size: var(--font-small);
	}
	.add {
		align-self: flex-start;
	}
	.spacer {
		flex: 1;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding-top: 0.5rem;
		border-top: 1px solid var(--line);
	}
	.hint {
		margin: 0.15rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.error {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	button:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.primary {
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.ghost {
		border: 1px solid var(--line);
		background: transparent;
		color: var(--text);
	}
	.danger {
		border: 1px solid var(--line);
		background: transparent;
		color: var(--danger-text);
	}
</style>
