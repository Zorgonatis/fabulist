<script lang="ts">
	// Studio Worlds pane (D51 §3 L2.b) — world definition CRUD.
	//
	// A world is a scenario + cast (persona refs) + lore books (D32). This pane is
	// the definition surface: instance list (`EntityList`) | slot-driven editor
	// (`SlotForm` over `ENTITY_SLOTS.world`). It is props-only (the studio shell
	// owns the frame, dock/float placement, Escape and the dirty guard), so a
	// future `window.open` popout can re-host it unchanged.
	//
	// Two D51 contracts are load-bearing here:
	//
	// 1. PASSTHROUGH. The draft is seeded from the WHOLE world wire body, so the
	//    `provenance` slot always rides through to `createWorld`/`updateWorld`
	//    untouched. The drawer this pane replaces (`WorldDrawer.save()`) omitted
	//    `provenance`, so a PUT wiped it to `{}` — the old live bug this pane
	//    fixes by construction (pinned by a Playwright round-trip).
	// 2. NO ASSEMBLY. Playing a world (materializing it into a chat) is a chat
	//    action owned by the rail (D51) — this pane has no "Start chat" control.
	import { onDestroy, onMount } from 'svelte';
	import type { Book, Persona, World } from '#lib/api';
	import {
		createWorld,
		deleteWorld,
		importWorldCard,
		listBooks,
		listPersonas,
		listWorlds,
		updateWorld
	} from '#lib/api';
	import {
		attachDefinitionImage,
		definitionInitials,
		fetchDefinitionImage
	} from '#lib/api/definitionImages';
	import EntityList from '#lib/components/EntityList.svelte';
	import DefinitionControls from '#lib/components/DefinitionControls.svelte';
	import CropEditor from './CropEditor.svelte';
	import SlotForm from '#lib/components/SlotForm.svelte';
	import { ENTITY_SLOTS } from '#lib/entitySlots';

	/** Props every studio pane receives from the shell (D51 §2.3). */
	interface StudioPaneProps {
		focusId?: string | null;
		startNew?: boolean;
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
	}

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty
	}: StudioPaneProps = $props();

	/** The editor draft: the exact world wire body, one key per slot. */
	type WorldDraft = Record<string, unknown>;

	let worlds = $state<World[]>([]);
	let personas = $state<Persona[]>([]);
	let books = $state<Book[]>([]);
	let loaded = $state(false);
	let busy = $state(false);
	let error = $state('');

	/** Open editor draft, or null when no world is selected. */
	let draft = $state<WorldDraft | null>(null);
	/** World being edited (`null` = the draft is a new world). */
	let editingId = $state<string | null>(null);
	/** SlotForm instance, for `validate()`. */
	let form = $state<{ validate: () => string | null } | null>(null);

	let dirty = $state(false);
	/** JSON of the draft as it was seeded / last saved — the dirty baseline. */
	let pristine = '';

	/** Circular avatars keyed by world id (`null` = fetched, no image). */
	let avatars = $state<Record<string, string | null>>({});
	/** Chosen file while `CropEditor` is open, or null. */
	let cropFile = $state<File | null>(null);
	let uploading = $state(false);
	let imageError = $state('');
	let imageInput = $state<HTMLInputElement | null>(null);
	/** Card-door state (issue #40): inline status/error, no dialogs. */
	let importingCard = $state(false);
	let cardStatus = $state('');
	let cardError = $state('');
	let cardInput = $state<HTMLInputElement | null>(null);

	const catalogs = $derived({
		personas: personas.map((persona) => ({ id: persona.persona_id, label: persona.name })),
		books: books.map((book) => ({ id: book.book_id, label: book.name }))
	});

	/** Name behind the editor avatar's initials fallback. */
	const editingName = $derived(
		editingId
			? (worlds.find((world) => world.world_id === editingId)?.name ?? '')
			: String(draft?.name ?? '')
	);

	/** Avatar URL for a world id, or null while it still shows initials. */
	function avatarUrl(id: string | null): string | null {
		return id ? (avatars[id] ?? null) : null;
	}

	/**
	 * Load missing row avatars (revoking any whose world left the list). Every
	 * definition is fetched once and cached; a world with no image resolves to
	 * `null` and shows its initials. The pane owns (and revokes) these URLs.
	 */
	async function hydrateAvatars(list: World[]): Promise<void> {
		const live = new Set(list.map((world) => world.world_id));
		for (const [id, url] of Object.entries(avatars)) {
			if (!live.has(id)) {
				if (url) URL.revokeObjectURL(url);
				delete avatars[id];
			}
		}
		for (const world of list) {
			if (world.world_id in avatars) continue;
			avatars[world.world_id] = null;
			avatars[world.world_id] = world.image_asset
				? await fetchDefinitionImage('world', world.world_id)
				: null;
		}
	}

	/** Re-read one avatar after its image changed (revokes the stale object URL). */
	async function refreshAvatar(id: string): Promise<void> {
		const stale = avatars[id];
		if (stale) URL.revokeObjectURL(stale);
		delete avatars[id];
		avatars[id] = await fetchDefinitionImage('world', id);
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
			await attachDefinitionImage('world', editingId, blob);
			cropFile = null;
			await refreshAvatar(editingId);
			await reloadWorlds();
			onChanged?.();
		} catch (err) {
			imageError = message(err);
		} finally {
			uploading = false;
		}
	}

	/** Re-read the world catalog (the list's source of truth after an image write). */
	async function reloadWorlds(): Promise<void> {
		try {
			worlds = await listWorlds();
			void hydrateAvatars(worlds);
			error = '';
		} catch (err) {
			error = message(err);
		}
	}

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	function snapshot(value: WorldDraft | null): string {
		return value ? JSON.stringify(value) : '';
	}

	/** Seed the draft from a world wire (or an empty body for a new world). */
	function seed(world: World | null): void {
		draft = world
			? {
					name: world.name,
					description: world.description,
					scenario: world.scenario,
					cast: [...world.cast],
					book_ids: [...world.book_ids],
					provenance: world.provenance
				}
			: { name: '', description: '', scenario: '', cast: [], book_ids: [], provenance: {} };
		pristine = snapshot(draft);
	}

	// Dirty = the draft differs from its seeded / last-saved baseline. SlotForm
	// mutates the draft in place, so the JSON read tracks every nested slot.
	$effect(() => {
		const isDirty = draft !== null && snapshot(draft) !== pristine;
		if (isDirty !== dirty) {
			dirty = isDirty;
			onDirty?.(isDirty);
		}
	});

	function confirmDiscard(): boolean {
		if (!dirty) return true;
		if (typeof window === 'undefined') return true;
		return window.confirm('Discard unsaved changes in this world?');
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
		try {
			const [loadedWorlds, loadedPersonas, loadedBooks] = await Promise.all([
				listWorlds(),
				listPersonas(),
				listBooks()
			]);
			worlds = loadedWorlds;
			void hydrateAvatars(loadedWorlds);
			personas = loadedPersonas;
			books = loadedBooks;
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loaded = true;
		}
	}

	/** Insert or replace a world in the list (keeps the fold's latest-wins view). */
	function upsert(saved: World): void {
		const index = worlds.findIndex((world) => world.world_id === saved.world_id);
		worlds = index >= 0 ? worlds.map((world, i) => (i === index ? saved : world)) : [...worlds, saved];
		void hydrateAvatars(worlds);
	}

	function startEdit(world: World): void {
		editingId = world.world_id;
		seed(world);
		error = '';
	}

	function startCreate(): void {
		if (!confirmDiscard()) return;
		editingId = null;
		seed(null);
		error = '';
	}

	function select(world: World): void {
		if (editingId === world.world_id && draft) return;
		if (!confirmDiscard()) return;
		startEdit(world);
	}

	function cancel(): void {
		editingId = null;
		draft = null;
		pristine = snapshot(null);
		error = '';
	}

	async function save(): Promise<void> {
		if (!draft || busy) return;
		const invalid = form?.validate() ?? null;
		if (invalid) {
			error = invalid;
			return;
		}
		busy = true;
		try {
			const body = {
				name: String(draft.name ?? '').trim(),
				description: String(draft.description ?? ''),
				scenario: String(draft.scenario ?? ''),
				cast: [...((draft.cast as string[]) ?? [])],
				book_ids: [...((draft.book_ids as string[]) ?? [])],
				// Passthrough (D51): the seeded provenance object rides through
				// untouched — never reconstructed from the editable slots.
				provenance: (draft.provenance as Record<string, unknown>) ?? {}
			};
			const saved = editingId ? await updateWorld(editingId, body) : await createWorld(body);
			upsert(saved);
			editingId = saved.world_id;
			seed(saved);
			error = '';
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function remove(): Promise<void> {
		if (!editingId || busy) return;
		const name = worlds.find((world) => world.world_id === editingId)?.name ?? '';
		if (!window.confirm(`Delete world “${name}”? Running chats keep their snapshot.`)) return;
		busy = true;
		try {
			await deleteWorld(editingId);
			worlds = worlds.filter((world) => world.world_id !== editingId);
			cancel();
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** A world imported through the header controls: refresh + focus it. */
	async function onImported(worldId: string): Promise<void> {
		if (!confirmDiscard()) return;
		await load();
		const found = worlds.find((world) => world.world_id === worldId);
		if (found) startEdit(found);
		onChanged?.();
	}

	/** Open the card file picker (PNG with an embedded STv2 payload, or JSON). */
	function pickCard(): void {
		cardInput?.click();
	}

	function onCardFile(event: Event): void {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0] ?? null;
		input.value = '';
		if (!file) return;
		void importCardFile(file);
	}

	/**
	 * The typed card door (issue #40, owner ruling D3): any valid card becomes a
	 * WORLD — its prose is the world definition, a `character_book` is a bonus
	 * (materialized + signature-deduped server-side). `importWorldCard` never
	 * throws; failures resolve `{ok:false, error}` and render the backend's
	 * precise parser message verbatim inline. Success refreshes the catalog,
	 * focuses the imported world for editing and tells the shell.
	 */
	async function importCardFile(file: File): Promise<void> {
		if (importingCard) return;
		importingCard = true;
		cardError = '';
		cardStatus = '';
		try {
			const result = await importWorldCard(file);
			if (!result.ok || !result.world) {
				cardError = result.error || 'could not import that card';
				return;
			}
			const label = result.world.name || '';
			cardStatus = label
				? `Imported world “${label}”${result.book_id ? ' with its lorebook' : ''}`
				: 'Imported card as a world';
			await onImported(result.world.world_id);
		} catch (err) {
			cardError = message(err);
		} finally {
			importingCard = false;
		}
	}

	// `focusId` / `startNew` are applied once the list has loaded, and again when
	// the shell sends a NEW request. Keyed on `requestSeq` when present: a
	// repeatable door (New → Cancel → New) re-sends the same `startNew` boolean, so
	// only a NEW `seq` can re-fire. Absent `seq` the legacy value latch applies.
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
			startCreate();
			return;
		}
		if (focusId) {
			const world = worlds.find((item) => item.world_id === focusId);
			if (!world) return;
			if (!confirmDiscard()) return;
			appliedKey = key;
			startEdit(world);
		}
	});
</script>

<div class="world-pane">
	{#snippet rowAvatar(world: World)}
		<span class="row-avatar" aria-hidden="true">
			{#if avatarUrl(world.world_id)}
				<img src={avatarUrl(world.world_id)} alt="" />
			{:else}
				<span class="initials">{definitionInitials(world.name || world.world_id)}</span>
			{/if}
		</span>
	{/snippet}

	<header class="pane-head">
		<h3>Worlds</h3>
		<span class="spacer"></span>
		<DefinitionControls kind="world" id={editingId} onImported={(id) => void onImported(id)} />
		<button
			type="button"
			class="ghost"
			disabled={busy || importingCard}
			title="Import a character card PNG or JSON as a new world"
			onclick={pickCard}
		>
			{importingCard ? 'Importing…' : 'Import card (PNG/JSON)'}
		</button>
		<input
			bind:this={cardInput}
			class="file-input"
			type="file"
			accept=".png,.json,image/png,application/json"
			aria-label="Import character card PNG or JSON as a world"
			onchange={onCardFile}
		/>
	</header>

	{#if cardError}
		<p class="error" role="alert">{cardError}</p>
	{:else if cardStatus}
		<p class="status" role="status">{cardStatus}</p>
	{/if}

	<div class="pane-body">
		<div class="list-col">
			<EntityList
				items={worlds}
				selectedId={editingId}
				getId={(world) => world.world_id}
				getLabel={(world) => world.name || world.world_id}
				getMeta={(world) => `${world.cast.length} cast · ${world.book_ids.length} books`}
				onSelect={(world) => select(world)}
				onNew={startCreate}
				rowActions={rowAvatar}
				empty={loaded ? 'No worlds yet — create one.' : 'Loading worlds…'}
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
					<h4>{editingId ? 'Edit world' : 'New world'}</h4>
				</div>
				<div class="image-controls">
					<button type="button" class="ghost" onclick={pickImage} disabled={!editingId || uploading}>
						{uploading ? 'Uploading…' : 'Attach image'}
					</button>
					{#if !editingId}
						<span class="hint">Save the world first, then attach an image.</span>
					{/if}
				</div>
				<form
					class="world-form"
					onsubmit={(event) => {
						event.preventDefault();
						void save();
					}}
				>
					{#if error}
						<p class="error" role="alert">{error}</p>
					{/if}
					{#key editingId ?? 'new'}
						<SlotForm bind:this={form} slots={ENTITY_SLOTS.world} value={draft} {catalogs} />
					{/key}
					<div class="form-actions">
						{#if editingId}
							<button type="button" class="danger" disabled={busy} onclick={() => void remove()}>
								Delete
							</button>
						{/if}
						<button type="button" class="ghost" disabled={busy} onclick={cancel}>Cancel</button>
						<button type="submit" class="primary" disabled={busy}>
							{busy ? 'Saving…' : 'Save world'}
						</button>
					</div>
				</form>
			{:else}
				<p class="hint">Select a world to edit, or create a new one.</p>
			{/if}
		</div>
	</div>

	<input
		bind:this={imageInput}
		class="file-input"
		type="file"
		accept="image/*"
		aria-label="Attach world image"
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
	.world-pane {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.pane-head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.5rem;
	}
	.spacer {
		flex: 1;
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.pane-body {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		align-items: flex-start;
	}
	.list-col {
		flex: 0 0 30%;
		min-width: 11rem;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.editor-col {
		flex: 1 1 16rem;
		min-width: 0;
	}
	.world-form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	h4 {
		margin: 0;
		font-size: var(--font-ui);
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
	}
	.image-controls {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		margin-bottom: 0.2rem;
	}
	.image-controls .hint {
		margin: 0;
	}
	.file-input {
		display: none;
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
		width: 2.5rem;
		height: 2.5rem;
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
	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.4rem;
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
		margin-right: auto;
		border: 1px solid var(--line);
		background: none;
		color: var(--text-muted);
	}
	.danger:hover:not(:disabled) {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
