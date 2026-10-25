<script lang="ts">
	// Persona pane (D51 §2.2/§2.3, L2.a) ---------------------------------------
	//
	// The Design Studio's persona editor: catalog list on the left, slot-driven
	// editor on the right. It is PROPS-ONLY (`StudioPaneProps`) — no page store,
	// no `window` at render time — so the studio shell can re-host it in a
	// popout later (D51 §2.3).
	//
	// Definitions are data (D4): the draft is the persona WIRE shape, seeded from
	// `listPersonas()` (the row is the full body, exactly as `PersonaDrawer` did)
	// and handed to `SlotForm` unchanged, so `extensions` / `provenance` ride
	// through every save untouched — they are read-only slots, never re-typed.
	// `ENTITY_SLOTS.persona` decides the fields; this pane hardcodes none.
	//
	// Every write goes through `#lib/api` (never a relative path — the studio's
	// docked panel shares the page with the composer). Delete is a tombstone
	// event with the drawer's confirm copy. The extra door the drawer lacked is
	// a definition-only card import (`importCard(file, { persona: true })`,
	// D51/L1.c): it creates NO session, only the persona definition.
	import { onDestroy, onMount } from 'svelte';
	import type { Book, Persona, PersonaWrite } from '#lib/api';
	import { createPersona, deletePersona, importCard, listBooks, listPersonas, updatePersona } from '#lib/api';
	import {
		attachDefinitionImage,
		definitionInitials,
		fetchDefinitionImage
	} from '#lib/api/definitionImages';
	import { ENTITY_SLOTS } from '#lib/entitySlots';
	import CropEditor from './CropEditor.svelte';
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

	/** The editable body — the full persona wire shape, passthroughs included. */
	type PersonaDraft = {
		name: string;
		description: string;
		personality: string;
		scenario: string;
		first_mes: string;
		alternate_greetings: string[];
		book_ids: string[];
		extensions: Record<string, unknown>;
		provenance: Record<string, unknown>;
	};

	let personas = $state<Persona[]>([]);
	/** idlist catalogs for `SlotForm` (`book_ids` picks from the book catalog). */
	let catalogs = $state<{ books: { id: string; label: string }[] }>({ books: [] });
	let selectedId = $state<string | null>(null);
	/** Open editor draft, or `null` while the list-only view shows. */
	let draft = $state<PersonaDraft | null>(null);
	/** Persona the open draft edits (`null` = the draft is a new persona). */
	let editingId = $state<string | null>(null);
	let loading = $state(true);
	/** True once the first catalog load resolved (gates the focus/startNew effects). */
	let loaded = $state(false);
	let busy = $state(false);
	let importingCard = $state(false);
	let error = $state('');
	let cardStatus = $state('');
	let cardError = $state('');
	let dirty = $state(false);

	let form = $state<{ validate: () => string | null } | null>(null);
	let cardInput = $state<HTMLInputElement | null>(null);

	/** Circular avatars keyed by persona id (`null` = fetched, no image). */
	let avatars = $state<Record<string, string | null>>({});
	/** Chosen file while `CropEditor` is open, or null. */
	let cropFile = $state<File | null>(null);
	let uploading = $state(false);
	let imageError = $state('');
	let imageInput = $state<HTMLInputElement | null>(null);

	/** The persona the open editor edits (drives the Delete action). */
	const editingPersona = $derived(
		editingId ? (personas.find((persona) => persona.persona_id === editingId) ?? null) : null
	);

	/** Name behind the editor avatar's initials fallback. */
	const editingName = $derived(editingPersona?.name ?? draft?.name ?? '');

	/** Render `url`/initials for one avatar slot (rows + editor reuse this markup). */
	function avatarUrl(id: string | null): string | null {
		return id ? (avatars[id] ?? null) : null;
	}

	/**
	 * Load missing row avatars (revoking any whose persona left the list).
	 *
	 * Every definition is fetched once and cached in {@link avatars}; a persona
	 * with no image resolves to `null` and shows its initials. The pane owns the
	 * object URLs and revokes them on departure / teardown.
	 */
	async function hydrateAvatars(list: Persona[]): Promise<void> {
		const live = new Set(list.map((persona) => persona.persona_id));
		for (const [id, url] of Object.entries(avatars)) {
			if (!live.has(id)) {
				if (url) URL.revokeObjectURL(url);
				delete avatars[id];
			}
		}
		for (const persona of list) {
			if (persona.persona_id in avatars) continue;
			// W9.16: only request when the definition HAS an image; fetching
			// blindly 404'd once per persona on every pane open.
			avatars[persona.persona_id] = persona.image_asset
				? await fetchDefinitionImage('persona', persona.persona_id)
				: null;
		}
	}

	/** Re-read one avatar after its image changed (revokes the stale object URL). */
	async function refreshAvatar(id: string): Promise<void> {
		const stale = avatars[id];
		if (stale) URL.revokeObjectURL(stale);
		delete avatars[id];
		avatars[id] = await fetchDefinitionImage('persona', id);
	}

	/** Open the file picker for a cropped definition image. */
	function pickImage(): void {
		imageInput?.click();
	}

	function onImageFile(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (!file) return;
		imageError = '';
		cropFile = file;
	}

	/** Upload the cropped PNG, then refresh this row's avatar + the catalog. */
	async function saveImage(blob: Blob): Promise<void> {
		if (!editingId || uploading) return;
		uploading = true;
		imageError = '';
		try {
			await attachDefinitionImage('persona', editingId, blob);
			cropFile = null;
			await refreshAvatar(editingId);
			await reloadList();
			onChanged?.();
		} catch (err) {
			imageError = message(err);
		} finally {
			uploading = false;
		}
	}

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** Report dirtiness to the shell exactly on transitions. */
	function setDirty(value: boolean): void {
		if (dirty === value) return;
		dirty = value;
		onDirty?.(value);
	}

	/** Discard guard for list switching (D51 §2.3 dirty guard). */
	function confirmDiscard(): boolean {
		if (!dirty) return true;
		if (typeof window === 'undefined') return true;
		return window.confirm('Discard unsaved changes?');
	}

	/**
	 * Copy a persona wire into an editable draft. `extensions` / `provenance`
	 * keep their object identity — SlotForm only displays them, and the save
	 * body carries them verbatim.
	 */
	function draftFrom(persona: Persona): PersonaDraft {
		return {
			name: persona.name,
			description: persona.description,
			personality: persona.personality,
			scenario: persona.scenario,
			first_mes: persona.first_mes,
			alternate_greetings: [...persona.alternate_greetings],
			book_ids: [...persona.book_ids],
			extensions: persona.extensions,
			provenance: persona.provenance
		};
	}

	function openEditor(persona: Persona): void {
		selectedId = persona.persona_id;
		editingId = persona.persona_id;
		// A fresh object re-seeds SlotForm's line buffers (its `value` identity
		// is the seed trigger).
		draft = draftFrom(persona);
		error = '';
		setDirty(false);
	}

	function selectPersona(persona: Persona): void {
		if (!confirmDiscard()) return;
		openEditor(persona);
	}

	function startCreate(): void {
		selectedId = null;
		editingId = null;
		draft = {
			name: '',
			description: '',
			personality: '',
			scenario: '',
			first_mes: '',
			alternate_greetings: [],
			book_ids: [],
			extensions: {},
			provenance: {}
		};
		error = '';
		setDirty(false);
	}

	function requestCreate(): void {
		if (!confirmDiscard()) return;
		startCreate();
	}

	function cancelForm(): void {
		draft = null;
		editingId = null;
		setDirty(false);
	}

	/** Insert or replace a saved persona in the list (latest-wins fold view). */
	function upsert(saved: Persona): void {
		const index = personas.findIndex((persona) => persona.persona_id === saved.persona_id);
		personas =
			index >= 0
				? personas.map((persona, i) => (i === index ? saved : persona))
				: [...personas, saved];
		void hydrateAvatars(personas);
	}

	onMount(() => {
		void load();
	});

	onDestroy(() => {
		for (const url of Object.values(avatars)) {
			if (url) URL.revokeObjectURL(url);
		}
	});

	async function load(): Promise<void> {
		loading = true;
		try {
			const [loadedPersonas, loadedBooks] = await Promise.all([listPersonas(), listBooks()]);
			personas = loadedPersonas;
			void hydrateAvatars(loadedPersonas);
			catalogs = {
				books: loadedBooks.map((book: Book) => ({ id: book.book_id, label: book.name || book.book_id }))
			};
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
			loaded = true;
		}
	}

	/** Re-read the catalog (the list's source of truth after any write). */
	async function reloadList(): Promise<void> {
		try {
			personas = await listPersonas();
			void hydrateAvatars(personas);
			error = '';
		} catch (err) {
			error = message(err);
		}
	}

	async function save(): Promise<void> {
		if (!draft || busy) return;
		// Required-name validation lives in SlotForm: it reveals the inline
		// message and returns the first one, or null when the draft is valid.
		const invalid = form?.validate() ?? null;
		if (invalid) {
			error = '';
			return;
		}
		busy = true;
		error = '';
		try {
			const body: PersonaWrite = {
				name: draft.name.trim(),
				description: draft.description.trim(),
				personality: draft.personality.trim(),
				scenario: draft.scenario.trim(),
				first_mes: draft.first_mes.trim(),
				alternate_greetings: [...draft.alternate_greetings],
				book_ids: [...draft.book_ids],
				extensions: draft.extensions,
				provenance: draft.provenance
			};
			const saved = editingId ? await updatePersona(editingId, body) : await createPersona(body);
			upsert(saved);
			selectedId = saved.persona_id;
			draft = null;
			editingId = null;
			setDirty(false);
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function remove(persona: Persona): Promise<void> {
		if (busy) return;
		if (typeof window === 'undefined') return;
		const label = persona.name || persona.persona_id;
		const confirmed = window.confirm(
			`Delete persona “${label}”? This appends a tombstone event — the definition stays in the log (D10) and a later save with the same id can resurrect it.`
		);
		if (!confirmed) return;
		busy = true;
		try {
			await deletePersona(persona.persona_id);
			if (selectedId === persona.persona_id) selectedId = null;
			if (editingId === persona.persona_id) {
				draft = null;
				editingId = null;
				setDirty(false);
			}
			await reloadList();
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** DefinitionControls imported a persona JSON: refresh and highlight it. */
	async function afterDefinitionImport(id: string): Promise<void> {
		await reloadList();
		onChanged?.();
		const imported = personas.find((persona) => persona.persona_id === id);
		if (imported) selectPersona(imported);
		else selectedId = id;
	}

	async function onCardFile(event: Event): Promise<void> {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (!file) return;
		await importCardFile(file);
	}

	/**
	 * The definition-only card door (D51): parse PNG/JSON and capture the card as
	 * a persona. Creates no session — the backend's `?persona=1` path
	 * (`importCard(..., { persona: true })`). Failure (`{ok:false,error}`) renders
	 * inline; success refreshes the catalog and selects the upserted persona.
	 */
	async function importCardFile(file: File): Promise<void> {
		if (importingCard) return;
		importingCard = true;
		cardError = '';
		cardStatus = '';
		try {
			const result = await importCard(file, { persona: true });
			if (!result.ok) {
				cardError = result.error || 'could not import that card';
				return;
			}
			const wire = result.persona ?? {};
			const id = typeof wire.persona_id === 'string' ? wire.persona_id : '';
			await reloadList();
			onChanged?.();
			const imported = id
				? (personas.find((persona) => persona.persona_id === id) ?? null)
				: null;
			const label = imported?.name || (typeof wire.name === 'string' ? wire.name : '');
			cardStatus = label ? `Imported persona “${label}”` : 'Imported card';
			if (imported) selectPersona(imported);
		} catch (err) {
			cardError = message(err);
		} finally {
			importingCard = false;
		}
	}

	// --- host requests (`focusId` selects, `startNew` opens the create form) ---
	// Each request is applied once, keyed on the shell's monotonic `requestSeq`
	// when present: a repeatable door (New → Cancel → New) re-sends the SAME
	// `startNew` boolean, so the boolean alone cannot re-fire — only a NEW `seq`
	// can. Absent `seq` (standalone pane) the legacy value latch applies. A focus
	// id that names no loaded persona retries when the catalog changes (e.g. a
	// card import just created it).
	let appliedKey: string | null = null;

	$effect(() => {
		if (!loaded) return;
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
			requestCreate();
			return;
		}
		if (focusId) {
			const target = personas.find((persona) => persona.persona_id === focusId);
			if (!target) return;
			appliedKey = key;
			selectPersona(target);
		}
	});
</script>

<div class="pane">
	{#snippet rowAvatar(persona: Persona)}
		<span class="row-avatar" aria-hidden="true">
			{#if avatarUrl(persona.persona_id)}
				<img src={avatarUrl(persona.persona_id)} alt="" />
			{:else}
				<span class="initials">{definitionInitials(persona.name || persona.persona_id)}</span>
			{/if}
		</span>
	{/snippet}

	<header class="pane-head">
		<DefinitionControls
			kind="persona"
			id={selectedId}
			onImported={(id) => void afterDefinitionImport(id)}
		/>
		<button
			type="button"
			class="ghost"
			disabled={busy || importingCard}
			onclick={() => cardInput?.click()}
		>
			{importingCard ? 'Importing…' : 'Import card'}
		</button>
		<input
			bind:this={cardInput}
			class="file-input"
			type="file"
			accept=".png,.json,image/png,application/json"
			aria-label="Import character card PNG or JSON"
			onchange={onCardFile}
		/>
	</header>

	{#if cardError}
		<p class="error" role="alert">{cardError}</p>
	{:else if cardStatus}
		<p class="status" role="status">{cardStatus}</p>
	{/if}

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading personas…</p>
	{:else}
		<div class="columns">
			<div class="list-col">
				<EntityList
					items={personas}
					{selectedId}
					getId={(persona: Persona) => persona.persona_id}
					getLabel={(persona: Persona) => persona.name || persona.persona_id}
					getMeta={(persona: Persona) => persona.scenario || persona.description}
					onSelect={(persona: Persona) => selectPersona(persona)}
					onNew={requestCreate}
					rowActions={rowAvatar}
					empty="No personas yet — use + New, or import a persona JSON or card above."
				/>
			</div>

			<div class="editor-col">
				{#if draft}
					<div class="editor-head">
						<span class="editor-avatar" aria-hidden="true">
							{#if avatarUrl(editingId)}
								<img src={avatarUrl(editingId)} alt="" />
							{:else}
								<span class="initials">{definitionInitials(editingName)}</span>
							{/if}
						</span>
						<h3>{editingId ? 'Edit persona' : 'New persona'}</h3>
					</div>
					<div class="image-controls">
						<button
							type="button"
							class="ghost"
							onclick={pickImage}
							disabled={!editingId || uploading}
						>
							{uploading ? 'Uploading…' : 'Attach image'}
						</button>
						{#if !editingId}
							<span class="hint">Save the persona first, then attach an image.</span>
						{/if}
					</div>
					{#key `persona:${editingId ?? 'new'}`}
						<form
							class="editor"
							onsubmit={(event) => {
								event.preventDefault();
								void save();
							}}
							oninput={() => setDirty(true)}
							onchange={() => setDirty(true)}
						>
							<SlotForm
								bind:this={form}
								slots={ENTITY_SLOTS.persona}
								value={draft as unknown as Record<string, unknown>}
								{catalogs}
							/>
							<div class="form-actions">
								{#if editingPersona}
									<button
										type="button"
										class="danger"
										disabled={busy}
										onclick={() => void remove(editingPersona)}
									>
										Delete
									</button>
								{/if}
								<span class="spacer"></span>
								{#if dirty}<span class="dirty-note">Unsaved changes</span>{/if}
								<button type="button" class="ghost" onclick={cancelForm} disabled={busy}>
									Cancel
								</button>
								<button type="submit" class="primary" disabled={busy}>
									{busy ? 'Saving…' : 'Save persona'}
								</button>
							</div>
						</form>
					{/key}
				{:else}
					<p class="hint">Select a persona to edit, or use + New.</p>
				{/if}
			</div>
		</div>
	{/if}

	<input
		bind:this={imageInput}
		class="file-input"
		type="file"
		accept="image/*"
		aria-label="Attach persona image"
		onchange={onImageFile}
	/>
	{#if cropFile}
		<CropEditor
			file={cropFile}
			saving={uploading}
			error={imageError}
			onSave={(blob) => void saveImage(blob)}
			onCancel={() => {
				if (!uploading) cropFile = null;
			}}
		/>
	{/if}
</div>

<style>
	.pane {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		height: 100%;
		min-height: 0;
	}
	.pane-head {
		flex: none;
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.columns {
		flex: 1;
		min-height: 0;
		display: flex;
		gap: 0.6rem;
	}
	.list-col {
		flex: 0 0 32%;
		min-width: 9rem;
		overflow: auto;
	}
	.editor-col {
		flex: 1;
		min-width: 0;
		overflow: auto;
	}
	.hint {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.editor-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-bottom: 0.5rem;
	}
	.image-controls {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		margin-bottom: 0.5rem;
	}
	.image-controls .hint {
		margin: 0;
	}
	.image-controls button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	.editor-avatar,
	.row-avatar {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 50%;
		overflow: hidden;
		background: var(--bg-hover);
		color: var(--text-dim);
		border: 1px solid var(--line);
	}
	.editor-avatar {
		width: 3rem;
		height: 3rem;
	}
	.row-avatar {
		width: 2rem;
		height: 2rem;
	}
	.editor-avatar img,
	.row-avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.initials {
		font-size: var(--font-small);
		font-weight: 600;
		letter-spacing: 0.02em;
		text-transform: uppercase;
	}
	.row-avatar .initials {
		font-size: var(--font-micro);
	}
	.error {
		margin: 0;
		padding: 0.35rem 0.5rem;
		font-size: var(--font-small);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.status {
		margin: 0;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.editor {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.form-actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		margin-top: 0.15rem;
	}
	.dirty-note {
		font-size: var(--font-micro);
		color: var(--warning-text, var(--text-dim));
	}
	.spacer {
		flex: 1;
	}
	.file-input {
		display: none;
	}
	.pane-head button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	.form-actions button {
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
