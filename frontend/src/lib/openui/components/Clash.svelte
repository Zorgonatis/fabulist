<script lang="ts">
	// VTK flavor: Clash — physical conflict, combat, confrontations.
	// OpenUI Lang call: Clash(attacker, defender, intensity, note, options?).
	// Preset: "Visual Toolkit (VTK)" → Clash. Impact frames, staged stances,
	// tension indicators, strike emphasis — never repeat the prose.
	//
	// This is an IMPACT FRAME, not a labelled row: two combat rails meet at a
	// central burst and the frame itself takes the hit. `intensity` (0-100) is
	// the runtime modulation signal — it drives the flash overlay, the shake
	// amplitude on the burst and, past two-thirds, a cracked, glitching border.
	// Colour comes from the shared tone tokens only (docs/UI-LANGUAGE.md §2, §4).
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { ClashProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<ClashProps> = $props();

	const pct = $derived(Math.min(100, Math.max(0, props.intensity)));
	// 0..1 — the single number the motion maths reads.
	const level = $derived(pct / 100);
	// The preset's impact mark when the narrator passes no glyph.
	const burstGlyph = $derived((props.options?.glyph ?? '').trim() || '💢');
	// Past two-thirds the frame gives way.
	const cracked = $derived(pct >= 67);
</script>

<figure class="vtk-clash" data-openui="Clash" style="--level: {level}">
	<div class="frame ui-frame-diegetic" class:cracked>
		<div class="stage">
			<div class="rail attacker">
				<span class="rail-role">attacker</span>
				<span class="name">{props.attacker}</span>
			</div>

			<div class="impact" aria-hidden="true">
				<span class="strikes"></span>
				<span class="burst">{burstGlyph}</span>
			</div>

			<div class="rail defender">
				<span class="rail-role">defender</span>
				<span class="name">{props.defender}</span>
			</div>
		</div>

		<div
			class="tension"
			role="meter"
			aria-label="Impact intensity"
			aria-valuenow={pct}
			aria-valuemin="0"
			aria-valuemax="100"
		>
			<span class="fill"></span>
		</div>

		<span class="flash" aria-hidden="true"></span>
		<span class="glitch" aria-hidden="true"></span>
		<span class="crack" aria-hidden="true"></span>
	</div>

	<figcaption class="caption">{props.note}</figcaption>
</figure>

<style>
	.vtk-clash {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		margin: 0;
		font-size: var(--font-body);
		color: var(--text);
	}

	/* The frame takes the hit: a soft impact wash behind the rails, plus the
	   positioning + clipping the overlays need. */
	.frame {
		position: relative;
		isolation: isolate;
		overflow: hidden;
		padding: 0.85rem 0.95rem 0.7rem;
		background:
			radial-gradient(120% 150% at 50% 42%, var(--tone-1-soft), transparent 62%),
			var(--bg-inset);
	}

	/* ── combat rails — asymmetry says who presses ─────────────────────── */
	.stage {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
		align-items: center;
		gap: 0.55rem;
	}
	.rail {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		min-width: 0;
	}
	.rail-role {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.18em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	/* Display register: the names carry the frame. */
	.name {
		font-size: clamp(1.05rem, 2.6vw, 1.4rem);
		font-weight: 700;
		line-height: 1.05;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		overflow-wrap: anywhere;
	}
	/* Attacker: left, toned, heavier rule — the one pressing. */
	.attacker {
		padding-left: 0.6rem;
		border-left: 3px solid var(--tone-1);
	}
	.attacker .name,
	.attacker .rail-role {
		color: var(--tone-1);
	}
	/* Defender: right, quieter, neutral rule — the one bracing. */
	.defender {
		align-items: flex-end;
		padding-right: 0.6rem;
		border-right: 3px solid var(--line-strong);
		text-align: right;
	}
	.defender .name {
		font-size: clamp(0.9375rem, 2vw, 1.15rem);
		font-weight: 600;
		color: var(--text-muted);
	}

	/* ── central divider + impact burst ───────────────────────────────── */
	.impact {
		position: relative;
		display: grid;
		place-items: center;
		width: clamp(3.4rem, 9vw, 4.6rem);
		height: clamp(3.4rem, 9vw, 4.6rem);
	}
	/* The divider the burst is pinned to. */
	.impact::before {
		content: '';
		position: absolute;
		top: -28%;
		bottom: -28%;
		width: 1px;
		background: linear-gradient(
			180deg,
			transparent,
			var(--line-strong) 22%,
			var(--line-strong) 78%,
			transparent
		);
	}
	/* Radiating strike lines: a masked ring of conic wedges behind the mark. */
	.strikes {
		position: absolute;
		inset: -16%;
		border-radius: 50%;
		background: repeating-conic-gradient(
			from 0deg,
			var(--tone-1-line) 0deg 2.6deg,
			transparent 2.6deg 22.5deg
		);
		-webkit-mask-image: radial-gradient(circle, transparent 30%, black 36%, black 68%, transparent 74%);
		mask-image: radial-gradient(circle, transparent 30%, black 36%, black 68%, transparent 74%);
		opacity: calc(0.16 + var(--level) * 0.74);
		animation: clash-strikes 10s linear infinite;
	}
	.burst {
		position: relative;
		z-index: 1;
		font-size: clamp(2rem, 6vw, 2.7rem);
		line-height: 1;
		text-shadow: 0 0 12px var(--tone-1-soft);
		animation: clash-burst 0.44s ease-out infinite;
	}

	/* ── tension readout ──────────────────────────────────────────────── */
	.tension {
		height: 6px;
		margin-top: 0.55rem;
		border-radius: 999px;
		background: var(--line);
		overflow: hidden;
	}
	.fill {
		display: block;
		height: 100%;
		width: calc(var(--level) * 100%);
		background: linear-gradient(90deg, var(--tone-2), var(--tone-1));
	}

	/* ── intensity-modulated motion ───────────────────────────────────── */
	/* The flash: opacity scales with `--level`, cadence quickens with it. */
	.flash {
		position: absolute;
		inset: 0;
		z-index: 2;
		pointer-events: none;
		background: radial-gradient(circle at 50% 45%, var(--tone-1-line), transparent 60%);
		opacity: 0;
		animation: clash-flash calc(1.35s - var(--level) * 0.75s) ease-out infinite;
	}
	/* High intensity cracks the frame: an offset border that jitters. */
	.glitch {
		position: absolute;
		inset: 3px;
		z-index: 3;
		pointer-events: none;
		border: 1px solid var(--tone-1-line);
		opacity: 0;
	}
	.frame.cracked .glitch {
		opacity: 1;
		animation: clash-glitch 2.6s steps(2, end) infinite;
	}
	/* Fracture bolts cut from corner triangles — no image, no SVG. */
	.crack {
		position: absolute;
		inset: 0;
		z-index: 3;
		pointer-events: none;
		opacity: 0;
		transition: opacity 0.3s ease-out;
	}
	.frame.cracked .crack {
		opacity: 1;
	}
	.crack::before,
	.crack::after {
		content: '';
		position: absolute;
		width: 42%;
		height: 42%;
		background: var(--tone-1-line);
		clip-path: polygon(0% 0%, 38% 28%, 100% 100%, 100% 98.5%, 36.5% 29.5%, 1.5% 1.5%);
	}
	.crack::before {
		top: 0;
		left: 0;
	}
	.crack::after {
		right: 0;
		bottom: 0;
		transform: rotate(180deg);
	}

	/* Note as a caption beneath the frame (prose register). `pre-line` keeps the
	   model's own line breaks — a caption used to run them together as spaces. */
	.caption {
		margin: 0;
		padding-left: 0.6rem;
		border-left: 2px solid var(--tone-1-line);
		font-size: var(--font-body);
		line-height: 1.45;
		font-style: italic;
		color: var(--text-muted);
		white-space: pre-line;
	}

	@keyframes clash-burst {
		0%,
		100% {
			transform: translate(0, 0) rotate(0deg);
		}
		20% {
			transform: translate(calc(var(--level) * -4px), calc(var(--level) * 2px))
				rotate(calc(var(--level) * -6deg));
		}
		40% {
			transform: translate(calc(var(--level) * 4px), calc(var(--level) * -2.5px))
				rotate(calc(var(--level) * 6deg));
		}
		60% {
			transform: translate(calc(var(--level) * -2.5px), calc(var(--level) * -1.5px))
				rotate(calc(var(--level) * 4deg));
		}
		80% {
			transform: translate(calc(var(--level) * 2.5px), calc(var(--level) * 2px))
				rotate(calc(var(--level) * -4deg));
		}
	}
	@keyframes clash-strikes {
		to {
			transform: rotate(360deg);
		}
	}
	@keyframes clash-flash {
		0%,
		70%,
		100% {
			opacity: 0;
		}
		78% {
			opacity: calc(0.2 + var(--level) * 0.6);
		}
	}
	@keyframes clash-glitch {
		0%,
		100% {
			transform: translate(0, 0);
		}
		40% {
			transform: translate(1px, -1px);
		}
		80% {
			transform: translate(-1px, 1px);
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.burst,
		.strikes,
		.flash,
		.frame.cracked .glitch {
			animation: none;
		}
		.crack {
			transition: none;
		}
	}
</style>
