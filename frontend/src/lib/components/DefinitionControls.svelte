<script lang="ts">
	// Reusable per-object definition controls (D39 / W9.1).
	//
	// The Import / Export row every management surface needs, driven entirely by
	// the `entities.ts` descriptor table — the one place that decides which kinds
	// support which operations. A kind with neither operation renders nothing.
	// `id` targets one definition for export (null exports the whole catalog);
	// `onImported` is called with the new id so the caller can refresh its list.
	//
	// ONE import door (issue #60): the button reads simply `Import` in every pane
	// it appears in — the pane already says what is being imported — and the
	// click runs the kind's single `importFile` handler, which detects the file
	// (card PNG, card JSON, exported definition, lore file) and imports it as
	// this kind. No format choice, no second dialog, no per-pane rules.
	import { ENTITY_KINDS } from '#lib/entities';
	import type { ObjectKind } from '#lib/entities';

	let {
		kind,
		id = null,
		onImported
	}: {
		kind: ObjectKind;
		id?: string | null;
		onImported?: (id: string) => void;
	} = $props();

	const spec = $derived(ENTITY_KINDS[kind]);
	let busy = $state(false);
	let status = $state('');
	let error = $state('');
	let fileInput = $state<HTMLInputElement | null>(null);

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	async function doExport(): Promise<void> {
		const runExport = spec.export;
		if (!runExport || busy) return;
		busy = true;
		error = '';
		try {
			status = `Exported ${await runExport(id)}`;
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	async function onFile(event: Event): Promise<void> {
		const runImport = spec.importFile;
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!runImport || !file) return;
		busy = true;
		error = '';
		status = '';
		try {
			const imported = await runImport(file);
			status = imported.label ? `Imported “${imported.label}”` : 'Imported';
			onImported?.(imported.id);
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}
</script>

{#if spec.importFile || spec.export}
	<div class="definition-controls">
		<div class="door">
			{#if spec.importFile}
				<button type="button" class="ghost" disabled={busy} onclick={() => fileInput?.click()}>
					Import
				</button>
			{/if}
			{#if spec.export}
				<button type="button" class="ghost" disabled={busy} onclick={() => void doExport()}>
					Export {spec.label}
				</button>
			{/if}
		</div>
		{#if error}
			<p class="error" role="alert">{error}</p>
		{:else if status}
			<p class="status" role="status">{status}</p>
		{/if}
		<input
			bind:this={fileInput}
			class="file-input"
			type="file"
			accept=".png,.json,image/png,application/json"
			aria-label="Import"
			onchange={onFile}
		/>
	</div>
{/if}

<style>
	.definition-controls {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.door {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.file-input {
		display: none;
	}
	.error {
		margin: 0;
		padding: 0.3rem 0.5rem;
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
	.ghost {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text);
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
	}
</style>
