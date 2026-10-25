<script lang="ts">
	// VTK flavor: Mindscape — inner conflict / cognition / altered perception.
	// OpenUI Lang call: Mindscape(title, debris[], texture, image, mood, options?).
	// Preset: "Visual Toolkit (VTK)" → Mindscape. Biological NPCs only; never
	// repeat prose in the visual.
	//
	// The brief is a mind spilling over itself, not a list: fragments carry their
	// own width, get thrown off the margin they "should" use and are pulled up
	// into the box above them, so the debris collides instead of stacking. Where
	// two adjacent fragments contradict each other, the pair is *drawn* as a
	// fight — the content of this component is the contradiction. Motion carries
	// the drive: weight sets each fragment's amplitude and tempo, so a scream
	// strains against its own box while an aside barely breathes
	// (docs/UI-LANGUAGE.md §4 — keyframes touch opacity/transform/colour only).
	// Colour comes from the shared tone tokens and the chrome kit only — no hue
	// offset, no raw hue (§2, §4).
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { MindscapeProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<MindscapeProps> = $props();

	// The kit's texture primitives. 'none' honours the narrator doc (explicitly
	// drops the backdrop — the model usually pairs it with an image); an absent
	// or unrecognised name still renders grain, because the brief asks for a
	// textured ground.
	const TEXTURES: Record<string, string> = {
		dots: 'ui-texture',
		grid: 'ui-texture-grid',
		grain: 'ui-texture-grain',
		none: ''
	};
	const texture = $derived(TEXTURES[String(props.texture).toLowerCase()] ?? TEXTURES.grain);
	const backdrop = $derived(props.image ? `url(${JSON.stringify(props.image)})` : '');
	// The trailing options object's glyph, when the model supplies one, is this
	// mindscape's mood face: the outward mask pinned in the corner, watching the
	// debris. It is absolutely placed, so it never displaces a fragment.
	const mask = $derived((props.options?.glyph ?? '').trim());

	// Scatter lanes: fixed, index-driven geometry — never random, so the server
	// render and the hydration pass throw the debris into the same places. `dx`
	// slides a fragment off the margin it would otherwise share, `width` breaks
	// the column (wide / narrow / mid), `overlap` pulls it up into its neighbour
	// (the collision) and `tilt` tips it off the baseline. `width` is the whole
	// box and dx + width stay inside 100%, so nothing is clipped by the panel.
	type Lane = { dx: string; width: string; overlap: string; tilt: string };
	const LANES: Lane[] = [
		{ dx: '0%', width: '72%', overlap: '0rem', tilt: '-1.1deg' },
		{ dx: '8%', width: '91%', overlap: '-0.55rem', tilt: '0.9deg' },
		{ dx: '21%', width: '58%', overlap: '-0.35rem', tilt: '-2.1deg' },
		{ dx: '-0.4rem', width: '90%', overlap: '-0.7rem', tilt: '1.3deg' },
		{ dx: '4%', width: '76%', overlap: '-0.45rem', tilt: '-0.6deg' },
		{ dx: '16%', width: '82%', overlap: '-0.6rem', tilt: '1.8deg' }
	];

	// Contradiction: two adjacent fragments fight when their weights are far
	// apart (a spike beside a whisper) or when one is a scream and the other is
	// not. The louder one takes the `loud` stance — thrown to the far margin,
	// right-aligned, wide — and the quieter takes `quiet` — pinned opposite,
	// narrowed, echoed, split off by a rift line. `??=` keeps each fragment's
	// first conflict as its stance, so a chained set stays deterministic.
	const WEIGHT_GAP = 35;
	const SCREAM_RANK = 100; // a scream outshouts a thought at the same weight
	const clampWeight = (weight: number) =>
		Math.min(100, Math.max(0, Number.isFinite(weight) ? weight : 0));
	const rankOf = (item: MindscapeProps['debris'][number]) =>
		clampWeight(item.weight) + (item.kind === 'scream' ? SCREAM_RANK : 0);
	type Stance = 'loud' | 'quiet';

	const field = $derived.by(() => {
		const debris = props.debris ?? [];
		const stance: (Stance | undefined)[] = debris.map(() => undefined);

		for (let i = 1; i < debris.length; i += 1) {
			const prev = debris[i - 1];
			const cur = debris[i];
			const weightSplit =
				Math.abs(clampWeight(prev.weight) - clampWeight(cur.weight)) >= WEIGHT_GAP;
			const crossRegister = (prev.kind === 'scream') !== (cur.kind === 'scream');
			if (!weightSplit && !crossRegister) continue;

			const loud = rankOf(prev) >= rankOf(cur) ? i - 1 : i;
			const quiet = loud === i - 1 ? i : i - 1;
			stance[loud] ??= 'loud';
			stance[quiet] ??= 'quiet';
		}

		return debris.map((item, i) => {
			const lane = LANES[i % LANES.length];
			const level = clampWeight(item.weight) / 100;
			return {
				text: item.text,
				kind: String(item.kind ?? '').toLowerCase(),
				weight: clampWeight(item.weight),
				level,
				lane,
				stance: stance[i] ?? '',
				// The heavier the thought, the higher it lies in the pile.
				z: 2 + Math.round(level * 8),
				rest: (0.55 + level * 0.45).toFixed(2),
				delay: `${i * 90}ms`,
				heavy: level >= 0.6
			};
		});
	});
</script>

<div
	class="vtk-mindscape ui-panel ui-frame-diegetic {texture}"
	class:has-mask={mask !== ''}
	data-openui="Mindscape"
>
	{#if backdrop}
		<span class="ui-glyph backdrop" style:background-image={backdrop} aria-hidden="true"></span>
	{/if}
	<!-- The haze is larger than the panel by design (inset -40%) and drifts; it
	     lives inside its own clip box so that decoration can never enlarge the
	     panel's scrollable overflow — the sizing contract is that the surface fits
	     its box (`scrollWidth === clientWidth`) at every layout. -->
	<span class="haze-clip" aria-hidden="true">
		<span class="haze"></span>
	</span>

	{#if mask}
		<span class="ui-glyph mask" aria-hidden="true">{mask}</span>
	{/if}

	<header class="ui-head head">
		<span class="title">{props.title}</span>
		<span class="ui-glyph scream" aria-hidden="true">
			(ﾟДﾟ)<span class="scream-echo">💢</span>
		</span>
	</header>

	<div class="field">
		{#each field as frag, i (i)}
			<p
				class="ui-alt fragment"
				data-kind={frag.kind}
				data-stance={frag.stance || undefined}
				data-heavy={frag.heavy ? 'true' : undefined}
				style="--w: {frag.weight}; --level: {frag.level}; --z: {frag.z}; --rest: {frag.rest}; --dx: {frag.lane.dx}; --width: {frag.lane.width}; --overlap: {frag.lane.overlap}; --tilt: {frag.lane.tilt}; --delay: {frag.delay}"
			>
				{frag.text}
			</p>
		{/each}
	</div>

	<footer class="ui-foot mood">{props.mood}</footer>
</div>

<style>
	.vtk-mindscape {
		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		overflow: hidden;
		/* The surface fits whatever box it is dropped into: never let a flex/grid
		   parent's content sizing push it past the panel width. */
		box-sizing: border-box;
		min-width: 0;
		max-width: 100%;
	}

	/* ── backdrop layers ─────────────────────────────────────────────── */
	/* The panel itself carries the kit's texture class; the image sits above it
	   as the VTK `.ui-glyph` graphic slot. */
	.backdrop {
		position: absolute;
		inset: 0;
		z-index: 0;
		background-position: center;
		background-size: cover;
		opacity: 0.22;
		filter: grayscale(0.4);
		-webkit-mask-image: radial-gradient(125% 125% at 70% 38%, black 30%, transparent 85%);
		mask-image: radial-gradient(125% 125% at 70% 38%, black 30%, transparent 85%);
		pointer-events: none;
	}
	/* The drift lives in the background: a slow, off-centre haze that breathes
	   behind the thoughts, never in the text opacity. It is oversized and clipped
	   by its own layer, so the panel's box stays the box. */
	.haze-clip {
		position: absolute;
		inset: 0;
		z-index: 0;
		overflow: hidden;
		pointer-events: none;
	}
	.haze {
		position: absolute;
		inset: -40%;
		z-index: 0;
		background: radial-gradient(circle at 55% 45%, var(--tone-1-soft), transparent 62%);
		pointer-events: none;
		animation: mindscape-haze 7.2s ease-in-out infinite;
	}

	/* ── corner mask (trailing options.glyph) ────────────────────────── */
	.vtk-mindscape .mask {
		position: absolute;
		top: 0.6rem;
		right: 0.7rem;
		z-index: 3;
		font-size: clamp(1.35rem, 3vw, 1.9rem);
		line-height: 1;
		color: var(--tone-1);
		opacity: 0.92;
		text-shadow: 0 0 16px var(--tone-1-soft);
		pointer-events: none;
		transform-origin: 55% 60%;
		animation: mindscape-sway 6.8s ease-in-out infinite;
	}
	/* The mask owns the corner; the head gives way to it rather than colliding. */
	.has-mask .head {
		padding-inline-end: 3rem;
	}

	/* ── head: the scream ────────────────────────────────────────────── */
	.head {
		position: relative;
		z-index: 1;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.75rem;
	}
	.title {
		/* Model-authored text: a long unbreakable token must wrap, not widen the
		   head past the panel. */
		min-width: 0;
		overflow-wrap: anywhere;
		padding-left: 0.55rem;
		border-left: 3px solid var(--tone-1);
		font-size: clamp(1rem, 1.6vw, 1.45rem);
		font-weight: 700;
		line-height: 1.15;
		letter-spacing: 0.09em;
		text-transform: uppercase;
		text-wrap: balance;
		color: var(--tone-1);
	}
	/* The enlarged glyph the brief asks for (§5) — the scream made visible, and
	   twitching, because it cannot hold still. */
	.scream {
		flex: none;
		font-size: clamp(2.4rem, 7vw, 3.6rem);
		line-height: 1;
		color: var(--tone-2);
		text-shadow: 0 0 20px var(--tone-1-soft);
		transform-origin: 55% 60%;
		animation: mindscape-sway 4.6s ease-in-out infinite;
	}
	.scream-echo {
		margin-left: -0.12em;
		font-size: max(0.875rem, 0.34em);
		vertical-align: super;
		opacity: 0.9;
	}

	/* ── the scatter field ───────────────────────────────────────────── */
	/* Not a column. Every fragment is off the margin it would share, sized on its
	   own lane and pulled up into the box above it, so the debris collides: the
	   edges cross, the fills stack, and the heavier thought lies on top.

	   Clipped on the inline axis only: the lean and the drift deliberately press
	   into the lane's edge, and a surface that is asked to fit its box must not
	   report that decoration as overflowing content (`scrollWidth`). Vertical
	   stays visible so the pile keeps its overlap and its glow. */
	.field {
		position: relative;
		z-index: 1;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		overflow-x: clip;
		overflow-y: visible;
	}
	.fragment {
		/* A mid-lane fragment. The inline style supplies every value below; the
		   defaults keep the rule sane if a payload arrives ragged. */
		--w: 50;
		--level: 0.5;
		--z: 4;
		--rest: 0.78;
		--dx: 0%;
		--width: 100%;
		--overlap: 0rem;
		--tilt: 0deg;
		--delay: 0ms;
		/* The motion vocabulary, all of it weight-driven: `--amp` is how far a
		   fragment wanders, `--jolt` is the scream's shake, `--dur` its tempo. */
		--amp: calc(0.9px + var(--w) * 0.055px);
		--jolt: calc(0.8px + var(--w) * 0.035px);
		--scale: calc(0.96 + var(--level) * 0.07);
		--dur: calc(6800ms - var(--w) * 46ms);
		position: relative;
		box-sizing: border-box;
		z-index: var(--z);
		width: var(--width);
		margin: 0;
		margin-inline-start: var(--dx);
		margin-block-start: var(--overlap);
		padding: 0.22rem 0.55rem;
		font-size: clamp(var(--font-ui), calc(var(--font-ui) + var(--w) * 0.1px), 1.5rem);
		line-height: 1.35;
		overflow-wrap: anywhere;
		/* A fragment is the model's own text: keep its line breaks, collapse its
		   space runs, and let it wrap inside the lane. */
		white-space: pre-line;
		color: color-mix(
			in srgb,
			var(--text) calc(88% - var(--level) * 26%),
			var(--tone, var(--tone-1))
		);
		background: var(--tone-soft, var(--tone-1-soft));
		border-left: 2px solid var(--tone-line, var(--tone-1-line));
		opacity: var(--rest);
		transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		transform-origin: left center;
		/* Two tracks: the fragment arrives, then its kind's loop takes over.
		   The loop's delay sits past the arrival so the two never fight over
		   `transform` — and every loop starts on the arrival's end state. */
		animation-name: mindscape-arrive, mindscape-drift;
		animation-duration: 0.55s, var(--dur);
		animation-timing-function: ease-out, ease-in-out;
		animation-delay: var(--delay), calc(var(--delay) + 0.6s);
		animation-iteration-count: 1, infinite;
		animation-fill-mode: both, none;
	}
	/* A scream is the loudest register: oversized, mono, letterspaced, framed —
	   a box with something inside it pressing out. */
	.field .fragment[data-kind='scream'] {
		--dur: calc(2900ms - var(--w) * 9ms);
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: clamp(var(--font-ui), calc(var(--font-ui) + var(--w) * 0.36px), 3.4rem);
		font-weight: 700;
		line-height: 1.05;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		background: var(--tone-soft, var(--tone-1-soft));
		border: 1px solid var(--tone-line, var(--tone-1-line));
		border-left: 3px solid var(--tone-1);
		text-shadow: 0 0 18px var(--tone-1-soft);
		animation-name: mindscape-arrive, mindscape-strain;
		animation-timing-function: ease-out, linear;
	}
	/* …and a second, tighter frame inside the box that shakes out of true: the
	   strain is visible because the cage gives before the thought does. */
	.field .fragment[data-kind='scream']::after {
		content: '';
		position: absolute;
		inset: 0.18rem;
		border: 1px dashed var(--tone-2-line);
		pointer-events: none;
		animation: mindscape-cage calc(var(--dur) * 0.9) ease-in-out infinite;
	}
	/* An aside stays at the smallest step — the quiet background mutter, barely
	   breathing. */
	.field .fragment[data-kind='aside'] {
		--amp: calc(0.4px + var(--w) * 0.02px);
		--dur: calc(9200ms - var(--w) * 14ms);
		font-size: var(--font-ui);
		font-style: italic;
		animation-name: mindscape-arrive, mindscape-breath;
	}
	/* A heavy thought burns: static glow only — box-shadow is never animated and
	   the pulse itself rides on the drift's opacity. */
	.field .fragment[data-heavy='true'] {
		text-shadow: 0 0 14px var(--tone-soft, var(--tone-1-soft));
	}

	/* ── contradiction: the pair is the content ──────────────────────── */
	/* The louder fragment is thrown to the far margin, right-aligned and wide,
	   bleeding past the panel's inner edge; the quieter one is pinned opposite,
	   narrowed, echoed slightly out of register and split off by a rift line —
	   the thought under the thought, and the line it cannot cross. */
	.field .fragment[data-stance='loud'] {
		--tone: var(--tone-1);
		--tone-soft: var(--tone-1-soft);
		--tone-line: var(--tone-1-line);
		margin-inline-start: auto;
		margin-inline-end: -0.3rem;
		/* Held just short of the field's edge: the loud fragment presses into the
		   panel's padding, but its drift never reaches the clip edge. */
		width: min(99%, calc(var(--width) + 10%));
		text-align: right;
		transform-origin: right center;
		border-left: 0;
		border-right: 3px solid var(--tone-1);
	}
	.field .fragment[data-stance='quiet'] {
		margin-inline-start: calc(var(--dx) - 0.4rem);
		max-width: 88%;
		transform-origin: left center;
	}
	.field .fragment[data-stance='quiet']:not([data-kind='scream']) {
		text-shadow:
			0.07em 0 0 color-mix(in srgb, var(--tone-2) 40%, transparent),
			-0.07em 0 0 color-mix(in srgb, var(--tone-3) 40%, transparent);
	}
	/* The rift the pair opens between them. */
	.field .fragment[data-stance='quiet']:not(:first-child)::before {
		content: '';
		position: absolute;
		top: -0.42rem;
		inset-inline: -5% 12%;
		height: 1px;
		background: linear-gradient(
			90deg,
			transparent,
			var(--tone-line, var(--tone-1-line)) 15% 80%,
			transparent
		);
		pointer-events: none;
		transform: rotate(-2.4deg);
		animation: mindscape-shear 5.4s ease-in-out infinite;
	}

	.mood {
		position: relative;
		z-index: 1;
		border-top-style: dashed;
		border-top-color: var(--tone-1-line);
		font-style: italic;
		color: var(--tone-1);
		white-space: pre-line;
		overflow-wrap: anywhere;
	}

	/* ── keyframes (opacity / transform only) ────────────────────────── */
	@keyframes mindscape-arrive {
		from {
			opacity: 0;
			transform: translate(-0.5rem, 6px) rotate(var(--tilt)) scale(calc(var(--scale) * 0.94));
		}
		to {
			opacity: var(--rest);
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
	}
	/* A thought's own weather: it drifts, dims and lifts at its own tempo —
	   heavier thoughts travel further, faster, and breathe harder. */
	@keyframes mindscape-drift {
		0%,
		100% {
			opacity: var(--rest);
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
		35% {
			opacity: calc(var(--rest) + 0.07);
			transform: translate(var(--amp), calc(var(--amp) * -0.7))
				rotate(calc(var(--tilt) + 0.6deg)) scale(var(--scale));
		}
		70% {
			opacity: calc(var(--rest) - 0.05);
			transform: translate(calc(var(--amp) * -0.8), calc(var(--amp) * 0.5))
				rotate(calc(var(--tilt) - 0.5deg)) scale(var(--scale));
		}
	}
	/* A scream does not drift, it strains: a jolt burst, then a swell that pushes
	   the text up and over its neighbours before it snaps back. */
	@keyframes mindscape-strain {
		0% {
			opacity: var(--rest);
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
		3% {
			transform: translate(calc(var(--jolt) * -1.1), var(--jolt))
				rotate(calc(var(--tilt) - 1deg)) scale(calc(var(--scale) * 1.02));
		}
		6% {
			transform: translate(var(--jolt), calc(var(--jolt) * -1.5))
				rotate(calc(var(--tilt) + 1.1deg)) scale(calc(var(--scale) * 1.05));
		}
		9% {
			transform: translate(calc(var(--jolt) * -0.6), var(--jolt))
				rotate(calc(var(--tilt) - 0.5deg)) scale(calc(var(--scale) * 1.03));
		}
		13% {
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
		46% {
			opacity: 1;
			transform: translate(0, calc(var(--amp) * -1.9)) rotate(var(--tilt))
				scale(calc(var(--scale) * 1.07));
		}
		70% {
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
		100% {
			opacity: var(--rest);
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
	}
	@keyframes mindscape-cage {
		0%,
		100% {
			transform: rotate(0) scale(1);
			opacity: 0.7;
		}
		12% {
			transform: rotate(-0.9deg) scale(0.985);
			opacity: 1;
		}
		36% {
			transform: rotate(0.8deg) scale(1.03);
			opacity: 0.5;
		}
		52% {
			transform: rotate(0) scale(1.05);
			opacity: 0.95;
		}
	}
	/* An aside all but still. */
	@keyframes mindscape-breath {
		0%,
		100% {
			opacity: var(--rest);
			transform: translate(0, 0) rotate(var(--tilt)) scale(var(--scale));
		}
		50% {
			opacity: calc(var(--rest) + 0.1);
			transform: translate(calc(var(--amp) * 0.7), calc(var(--amp) * -1))
				rotate(var(--tilt)) scale(calc(var(--scale) * 1.006));
		}
	}
	@keyframes mindscape-shear {
		0%,
		100% {
			transform: rotate(-2.6deg) translateX(-1.5%);
			opacity: 0.5;
		}
		50% {
			transform: rotate(-1.1deg) translateX(2%);
			opacity: 1;
		}
	}
	@keyframes mindscape-haze {
		0%,
		100% {
			opacity: 0.4;
			transform: translate3d(-7%, -4%, 0) scale(1);
		}
		50% {
			opacity: 0.85;
			transform: translate3d(7%, 4%, 0) scale(1.1);
		}
	}
	@keyframes mindscape-sway {
		0%,
		100% {
			transform: rotate(1.6deg) translateY(0) scale(1);
		}
		50% {
			transform: rotate(-1.4deg) translateY(2px) scale(1.035);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		/* No motion at all — but the scatter, the stances, the rift, the glow and
		   the weight grading are declared outside the keyframes, so the field still
		   collides and still contradicts. These selectors deliberately out-specify
		   the per-kind `animation-name` rules (and the pseudo-elements) above: a
		   media query adds no specificity of its own. */
		.vtk-mindscape .field .fragment,
		.vtk-mindscape .field .fragment[data-kind]::after,
		.vtk-mindscape .field .fragment[data-stance]::before,
		.vtk-mindscape .haze,
		.vtk-mindscape .scream,
		.vtk-mindscape .mask {
			animation: none;
		}
	}
</style>
