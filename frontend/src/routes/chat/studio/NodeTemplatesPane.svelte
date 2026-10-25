<script lang="ts">
	// Node-templates pane (W9.10, R21).
	//
	// The palette LIBRARY as an asset (`#lib/api/node_templates`): every node
	// template the graph editor can instantiate, grouped by its `klass` (the
	// engine's primitive vocabulary). R21 makes a template an ASSET, and NN7 says
	// asset definitions are edited in the studio with NO chat and no session — so
	// this pane is plain CRUD over `POST/PATCH/DELETE /api/node-templates`, props
	// only (`StudioPaneProps`), exactly like the persona/world panes.
	//
	// What the fields MEAN (they are not free-form decoration):
	//  · `implements` is the canonical node id whose BEHAVIOUR runs — the builder
	//    resolves a topology row's `template` to this factory, which is why the
	//    server refuses (422) an `implements` that is not a node contract. The
	//    datalist offers the behaviours the loaded library already implements;
	//    the server stays the authority and its reason is shown verbatim.
	//  · `interaction` / `step_id` are the PROMPT DOOR: the flow interaction or W6
	//    step that owns the prompt, so the palette can hand a door to the host.
	//  · `config` is node data (e.g. `{ "max_lanes": 4 }`). It is edited as JSON
	//    TEXT so a cleared or half-typed value stays visible instead of being
	//    coerced; the save-time check reports it like any other bad field.
	//
	// Deletes go through a confirm and keep the server's refusals visible: a
	// template a stored topology references is a 409 (nothing is written) and an
	// unrunnable row is a 422 — both surface inline, never swallowed.
	import { onMount } from 'svelte';
	import {
		NODE_TEMPLATE_KLASSES,
		createNodeTemplate,
		deleteNodeTemplate,
		getNodeTemplateLibrary,
		updateNodeTemplate,
		type NodeTemplate,
		type NodeTemplateKlass,
		type NodeTemplateWrite
	} from '#lib/api/node_templates';

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

	/** The editable form value; `config` stays text (see the header note). */
	interface TemplateDraft {
		name: string;
		klass: NodeTemplateKlass;
		implements: string;
		interaction: string;
		step_id: string;
		description: string;
		config: string;
	}

	let templates = $state<NodeTemplate[]>([]);
	/** The group order (the server's, falling back to the engine's vocabulary). */
	let klasses = $state<NodeTemplateKlass[]>([...NODE_TEMPLATE_KLASSES]);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	/** The row the list highlights (the one the editor was seeded from). */
	let selectedId = $state<string | null>(null);
	/** The open editor (`null` = the list only). */
	let draft = $state<TemplateDraft | null>(null);
	/** Template being edited (`null` = the draft is a new one). */
	let editingId = $state<string | null>(null);
	/** JSON snapshot of the draft at seed time; drift means unsaved edits. */
	let baseline = $state('');

	const dirty = $derived(
		draft !== null && (editingId === null || JSON.stringify(draft) !== baseline)
	);
	$effect(() => {
		onDirty?.(dirty);
	});

	/**
	 * The behaviours this library already implements — the datalist behind the
	 * `implements` field. It is a CONVENIENCE, not a closed set: the canonical
	 * vocabulary lives in the engine (`NODE_CONTRACTS`) and a 422 names it.
	 */
	const behaviourOptions = $derived(
		[...new Set(templates.map((template) => template.implements).filter(Boolean))].sort()
	);

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	function groupOf(klass: NodeTemplateKlass): NodeTemplate[] {
		return templates.filter((template) => template.klass === klass);
	}

	/** The prompt door as one line: the flow interaction and/or the W6 step. */
	function doorLabel(template: NodeTemplate): string {
		const parts: string[] = [];
		if (template.interaction) parts.push(template.interaction);
		if (template.step_id) parts.push(`step ${template.step_id}`);
		return parts.length ? parts.join(' · ') : '—';
	}

	/** A stored config as the editor's text (empty when there is none). */
	function configText(config: Record<string, unknown>): string {
		return Object.keys(config).length ? JSON.stringify(config, null, 2) : '';
	}

	function seedDraft(template: NodeTemplate): TemplateDraft {
		return {
			name: template.name,
			klass: template.klass,
			implements: template.implements,
			interaction: template.interaction ?? '',
			step_id: template.step_id ?? '',
			description: template.description,
			config: configText(template.config)
		};
	}

	// --- loading ---------------------------------------------------------------

	/** Re-read the library without touching the editor or the loading flag. */
	async function reload(): Promise<void> {
		const library = await getNodeTemplateLibrary();
		templates = library.templates;
		klasses = library.klasses.length ? library.klasses : [...NODE_TEMPLATE_KLASSES];
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try {
			await reload();
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	// --- editor openers --------------------------------------------------------

	function startCreate(klass: NodeTemplateKlass = NODE_TEMPLATE_KLASSES[0]): void {
		selectedId = null;
		editingId = null;
		draft = {
			name: '',
			klass,
			implements: '',
			interaction: '',
			step_id: '',
			description: '',
			config: ''
		};
		baseline = JSON.stringify(draft);
		error = '';
	}

	/** Klass picker: the select gives a plain string; the draft owns the narrow type. */
	function setKlass(value: string): void {
		if (!draft) return;
		draft.klass = value as NodeTemplateKlass;
	}

	function startEdit(template: NodeTemplate): void {
		selectedId = template.id;
		editingId = template.id;
		draft = seedDraft(template);
		baseline = JSON.stringify(draft);
		error = '';
	}

	function cancelForm(): void {
		draft = null;
		editingId = null;
		error = '';
	}

	// --- writes ----------------------------------------------------------------

	/** Draft -> wire body, or the reason it cannot be sent. */
	function writeOf(source: TemplateDraft): { write: NodeTemplateWrite } | { problem: string } {
		const name = source.name.trim();
		if (!name) return { problem: 'a node template needs a non-empty name' };
		const implementsId = source.implements.trim();
		if (!implementsId) {
			return { problem: '`implements` must name the canonical node whose behaviour runs' };
		}
		let config: Record<string, unknown> = {};
		const text = source.config.trim();
		if (text) {
			let parsed: unknown;
			try {
				parsed = JSON.parse(text);
			} catch {
				return { problem: 'config is not valid JSON' };
			}
			if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
				return { problem: 'config must be a JSON object, e.g. { "max_lanes": 4 }' };
			}
			config = parsed as Record<string, unknown>;
		}
		return {
			write: {
				name,
				klass: source.klass,
				implements: implementsId,
				// Empty optional fields are sent as `''` (the server reads that as
				// "unset"), never omitted: an omitted field keeps the stored value.
				interaction: source.interaction.trim(),
				step_id: source.step_id.trim(),
				config,
				description: source.description
			}
		};
	}

	async function save(): Promise<void> {
		if (!draft || busy) return;
		const result = writeOf(draft);
		if ('problem' in result) {
			error = result.problem;
			return;
		}
		busy = true;
		error = '';
		try {
			const savedId = editingId;
			const saved = savedId
				? await updateNodeTemplate(savedId, result.write)
				: await createNodeTemplate(result.write);
			await reload();
			selectedId = saved.id;
			draft = null;
			editingId = null;
			onChanged?.();
		} catch (err) {
			// The API's own reason (422 unrunnable row / 404 gone), verbatim.
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function remove(target: NodeTemplate): Promise<void> {
		const label = target.name || target.id;
		const copy =
			`Delete the node template “${label}”? The palette stops offering it. ` +
			'A stored topology that still references it blocks the delete (409) — rewire that first. ' +
			'This appends a tombstone event (D10); the definition stays in the log.';
		if (typeof window === 'undefined' || !window.confirm(copy)) return;
		busy = true;
		error = '';
		try {
			await deleteNodeTemplate(target.id);
			if (selectedId === target.id) selectedId = null;
			if (editingId === target.id) cancelForm();
			await reload();
			onChanged?.();
		} catch (err) {
			// 409 (a stored topology references it) / 422 (unrunnable row).
			error = message(err);
		} finally {
			busy = false;
		}
	}

	// --- host requests (`focusId` edits a row, `startNew` opens the create form) --
	// Keyed on `requestSeq` when present so a repeated door click re-fires; a
	// plain prop change still applies once per value (standalone pane).
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
		if (!focusId) return;
		const found = templates.find((template) => template.id === focusId);
		if (!found) return;
		appliedKey = key;
		startEdit(found);
	});
</script>

<div class="templates-pane">
	<header class="pane-head">
		<h2 class="pane-title">Node templates</h2>
		<span class="pane-meta">{templates.length} in the library</span>
		<button type="button" class="primary" onclick={() => startCreate()}>+ New template</button>
	</header>

	<p class="hint">
		The library behind the graph editor's palette. A step built from a template runs the behaviour
		named in <code>Implements</code> — a template is a label and some settings on top of a real
		step, never new code. Edits here change what the editor offers; the engine rebuilds the flow
		on its next turn.
	</p>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	<div class="pane-body">
		<div class="list-col">
			{#if loading}
				<p class="hint">Loading node templates…</p>
			{:else if templates.length === 0}
				<p class="hint">No templates in the library — use “+ New template”.</p>
			{:else}
				{#each klasses as klass (klass)}
					{@const rows = groupOf(klass)}
					{#if rows.length}
						<section class="group" aria-labelledby={`klass-${klass}`}>
							<header class="group-head">
								<h3 id={`klass-${klass}`}>{klass}</h3>
								<button type="button" class="ghost" onclick={() => startCreate(klass)}>
									+ New
								</button>
							</header>
							<ul class="rows">
								{#each rows as template (template.id)}
									<li class="row" class:selected={selectedId === template.id}>
										<button
											type="button"
											class="row-main"
											title="Edit this template"
											aria-current={selectedId === template.id ? 'true' : undefined}
											onclick={() => startEdit(template)}
										>
											<span class="row-label">{template.name}</span>
											<span class="row-meta">
												<code>{template.implements || '—'}</code>
												<span class="door">door: {doorLabel(template)}</span>
												{#if template.builtin}<span class="badge">builtin</span>{/if}
											</span>
										</button>
										<button
											type="button"
											class="danger"
											aria-label={`Delete ${template.name}`}
											disabled={busy}
											onclick={() => void remove(template)}
										>
											Delete
										</button>
									</li>
								{/each}
							</ul>
						</section>
					{/if}
				{/each}
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
						<h3 class="editor-title">{editingId ? 'Edit template' : 'New template'}</h3>

						<label class="field">
							<span>Name</span>
							<input bind:value={draft.name} placeholder="Narrator" spellcheck="false" />
						</label>

						<label class="field">
							<span>Klass</span>
							<select
								value={draft.klass}
								onchange={(event) => setKlass(event.currentTarget.value)}
							>
								{#each NODE_TEMPLATE_KLASSES as klass (klass)}
									<option value={klass}>{klass}</option>
								{/each}
							</select>
						</label>

						<label class="field">
							<span>Implements</span>
							<input
								bind:value={draft.implements}
								list="node-template-implements"
								placeholder="narrate"
								spellcheck="false"
								title="The built-in step this template runs."
							/>
						</label>
						<datalist id="node-template-implements">
							{#each behaviourOptions as option (option)}
								<option value={option}></option>
							{/each}
						</datalist>

						<label class="field">
							<span>Prompt door · interaction</span>
							<input bind:value={draft.interaction} placeholder="optional" spellcheck="false" />
						</label>

						<label class="field">
							<span>Prompt door · step id</span>
							<input bind:value={draft.step_id} placeholder="optional" spellcheck="false" />
						</label>

						<label class="field">
							<span>Description</span>
							<textarea
								rows="2"
								bind:value={draft.description}
								placeholder="what this node is for (shown in the palette tooltip)"
							></textarea>
						</label>

						<label class="field">
							<span>Config (JSON object)</span>
							<textarea
								rows="3"
								bind:value={draft.config}
								placeholder={'{ "max_lanes": 4 }'}
								spellcheck="false"
							></textarea>
						</label>

						{#if editingId && templates.some((t) => t.id === editingId && t.builtin)}
							<p class="hint">
								This is a built-in seed — it edits and deletes like any other template.
							</p>
						{/if}

						<div class="actions">
							{#if editingId}
								{@const target = templates.find((t) => t.id === editingId)}
								{#if target}
									<button
										type="button"
										class="danger"
										disabled={busy}
										onclick={() => void remove(target)}
									>
										Delete
									</button>
								{/if}
							{/if}
							<span class="spacer"></span>
							<button type="button" class="ghost" onclick={cancelForm} disabled={busy}>
								Cancel
							</button>
							<button type="submit" class="primary" disabled={busy}>
								{busy ? 'Saving…' : editingId ? 'Save template' : 'Create template'}
							</button>
						</div>
					</form>
				{/key}
			{:else}
				<p class="hint">Select a template to edit it, or use “+ New template”.</p>
			{/if}
		</div>
	</div>
</div>

<style>
	.templates-pane {
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
	.pane-meta {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.pane-body {
		display: flex;
		flex-wrap: wrap;
		gap: 0.7rem;
		align-items: flex-start;
	}
	.list-col {
		flex: 1 1 15rem;
		min-width: 12rem;
	}
	.editor-col {
		flex: 1 1 16rem;
		min-width: 0;
	}
	.group {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding-top: 0.45rem;
		border-top: 1px solid var(--line);
	}
	.group-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.group-head h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		padding: 0.15rem 0.25rem;
		border: 1px solid transparent;
		border-radius: 6px;
	}
	.row.selected {
		border-color: var(--accent-line);
		background: var(--accent-soft);
	}
	.row-main {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.1rem;
		padding: 0.15rem 0.2rem;
		border: none;
		border-radius: 5px;
		background: none;
		color: var(--text);
		text-align: left;
		cursor: pointer;
	}
	.row-main:hover {
		background: var(--bg-hover);
	}
	.row-label {
		font-size: var(--font-ui);
	}
	.row-meta {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		flex-wrap: wrap;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.row-meta code {
		padding: 0.02rem 0.25rem;
		border: 1px solid var(--line);
		border-radius: 4px;
	}
	.badge {
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 0.02rem 0.3rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.editor {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.editor-title {
		margin: 0;
		font-size: var(--font-ui);
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.field > span {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	textarea {
		resize: vertical;
		min-height: 2.4rem;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding-top: 0.5rem;
		border-top: 1px solid var(--line);
	}
	.spacer {
		flex: 1;
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
	.primary:hover:not(:disabled) {
		background: var(--accent);
		color: var(--on-accent);
	}
	.ghost {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text);
		padding: 0.15rem 0.5rem;
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
	}
	.danger {
		border: 1px solid var(--line);
		background: none;
		color: var(--text-muted);
		padding: 0.2rem 0.45rem;
	}
	.danger:hover:not(:disabled) {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
