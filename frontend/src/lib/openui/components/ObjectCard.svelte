<script lang="ts">
	// Generic card: an in-world object, item or quest artefact — the pocket tag.
	// OpenUI Lang call: ObjectCard(name, kind, description, {glyph?}).
	// Legacy sessions may carry ArtifactCard(...) — it renders identically (the
	// registry aliases the old name to this component).
	//
	// Owner review 2026-10-11: the card "needs a solid visual anchor (a similar
	// emoji…)". The anchor is authored HERE rather than left to the model: the
	// narrator's `glyph` wins when it passes one, otherwise `kind` picks a curated
	// emoji, so the card never renders as a name with no object in it. Detail-
	// heavy inspection is the `Artifact` flavor's job — this one stays a tag.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	type ObjectCardProps = {
		name: string;
		kind: string;
		description: string;
		options?: { glyph?: string };
	};

	let { props }: ComponentRenderProps<ObjectCardProps> = $props();

	// Authored defaults, one per kind the narrator doc names (Relic / Weapon /
	// Curio / Quest item / Trinket); anything else gets the parcel. Matched on the
	// lowercased kind so "Cursed Relic" and "relic" land the same anchor.
	const KIND_GLYPHS: { match: string; glyph: string }[] = [
		{ match: 'relic', glyph: '🗝️' },
		{ match: 'weapon', glyph: '🗡️' },
		{ match: 'curio', glyph: '🧿' },
		{ match: 'quest', glyph: '🧭' },
		{ match: 'trinket', glyph: '🪙' }
	];
	const DEFAULT_GLYPH = '📦';

	const kind = $derived((props.kind ?? '').trim());
	const glyph = $derived(
		props.options?.glyph?.trim() ||
			KIND_GLYPHS.find((entry) => kind.toLowerCase().includes(entry.match))?.glyph ||
			DEFAULT_GLYPH
	);
</script>

<article class="object-card ui-panel" data-openui="ObjectCard">
	<span class="anchor" aria-hidden="true">{glyph}</span>
	<div class="body">
		{#if kind}
			<span class="kind ui-tag">{kind}</span>
		{/if}
		<h3 class="name">{props.name}</h3>
		<p class="description">{props.description}</p>
	</div>
</article>

<style>
	.object-card {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr);
		align-items: center;
		gap: 0.7rem;
		padding: 0.5rem 0.65rem;
		border-left: 3px solid var(--tone-1);
		font-size: var(--font-body);
	}

	/* The anchor: a solid tile, so the emoji reads as an object on the tag rather
	   than clip-art floating in the corner. */
	.anchor {
		display: grid;
		place-items: center;
		width: 3.5rem;
		height: 3.5rem;
		border: 1px solid var(--tone-1-line);
		border-radius: 10px;
		background-color: var(--tone-1-soft);
		font-size: 46px;
		line-height: 1;
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		min-width: 0;
	}
	/* Stamped micro-label: 11px is the floor for a true label (§3), and the kit's
	   `.ui-tag` already carries the tone-soft fill and tone-line border. */
	.kind {
		align-self: flex-start;
		font-size: var(--font-micro);
		letter-spacing: 0.12em;
	}
	.name {
		margin: 0;
		font-size: var(--font-body);
		font-weight: 600;
		line-height: 1.25;
		color: var(--text);
		overflow-wrap: anywhere;
	}
	/* The description keeps the model's own lines: `pre-line` preserves newlines
	   (a `\n` used to collapse to a space) while collapsing runs of spaces, and it
	   WRAPS — the tag is compact on one line, and long text folds onto more lines
	   instead of being cut off. */
	.description {
		margin: 0;
		font-size: var(--font-body);
		line-height: 1.35;
		color: var(--text-muted);
		white-space: pre-line;
	}
</style>
