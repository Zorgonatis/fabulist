<script lang="ts">
	// The full-screen block editor (W6.5).
	//
	// ONE block, edited in an in-app overlay (NOT `window.open`): a monospace
	// source pane beside a live markdown-lite preview (`#lib/markdown`
	// `renderMarkdown`, the same renderer the transcript uses), a small
	// formatting toolbar, and the VARIABLE INSERTER inside the editor — chips
	// splice `{{name}}` at the caret of the source field.
	//
	// The overlay registers its own uiLayers surface, so Escape closes THIS
	// editor before it reaches the prompt drawer underneath.
	import { onMount, tick, untrack } from 'svelte';
	import { register } from '#lib/uiLayers';
	import { renderMarkdown } from '#lib/markdown';
	import type { PromptVariableSpec } from '#lib/api/prompts';
	import type { BlockSource } from './StepEditor.svelte';

	let {
		scope,
		title,
		source,
		value,
		variables = [],
		onSave,
		onClose
	}: {
		/** Where the block lives, shown in the header ("Narrator turn"). */
		scope: string;
		/** Block name. */
		title: string;
		/** Source badge resolved by `StepEditor.sourceBadge`. */
		source: BlockSource;
		/** The block's current content. */
		value: string;
		variables?: PromptVariableSpec[];
		onSave: (content: string) => void;
		onClose: () => void;
	} = $props();

	/** Local draft. The overlay is mounted fresh per block, so the initial prop
	 * read is the right seed (`untrack` — a prop read in a state initializer must
	 * not become a tracked dependency; there is no prop sync by design). */
	let text = $state(untrack(() => value));
	let sourceEl = $state<HTMLTextAreaElement | null>(null);

	const dirty = $derived(text !== value);

	onMount(() => register('prompt-block', () => onClose()));

	/** Put the caret back after a toolbar / inserter edit (await the flush so
	 * the new `value` is in the DOM before the range is restored). */
	async function focusRange(start: number, end: number): Promise<void> {
		await tick();
		const el = sourceEl;
		if (!el) return;
		el.focus();
		el.setSelectionRange(start, end);
	}

	/** Wrap the selection (or a placeholder) in `marker` — bold / italic / code. */
	async function wrap(marker: string, placeholder: string): Promise<void> {
		const el = sourceEl;
		if (!el) return;
		const start = el.selectionStart ?? text.length;
		const end = el.selectionEnd ?? start;
		const inner = text.slice(start, end) || placeholder;
		text = text.slice(0, start) + marker + inner + marker + text.slice(end);
		await focusRange(start + marker.length, start + marker.length + inner.length);
	}

	/** Prefix the caret's line — heading / list. */
	async function prefixLine(marker: string): Promise<void> {
		const el = sourceEl;
		if (!el) return;
		const start = el.selectionStart ?? text.length;
		const lineStart = text.lastIndexOf('\n', Math.max(0, start - 1)) + 1;
		text = text.slice(0, lineStart) + marker + text.slice(lineStart);
		await focusRange(start + marker.length, start + marker.length);
	}

	/** Splice `{{name}}` at the caret of the source field. */
	async function insertVariable(name: string): Promise<void> {
		const el = sourceEl;
		if (!el) return;
		const start = el.selectionStart ?? text.length;
		const end = el.selectionEnd ?? start;
		const token = `{{${name}}}`;
		text = text.slice(0, start) + token + text.slice(end);
		await focusRange(start + token.length, start + token.length);
	}

	function save(): void {
		onSave(text);
		onClose();
	}
</script>

<div class="scrim" role="presentation" onclick={onClose}></div>

<div class="block-editor" role="dialog" aria-modal="true" aria-label={`Edit ${title}`}>
	<header>
		<div class="head-left">
			<span class="scope">{scope}</span>
			<h2>{title}</h2>
			<span class="badge source-{source.kind}">{source.label}</span>
			{#if dirty}<span class="dirty">● unsaved</span>{/if}
		</div>
		<div class="head-actions">
			<button type="button" class="btn primary" disabled={source.readonly || !dirty} onclick={save}>
				Apply
			</button>
			<button type="button" class="btn" onclick={onClose}>Cancel</button>
			<button type="button" class="close" aria-label="Close editor" onclick={onClose}>×</button>
		</div>
	</header>

	{#if source.readonly}
		<p class="notice">
			Pinned (engine-filled): this block's content is resolved from live state at build time —
			there is nothing to author.
		</p>
	{:else if source.kind === 'engine-default'}
		<p class="notice">
			Empty = the built-in <strong>{scope}</strong> prompt. Type here to override it.
		</p>
	{/if}

	<div class="toolbar">
		<div class="tools" role="group" aria-label="Formatting">
			<button type="button" class="tool" title="Bold" onclick={() => void wrap('**', 'bold')}
				><strong>B</strong></button
			>
			<button type="button" class="tool" title="Italic" onclick={() => void wrap('*', 'italic')}
				><em>I</em></button
			>
			<button type="button" class="tool" title="Heading" onclick={() => void prefixLine('## ')}
				>H</button
			>
			<button type="button" class="tool" title="List item" onclick={() => void prefixLine('- ')}
				>•</button
			>
			<button type="button" class="tool" title="Inline code" onclick={() => void wrap('`', 'code')}
				>&lt;/&gt;</button
			>
		</div>
		{#if variables.length}
			<div class="inserter" role="group" aria-label="Insert variable">
				<span class="inserter-label">Insert variable:</span>
				{#each variables as variable (variable.name)}
					<button
						type="button"
						class="var-btn"
						title={variable.description || `{{${variable.name}}}`}
						onclick={() => void insertVariable(variable.name)}
					>
						{variable.name}
					</button>
				{/each}
			</div>
		{/if}
	</div>

	<div class="panes">
		<label class="pane source-pane">
			<span class="pane-label">Source</span>
			<textarea
				bind:this={sourceEl}
				class="source"
				aria-label={`${title} source`}
				spellcheck="false"
				disabled={source.readonly}
				value={text}
				oninput={(event) => (text = event.currentTarget.value)}
			></textarea>
		</label>
		<div class="pane preview-pane">
			<span class="pane-label">Preview</span>
			<div class="preview">
				{#if text}
					{@html renderMarkdown(text)}
				{:else}
					<p class="empty">Nothing to preview yet.</p>
				{/if}
			</div>
		</div>
	</div>
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 40;
	}
	.block-editor {
		position: fixed;
		inset: 0;
		z-index: 41;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		color: var(--text);
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.6rem 1rem;
		border-bottom: 1px solid var(--line);
		flex-wrap: wrap;
	}
	.head-left {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.scope {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		opacity: 0.6;
	}
	h2 {
		margin: 0;
		font-size: var(--font-lg);
	}
	.dirty {
		color: var(--warn);
		font-size: var(--font-small);
	}
	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	.badge {
		font-size: var(--font-micro);
		border: 1px solid var(--line-strong);
		border-radius: 999px;
		padding: 0.05rem 0.45rem;
	}
	.badge.source-authored {
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.badge.source-engine-default {
		border-color: var(--warn);
		color: var(--warn);
	}
	.badge.source-pinned {
		opacity: 0.65;
	}
	.btn {
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
		font-size: var(--font-small);
		padding: 0.2rem 0.6rem;
		cursor: pointer;
	}
	.btn.primary {
		background: var(--accent);
		border-color: var(--accent);
		color: var(--on-accent);
	}
	.btn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.close {
		background: transparent;
		border: none;
		color: var(--text-muted);
		cursor: pointer;
		font-size: 1.2rem;
		line-height: 1;
	}
	.notice {
		margin: 0;
		padding: 0.4rem 1rem;
		font-size: var(--font-small);
		background: var(--accent-soft);
	}
	.toolbar {
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex-wrap: wrap;
		padding: 0.45rem 1rem;
		border-bottom: 1px solid var(--line);
	}
	.tools {
		display: flex;
		gap: 0.2rem;
	}
	.tool {
		min-width: 1.9rem;
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
		cursor: pointer;
		font-size: var(--font-small);
		padding: 0.15rem 0.35rem;
	}
	.tool:hover:not(:disabled) {
		background: var(--bg-hover);
	}
	.tool:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.inserter {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		flex-wrap: wrap;
	}
	.inserter-label {
		font-size: var(--font-small);
		opacity: 0.7;
	}
	.var-btn {
		background: var(--bg-raised);
		color: var(--accent-strong);
		border: 1px solid var(--line);
		border-radius: 999px;
		cursor: pointer;
		font-size: var(--font-micro);
		padding: 0.1rem 0.5rem;
	}
	.var-btn:hover {
		background: var(--bg-hover);
	}
	.panes {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 0.75rem;
		padding: 0.75rem 1rem;
	}
	.pane {
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.pane-label {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.06em;
		opacity: 0.6;
		margin-bottom: 0.25rem;
	}
	.source {
		flex: 1;
		min-height: 0;
		resize: none;
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.6rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
		font-size: var(--font-small);
		line-height: 1.5;
	}
	.preview-pane .preview {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.6rem;
	}
	.preview :global(h1),
	.preview :global(h2),
	.preview :global(h3) {
		margin: 0.6rem 0 0.3rem;
	}
	.preview :global(p) {
		margin: 0 0 0.5rem;
	}
	.preview :global(code) {
		background: var(--bg-raised);
		border-radius: 3px;
		padding: 0.05rem 0.25rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	}
	.preview :global(li) {
		margin-left: 1.1rem;
	}
	.empty {
		opacity: 0.6;
		font-size: var(--font-small);
		margin: 0;
	}
</style>
