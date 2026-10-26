<script lang="ts">
	// Rules section (W6.6, PLAN §2.3): the ONE directive control.
	//
	// Presentational + pure callbacks: the ordered directive ROWS live on the
	// active preset (`draft.directives`) and the parent owns every mutation
	// (materialize-on-first-edit, reorder, delete). This component renders the
	// list and hands each interaction back through `on*` props — it never talks
	// to the API itself, so there is exactly one write path (the drawer's Save).
	//
	// Each row is a rule: title · one-line description · a SINGLE enable toggle
	// bound to the row's `enabled` (the one source the engine reads). NOTHING is
	// protected — no lock badge, no refusal.
	import type { PromptDirective } from '#lib/api/prompts';

	let {
		rows,
		inherited,
		factoryError,
		factoryCount,
		catalog,
		editingId,
		dragOverIndex,
		knownNames,
		unknownVars,
		onBack,
		onAdd,
		onMove,
		onDelete,
		onToggle,
		onPatch,
		onToggleEdit,
		/** Issue #39: the persisted Anti Injection product setting (default OFF). */
		antiInjection,
		antiInjectionBusy,
		antiInjectionError,
		onToggleAntiInjection,
		onDragStart,
		onDragOver,
		onDragEnd,
		onDrop,
		onFocusField
	}: {
		rows: PromptDirective[];
		/** The preset stores NO directive rows — the list shown is inherited. */
		inherited: boolean;
		/** Factory-row fetch failure (only meaningful while inherited). */
		factoryError: string;
		/** How many factory rows loaded (0 => still loading / failed). */
		factoryCount: number;
		/** Directive id + label catalog, for the degrade hint. */
		catalog: { id: string; label: string }[];
		editingId: string | null;
		dragOverIndex: number | null;
		knownNames: string;
		unknownVars: string[];
		onBack: () => void;
		onAdd: () => void;
		onMove: (from: number, to: number) => void;
		onDelete: (id: string) => void;
		onToggle: (id: string) => void;
		/** Current Anti Injection feature flag (engine renders the clause iff on). */
		antiInjection: boolean;
		antiInjectionBusy: boolean;
		antiInjectionError: string;
		onToggleAntiInjection: () => void;
		onPatch: (id: string, patch: Partial<PromptDirective>) => void;
		onToggleEdit: (id: string) => void;
		onDragStart: (index: number, event: DragEvent) => void;
		onDragOver: (index: number, event: DragEvent) => void;
		onDragEnd: () => void;
		onDrop: (index: number, event: DragEvent) => void;
		/** Register a field as the variable-insertion target. */
		onFocusField: (
			apply: (value: string) => void,
			el: HTMLInputElement | HTMLTextAreaElement
		) => void;
	} = $props();

	/** Directive editor placeholder (literal: `{{...}}` inside a Svelte attribute
	 * is parsed as an expression). */
	const DIRECTIVE_CONTENT_PLACEHOLDER = 'Rule text — type {{name}} to insert a variable';

	/** One-line description shown under the title (never rendered into the prompt). */
	function rowDescription(row: PromptDirective): string {
		return row.description?.trim() || 'No description yet.';
	}
</script>

<div class="directives">
	<div class="section-head">
		<button type="button" class="back" onclick={onBack}>← Steps</button>
		<h3>Rules</h3>
		<button type="button" class="btn" onclick={onAdd}>+ add rule</button>
	</div>
	<p class="sub">
		The rules the narrator is given, in the order it reads them. Edit a row to change what the
		narrator is told.
	</p>

	<!-- Issue #39 / #77: the Anti Injection product setting. It is NOT a directive
	     row: the clause's text always lives in the row below (content home), and
	     this switch gates whether the engine RENDERS the instruction framing into
	     the prompt — the clause, the guard line, both <preset_def>/<preset_runtime>
	     bounds and the framing's bound paragraph. Content framing (the per-turn
	     envelope tags, the lane brief tags) renders either way. -->
	<div class="anti-injection" class:on={antiInjection}>
		<label class="ai-switch">
			<input
				type="checkbox"
				checked={antiInjection}
				disabled={antiInjectionBusy}
				aria-describedby="anti-injection-hint"
				onchange={onToggleAntiInjection}
			/>
			<span class="ai-name">Anti Injection</span>
			<span class="badge ai-state">{antiInjection ? 'on' : 'off'}</span>
		</label>
		<p class="hint" id="anti-injection-hint">
			Reminds the narrator that the engine's notes are the engine's words, not the player's.
			Off: that line, the trust bounds and their guard are left out of the prompt entirely.
		</p>
		{#if antiInjectionError}
			<p class="error" role="alert">{antiInjectionError}</p>
		{/if}
	</div>
	{#if inherited}
		<p class="inherited-note">Inherited from the shipped defaults — edit to make them yours.</p>
		{#if factoryError}
			<p class="error" role="alert">{factoryError}</p>
			{#if catalog.length}
				<p class="hint">
					Known default rules: {catalog.map((d) => d.label).join(', ')}
				</p>
			{/if}
		{:else if factoryCount === 0}
			<p class="hint">Loading the built-in rules…</p>
		{/if}
	{/if}

	<ol class="modules" class:inherited>
		{#each rows as row, index (row.id)}
			<li
				class:drop-target={dragOverIndex === index}
				class:disabled={!row.enabled}
				ondragover={(event) => onDragOver(index, event)}
				ondrop={(event) => onDrop(index, event)}
			>
				<button
					type="button"
					class="handle"
					draggable="true"
					aria-label="Drag to reorder"
					title="Drag to reorder"
					ondragstart={(event) => onDragStart(index, event)}
					ondragend={onDragEnd}
				>
					⋮⋮
				</button>
				<input
					type="checkbox"
					checked={row.enabled}
					aria-label="Enable rule"
					onchange={() => onToggle(row.id)}
				/>
				<span class="rule-title">
					<span class="mod-name">{row.name}</span>
					{#if row.id === 'engine_context' && !antiInjection}
						<span class="rule-desc gated">
							Left out of the prompt while Anti Injection is off
						</span>
					{:else}
						<span class="rule-desc">{rowDescription(row)}</span>
					{/if}
				</span>
				<span class="badge tier-badge" title="Trim order {row.priority}{row.core ? ' · core' : ''}">
					group {row.tier}
				</span>
				<div class="row-actions">
					<button
						type="button"
						class="icon"
						title="Move up"
						aria-label="Move up"
						disabled={index === 0}
						onclick={() => onMove(index, index - 1)}
					>
						↑
					</button>
					<button
						type="button"
						class="icon"
						title="Move down"
						aria-label="Move down"
						disabled={index === rows.length - 1}
						onclick={() => onMove(index, index + 1)}
					>
						↓
					</button>
					<button
						type="button"
						class="icon"
						title="Edit rule"
						aria-label="Edit rule"
						aria-expanded={editingId === row.id}
						onclick={() => onToggleEdit(row.id)}
					>
						✎
					</button>
					<button
						type="button"
						class="icon danger"
						title="Delete rule"
						aria-label="Delete rule"
						onclick={() => onDelete(row.id)}
					>
						×
					</button>
				</div>

				{#if editingId === row.id}
					<div class="editor">
						<div class="directive-fields">
							<label class="surface-field inline">
								<span>Name</span>
								<input
									type="text"
									class="surface-input"
									aria-label="Rule name"
									value={row.name}
									oninput={(event) => onPatch(row.id, { name: event.currentTarget.value })}
								/>
							</label>
							<label class="surface-field inline">
								<span>Description</span>
								<input
									type="text"
									class="surface-input"
									aria-label="Rule description"
									placeholder="Shown here in settings, never sent to the narrator"
									value={row.description}
									oninput={(event) => onPatch(row.id, { description: event.currentTarget.value })}
								/>
							</label>
							<label class="surface-field inline">
								<span>Tier</span>
								<input
									type="number"
									class="surface-input depth"
									aria-label="Rule group"
									min="0"
									max="4"
									value={row.tier}
									oninput={(event) =>
										onPatch(row.id, { tier: Number(event.currentTarget.value) || 0 })}
								/>
							</label>
							<label class="surface-field inline">
								<span>Priority</span>
								<input
									type="number"
									class="surface-input depth"
									aria-label="Trim order"
									title="Trim order when the prompt runs long (lower goes first)"
									value={row.priority}
									oninput={(event) =>
										onPatch(row.id, { priority: Number(event.currentTarget.value) || 0 })}
								/>
							</label>
						</div>
						<textarea
							class="content"
							rows="6"
							aria-label="Rule text"
							placeholder={DIRECTIVE_CONTENT_PLACEHOLDER}
							value={row.content}
							onfocus={(event) =>
								onFocusField((v) => onPatch(row.id, { content: v }), event.currentTarget)}
							oninput={(event) => onPatch(row.id, { content: event.currentTarget.value })}
						></textarea>
					</div>
				{/if}
			</li>
		{:else}
			<li class="empty-row">No rules yet.</li>
		{/each}
	</ol>

	{#if unknownVars.length}
		<p class="hint"><span class="warn">Unknown placeholders: {unknownVars.join(', ')}</span></p>
	{/if}
	<p class="hint">Known variables: {knownNames}. Unknown placeholders are left as written.</p>
</div>

<style>
	.directives {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	h3 {
		margin: 0.4rem 0 0;
		font-size: 0.66rem;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.back {
		font: inherit;
		font-size: 0.7rem;
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-strong);
		cursor: pointer;
	}
	.btn {
		font: inherit;
		font-size: 0.72rem;
		font-weight: 600;
		padding: 0.3rem 0.6rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
		white-space: nowrap;
	}
	.btn:hover:not(:disabled) {
		background: var(--accent-soft);
	}
	.sub {
		margin: 0.1rem 0 0;
		font-size: 0.68rem;
		color: var(--text-dim);
	}
	.anti-injection {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		margin: 0.35rem 0 0.1rem;
		padding: 0.45rem 0.55rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
	}
	.anti-injection.on {
		border-color: var(--accent-line);
	}
	.ai-switch {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		cursor: pointer;
	}
	.ai-switch input[type='checkbox'] {
		width: 0.95rem;
		height: 0.95rem;
		accent-color: var(--accent);
	}
	.ai-name {
		font-size: 0.82rem;
		font-weight: 600;
	}
	.ai-state {
		color: var(--text-muted);
	}
	.rule-desc.gated {
		color: var(--warn);
	}
	.inherited-note {
		margin: 0 0 0.5rem;
		font-size: 0.72rem;
		color: var(--warn);
	}
	.error {
		margin: 0.5rem 1rem 0;
		padding: 0.45rem 0.6rem;
		font-size: 0.75rem;
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.hint {
		margin: 0.1rem 0 0;
		font-size: 0.66rem;
		color: var(--text-dim);
	}
	.hint .warn {
		margin-left: 0.35rem;
		color: var(--warn);
	}
	.modules {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.modules > li {
		display: grid;
		grid-template-columns: auto auto 1fr auto auto;
		align-items: start;
		gap: 0.35rem;
		padding: 0.35rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.modules.inherited > li {
		opacity: 0.62;
		border-style: dashed;
	}
	.modules > li.drop-target {
		border-color: var(--accent-strong);
	}
	.modules > li.disabled {
		opacity: 0.55;
	}
	.modules.inherited > li.disabled {
		opacity: 0.4;
	}
	.handle {
		font: inherit;
		font-size: 0.8rem;
		line-height: 1;
		padding: 0.2rem 0.25rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-inset);
		color: var(--text-muted);
		cursor: grab;
	}
	.handle:active {
		cursor: grabbing;
	}
	.modules input[type='checkbox'] {
		width: 0.95rem;
		height: 0.95rem;
		margin-top: 0.15rem;
		accent-color: var(--accent);
	}
	.rule-title {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		min-width: 0;
	}
	.mod-name {
		font-size: 0.82rem;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.rule-desc {
		font-size: 0.66rem;
		color: var(--text-dim);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.badge {
		font-size: 0.62rem;
		padding: 0.1rem 0.4rem;
		border-radius: 999px;
		background: var(--bg-inset);
		color: var(--text-muted);
		border: 1px solid var(--line);
		white-space: nowrap;
	}
	.badge.tier-badge {
		color: var(--text-muted);
	}
	.row-actions {
		display: flex;
		gap: 0.05rem;
		justify-self: end;
	}
	.icon {
		font: inherit;
		font-size: 0.75rem;
		line-height: 1;
		width: 1.3rem;
		height: 1.3rem;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.icon:hover:not(:disabled) {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.icon:disabled {
		opacity: 0.35;
		cursor: default;
	}
	.icon.danger:hover {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.editor {
		grid-column: 1 / -1;
	}
	.directive-fields {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	.directive-fields .surface-field.inline span {
		width: 4rem;
	}
	.surface-input {
		font: inherit;
		font-size: 0.72rem;
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		padding: 0.3rem 0.45rem;
	}
	.surface-input:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.surface-field {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		font-size: 0.68rem;
		color: var(--text-muted);
	}
	.surface-field.inline {
		flex-direction: row;
		align-items: center;
		gap: 0.4rem;
	}
	.surface-field.inline span {
		width: 7rem;
		flex: none;
	}
	.content {
		width: 100%;
		resize: vertical;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.72rem;
		line-height: 1.4;
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		padding: 0.4rem 0.5rem;
	}
	.content:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.empty-row {
		display: block !important;
		font-size: 0.75rem;
		color: var(--text-dim);
		border-style: dashed !important;
	}
</style>
