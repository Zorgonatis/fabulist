<script lang="ts">
	// Book pane (D51 §2.2/§2.3, lane L2.c) — the Design Studio's Books tab.
	//
	// Migrated from `routes/chat/WIEditor.svelte`: the catalog list + the
	// entry-row editor (`keys` comma-text, `content`, `constant`,
	// `insertion_order`, `case_sensitive`, `comment`, `enabled`), the ST
	// lore-file import door, and the D4 rule that unknown entry keys ride through
	// untouched. Two live bugs the migration closes (D51 §5):
	//
	//   * `provenance` is carried on EVERY save. The old `WIEditor.save()` omitted
	//     it and `PUT /api/books/{id}` defaults it to `{}`, wiping the value.
	//   * unknown entry keys are re-emitted verbatim: each row keeps an `extra`
	//     bag of everything the editor does not own and spreads it back on save.
	//
	// Props-only (StudioPaneProps): the shell injects `focusId` / `startNew` and
	// listens to `onChanged` / `onDirty`, so a future `window.open` popout can
	// re-host the pane unchanged. All catalog I/O goes through `#lib/api`
	// (API_BASE, never a relative path — project gotcha).
	//
	// SlotForm renders the two scalar slots a book body has: `name` (required) and
	// `provenance` (read-only passthrough). `ENTITY_SLOTS` carries no `book` table
	// (its keys are persona / world / assistant), so the pane defines a local
	// two-row table rather than reading the shared registry; the entries body is
	// bespoke and rendered below the slots.
	import { onMount } from 'svelte';
	import type { Book, BookEntry } from '#lib/api';
	import { createBook, deleteBook, listBooks, normalizeBookEntry, putBook } from '#lib/api';
	import type { SlotSpec } from '#lib/entitySlots';
	import DefinitionControls from '#lib/components/DefinitionControls.svelte';
	import EntityList from '#lib/components/EntityList.svelte';
	import SlotForm from '#lib/components/SlotForm.svelte';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty
	}: {
		focusId?: string | null;
		startNew?: boolean;
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
	} = $props();

	/** The two scalar slots of a book body; `provenance` is passthrough. */
	const bookSlots: SlotSpec[] = [
		{
			key: 'name',
			label: 'Name',
			control: 'text',
			required: true,
			placeholder: 'Night Market lore'
		},
		{
			key: 'provenance',
			label: 'Provenance',
			control: 'json',
			passthrough: true,
			help: 'Origin metadata — read-only, carried on every save.'
		}
	];

	/** One editable entry row; `extra` carries the entry's unknown keys (D4). */
	interface EntryRow {
		keysText: string;
		content: string;
		constant: boolean;
		insertion_order: number;
		case_sensitive: boolean;
		comment: string;
		enabled: boolean;
		extra: Record<string, unknown>;
	}

	let books = $state<Book[]>([]);
	/** Selected catalog book id; `null` with `creating` false means no editor. */
	let selectedId = $state<string | null>(null);
	let creating = $state(false);
	/** Scalar draft body (`name` + passthrough `provenance`); SlotForm mutates it. */
	let body = $state<{ name: string; provenance: Record<string, unknown> }>({
		name: '',
		provenance: {}
	});
	let rows = $state<EntryRow[]>([]);
	/** The serialized draft the current editor loaded — the dirty baseline. */
	let baseline = $state(JSON.stringify({ name: '', entries: [] as BookEntry[] }));
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	let status = $state('');
	/** File input + in-flight flag for the ST lore import door. */
	let importInput = $state<HTMLInputElement | null>(null);
	let importing = $state(false);
	let slotForm = $state<ReturnType<typeof SlotForm>>();

	/** The book the editor is editing (null while creating / nothing selected). */
	const editingBook = $derived(
		selectedId ? (books.find((book) => book.book_id === selectedId) ?? null) : null
	);
	/** True while the editor is open (existing book or a create draft). */
	const editing = $derived(creating || selectedId !== null);
	const dirty = $derived(serialize() !== baseline);

	/** Canonical entry keys the editor owns; everything else is passthrough. */
	const ENTRY_KEYS = [
		'keys',
		'content',
		'constant',
		'insertion_order',
		'case_sensitive',
		'comment',
		'enabled'
	];

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** JSON-only deep clone (the wire data is JSON by construction). */
	function cloneJson<T>(value: T): T {
		return JSON.parse(JSON.stringify(value)) as T;
	}

	function splitKeys(text: string): string[] {
		return text
			.split(',')
			.map((key) => key.trim())
			.filter((key) => key.length > 0);
	}

	/** Serialized {name, entries} — the dirty-comparison + save payload. */
	function serialize(): string {
		return JSON.stringify({ name: body.name, entries: toEntries(rows) });
	}

	function toRows(book: Book): EntryRow[] {
		return book.entries.map((entry) => {
			const extra: Record<string, unknown> = {};
			for (const [key, value] of Object.entries(entry)) {
				if (!ENTRY_KEYS.includes(key)) extra[key] = value;
			}
			return {
				keysText: entry.keys.join(', '),
				content: entry.content,
				constant: entry.constant,
				insertion_order: entry.insertion_order,
				case_sensitive: entry.case_sensitive,
				comment: entry.comment,
				enabled: entry.enabled !== false,
				extra
			};
		});
	}

	/** Rows -> wire entries. Unknown keys ride first so the canonical shape wins. */
	function toEntries(source: readonly EntryRow[]): BookEntry[] {
		return source.map((row) => ({
			...row.extra,
			keys: splitKeys(row.keysText),
			content: row.content,
			constant: row.constant,
			insertion_order: Number.isFinite(row.insertion_order) ? row.insertion_order : 0,
			case_sensitive: row.case_sensitive,
			comment: row.comment,
			enabled: row.enabled
		}));
	}

	/** Load a book into the editable draft (also the post-save reseed). */
	function applyLoaded(book: Book): void {
		body = { name: book.name, provenance: cloneJson(book.provenance) };
		rows = toRows(book);
		baseline = serialize();
		status = '';
	}

	/** Replace or append a book in the catalog with the freshly persisted one. */
	function upsert(book: Book): void {
		const index = books.findIndex((item) => item.book_id === book.book_id);
		books = index >= 0 ? books.map((item, i) => (i === index ? book : item)) : [...books, book];
	}

	function confirmDiscard(): boolean {
		if (!dirty) return true;
		if (typeof window === 'undefined') return true;
		return window.confirm('Discard unsaved changes to this book?');
	}

	function select(book: Book): void {
		if (!confirmDiscard()) return;
		selectedId = book.book_id;
		creating = false;
		applyLoaded(book);
		error = '';
	}

	/** Seed a fresh create draft without a dirty guard (mount / revert paths). */
	function resetCreate(): void {
		selectedId = null;
		creating = true;
		body = { name: '', provenance: {} };
		rows = [];
		baseline = serialize();
	}

	function startCreate(): void {
		if (!confirmDiscard()) return;
		resetCreate();
		error = '';
		status = '';
	}

	function revert(): void {
		if (creating) {
			resetCreate();
			return;
		}
		if (editingBook) applyLoaded(editingBook);
	}

	function addEntry(): void {
		rows = [
			...rows,
			{
				keysText: '',
				content: '',
				constant: false,
				insertion_order: 0,
				case_sensitive: false,
				comment: '',
				enabled: true,
				extra: {}
			}
		];
	}

	function removeEntry(index: number): void {
		rows = rows.filter((_, i) => i !== index);
	}

	async function save(): Promise<void> {
		if (busy || !editing) return;
		const invalid = slotForm?.validate();
		if (invalid) {
			error = invalid;
			return;
		}
		const name = body.name.trim();
		if (!name) {
			error = 'Give the book a name.';
			return;
		}
		busy = true;
		try {
			// `provenance` rides on EVERY save (D51 §5): omitting it makes the PUT
			// default the field back to `{}`.
			const payload = { name, entries: toEntries(rows), provenance: body.provenance };
			const wasCreate = creating;
			const saved = wasCreate ? await createBook(payload) : await putBook(selectedId!, payload);
			upsert(saved);
			selectedId = saved.book_id;
			creating = false;
			applyLoaded(saved);
			status = wasCreate ? `Created “${saved.name}”` : 'Saved';
			error = '';
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function remove(book: Book): Promise<void> {
		const label = book.name || book.book_id;
		// Same tombstone copy the persona / assistant drawers use (D51: a delete
		// appends a tombstone; the definition stays in the log, D10).
		const confirmed = window.confirm(
			`Delete book “${label}”? This appends a tombstone event — the definition stays in the log (D10) and a later save with the same id can resurrect it.`
		);
		if (!confirmed) return;
		busy = true;
		try {
			await deleteBook(book.book_id);
			books = books.filter((item) => item.book_id !== book.book_id);
			if (selectedId === book.book_id || creating) {
				selectedId = null;
				creating = false;
				body = { name: '', provenance: {} };
				rows = [];
				baseline = serialize();
			}
			status = `Deleted “${label}”.`;
			error = '';
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	onMount(() => {
		void load();
	});

	async function load(): Promise<void> {
		loading = true;
		try {
			books = await listBooks();
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
			loaded = true;
		}
	}

	/** Re-read the catalog (the source of truth for the list). */
	async function reloadList(): Promise<void> {
		try {
			books = await listBooks();
			error = '';
		} catch (err) {
			error = message(err);
		}
	}

	/** A definition import lands a row; select it so the change is visible. */
	async function afterImport(id: string): Promise<void> {
		await reloadList();
		const found = books.find((book) => book.book_id === id);
		if (found) select(found);
		onChanged?.();
	}

	// --- focus / startNew (StudioPaneProps) ------------------------------------
	// Applied once the catalog has loaded, and again whenever the shell sends a NEW
	// request. Keyed on `requestSeq` when present (a repeatable door re-sends the
	// same `startNew` boolean, so only a NEW `seq` can re-fire); absent `seq` the
	// legacy value latch applies. The no-request key (`initial`) preserves the
	// original behaviour of selecting the first book once the catalog loads.
	let loaded = $state(false);
	let appliedKey = $state<string | null>(null);

	$effect(() => {
		if (!loaded) return;
		const key =
			requestSeq != null
				? `seq:${requestSeq}`
				: startNew
					? 'new'
					: focusId
						? `focus:${focusId}`
						: 'initial';
		if (key === appliedKey) return;
		if (startNew) {
			appliedKey = key;
			startCreate();
			return;
		}
		const fid = focusId ?? null;
		const target = (fid && books.find((book) => book.book_id === fid)) || books[0] || null;
		if (target) {
			appliedKey = key;
			select(target);
		}
	});

	/** Push the dirty flag up so the shell can guard tab switches / close. */
	$effect(() => {
		onDirty?.(dirty);
	});

	// --- ST lore-file import (ported from WIEditor) ----------------------------

	/** Pull the raw entry list out of any accepted world-info / book shape. */
	function extractEntries(raw: unknown): Record<string, unknown>[] {
		const isRecord = (value: unknown): value is Record<string, unknown> =>
			typeof value === 'object' && value !== null && !Array.isArray(value);
		// Only objects that actually look like ST entries qualify, so a card or an
		// arbitrary JSON blob resolves to "no entries" instead of a junk book.
		const isEntry = (value: unknown): value is Record<string, unknown> =>
			isRecord(value) && ('content' in value || 'keys' in value || 'key' in value);
		let source: unknown = raw;
		if (isRecord(raw) && 'entries' in raw) source = raw.entries;
		if (Array.isArray(source)) return source.filter(isEntry);
		// A raw ST file is a numeric-id -> entry mapping; a character_book's
		// `entries` may itself be that mapping.
		if (isRecord(source)) return Object.values(source).filter(isEntry);
		return [];
	}

	/** Book name from the JSON, else the file stem. */
	function importName(raw: unknown, filename: string): string {
		if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
			const name = (raw as Record<string, unknown>).name;
			if (typeof name === 'string' && name.trim()) return name.trim();
		}
		return filename.replace(/\.json$/i, '').trim() || 'Imported lore';
	}

	/** Import a world-info JSON as a NEW book in the catalog (`POST /api/books`). */
	async function onImportFile(event: Event): Promise<void> {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file || importing) return;
		importing = true;
		try {
			const parsed: unknown = JSON.parse(await file.text());
			const rawEntries = extractEntries(parsed);
			if (rawEntries.length === 0) {
				throw new Error('no world-info entries found in that file');
			}
			const saved = await createBook({
				name: importName(parsed, file.name),
				entries: rawEntries.map((entry) => normalizeBookEntry(entry))
			});
			upsert(saved);
			select(saved);
			status = `Imported “${saved.name}” (${saved.entries.length} ${
				saved.entries.length === 1 ? 'entry' : 'entries'
			}) as a new catalog book.`;
			error = '';
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			importing = false;
		}
	}
</script>

<div class="pane">
	<header class="pane-head">
		<span class="pane-title">Books</span>
		{#if dirty}<span class="dirty" aria-live="polite">● unsaved</span>{/if}
		<span class="spacer"></span>
		<DefinitionControls kind="book" id={selectedId} onImported={(id) => void afterImport(id)} />
		<button
			type="button"
			class="ghost"
			disabled={importing}
			title="Import a SillyTavern world-info JSON as a new book in this catalog"
			aria-label="Import a world-info JSON as a new book in the lore catalog"
			onclick={() => importInput?.click()}
		>
			{importing ? 'Importing…' : 'Import lore (JSON)'}
		</button>
		<input
			bind:this={importInput}
			class="file-input"
			type="file"
			accept=".json,application/json"
			aria-label="Import a world-info JSON file"
			onchange={(event) => void onImportFile(event)}
		/>
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}
	{#if status}
		<p class="status" role="status">{status}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading books…</p>
	{:else}
		<div class="cols">
			<section class="list-col" aria-label="Book catalog">
				<EntityList
					items={books}
					{selectedId}
					getId={(book: Book) => book.book_id}
					getLabel={(book: Book) => book.name || book.book_id}
					getMeta={(book: Book) =>
						`${book.entries.length} ${book.entries.length === 1 ? 'entry' : 'entries'}`}
					onSelect={(book: Book) => select(book)}
					onNew={startCreate}
					empty="No lore books yet — use + New, or import a world-info JSON above."
				/>
			</section>

			<section class="editor-col" aria-label="Book editor">
				{#if editing}
					<form
						class="book-form"
						onsubmit={(event) => {
							event.preventDefault();
							void save();
						}}
					>
						<h3>{creating ? 'New book' : 'Edit book'}</h3>

						<!-- name + provenance (passthrough, read-only) -->
						<SlotForm bind:this={slotForm} slots={bookSlots} value={body} />

						<div class="section-head">
							<span class="count">{rows.length} {rows.length === 1 ? 'entry' : 'entries'}</span>
							<button type="button" class="ghost small" onclick={addEntry}>+ Add entry</button>
						</div>

						{#each rows as row, index (`entry:${index}`)}
							<div class="entry" class:off={!row.enabled}>
								<div class="entry-head">
									<label class="check" title="Include this entry when the book activates">
										<input type="checkbox" bind:checked={row.enabled} />
										<span>enabled</span>
									</label>
									<label class="check">
										<input type="checkbox" bind:checked={row.constant} />
										<span title="Always fire, no keyword needed">constant</span>
									</label>
									<label class="check">
										<input type="checkbox" bind:checked={row.case_sensitive} />
										<span>case</span>
									</label>
									<label class="order">
										<span>order</span>
										<input type="number" bind:value={row.insertion_order} />
									</label>
									<button
										type="button"
										class="icon danger"
										title="Remove entry"
										aria-label={`Remove entry ${index + 1}`}
										onclick={() => removeEntry(index)}
									>
										×
									</button>
								</div>
								<label class="field">
									<span>Keys</span>
									<input bind:value={row.keysText} placeholder="comma, separated, keywords" />
								</label>
								<label class="field">
									<span>Content</span>
									<textarea bind:value={row.content} rows="3" placeholder="Lore text"></textarea>
								</label>
								<label class="field">
									<span>Comment</span>
									<input bind:value={row.comment} placeholder="Optional label" />
								</label>
							</div>
						{/each}

						<div class="form-actions">
							{#if editingBook}
								<button type="button" class="danger" onclick={() => void remove(editingBook)}>
									Delete
								</button>
							{/if}
							<span class="spacer"></span>
							<button type="button" class="ghost" disabled={!dirty || busy} onclick={revert}>
								Revert
							</button>
							<button type="submit" class="primary" disabled={busy}>
								{busy ? 'Saving…' : 'Save'}
							</button>
						</div>
					</form>
				{:else}
					<p class="hint">Select a book to edit, or create one.</p>
				{/if}
			</section>
		</div>
	{/if}
</div>

<style>
	.pane {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		min-width: 0;
	}
	.pane-head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.pane-title {
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text-muted);
	}
	.dirty {
		font-size: var(--font-micro);
		color: var(--warn);
	}
	.spacer {
		flex: 1;
	}
	.file-input {
		display: none;
	}
	/* List column + editor; wraps to a single column when the pane is narrow. */
	.cols {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		align-items: flex-start;
	}
	.list-col {
		flex: 1 1 12rem;
		min-width: 0;
	}
	.editor-col {
		flex: 2 1 22rem;
		min-width: 0;
	}
	.book-form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
		color: var(--text);
	}
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.count {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.entry {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		padding: 0.5rem 0.6rem;
	}
	.entry.off {
		opacity: 0.55;
	}
	.entry-head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.35rem 0.6rem;
	}
	.check {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
		cursor: pointer;
	}
	.order {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.order input {
		width: 4.5rem;
		font: inherit;
		font-size: var(--font-small);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.2rem 0.35rem;
	}
	.icon {
		margin-left: auto;
		font: inherit;
		font-size: var(--font-ui);
		line-height: 1;
		width: 1.4rem;
		height: 1.4rem;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.icon:hover {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.field input,
	.field textarea {
		font: inherit;
		font-size: var(--font-ui);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.35rem 0.5rem;
		resize: vertical;
	}
	.form-actions {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	button.small {
		padding: 0.2rem 0.5rem;
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
	.primary:hover:not(:disabled) {
		background: var(--accent);
		color: var(--on-accent);
	}
	.ghost {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text);
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
	}
	.danger {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--danger-text);
	}
	.danger:hover:not(:disabled) {
		background: var(--danger-soft);
	}
	.error {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.status {
		margin: 0;
		font-size: var(--font-small);
		color: var(--accent-strong);
	}
	.hint {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
</style>
