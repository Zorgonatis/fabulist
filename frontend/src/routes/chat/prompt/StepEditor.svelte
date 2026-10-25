<script module lang="ts">
	// Source badges (W6.4/W6.5): "can I edit this block, and who fills it?"
	//
	// Exported from the module script so the drawer and `PromptBlockEditor` read
	// the ONE spelling of the rule instead of re-deriving it:
	//   * `system` with a non-empty body  -> Authored (overrides the engine part)
	//   * `system` with an empty body     -> Engine default (type to override)
	//   * engine-filled structural rows   -> Pinned (read-only)
	//   * free `text` blocks              -> Authored (always user-written)
	import type { PromptModule } from '#lib/api/prompts';

	/** Structural types whose content the engine resolves at build time. */	const PINNED_TYPES = new Set([
		'narrator_system',
		'library',
		'history',
		'facts',
		'user',
		'recalled',
		'payload',
		'turn_payload'
	]);

	export interface BlockSource {
		kind: 'authored' | 'engine-default' | 'pinned';
		label: string;
		/** Engine-filled rows have nothing to author. */
		readonly: boolean;
	}

	/** The source badge for one block. */
	export function sourceBadge(module: PromptModule): BlockSource {
		if (module.type === 'system') {
			return module.content.trim() !== ''
				? { kind: 'authored', label: 'Authored', readonly: false }
				: { kind: 'engine-default', label: 'Engine default', readonly: false };
		}
		if (module.type === 'text') {
			return { kind: 'authored', label: 'Authored', readonly: false };
		}
		if (PINNED_TYPES.has(module.type)) {
			return { kind: 'pinned', label: 'Pinned (engine-filled)', readonly: true };
		}
		// Unknown structural type: treat it as engine-filled (never offer an edit
		// that cannot reach the engine).
		return { kind: 'pinned', label: 'Pinned (engine-filled)', readonly: true };
	}
</script>

<script lang="ts">
	// The Step editor (W6.4): the ordered block list for ONE step, in the
	// SillyTavern shape — drag handle · enable · name · source badge · type ·
	// role, with the up/down buttons as the keyboard/a11y fallback for drag.
	//
	// Presentational only: the drawer owns the draft preset and hands in the
	// block list plus the mutation callbacks. Reordering reuses the pure
	// `../promptSort` helper on the drawer side.
	// (`PromptModule` comes from this file's module script — one import.)
	import type { PromptModuleType, PromptRole } from '#lib/api/prompts';

	let {
		step,
		modules,
		structuralTypes,
		inherited,
		agentOptions,
		onBack,
		onToggle,
		onDelete,
		onMove,
		onAddBlock,
		onSetRole,
		onRebind,
		onEditBlock
	}: {
		step: {
			id: string;
			title: string;
			blurb: string;
			trigger: string;
			defaultAgent: string;
			boundAgent: string;
			systemSource: 'authored' | 'engine';
		};
		modules: PromptModule[];
		/** Structural block types this step's flow accepts (engine contract). */
		structuralTypes: PromptModuleType[];
		/** True while the step still runs the default pipeline's blocks. */
		inherited: boolean;
		/** Agents offered by the inline rebind select. */
		agentOptions: string[];
		onBack: () => void;
		onToggle: (id: string) => void;
		onDelete: (id: string) => void;
		onMove: (from: number, to: number) => void;
		onAddBlock: (type: PromptModuleType) => void;
		onSetRole: (id: string, role: PromptRole) => void;
		onRebind: (agent: string) => void;
		onEditBlock: (module: PromptModule) => void;
	} = $props();

	const ROLES: PromptRole[] = ['system', 'user', 'assistant'];

	/** Structural types that can still be added (each may appear once; `text`
	 * blocks are always addable). */
	const availableBlocks = $derived.by((): PromptModuleType[] => {
		const present = new Set(modules.map((m) => m.type));
		return structuralTypes.filter((type) => !present.has(type));
	});

	let dragFrom = $state<number | null>(null);
	let dragOverIndex = $state<number | null>(null);

	function onDragStart(index: number, event: DragEvent): void {
		dragFrom = index;
		if (event.dataTransfer) {
			event.dataTransfer.effectAllowed = 'move';
			try {
				event.dataTransfer.setData('text/plain', String(index));
			} catch {
				// Some browsers block setData for non-text drags; index is in-memory anyway.
			}
		}
	}

	function onDragOver(index: number, event: DragEvent): void {
		event.preventDefault(); // declares this row a valid drop target
		dragOverIndex = index;
	}

	function onDrop(index: number, event: DragEvent): void {
		event.preventDefault();
		const raw = event.dataTransfer?.getData('text/plain');
		const from = dragFrom ?? (raw ? Number(raw) : null);
		if (from !== null && !Number.isNaN(from)) onMove(from, index);
		onDragEnd();
	}

	function onDragEnd(): void {
		dragFrom = null;
		dragOverIndex = null;
	}

	function onAddChange(event: Event): void {
		const select = event.currentTarget as HTMLSelectElement;
		const value = select.value as PromptModuleType | '';
		select.value = '';
		if (value) onAddBlock(value);
	}
</script>

<header class="step-head">
	<button type="button" class="back" onclick={onBack}>← All steps</button>
	<h3>{step.title}</h3>
	<p class="blurb">{step.blurb}</p>
	<p class="trigger">{step.trigger}</p>
	<div class="head-meta">
		<label class="agent">
			<span class="meta-label">agent</span>
			<select
				aria-label={`${step.title} agent`}
				value={step.boundAgent}
				onchange={(event) => onRebind(event.currentTarget.value)}
			>
				<option value="">default ({step.defaultAgent || '—'})</option>
				{#each agentOptions as agent (agent)}
					<option value={agent}>{agent}</option>
				{/each}
				{#if step.boundAgent && !agentOptions.includes(step.boundAgent)}
					<option value={step.boundAgent}>{step.boundAgent} (custom)</option>
				{/if}
			</select>
		</label>
		<span class="badge" class:authored={step.systemSource === 'authored'}>
			system: {step.systemSource === 'authored' ? 'Authored' : 'Engine default'}
		</span>
	</div>
</header>

{#if inherited}
	<p class="inherited-note">
		Inherited from the default pipeline — the first edit copies these blocks into this preset.
	</p>
{/if}

<div class="section-head">
	<h4>Blocks ({modules.length})</h4>
	<select class="add-block" value="" aria-label="Add block" onchange={onAddChange}>
		<option value="">+ add block</option>
		<option value="text">+ text block</option>
		{#each availableBlocks as type (type)}
			<option value={type}>+ {type}</option>
		{/each}
	</select>
</div>

<ol class="blocks">
	{#each modules as module, index (module.id)}
		{@const source = sourceBadge(module)}
		<li
			class:drop-target={dragOverIndex === index}
			class:disabled={!module.enabled}
			ondragover={(event) => onDragOver(index, event)}
			ondrop={(event) => onDrop(index, event)}
		>
			<button
				type="button"
				class="handle"
				draggable="true"
				aria-label="Drag to reorder"
				title="Drag to reorder (or use ↑ / ↓)"
				ondragstart={(event) => onDragStart(index, event)}
				ondragend={onDragEnd}
			>
				⋮⋮
			</button>
			<input
				type="checkbox"
				checked={module.enabled}
				aria-label="Enable block"
				onchange={() => onToggle(module.id)}
			/>
			<span class="mod-name">{module.name}</span>
			<span class="badge source-{source.kind}" title={source.readonly
				? 'Filled by the engine at build time — read-only'
				: source.kind === 'engine-default'
					? 'Empty = the engine default prompt; type to override'
					: 'Your text overrides the engine default'}>
				{source.label}
			</span>
			<span class="badge type-badge">{module.type}</span>
			{#if module.type === 'text'}
				<select
					class="role"
					aria-label="Message role"
					value={module.role}
					onchange={(event) =>
						onSetRole(module.id, (event.currentTarget as HTMLSelectElement).value as PromptRole)}
				>
					{#each ROLES as role (role)}
						<option value={role}>{role}</option>
					{/each}
				</select>
			{:else}
				<span class="role-static">{module.role}</span>
			{/if}
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
					disabled={index === modules.length - 1}
					onclick={() => onMove(index, index + 1)}
				>
					↓
				</button>
				<button
					type="button"
					class="icon"
					title={source.readonly ? 'Engine-filled — not editable' : 'Edit content'}
					aria-label="Edit content"
					disabled={source.readonly}
					onclick={() => onEditBlock(module)}
				>
					✎
				</button>
				{#if module.type === 'text'}
					<button
						type="button"
						class="icon danger"
						title="Delete block"
						aria-label="Delete block"
						onclick={() => onDelete(module.id)}
					>
						×
					</button>
				{/if}
			</div>
		</li>
	{:else}
		<li class="empty-row">No blocks. Use “+ add block” to build this step.</li>
	{/each}
</ol>

<style>
	.step-head {
		border-bottom: 1px solid var(--line);
		padding-bottom: 0.5rem;
		margin-bottom: 0.6rem;
	}
	.back {
		background: transparent;
		border: none;
		color: var(--accent-strong);
		cursor: pointer;
		font-size: var(--font-small);
		padding: 0;
	}
	h3 {
		margin: 0.3rem 0 0.15rem;
		font-size: var(--font-lg);
	}
	.blurb {
		margin: 0;
		font-size: var(--font-small);
		opacity: 0.8;
	}
	.trigger {
		margin: 0.15rem 0 0;
		font-size: var(--font-micro);
		opacity: 0.6;
	}
	.head-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.4rem;
		font-size: var(--font-small);
	}
	.agent {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}
	.meta-label {
		opacity: 0.6;
	}
	.agent select {
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
		font-size: var(--font-small);
		padding: 0.1rem 0.2rem;
	}
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	h4 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.add-block {
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
		font-size: var(--font-small);
		padding: 0.15rem 0.3rem;
	}
	.inherited-note {
		font-size: var(--font-small);
		background: var(--accent-soft);
		border-radius: 4px;
		padding: 0.3rem 0.5rem;
		margin: 0 0 0.5rem;
	}
	.blocks {
		list-style: none;
		margin: 0.5rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.blocks > li {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.4rem;
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.4rem 0.5rem;
	}
	.blocks > li.drop-target {
		border-color: var(--accent);
	}
	.blocks > li.disabled .mod-name {
		opacity: 0.5;
	}
	.handle {
		cursor: grab;
		background: transparent;
		border: none;
		color: var(--text-dim);
		padding: 0 0.15rem;
	}
	.mod-name {
		font-weight: 600;
		font-size: var(--font-ui);
	}
	.badge {
		font-size: var(--font-micro);
		border: 1px solid var(--line-strong);
		border-radius: 999px;
		padding: 0.05rem 0.45rem;
		opacity: 0.9;
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
	.role,
	.role-static {
		font-size: var(--font-micro);
		opacity: 0.75;
	}
	.role {
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 4px;
	}
	.row-actions {
		display: flex;
		gap: 0.15rem;
		margin-left: auto;
	}
	.icon {
		background: transparent;
		border: 1px solid transparent;
		border-radius: 4px;
		color: var(--text-muted);
		cursor: pointer;
		font-size: var(--font-small);
		padding: 0.05rem 0.3rem;
	}
	.icon:hover:not(:disabled) {
		background: var(--bg-hover);
		/* On a wash the boundary needs --line-strong: --line is a 3:1 boundary
		   against the raised/panel/inset rungs, but it drops to 2.73:1 on the
		   warm --bg-hover (and 3.34:1 at neutral). --line-strong holds 3.60:1
		   warm / 4.40:1 neutral there. */
		border-color: var(--line-strong);
	}
	.icon:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}
	.icon.danger:hover {
		color: var(--danger-text);
	}
	.empty-row {
		padding: 0.5rem;
		opacity: 0.7;
		font-size: var(--font-small);
	}
</style>
