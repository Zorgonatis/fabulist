<script lang="ts">
	// VTK flavor: Artifact — an RPG inspection card that FEATURES the object.
	// OpenUI Lang call: Artifact(name, kind, rarity, flavor, stats[], options?).
	// Preset: "Visual Toolkit (VTK)" → Artifact ("over-the-top inspection card
	// with funny stats, icons").
	//
	// Composition: an oversized glyph anchor (the model's `options.glyph`, else a
	// rarity-coded monogram) carries the eye, the name sits in the display
	// register beside it, flavour runs in the prose register at pitch line-height,
	// and `stats[]` stacks as modifier rows. The card sells the item; it does not
	// recap it. `options.art` is deliberately unconsumed — the anchor is the one
	// featured slot.
	//
	// Rarity ramp: the ONE sanctioned fixed palette (game-diegetic convention —
	// docs/UI-LANGUAGE.md §2's documented exception). Each tier sets scoped
	// `--r-*` variables that drive border weight, ink colour, aura intensity and
	// corner marks; they are declared here and never promoted into the app's tone
	// tokens. Intensity scales with the tier, the card's structure never changes.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { ArtifactProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<ArtifactProps> = $props();

	type Rarity = 'trash' | 'common' | 'uncommon' | 'rare' | 'epic' | 'legendary' | 'cursed';

	// Tiers, dim → loud. Synonyms a narrator might reach for fold into the nearest
	// tier; an unrecognised word reads as `common` rather than dropping the ramp.
	const RARITY_ALIASES: Record<string, Rarity> = {
		junk: 'trash',
		broken: 'trash',
		normal: 'common',
		plain: 'common',
		unusual: 'uncommon',
		enchanted: 'rare',
		mythic: 'epic',
		divine: 'legendary',
		hexed: 'cursed',
		damned: 'cursed'
	};
	const RARITIES = ['trash', 'common', 'uncommon', 'rare', 'epic', 'legendary', 'cursed'] as const;
	const isRarity = (word: string): word is Rarity => (RARITIES as readonly string[]).includes(word);

	const rarity: Rarity = $derived.by(() => {
		const word = (props.rarity ?? '').trim().toLowerCase();
		return isRarity(word) ? word : (RARITY_ALIASES[word] ?? 'common');
	});
	// The label shows the narrator's own word; an empty one falls back to the tier.
	const rarityLabel = $derived((props.rarity ?? '').trim() || rarity);

	const glyph = $derived((props.options?.glyph ?? '').trim());
	// Never empty: no glyph means the name's first letter stands in as the sigil.
	const monogram = $derived(Array.from((props.name ?? '').trim())[0]?.toUpperCase() ?? '◆');

	type Sign = 'pos' | 'neg' | null;
	type StatRow = { label: string; value: string; sign: Sign };

	// Direction is claimed only where the value states it as a signed modifier
	// (`+12`, `−3`): an unsigned number is a quantity, not a modifier, so it and
	// any unparseable value stay neutral.
	function readSign(value: string): Sign {
		const sign = value.match(/^([+±]|[-−–])\s*\d/);
		if (!sign) return null;
		return sign[1] === '-' || sign[1] === '−' || sign[1] === '–' ? 'neg' : 'pos';
	}

	// Schema shape is "Label: value"; a line without a colon stays whole.
	function readStat(line: string): StatRow {
		const text = String(line ?? '').trim();
		const at = text.indexOf(':');
		const label = at === -1 ? '' : text.slice(0, at).trim();
		const value = (at === -1 ? text : text.slice(at + 1)).trim();
		return { label, value, sign: readSign(value) };
	}

	const stats = $derived((props.stats ?? []).map(readStat));
</script>

<article
	class="vtk-artifact ui-frame-diegetic ui-anim-rise"
	data-openui="Artifact"
	data-rarity={rarity}
>
	<span class="aura" aria-hidden="true"></span>
	<span class="corners" aria-hidden="true"></span>

	<header class="head">
		<span class="ui-glyph anchor" class:monogram={!glyph} aria-hidden="true">
			{glyph || monogram}
		</span>
		<div class="titles">
			<h3 class="name">{props.name}</h3>
			<div class="labels">
				{#if props.kind}
					<span class="kind ui-bracket">{props.kind}</span>
				{/if}
				<span class="rarity ui-chip">{rarityLabel}</span>
			</div>
		</div>
	</header>

	{#if props.flavor}
		<p class="flavor">{props.flavor}</p>
	{/if}

	{#if stats.length}
		<p class="readout ui-section">Readout</p>
		<ul class="stats">
			{#each stats as stat, i (i)}
				<li class="stat ui-stat" class:bare={!stat.label} data-sign={stat.sign}>
					<span class="stat-label">{stat.label}</span>
					<span class="stat-value">{stat.value}</span>
				</li>
			{/each}
		</ul>
	{/if}
</article>

<style>
	/* ── the card ─────────────────────────────────────────────────────── */
	.vtk-artifact {
		/* Rarity ramp. The defaults below ARE the `common` tier: white ink, a
		   hairline frame, a whisper of aura, small corner marks. */
		--r: 0, 0%, 88%;
		--r-ink: hsl(var(--r));
		--r-soft: hsl(var(--r), 0.14);
		--r-line: hsl(var(--r), 0.42);
		--r-glow: hsl(var(--r), 0.5);
		--r-weight: 1px;
		--r-aura: 0.1;
		--r-marks: 0.34;
		--r-mark: 10px;
		/* Modifier tones — the universal bonus / penalty convention. */
		--r-pos: hsl(142, 46%, 62%);
		--r-neg: hsl(352, 78%, 66%);

		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		overflow: hidden;
		/* The kit's diegetic frame, re-bordered from the ramp: width, ink and the
		   capped top edge all come from the tier. */
		border: var(--r-weight) solid var(--r-line);
		border-top-width: calc(var(--r-weight) + 1px);
		border-top-color: var(--r-ink);
		/* Static ring, its reach scaled by the tier's aura. */
		box-shadow: 0 0 calc(26px * var(--r-aura)) -6px var(--r-glow);
	}

	/* — trash: junk-grade, dim, flat — */
	.vtk-artifact[data-rarity='trash'] {
		--r: 220, 7%, 58%;
		--r-aura: 0;
		--r-marks: 0.22;
		--r-mark: 9px;
	}
	/* — common: the base defaults above — */
	.vtk-artifact[data-rarity='uncommon'] {
		--r: 142, 48%, 60%;
		--r-aura: 0.2;
		--r-marks: 0.48;
		--r-mark: 12px;
	}
	.vtk-artifact[data-rarity='rare'] {
		--r: 205, 84%, 68%;
		--r-weight: 2px;
		--r-aura: 0.36;
		--r-marks: 0.62;
		--r-mark: 13px;
	}
	.vtk-artifact[data-rarity='epic'] {
		--r: 273, 70%, 72%;
		--r-weight: 2px;
		--r-aura: 0.54;
		--r-marks: 0.74;
		--r-mark: 14px;
	}
	.vtk-artifact[data-rarity='legendary'] {
		--r: 43, 92%, 63%;
		--r-weight: 3px;
		--r-aura: 0.82;
		--r-marks: 0.9;
		--r-mark: 16px;
	}
	.vtk-artifact[data-rarity='cursed'] {
		--r: 352, 80%, 62%;
		--r-weight: 3px;
		--r-aura: 0.74;
		--r-marks: 0.86;
		--r-mark: 15px;
	}

	/* ── aura + corner marks ──────────────────────────────────────────── */
	/* A tinted wash whose opacity IS the tier's aura; the two breathing tiers
	   animate opacity only (box-shadow is never animated, kit rule). */
	.aura {
		position: absolute;
		inset: -30% -18%;
		z-index: 0;
		background: radial-gradient(circle at 50% 12%, var(--r-glow), transparent 62%);
		opacity: calc(var(--r-aura) * 0.55);
		pointer-events: none;
	}
	.vtk-artifact[data-rarity='legendary'] .aura,
	.vtk-artifact[data-rarity='cursed'] .aura {
		animation: artifact-aura 5.5s ease-in-out infinite;
	}
	/* Four corner marks, always present, drawn from the tier ink with gradients:
	   size and opacity are the ramp's, so only the intensity changes. */
	.corners {
		position: absolute;
		inset: 3px;
		z-index: 0;
		opacity: var(--r-marks);
		pointer-events: none;
		background:
			linear-gradient(var(--r-ink), var(--r-ink)) top left / var(--r-mark) 2px no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) top left / 2px var(--r-mark) no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) top right / var(--r-mark) 2px no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) top right / 2px var(--r-mark) no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) bottom left / var(--r-mark) 2px no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) bottom left / 2px var(--r-mark) no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) bottom right / var(--r-mark) 2px no-repeat,
			linear-gradient(var(--r-ink), var(--r-ink)) bottom right / 2px var(--r-mark) no-repeat;
	}

	/* ── head: glyph anchor + name ────────────────────────────────────── */
	.head {
		position: relative;
		z-index: 1;
		display: flex;
		align-items: center;
		gap: 0.85rem;
		padding-bottom: 0.6rem;
		border-bottom: 1px solid var(--line);
	}
	/* The oversized glyph the brief asks for: 48-64px, on its own tinted plate. */
	.anchor {
		flex: none;
		width: 4.75rem;
		height: 4.75rem;
		border: 1px solid var(--r-line);
		border-radius: 10px;
		background:
			radial-gradient(circle at 50% 36%, var(--r-soft), transparent 72%),
			var(--bg-inset);
		font-size: clamp(3rem, 6vw, 4rem);
		color: var(--r-ink);
		text-shadow: 0 0 calc(24px * var(--r-aura)) var(--r-glow);
	}
	/* No glyph from the model: a mono sigil at one step down, same tier ink. */
	.anchor.monogram {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: clamp(2.25rem, 4.6vw, 3rem);
		font-weight: 700;
		letter-spacing: 0.04em;
	}
	.titles {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		min-width: 0;
	}
	.name {
		margin: 0;
		font-size: clamp(1.15rem, 2.4vw, 1.55rem);
		font-weight: 700;
		line-height: 1.15;
		letter-spacing: 0.01em;
		color: var(--text);
		overflow-wrap: anywhere;
		text-shadow: 0 0 calc(14px * var(--r-aura)) var(--r-glow);
	}
	.labels {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.45rem;
	}
	/* The rarity label speaks in the tier's ink; the kind bracket stays quiet. */
	.rarity {
		border-color: var(--r-line);
		background: var(--r-soft);
		color: var(--r-ink);
	}

	/* ── the pitch ────────────────────────────────────────────────────── */
	.flavor {
		position: relative;
		z-index: 1;
		margin: 0;
		padding-left: 0.6rem;
		border-left: 2px solid var(--r-line);
		font-size: var(--font-body);
		line-height: 1.7;
		color: var(--text);
		white-space: pre-line;
	}

	/* ── readout ──────────────────────────────────────────────────────── */
	/* Kit's `▶` section marker, tinted to the tier for this card only. */
	.readout {
		--tone: var(--r-ink);
		position: relative;
		z-index: 1;
		/* Hugs the rows it labels rather than floating between them. */
		margin: 0.1rem 0 -0.15rem;
	}
	.stats {
		position: relative;
		z-index: 1;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.stat {
		padding: 0.35rem 0;
	}
	.stat + .stat {
		border-top: 1px solid var(--line);
	}
	/* A colon-less line is a quantity, not a pair: let it span the row. */
	.stat.bare {
		grid-template-columns: minmax(0, 1fr);
	}
	.stat.bare > .stat-label {
		display: none;
	}
	.stat-value {
		font-variant-numeric: tabular-nums;
	}
	.stat[data-sign='pos'] .stat-value {
		color: var(--r-pos);
	}
	.stat[data-sign='neg'] .stat-value {
		color: var(--r-neg);
	}

	@keyframes artifact-aura {
		0%,
		100% {
			opacity: calc(var(--r-aura) * 0.3);
		}
		50% {
			opacity: calc(var(--r-aura) * 0.8);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.aura {
			animation: none;
		}
	}
</style>
