<script lang="ts">
	// Studio Assistants pane (D51 §2.2 / L2.d).
	//
	// Props-only (StudioPaneProps): the shell owns layout, persistence and the
	// Escape/dirty guard; this pane owns the assistant catalog CRUD. It reads no
	// page stores, so a future `window.open` popout can re-host it unchanged.
	//
	// The editor is the shared `SlotForm` driven by `ENTITY_SLOTS.assistant`. The
	// D47 "absent stays absent" rule is SAVE-PATH semantics owned HERE (not in
	// SlotForm): SlotForm carries `''` for the actor "— none —" option, and
	// `buildWrite()` OMITS the `actor` key from the body entirely when it is
	// empty — never `actor: ""`. A real persona id rides through verbatim.
	//
	// Saves go straight to the `#lib/api` client fns (create/update/delete) rather
	// than the `entities.ts` descriptor, because the descriptor's `asAssistantWrite`
	// adapter re-adds `actor: ""` unconditionally, which would defeat that ruling.
	import { onMount } from 'svelte';
	import {
		createAssistant,
		deleteAssistant,
		listAssistants,
		listPersonas,
		updateAssistant
	} from '#lib/api';
	import type { AssistantDefinition, AssistantWrite } from '#lib/api';
	import { ENTITY_SLOTS } from '#lib/entitySlots';
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

	const slots = ENTITY_SLOTS.assistant;

	let assistants = $state<AssistantDefinition[]>([]);
	let personas = $state<{ id: string; label: string }[]>([]);
	let selectedId = $state<string | null>(null);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');

	/** SlotForm draft (exact wire field names); `null` while the list is showing. */
	let draft = $state<Record<string, unknown> | null>(null);
	/** Assistant being edited (`null` = the draft is a new assistant). */
	let editingId = $state<string | null>(null);
	/** JSON snapshot of the draft at seed time; any drift means unsaved edits. */
	let baseline = $state('');

	/** Bound SlotForm instance (its `validate()` renders inline required errors). */
	let form = $state<ReturnType<typeof SlotForm> | null>(null);

	const catalogs = $derived({ personas });

	/**
	 * Unsaved work: a new (unsaved) assistant draft, or edits that drifted from the
	 * seeded snapshot. Merely selecting an unchanged existing row is not dirty.
	 */
	const dirty = $derived(
		draft !== null && (editingId === null || JSON.stringify(draft) !== baseline)
	);
	$effect(() => {
		onDirty?.(dirty);
	});

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	function stringOf(value: unknown): string {
		return typeof value === 'string' ? value : '';
	}

	function stringList(value: unknown): string[] {
		return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
	}

	onMount(() => {
		void loadAll();
	});

	// --- host requests (`focusId` selects, `startNew` opens the create form) ---
	// Applied once the catalog has loaded, and again when the shell sends a NEW
	// request. Keyed on `requestSeq` when present: a repeatable door (New → Cancel
	// → New) re-sends the same `startNew` boolean, so only a NEW `seq` can re-fire.
	// Absent `seq` (standalone pane) the legacy value latch applies.
	let appliedKey: string | null = null;

	$effect(() => {
		if (loading) return;
		const key =
			requestSeq != null
				? `seq:${requestSeq}`
				: startNew
					? 'new'
					: focusId
						? `focus:${focusId}`
						: null;
		if (!key || key === appliedKey) return;
		if (startNew) {
			appliedKey = key;
			startCreate();
			return;
		}
		if (focusId) {
			const found = assistants.find((row) => row.id === focusId);
			if (!found) return;
			appliedKey = key;
			startEdit(found);
		}
	});

	/** Load the assistant catalog plus the actor picker's persona catalog. */
	async function loadAll(): Promise<void> {
		loading = true;
		error = '';
		try {
			const [catalog, personaList] = await Promise.all([listAssistants(), listPersonas()]);
			assistants = catalog.assistants;
			personas = personaList.map((persona) => ({
				id: persona.persona_id,
				label: persona.name || persona.persona_id
			}));
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	/** Re-read only the assistant catalog (saves / deletes / imports). */
	async function reloadCatalog(): Promise<void> {
		assistants = (await listAssistants()).assistants;
	}

	/** Seed a SlotForm draft from a wire definition (absent actor → "— none —"). */
	function seedDraft(body: Record<string, unknown>): Record<string, unknown> {
		return {
			name: stringOf(body.name),
			persona: stringOf(body.persona),
			style_notes: stringOf(body.style_notes),
			agent: stringOf(body.agent) || 'narrator',
			trigger: stringOf(body.trigger) || 'manual',
			keywords: stringList(body.keywords),
			actor: stringOf(body.actor)
		};
	}

	function startCreate(): void {
		selectedId = null;
		editingId = null;
		draft = {
			name: '',
			persona: '',
			style_notes: '',
			agent: 'narrator',
			trigger: 'manual',
			keywords: [],
			actor: ''
		};
		baseline = JSON.stringify(draft);
		error = '';
	}

	function startEdit(row: AssistantDefinition): void {
		selectedId = row.id;
		editingId = row.id;
		draft = seedDraft(row as unknown as Record<string, unknown>);
		baseline = JSON.stringify(draft);
		error = '';
	}

	function cancelForm(): void {
		draft = null;
		editingId = null;
		error = '';
	}

	/**
	 * Build the `POST/PUT /api/assistants` body. The D47 ruling lives here: an
	 * empty actor OMITS the key so absent stays absent; only a real id is sent.
	 */
	function buildWrite(source: Record<string, unknown>): AssistantWrite {
		const body: AssistantWrite = {
			name: stringOf(source.name).trim(),
			persona: stringOf(source.persona),
			style_notes: stringOf(source.style_notes),
			agent: stringOf(source.agent).trim() || 'narrator',
			trigger: stringOf(source.trigger) || 'manual',
			keywords: stringList(source.keywords)
		};
		const actor = stringOf(source.actor).trim();
		if (actor) body.actor = actor;
		return body;
	}

	async function save(): Promise<void> {
		if (!draft || busy) return;
		const firstError = form?.validate() ?? null;
		if (firstError) {
			error = firstError;
			return;
		}
		busy = true;
		error = '';
		try {
			const body = buildWrite(draft);
			let savedId = editingId;
			if (editingId) {
				await updateAssistant(editingId, body);
			} else {
				savedId = (await createAssistant(body)).id;
			}
			await reloadCatalog();
			selectedId = savedId;
			draft = null;
			editingId = null;
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** Tombstone one definition (append-only, D10); refresh the list after. */
	async function removeById(id: string): Promise<void> {
		const row = assistants.find((item) => item.id === id);
		const label = row?.name || id;
		const copy =
			row?.demo === true
				? `Delete built-in placeholder assistant “${label}”? It is only a placeholder (not a preset port). This appends a tombstone event (D10) — the definition stays in the log and a later save with the same id can resurrect it.`
				: `Delete assistant “${label}”? This appends a tombstone event — the definition stays in the log (D10) and a later save with the same id can resurrect it.`;
		if (typeof window === 'undefined' || !window.confirm(copy)) return;
		busy = true;
		try {
			await deleteAssistant(id);
			if (selectedId === id) selectedId = null;
			if (editingId === id) cancelForm();
			await reloadCatalog();
			error = '';
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** An import creates/updates an assistant; focus it and open the editor. */
	async function afterImport(id: string): Promise<void> {
		try {
			await reloadCatalog();
			const found = assistants.find((row) => row.id === id);
			selectedId = id;
			if (found) startEdit(found);
			onChanged?.();
		} catch (err) {
			error = message(err);
		}
	}
</script>

<div class="assistant-pane">
	<header class="pane-head">
		<h2 class="pane-title">Assistants</h2>
		<DefinitionControls kind="assistant" id={selectedId} onImported={(id) => void afterImport(id)} />
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	<div class="pane-body">
		<div class="list-col">
			{#if loading}
				<p class="hint">Loading assistants…</p>
			{:else}
				<EntityList
					items={assistants}
					{selectedId}
					getId={(row: AssistantDefinition) => row.id}
					getLabel={(row: AssistantDefinition) => row.name || row.id}
					getMeta={(row: AssistantDefinition) => (row.actor ? `as ${row.actor}` : '')}
					onSelect={(row: AssistantDefinition) => startEdit(row)}
					onNew={startCreate}
					empty="No assistants yet — use + New, or import an assistant JSON above."
				>
					{#snippet rowActions(row: AssistantDefinition)}
						{#if row.demo}
							<span class="demo-badge" title="Built-in placeholder (not a preset port)">demo</span>
						{/if}
					{/snippet}
				</EntityList>
			{/if}
		</div>

		<div class="editor-col">
			{#if draft}
				{#key editingId ?? 'new'}
					<form
						class="editor"
						onsubmit={(event) => {
							event.preventDefault();
							void save();
						}}
					>
						<h3 class="editor-title">{editingId ? 'Edit assistant' : 'New assistant'}</h3>
						<SlotForm bind:this={form} {slots} value={draft} {catalogs} />
						<div class="actions">
							{#if editingId}
								{@const targetId = editingId}
								<button type="button" class="danger" onclick={() => void removeById(targetId)}>
									Delete
								</button>
							{/if}
							<span class="spacer"></span>
							<button type="button" class="ghost" onclick={cancelForm} disabled={busy}>
								Cancel
							</button>
							<button type="submit" class="primary" disabled={busy}>
								{busy ? 'Saving…' : 'Save assistant'}
							</button>
						</div>
					</form>
				{/key}
			{:else}
				<p class="hint">Select an assistant to edit it, or use “+ New”.</p>
			{/if}
		</div>
	</div>
</div>

<style>
	.assistant-pane {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		min-height: 0;
	}
	.pane-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.pane-title {
		margin: 0;
		font-size: var(--font-ui);
	}
	.pane-body {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		align-items: flex-start;
	}
	.list-col {
		flex: 0 1 13rem;
		min-width: 10rem;
	}
	.editor-col {
		flex: 1 1 18rem;
		min-width: 0;
	}
	.editor {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.editor-title {
		margin: 0;
		font-size: var(--font-ui);
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		margin-top: 0.15rem;
	}
	.spacer {
		flex: 1;
	}
	.hint {
		margin: 0.2rem 0;
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
	.demo-badge {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.05rem 0.3rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
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
		background: none;
		color: var(--text-muted);
	}
	.danger:hover:not(:disabled) {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
