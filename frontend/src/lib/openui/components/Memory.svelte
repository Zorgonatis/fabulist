<script lang="ts">
	// VTK flavor: Memory — a sepia flashback that frays as it comes back.
	// OpenUI Lang call: Memory(era, fragments[], edge[, {glyph}]).
	// Preset: "Visual Toolkit (VTK)" → Memory.
	//
	// A SYMBOLIC SHAPE, not a box: a tone-soft "photo" carrying the kit's texture
	// with the era captioned by hand beneath it, then 2-4 recalled shards stacked,
	// tilted and overlapping — recall arriving in pieces. The last shard carries
	// the `edge` dissolve. An optional {glyph} stamps the medium in the corner.
	// Colour is tone tokens only; no hue offset (docs/UI-LANGUAGE.md §2).
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { MemoryProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<MemoryProps> = $props();

	const fragments = $derived(props.fragments ?? []);
	const glyph = $derived(props.options?.glyph ?? '');
</script>

<div class="vtk-memory" data-openui="Memory">
	{#if glyph}
		<span class="stamp ui-glyph" aria-hidden="true">{glyph}</span>
	{/if}

	<figure class="polaroid">
		<span class="photo ui-texture-grain" aria-hidden="true"></span>
		<figcaption class="era">{props.era}</figcaption>
	</figure>

	<div class="shards">
		{#each fragments as fragment, i (i)}
			<p
				class="shard ui-alt"
				class:dissolve={Boolean(props.edge) && i === fragments.length - 1}
				style="--i: {i}"
			>
				{fragment}
			</p>
		{/each}
	</div>

	{#if props.edge}
		<p class="edge">{props.edge}</p>
	{/if}
</div>

<style>
	.vtk-memory {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
		font-size: var(--font-body);
		color: var(--text);
	}

	/* The medium: a candle / tape / leaf pressed into the corner. */
	.stamp {
		position: absolute;
		top: -0.15rem;
		right: 0.1rem;
		z-index: 2;
		font-size: 1.35rem;
		opacity: 0.9;
		transform: rotate(6deg);
	}

	/* ── polaroid: tone-soft photo + handwritten caption ────────────── */
	.polaroid {
		margin: 0 0.35rem;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.45rem 0.45rem 0.3rem;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 3px;
		transform: rotate(-0.7deg);
		box-shadow: 0 10px 18px -14px var(--tone-1-soft);
	}
	.photo {
		position: relative;
		display: block;
		aspect-ratio: 16 / 10;
		border-radius: 2px;
		background-color: var(--bg-inset);
		overflow: hidden;
	}
	/* The tone wash + vignette sit above the kit's grain texture. */
	.photo::after {
		content: '';
		position: absolute;
		inset: 0;
		background:
			radial-gradient(140% 120% at 30% 15%, var(--tone-1-soft), transparent 68%),
			linear-gradient(160deg, var(--tone-2-soft), transparent 62%);
	}
	.era {
		margin: 0;
		padding-bottom: 0.1rem;
		font-size: var(--font-body);
		font-style: italic;
		letter-spacing: 0.02em;
		text-align: center;
		color: var(--text-muted);
		transform: rotate(1deg);
	}

	/* ── recalled shards: tilted, staggered, overlapping ────────────── */
	.shards {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		margin: 0 0.35rem;
	}
	.shard {
		max-width: 100%;
		margin: 0;
		margin-inline-start: var(--indent, 0rem);
		padding: 0.42rem 0.7rem;
		font-size: var(--font-body);
		line-height: 1.45;
		color: var(--text);
		white-space: pre-line;
		background: var(--tone-soft, var(--tone-1-soft));
		border: 1px solid var(--tone-line, var(--tone-1-line));
		border-left-width: 2px;
		border-radius: 2px;
		transform: rotate(var(--tilt, 0deg));
		/* Gentle staggered arrival; the keyframes carry the tilt. */
		animation: shard-in 0.5s ease-out both;
		animation-delay: calc(var(--i) * 130ms);
	}
	.shard + .shard {
		margin-top: -0.5rem; /* the pieces overlap as they arrive */
	}
	.shard:nth-child(4n + 1) {
		--tilt: -1.2deg;
		--indent: 0rem;
	}
	.shard:nth-child(4n + 2) {
		--tilt: 1.6deg;
		--indent: 1.1rem;
	}
	.shard:nth-child(4n + 3) {
		--tilt: -1.7deg;
		--indent: 0.3rem;
	}
	.shard:nth-child(4n) {
		--tilt: 1.1deg;
		--indent: 1.6rem;
	}
	/* The recall frays at the end: `edge` fades the last shard into the shell. */
	.shard.dissolve {
		-webkit-mask-image: linear-gradient(180deg, #000 46%, transparent 100%);
		mask-image: linear-gradient(180deg, #000 46%, transparent 100%);
	}

	.edge {
		margin: 0 0.5rem;
		font-size: var(--font-body);
		font-style: italic;
		line-height: 1.4;
		color: var(--text-dim);
		white-space: pre-line;
	}

	@keyframes shard-in {
		from {
			opacity: 0;
			transform: translateY(7px) rotate(var(--tilt, 0deg));
		}
		to {
			opacity: 1;
			transform: translateY(0) rotate(var(--tilt, 0deg));
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.shard {
			animation: none;
		}
	}
</style>
