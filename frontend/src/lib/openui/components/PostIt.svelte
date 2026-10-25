<script lang="ts">
	// VTK flavor: PostIt — one to three sticky notes, pinned to a wall.
	// OpenUI Lang call: PostIt(notes[], options?).
	// Preset: "Artifact reproductions" → PostIt.
	//
	// The COUNT is the model's choice: one note is a confession slipped under a
	// door, three are a conspiracy wall. The component never pads or truncates
	// the beat — 1–3 notes render as a fanned cluster on a quiet wall wash, and
	// zero notes render nothing (the gate rejects an empty list anyway).
	//
	// The SHAPE is a real note, not a square card (owner pick 2026-10-14, ref
	// `postit-12`: "shape is good" — varied by colour). The bottom-right corner is
	// curled, and three pieces do it: the sheet (`::before`) is clipped with that
	// corner cut away, the fold (`::after`) fills the notch with a hard-stop 45°
	// gradient, and a STATIC `drop-shadow` on the fold draws the crease where the
	// flap lifts off. The note's own soft `box-shadow` stays on the root, because
	// `clip-path` clips shadows as well as fills — which is why the paper is a
	// pseudo-element and the shadow is not. The ink sits at `z-index: 1`, above
	// both, so the flap never swallows the last line of a note.
	//
	// COLOUR — the sticky-pastel FAMILY (D58 exception list: CRT phosphor green,
	// BSOD blue, sticky pastels). This component is the generalised case of that
	// last entry: four pastel literals — pale yellow → pink → blue → green — one
	// per note slot by INDEX, each shifted on its lightness axis by `--bg-warmth`
	// so the family warms with the session instead of arriving as foreign hue
	// chips (UI-LANGUAGE §2: derive once at the token level, "surface
	// warmth/coolness shifts derive from `--bg-warmth`; no new hues"). Slot 0 is
	// the canonical sticky yellow `hsl(46 80% 70%)` and is the single-note colour.
	// The fold's two stops are `color-mix()` derivations of the slot tint, so the
	// family is four literals and no more. The ink, the note edge (`--line`, §4
	// chrome kit) and the wall (`var(--bg)`) stay tokens.
	//
	// The pen is italic — owner-resolved 2026-10-14, "something italic for the
	// postits": a humanist/serif chain of broadly-available system faces, no
	// webfont fetch and no single-platform handwriting font.
	//
	// Motion: each note drops in and rotate-settles on the shared 250 ms stagger,
	// so three notes have all landed inside a second. Tilts are deterministic
	// (replay and fork render identically) and everything is off under
	// `prefers-reduced-motion: reduce`.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	// Local prop types — Wave 2 replaces these with `PostItProps` imported from
	// `../schemas/flavors` (zod key order = DSL positional order). `options` is the
	// DSL's uniform trailing argument; PostIt renders nothing from it, so it stays
	// an opaque record rather than a prop with invented behaviour.
	type PostItNote = { text: string };
	type PostItProps = { notes: PostItNote[]; options?: Record<string, unknown> };

	let { props }: ComponentRenderProps<PostItProps> = $props();

	// The gate caps `notes` at 3 in zod; the renderer clamps too, so a hand-edited
	// log can never widen the wall (plan §1.5, §4.4).
	const notes = $derived((props.notes ?? []).slice(0, 3));
</script>

{#if notes.length}
	<div class="vtk-postit ui-frame-diegetic" data-openui="PostIt" data-count={notes.length}>
		<div class="cluster" data-count={notes.length} style="--n: {notes.length}">
			{#each notes as note, i (i)}
				<article class="note" style="--i: {i}">
					<p class="note-text">{note.text}</p>
				</article>
			{/each}
		</div>
	</div>
{/if}

<style>
	.vtk-postit {
		padding: 0; /* the wash runs edge to edge; the kit frame keeps border/radius */
		overflow: hidden;
		color: var(--text);
	}

	/* The wall: `--bg` (already warmed by the session's `--bg-warmth`) with one
	   soft vertical wash so the notes have somewhere to cast a shadow. */
	.cluster {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr);
		place-items: center;
		padding: 1.4rem 1rem 1.6rem;
		background-color: var(--bg);
		background-image: linear-gradient(
			180deg,
			color-mix(in srgb, var(--bg-raised) 55%, transparent),
			transparent 72%
		);
	}

	/* Every note shares one grid cell, so the later notes lap over the earlier
	   ones — the deterministic `--dx`/`--dy` fan below IS the arrangement, and the
	   cluster stays centred whatever the count.
	   The root carries the arrangement, the reveal and the shadow; the paper and
	   the fold are its two pseudo-elements (see the header: a clipped element
	   cannot cast). */
	.note {
		--note: 7rem; /* square */
		--curl: 1.35rem; /* the folded corner ≈ 19% of the sheet, as in the reference */
		/* Slot 0 of the sticky-pastel family, and the base every note falls back
		   to — so even a hand-edited log stays in the family. */
		--note-tint: hsl(46 80% calc(70% + var(--bg-warmth, 0%)));
		position: relative;
		grid-area: 1 / 1;
		width: var(--note);
		aspect-ratio: 1;
		margin: 0;
		padding: 0.7rem 0.75rem;
		/* The note's own colour, and the only place the slot tint is painted: the
		   fold below is opaque over the square corner this background still keeps. */
		background-color: var(--note-tint);
		/* Soft and two-layered — a wide cast plus a tight contact shade. Static. */
		box-shadow:
			0 14px 16px -13px var(--shadow),
			0 3px 6px -4px var(--shadow);
		transform: translate(var(--dx), var(--dy)) rotate(var(--tilt));
		/* Reveal contract (D61): one stagger constant from chrome.css, with a
		   literal fallback so the pane animates outside a diegetic host too. */
		animation: postit-drop 320ms ease-out both;
		animation-delay: calc(
			var(--i) * min(var(--reveal-stagger, 250ms), var(--reveal-budget, 3500ms) / var(--n, 1))
		);
	}

	/* The sheet — real paper: one faint sheen catching the light from the
	   top-left, the `--line` edge, and the corner cut out of both. The edge
	   breaking at that diagonal is the whole read; a square outline with a
	   triangle drawn inside it reads as an icon, not as a note. The tint itself
	   comes from the note behind this layer. */
	.note::before {
		content: '';
		position: absolute;
		inset: 0;
		background-image: linear-gradient(
			155deg,
			color-mix(in srgb, var(--text) 6%, transparent),
			transparent 58%
		);
		border: 1px solid var(--line);
		clip-path: polygon(
			0 0,
			100% 0,
			100% calc(100% - var(--curl)),
			calc(100% - var(--curl)) 100%,
			0 100%
		);
	}

	/* The fold — the hard 45° stop turns this square into exactly the triangle the
	   sheet lost, so paper and flap meet on one crease line. The stops run from
	   the lit fold-edge into the shade the curled tip falls into; both are derived
	   from the slot tint, so every family colour folds in its own register. */
	.note::after {
		content: '';
		position: absolute;
		right: 0;
		bottom: 0;
		width: var(--curl);
		height: var(--curl);
		background-image: linear-gradient(
			135deg,
			transparent 0 50%,
			color-mix(in srgb, var(--note-tint) 82%, var(--text)) 50%,
			color-mix(in srgb, var(--note-tint) 70%, var(--bg-inset)) 100%
		);
		/* Because the transparent half is not painted, `drop-shadow` follows the
		   TRIANGLE rather than its bounding square: this soft line up-left of the
		   crease is the flap lifting off the wall. Static, like every shadow here. */
		filter: drop-shadow(-1px -1px 1.5px var(--shadow));
		pointer-events: none;
	}

	/* The sticky-pastel family, slotted by INDEX (slot 0 = the base yellow above):
	   pale yellow, pink, blue, green. Each slot takes the same `--bg-warmth` shift
	   on its lightness axis, so the family warms with the session exactly as the
	   canonical yellow does. The 3-note clamp means a wall shows at most the first
	   three; the green slot is written down so the family is declared in one place
	   and stays in-register whenever the cap is revisited. */
	.note:nth-child(2) {
		--note-tint: hsl(340 76% calc(77% + var(--bg-warmth, 0%))); /* pastel pink */
	}
	.note:nth-child(3) {
		--note-tint: hsl(205 72% calc(78% + var(--bg-warmth, 0%))); /* pastel blue */
	}
	.note:nth-child(4) {
		--note-tint: hsl(142 52% calc(76% + var(--bg-warmth, 0%))); /* pastel green */
	}

	/* Deterministic tilt + fan, one rule per count/slot: the tilt order is fixed
	   ([-4deg, +3deg, -2deg]) and notes 2-3 lap note 1's edge by ~1.6rem, so the
	   cluster reads as pinned notes rather than a row. One note sits centred, a
	   pair sits symmetric, three straddle the base note. */
	.cluster[data-count='1'] .note:nth-child(1) {
		--tilt: -4deg;
		--dx: 0rem;
		--dy: 0.4rem;
	}
	.cluster[data-count='2'] .note:nth-child(1) {
		--tilt: -4deg;
		--dx: -3rem;
		--dy: 0.4rem;
	}
	.cluster[data-count='2'] .note:nth-child(2) {
		--tilt: 3deg;
		--dx: 3rem;
		--dy: 1.5rem;
	}
	.cluster[data-count='3'] .note:nth-child(1) {
		--tilt: -4deg;
		--dx: 0rem;
		--dy: 0.4rem;
	}
	.cluster[data-count='3'] .note:nth-child(2) {
		--tilt: 3deg;
		--dx: 5.4rem;
		--dy: 1.5rem;
	}
	.cluster[data-count='3'] .note:nth-child(3) {
		--tilt: -2deg;
		--dx: -5.4rem;
		--dy: 0.1rem;
	}

	/* The pen: humanist/serif italic, system faces only (owner 2026-10-14).
	   Positioned above the sheet and the fold. */
	.note-text {
		position: relative;
		z-index: 1;
		margin: 0;
		font-family: Georgia, 'Iowan Old Style', 'Times New Roman', 'Noto Serif', serif;
		font-style: italic;
		font-size: var(--font-body);
		line-height: 1.35;
		overflow-wrap: anywhere;
		/* Written by hand, so the model's line breaks are kept (they used to
		   collapse to spaces) while runs of spaces still collapse. */
		white-space: pre-line;
		color: var(--bg); /* the ink is the wall's own dark — a token */
	}

	@keyframes postit-drop {
		from {
			opacity: 0;
			transform: translate(var(--dx), calc(var(--dy) - 1.6rem)) rotate(calc(var(--tilt) - 5deg));
		}
		to {
			opacity: 1;
			transform: translate(var(--dx), var(--dy)) rotate(var(--tilt));
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.note {
			animation: none;
		}
	}
</style>
