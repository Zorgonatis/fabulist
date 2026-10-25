<script lang="ts">
	// Image Gen pane (COMFY-PLAN Wave 4, lane V2) — a definition CRUD pane hosted
	// as an optional, collapsed-by-default tab in the Connections drawer (it left
	// the Design Studio in the 2026-10-15 W2 lane).
	//
	// Mirrors `BookPane.svelte`: a catalog list + an editor, with the same
	// `StudioPaneProps` contract (`focusId` / `startNew` / `requestSeq` /
	// `onChanged` / `onDirty`) so a host can mount it unchanged. Definitions are
	// session-free assets (non-negotiable 7): all I/O goes through
	// `#lib/api/imagegen` (API_BASE, never a relative path — project gotcha) and
	// needs NO chat open.
	//
	// The editor holds a name, the raw ComfyUI API-format workflow JSON (validated
	// client-side with a live message), and the prompt template. The read-only
	// "detected parameters" summary parses the workflow JSON client-side through
	// `detectWorkflowParameters`, which mirrors the node classes the engine maps
	// (`KSampler`/`KSamplerAdvanced`/`ClownsharKSampler_Beta`, `CLIPTextEncode`,
	// `EmptyLatentImage`).
	import { onMount } from 'svelte';
	import type { ImageGenDefinition } from '#lib/api/imagegen';
	import {
		createImageGenDefinition,
		deleteImageGenDefinition,
		detectWorkflowParameters,
		listImageGenDefinitions
	} from '#lib/api/imagegen';

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

	/** The editable draft body. */
	interface Draft {
		name: string;
		workflowText: string;
		promptTemplate: string;
	}

	let definitions = $state<ImageGenDefinition[]>([]);
	/** Selected catalog id; `null` with `creating` false means no editor. */
	let selectedId = $state<string | null>(null);
	let creating = $state(false);
	let draft = $state<Draft>({ name: '', workflowText: '', promptTemplate: '' });
	/** The serialized draft the current editor loaded — the dirty baseline. */
	let baseline = $state(JSON.stringify({ name: '', workflowText: '', promptTemplate: '' }));
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	let status = $state('');

	/** The definition the editor is editing (null while creating / nothing selected). */
	const editingDefinition = $derived(
		selectedId ? (definitions.find((item) => item.imagegen_id === selectedId) ?? null) : null
	);
	/** True while the editor is open (existing definition or a create draft). */
	const editing = $derived(creating || selectedId !== null);
	const dirty = $derived(serialize() !== baseline);

	/** Live parse of the workflow textarea: `{workflow, error}`. */
	const parsedWorkflow = $derived.by(
		(): { workflow: Record<string, unknown> | null; error: string } => {
			const text = draft.workflowText.trim();
			if (!text) return { workflow: null, error: 'Workflow JSON is empty.' };
			try {
				const value: unknown = JSON.parse(text);
				if (!value || typeof value !== 'object' || Array.isArray(value)) {
					return { workflow: null, error: 'Workflow must be a JSON object.' };
				}
				return { workflow: value as Record<string, unknown>, error: '' };
			} catch (err) {
				return { workflow: null, error: `Invalid JSON: ${message(err)}` };
			}
		}
	);

	/** The detected adjustable map, mirrored from the engine's node classes. */
	const detected = $derived(detectWorkflowParameters(parsedWorkflow.workflow ?? {}));
	/** Read-only summary rows for the seed / positive / negative adjustables. */
	const detectedRows = $derived([
		{ label: 'Seed', target: detected.seed },
		{ label: 'Positive', target: detected.positive },
		{ label: 'Negative', target: detected.negative }
	]);

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** Serialized draft — the dirty-comparison key. */
	function serialize(): string {
		return JSON.stringify(draft);
	}

	/** Pretty workflow text for the textarea (empty when there is no workflow). */
	function formatWorkflow(workflow: Record<string, unknown>): string {
		return Object.keys(workflow).length ? JSON.stringify(workflow, null, 2) : '';
	}

	/** Load a definition into the editable draft (also the post-save reseed). */
	function applyLoaded(definition: ImageGenDefinition): void {
		draft = {
			name: definition.name,
			workflowText: formatWorkflow(definition.workflow),
			promptTemplate: definition.prompt_template
		};
		baseline = serialize();
		status = '';
	}

	/** Replace or append a definition in the catalog with the persisted one. */
	function upsert(definition: ImageGenDefinition): void {
		const index = definitions.findIndex((item) => item.imagegen_id === definition.imagegen_id);
		definitions =
			index >= 0
				? definitions.map((item, i) => (i === index ? definition : item))
				: [...definitions, definition];
	}

	function confirmDiscard(): boolean {
		if (!dirty) return true;
		if (typeof window === 'undefined') return true;
		return window.confirm('Discard unsaved changes to this image gen definition?');
	}

	function select(definition: ImageGenDefinition): void {
		if (!confirmDiscard()) return;
		selectedId = definition.imagegen_id;
		creating = false;
		applyLoaded(definition);
		error = '';
	}

	/** Seed a fresh create draft without a dirty guard (mount / revert paths). */
	function resetCreate(): void {
		selectedId = null;
		creating = true;
		draft = { name: '', workflowText: '', promptTemplate: '' };
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
		if (editingDefinition) applyLoaded(editingDefinition);
	}

	async function save(): Promise<void> {
		if (busy || !editing) return;
		const name = draft.name.trim();
		if (!name) {
			error = 'Give the definition a name.';
			return;
		}
		const workflow = parsedWorkflow.workflow;
		if (!workflow) {
			error = parsedWorkflow.error || 'Workflow JSON is invalid.';
			return;
		}
		busy = true;
		try {
			const wasCreate = creating;
			const saved = await createImageGenDefinition({
				imagegen_id: wasCreate ? undefined : (selectedId ?? undefined),
				name,
				workflow,
				prompt_template: draft.promptTemplate,
				provenance: editingDefinition?.provenance ?? {}
			});
			upsert(saved);
			selectedId = saved.imagegen_id;
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

	async function remove(definition: ImageGenDefinition): Promise<void> {
		const label = definition.name || definition.imagegen_id;
		// A delete appends a tombstone; the definition stays in the log (D10).
		const confirmed = window.confirm(
			`Delete image gen definition “${label}”? This appends a tombstone event — the definition stays in the log (D10).`
		);
		if (!confirmed) return;
		busy = true;
		try {
			await deleteImageGenDefinition(definition.imagegen_id);
			definitions = definitions.filter((item) => item.imagegen_id !== definition.imagegen_id);
			if (selectedId === definition.imagegen_id || creating) {
				selectedId = null;
				creating = false;
				draft = { name: '', workflowText: '', promptTemplate: '' };
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
			definitions = await listImageGenDefinitions();
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
			loaded = true;
		}
	}

	// --- focus / startNew (StudioPaneProps) ------------------------------------
	// Applied once the catalog has loaded, and again whenever the shell sends a NEW
	// request. Keyed on `requestSeq` when present; absent `seq` the legacy value
	// latch applies. The no-request key (`initial`) selects the first definition.
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
		const target =
			(fid && definitions.find((item) => item.imagegen_id === fid)) || definitions[0] || null;
		if (target) {
			appliedKey = key;
			select(target);
		}
	});

	/** Push the dirty flag up so the shell can guard tab switches / close. */
	$effect(() => {
		onDirty?.(dirty);
	});
</script>

<div class="pane">
	<header class="pane-head">
		<span class="pane-title">Image Gen</span>
		{#if dirty}<span class="dirty" aria-live="polite">● unsaved</span>{/if}
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}
	{#if status}
		<p class="status" role="status">{status}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading image gen definitions…</p>
	{:else}
		<div class="cols">
			<section class="list-col" aria-label="Image gen definitions">
				<div class="list-head">
					<span class="count">
						{definitions.length}
						{definitions.length === 1 ? 'definition' : 'definitions'}
					</span>
					<button type="button" class="ghost small" onclick={startCreate}>+ New</button>
				</div>
				{#if definitions.length === 0}
					<p class="hint">No image gen definitions yet — use + New to add one.</p>
				{:else}
					<ul class="list">
						{#each definitions as definition (definition.imagegen_id)}
							<li>
								<button
									type="button"
									class="list-item"
									class:active={definition.imagegen_id === selectedId}
									onclick={() => select(definition)}
								>
									<span class="item-label">{definition.name || definition.imagegen_id}</span>
									<span class="item-meta">
										{Object.keys(definition.workflow).length} nodes
									</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			<section class="editor-col" aria-label="Image gen definition editor">
				{#if editing}
					<form
						class="definition-form"
						onsubmit={(event) => {
							event.preventDefault();
							void save();
						}}
					>
						<h3>{creating ? 'New definition' : 'Edit definition'}</h3>

						<label class="field">
							<span>Name</span>
							<input bind:value={draft.name} placeholder="Portrait (SDXL)" />
						</label>

						<label class="field">
							<span>Prompt template</span>
							<textarea
								bind:value={draft.promptTemplate}
								rows="3"
								placeholder="The generator agent's system prompt — renders the user request into positive/negative prompt text."
							></textarea>
						</label>

						<label class="field">
							<span>Workflow JSON (ComfyUI API format)</span>
							<textarea
								bind:value={draft.workflowText}
								rows="10"
								spellcheck="false"
								placeholder={'{\n  "3": { "class_type": "KSampler", "inputs": { … } }\n}'}
							></textarea>
						</label>
						{#if parsedWorkflow.error}
							<p class="field-error" role="alert">{parsedWorkflow.error}</p>
						{/if}

						<div class="detected">
							<span class="detected-title">Detected parameters</span>
							{#each detectedRows as row (row.label)}
								<div class="detected-row">
									<span class="detected-label">{row.label}</span>
									<span class="detected-value">
										{row.target ? `node ${row.target.node_id} · ${row.target.input}` : '—'}
									</span>
								</div>
							{/each}
							<div class="detected-row">
								<span class="detected-label">Resolution</span>
								<span class="detected-value">
									{detected.resolution
										? `node ${detected.resolution.node_id} · ${detected.resolution.width_input}/${detected.resolution.height_input}`
										: '—'}
								</span>
							</div>
						</div>

						<div class="form-actions">
							{#if editingDefinition}
								<button type="button" class="danger" onclick={() => void remove(editingDefinition)}>
									Delete
								</button>
							{/if}
							<span class="spacer"></span>
							<button type="button" class="ghost" disabled={!dirty || busy} onclick={revert}>
								Cancel
							</button>
							<button type="submit" class="primary" disabled={busy || !!parsedWorkflow.error}>
								{busy ? 'Saving…' : 'Save'}
							</button>
						</div>
					</form>
				{:else}
					<p class="hint">Select a definition to edit, or create one.</p>
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
	.list-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		margin-bottom: 0.4rem;
	}
	.count {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.list-item {
		width: 100%;
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: 0.4rem;
		text-align: left;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text);
		padding: 0.35rem 0.5rem;
		cursor: pointer;
	}
	.list-item:hover {
		background: var(--bg-hover);
	}
	.list-item.active {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.item-label {
		font-size: var(--font-ui);
	}
	.item-meta {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.definition-form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
		color: var(--text);
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
	.field textarea {
		font-family: var(--font-mono, monospace);
		font-size: var(--font-small);
	}
	.field-error {
		margin: 0;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
	.detected {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
		padding: 0.5rem 0.6rem;
	}
	.detected-title {
		font-size: var(--font-micro);
		font-weight: 600;
		color: var(--text-muted);
	}
	.detected-row {
		display: flex;
		justify-content: space-between;
		gap: 0.5rem;
		font-size: var(--font-small);
	}
	.detected-label {
		color: var(--text-muted);
	}
	.detected-value {
		color: var(--text);
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
