<script lang="ts">
	// Image-gen adjustables strip (COMFY-PLAN Wave 4, lane V3).
	//
	// Opens above the Composer with the description already in the POSITIVE prompt
	// (what the user typed IS the prompt): positive/negative are EDITABLE here, plus
	// seed (number + dice) and width/height prefilled from the definition's
	// `param_map` — the user reviews and edits BEFORE the run is submitted (the
	// plan's "you stay in charge" contract). Two distinct actions: "Prompt Enhance"
	// (beside the seed/size controls) asks the generator agent for prompt text,
	// "Generate Image" submits the run with the explicit `overrides`. Cancel discards
	// the strip untouched.
	//
	// The parent remounts this component (via `{#key}`) whenever a fresh render
	// arrives, so the local editable state is seeded once at construction from the
	// props — no `$effect` write-back loop.
	import type { ImageGenDefinition, ImageGenOverrides } from '#lib/api/imagegen';
	import { definitionParamValue } from '#lib/api/imagegen';

	let {
		definitions = [],
		definitionId = '',
		positive = $bindable(''),
		negative = '',
		rendering = false,
		running = false,
		error = '',
		onDefinition,
		onAiGenerate,
		onGenerate,
		onCancel
	}: {
		definitions?: ImageGenDefinition[];
		definitionId?: string;
		positive?: string;
		negative?: string;
		rendering?: boolean;
		running?: boolean;
		error?: string;
		onDefinition?: (id: string) => void;
		onAiGenerate?: () => void;
		onGenerate: (overrides: ImageGenOverrides) => void;
		onCancel: () => void;
	} = $props();

	/** The definition the strip is adjusting (read once at construction). */
	function currentDefinition(): ImageGenDefinition | null {
		return definitions.find((item) => item.imagegen_id === definitionId) ?? null;
	}

	const definition = currentDefinition();

	function resolutionTarget(input: string | undefined) {
		const resolution = definition?.param_map.resolution;
		if (!resolution || !input) return undefined;
		return { node_id: resolution.node_id, input };
	}

	function prefillWidth(): number | null {
		const value = definitionParamValue(definition, resolutionTarget(definition?.param_map.resolution?.width_input));
		return typeof value === 'number' ? value : null;
	}

	function prefillHeight(): number | null {
		const value = definitionParamValue(definition, resolutionTarget(definition?.param_map.resolution?.height_input));
		return typeof value === 'number' ? value : null;
	}

	function prefillSeed(): number {
		const value = definitionParamValue(definition, definition?.param_map.seed);
		return typeof value === 'number' ? value : 0;
	}

	// Editable state, seeded once from the rendered prompts + workflow defaults.
	// The parent remounts this component via {#key} on every fresh render, so the
	// one-time capture is intentional (no write-back loop). `positive` is bound
	// two-way (the parent reads the typed prompt for "Prompt Enhance").
	// svelte-ignore state_referenced_locally
	let neg = $state(negative);
	let seed = $state<number>(prefillSeed());
	let width = $state<number | null>(prefillWidth());
	let height = $state<number | null>(prefillHeight());

	function rollSeed(): void {
		// ComfyUI seeds are 64-bit; JS numbers are exact to 2^53. Stay well inside.
		seed = Math.floor(Math.random() * 2 ** 32);
	}

	function generate(): void {
		if (!definition) return;
		const overrides: ImageGenOverrides = {};
		// A cleared number input binds `null`/`undefined`; skip it rather than
		// sending a bad override (the engine's apply_overrides degrades, but an
		// explicit NaN/null seed is never what the user meant).
		if (definition.param_map.seed && typeof seed === 'number' && Number.isFinite(seed)) {
			overrides.seed = seed;
		}
		if (definition.param_map.positive) overrides.positive = positive;
		if (definition.param_map.negative) overrides.negative = neg;
		if (definition.param_map.resolution) {
			if (typeof width === 'number' && Number.isFinite(width)) overrides.width = width;
			if (typeof height === 'number' && Number.isFinite(height)) overrides.height = height;
		}
		onGenerate(overrides);
	}
</script>

<section class="strip" aria-label="Image generation adjustables">
	<header class="strip-head">
		<span class="strip-title">Image adjustables</span>
		{#if rendering}
			<span class="strip-note" aria-live="polite">Enhancing prompt…</span>
		{/if}
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	<div class="grid">
		{#if definitions.length > 1}
			<label class="field definition-field">
				<span>Definition</span>
				<select
					value={definitionId}
					disabled={running || rendering}
					onchange={(event) => onDefinition?.(event.currentTarget.value)}
				>
					{#each definitions as item (item.imagegen_id)}
						<option value={item.imagegen_id}>{item.name || item.imagegen_id}</option>
					{/each}
				</select>
			</label>
		{/if}

		{#if definition?.param_map.seed}
			<label class="field seed-field">
				<span>Seed</span>
				<span class="seed-row">
					<input type="number" bind:value={seed} disabled={running} />
					<button
						type="button"
						class="dice"
						title="Randomize seed"
						aria-label="Randomize seed"
						onclick={rollSeed}
						disabled={running}
					>
						🎲
					</button>
				</span>
			</label>
		{/if}

		{#if definition?.param_map.resolution}
			<label class="field size-field">
				<span>Width</span>
				<input type="number" bind:value={width} disabled={running} />
			</label>
			<label class="field size-field">
				<span>Height</span>
				<input type="number" bind:value={height} disabled={running} />
			</label>
		{/if}

		{#if onAiGenerate}
			<button
				type="button"
				class="enhance"
				title="Ask the generator agent to write the prompts from your description"
				onclick={onAiGenerate}
				disabled={running || rendering || !definition}
			>
				{rendering ? 'Enhancing…' : 'Prompt Enhance'}
			</button>
		{/if}
	</div>

	<label class="field prompt-field">
		<span>Positive prompt</span>
		{#if definition?.param_map.positive}
			<textarea
				bind:value={positive}
				rows="3"
				spellcheck="false"
				placeholder="What do you want to see?"
				disabled={running}
			></textarea>
		{:else}
			<textarea
				value={positive}
				rows="3"
				readonly
				disabled
				placeholder="What do you want to see?"
			></textarea>
			<span class="not-adjustable">Not adjustable for this workflow.</span>
		{/if}
	</label>

	<label class="field prompt-field">
		<span>Negative prompt</span>
		{#if definition?.param_map.negative}
			<textarea bind:value={neg} rows="2" spellcheck="false" disabled={running}></textarea>
		{:else}
			<textarea value={negative} rows="2" readonly disabled></textarea>
			<span class="not-adjustable">Not adjustable for this workflow.</span>
		{/if}
	</label>

	<div class="strip-actions">
		<span class="spacer"></span>
		<button type="button" class="ghost" onclick={onCancel} disabled={running}>Cancel</button>
		<button type="button" class="primary" onclick={generate} disabled={running || rendering || !definition}>
			{running ? 'Generating…' : 'Generate Image'}
		</button>
	</div>
</section>

<style>
	.strip {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.6rem 1rem 0.7rem;
		border-top: 1px solid var(--line);
		background: var(--bg-panel);
	}
	.strip-head {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
	}
	.strip-title {
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text-muted);
	}
	.strip-note {
		font-size: var(--font-micro);
		color: var(--accent-strong);
	}
	.grid {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		align-items: flex-end;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.definition-field {
		flex: 1 1 12rem;
		min-width: 0;
	}
	.seed-field,
	.size-field {
		flex: 0 0 auto;
	}
	.seed-row {
		display: flex;
		align-items: center;
		gap: 0.25rem;
	}
	.field input,
	.field select,
	.field textarea {
		font: inherit;
		font-size: var(--font-ui);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		padding: 0.3rem 0.45rem;
	}
	.field input[type='number'] {
		width: 6.5rem;
	}
	.field textarea {
		resize: vertical;
		width: 100%;
	}
	.prompt-field {
		width: 100%;
	}
	.not-adjustable {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.dice {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.3rem 0.45rem;
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text);
		cursor: pointer;
	}
	.dice:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	/* The strip's secondary-prominent action, sitting with the seed/size controls.
	   Accent-tinted like .primary but on the raised surface so the two read apart. */
	.enhance {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.3rem 0.7rem;
		border: 1px solid var(--accent-line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
		white-space: nowrap;
		align-self: flex-end;
	}
	.enhance:hover:not(:disabled) {
		background: var(--accent-soft);
	}
	.enhance:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.strip-actions {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.spacer {
		flex: 1;
	}
	.strip-actions button {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.35rem 0.75rem;
		border-radius: 6px;
		cursor: pointer;
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
	.strip-actions button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.error {
		margin: 0;
		padding: 0.4rem 0.55rem;
		font-size: var(--font-small);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
