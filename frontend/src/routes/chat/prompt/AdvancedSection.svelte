<script lang="ts">
	// Advanced token text (W6.7, PLAN §2.4): the D27 surfaces, relabelled.
	//
	// "Surfaces" was unsearchable jargon for "the token text the engine
	// substitutes outside the step blocks". Four labelled, described groups:
	// component library reference · recalled reference wrapper · history
	// projection · legacy directive text. Presentational + pure callbacks — the
	// parent owns the draft (`draft.surfaces`) and the single Save path.
	import type {
		PromptHistorySurfaces,
		PromptRegexRow,
		PromptSurfaces
	} from '#lib/api';

	let {
		surfaces,
		editingDefault,
		presetName,
		actionBusy,
		saving,
		legacyOverrideCount,
		unknownVars,
		libraryDocChars,
		regexRows,
		historyKnobs,
		onBack,
		onCommitToDefault,
		onResetToFactory,
		onMigrateDirectives,
		setLibraryDoc,
		setRecalled,
		setRecalledHeader,
		setRegexRow,
		addRegexRow,
		removeRegexRow,
		setHistory,
		onFocusField
	}: {
		surfaces: PromptSurfaces;
		editingDefault: boolean;
		presetName: string;
		actionBusy: boolean;
		saving: boolean;
		legacyOverrideCount: number;
		unknownVars: string[];
		libraryDocChars: number;
		regexRows: PromptRegexRow[];
		historyKnobs: PromptHistorySurfaces;
		onBack: () => void;
		onCommitToDefault: () => void;
		onResetToFactory: () => void;
		onMigrateDirectives: () => void;
		setLibraryDoc: (value: string) => void;
		setRecalled: (key: 'framing' | 'open' | 'close', value: string) => void;
		setRecalledHeader: (key: 'recall' | 'world_info' | 'recap' | 'bts', value: string) => void;
		setRegexRow: (index: number, patch: Partial<PromptRegexRow>) => void;
		addRegexRow: () => void;
		removeRegexRow: (index: number) => void;
		setHistory: (patch: PromptHistorySurfaces) => void;
		/** Register a field as the variable-insertion target. */
		onFocusField: (
			apply: (value: string) => void,
			el: HTMLInputElement | HTMLTextAreaElement
		) => void;
	} = $props();

	/** D27 surfaces placeholders (literal; `{{...}}` in attributes is an
	 * EXPRESSION in Svelte — keep braces out of attribute strings). */
	const SURFACE_PLACEHOLDER = 'Inherited from the factory default — type to override';
	const SURFACE_TEXTAREA_PLACEHOLDER = 'Type to override the factory default';

	/** The recalled section headers (D27 surface 2). */
	const RECALLED_HEADERS: { key: 'recall' | 'world_info' | 'recap' | 'bts'; label: string }[] = [
		{ key: 'recall', label: 'Recall' },
		{ key: 'world_info', label: 'World info' },
		{ key: 'recap', label: 'Recap' },
		{ key: 'bts', label: 'Behind the scenes' }
	];
</script>

<div class="surfaces">
	<div class="section-head">
		<button type="button" class="back" onclick={onBack}>← Steps</button>
		<h3>Advanced — token text</h3>
	</div>
	<p class="sub">
		Every prompt-contributing text the engine substitutes outside the step blocks.
	</p>
	{#if editingDefault}
		<div class="surface-toolbar">
			<button
				type="button"
				class="btn primary"
				disabled={actionBusy || saving}
				title="Save any edits, then write the default to prompts/factory-defaults.json"
				onclick={onCommitToDefault}
			>
				{actionBusy ? 'Working…' : 'Commit to default'}
			</button>
			<button
				type="button"
				class="btn danger"
				disabled={actionBusy || saving}
				title="Re-seed the default from the factory image (discards edits)"
				onclick={onResetToFactory}
			>
				Reset to factory
			</button>
			<span class="toolbar-note">These surfaces drive the live default pipeline (D26).</span>
		</div>
	{:else}
		<p class="default-note">
			Advanced token text is carried by <strong>{presetName}</strong>. Commit/reset apply to the
			default preset.
		</p>
	{/if}

	<!-- Group 1 — the OpenUI component doc the narrator sees. -->
	<h3>Component library reference</h3>
	<p class="sub">
		The OpenUI component doc substituted for <code>&lt;&lt;library&gt;&gt;</code> — what the
		narrator sees.
	</p>
	<textarea
		class="surface"
		rows="6"
		aria-label="Library doc"
		placeholder={SURFACE_TEXTAREA_PLACEHOLDER}
		value={surfaces.library_doc ?? ''}
		onfocus={(event) => onFocusField(setLibraryDoc, event.currentTarget)}
		oninput={(event) => setLibraryDoc(event.currentTarget.value)}
	></textarea>
	<p class="hint">{libraryDocChars} chars</p>

	<!-- Group 2 — how recalled memories are wrapped in the post-user block. -->
	<h3>Recalled reference wrapper</h3>
	<p class="sub">
		The post-user <code>&lt;recalled&gt;</code> wrapper copy and its section headers.
	</p>
	<label class="surface-field">
		<span>Framing</span>
		<textarea
			class="surface"
			rows="2"
			aria-label="Recalled framing"
			placeholder={SURFACE_PLACEHOLDER}
			value={surfaces.recalled?.framing ?? ''}
			onfocus={(event) => onFocusField((v) => setRecalled('framing', v), event.currentTarget)}
			oninput={(event) => setRecalled('framing', event.currentTarget.value)}
		></textarea>
	</label>
	<label class="surface-field">
		<span>Open</span>
		<textarea
			class="surface"
			rows="2"
			aria-label="Recalled open"
			placeholder={SURFACE_PLACEHOLDER}
			value={surfaces.recalled?.open ?? ''}
			onfocus={(event) => onFocusField((v) => setRecalled('open', v), event.currentTarget)}
			oninput={(event) => setRecalled('open', event.currentTarget.value)}
		></textarea>
	</label>
	<label class="surface-field">
		<span>Close</span>
		<textarea
			class="surface"
			rows="2"
			aria-label="Recalled close"
			placeholder={SURFACE_PLACEHOLDER}
			value={surfaces.recalled?.close ?? ''}
			onfocus={(event) => onFocusField((v) => setRecalled('close', v), event.currentTarget)}
			oninput={(event) => setRecalled('close', event.currentTarget.value)}
		></textarea>
	</label>

	{#each RECALLED_HEADERS as header (header.key)}
		<label class="surface-field inline">
			<span>{header.label}</span>
			<input
				type="text"
				class="surface-input"
				aria-label={`${header.label} header`}
				placeholder={SURFACE_PLACEHOLDER}
				value={surfaces.recalled?.headers?.[header.key] ?? ''}
				onfocus={(event) =>
					onFocusField((v) => setRecalledHeader(header.key, v), event.currentTarget)}
				oninput={(event) => setRecalledHeader(header.key, event.currentTarget.value)}
			/>
		</label>
	{/each}

	<!-- Group 3 — rewrites and depth applied to projected history. -->
	<h3>History projection</h3>
	<p class="sub">
		Rewrites applied when projecting prompt history, plus the depth and DSL-trim knobs.
	</p>
	{#each regexRows as row, index (row.id)}
		<div class="regex-row">
			<input
				type="checkbox"
				checked={row.enabled}
				aria-label="Enable regex"
				onchange={(event) => setRegexRow(index, { enabled: event.currentTarget.checked })}
			/>
			<input
				type="text"
				class="surface-input"
				aria-label="Regex name"
				placeholder="name"
				value={row.name}
				oninput={(event) => setRegexRow(index, { name: event.currentTarget.value })}
			/>
			<textarea
				class="surface regex-find"
				rows="2"
				aria-label="Regex find"
				placeholder="find (regex)"
				value={row.find}
				onfocus={(event) => onFocusField((v) => setRegexRow(index, { find: v }), event.currentTarget)}
				oninput={(event) => setRegexRow(index, { find: event.currentTarget.value })}
			></textarea>
			<textarea
				class="surface regex-find"
				rows="2"
				aria-label="Regex replace"
				placeholder="replace"
				value={row.replace}
				onfocus={(event) =>
					onFocusField((v) => setRegexRow(index, { replace: v }), event.currentTarget)}
				oninput={(event) => setRegexRow(index, { replace: event.currentTarget.value })}
			></textarea>
			<input
				type="text"
				class="surface-input flags"
				aria-label="Regex flags"
				placeholder="flags"
				value={row.flags}
				oninput={(event) => setRegexRow(index, { flags: event.currentTarget.value })}
			/>
			<input
				type="number"
				class="surface-input depth"
				aria-label="Regex min depth"
				title="Minimum history depth"
				min="0"
				value={row.min_depth}
				oninput={(event) => setRegexRow(index, { min_depth: Number(event.currentTarget.value) || 0 })}
			/>
			<label class="tiny">
				<input
					type="checkbox"
					checked={row.prompt_only}
					onchange={(event) => setRegexRow(index, { prompt_only: event.currentTarget.checked })}
				/>
				prompt only
			</label>
			<button
				type="button"
				class="icon danger"
				title="Remove regex"
				aria-label="Remove regex"
				onclick={() => removeRegexRow(index)}
			>
				×
			</button>
		</div>
	{:else}
		<p class="hint">No regex scripts — inherited from the factory default.</p>
	{/each}
	<button type="button" class="btn" onclick={addRegexRow}>+ add regex</button>

	<div class="history-knobs">
		<label class="tiny">
			Depth
			<input
				type="number"
				class="surface-input depth"
				aria-label="History depth"
				min="0"
				value={historyKnobs.depth ?? ''}
				oninput={(event) =>
					setHistory({
						depth: event.currentTarget.value === '' ? undefined : Number(event.currentTarget.value)
					})}
			/>
		</label>
		<label class="tiny">
			<input
				type="checkbox"
				checked={historyKnobs.strip_dsl ?? false}
				onchange={(event) => setHistory({ strip_dsl: event.currentTarget.checked })}
			/>
			Strip openui DSL from history
		</label>
	</div>

	<!-- Group 4 — legacy override map, kept until migrated into the Rules rows. -->
	<h3>Legacy directive text</h3>
	<p class="sub">
		Old directive override text this preset still carries — fold it into the Rules rows.
	</p>
	{#if legacyOverrideCount > 0}
		<button
			type="button"
			class="btn"
			title="Fold each override into the matching directive row's content, then clear the map"
			onclick={onMigrateDirectives}
		>
			Migrate {legacyOverrideCount} legacy text override{legacyOverrideCount === 1 ? '' : 's'}
		</button>
	{:else}
		<p class="hint">No legacy text overrides.</p>
	{/if}
	{#if unknownVars.length}
		<p class="hint"><span class="warn">Unknown tokens: {unknownVars.join(', ')}</span></p>
	{/if}
</div>

<style>
	.surfaces {
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
	.btn.primary {
		border-color: var(--accent-hover);
		background: var(--accent-hover);
		color: var(--on-accent);
	}
	.btn.primary:hover:not(:disabled) {
		background: var(--accent);
	}
	.btn.danger {
		border-color: var(--danger-text);
		color: var(--danger-text);
	}
	.btn.danger:hover:not(:disabled) {
		background: var(--danger-soft);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.sub {
		margin: 0.1rem 0 0;
		font-size: 0.68rem;
		color: var(--text-dim);
	}
	.default-note {
		margin: 0 0 0.5rem;
		font-size: 0.72rem;
		color: var(--text-dim);
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
	.surface-toolbar {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
		padding: 0.4rem 0.5rem;
		border: 1px solid var(--accent-line);
		border-radius: 8px;
		background: var(--accent-soft);
	}
	.toolbar-note {
		font-size: 0.64rem;
		color: var(--text-muted);
	}
	.surface {
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
	.surface:focus-visible,
	.surface-input:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
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
	.regex-row {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.3rem;
		padding: 0.35rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.regex-row .surface-input {
		flex: 1 1 6rem;
		min-width: 0;
	}
	.regex-row .regex-find {
		flex: 1 1 100%;
	}
	.regex-row .flags {
		flex: 0 0 3.5rem;
	}
	.regex-row .depth {
		flex: 0 0 3.5rem;
	}
	.tiny {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.68rem;
		color: var(--text-muted);
		white-space: nowrap;
	}
	.history-knobs {
		display: flex;
		gap: 1rem;
		align-items: center;
		flex-wrap: wrap;
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
	.icon.danger:hover {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
