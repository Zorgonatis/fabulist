<script lang="ts">
	// VTK flavor: Monitor — the machine's own screen, not narration about it.
	// OpenUI Lang call: Monitor(mode, title, lines[], {theme?}).
	// Preset: "Artifact reproductions" → Monitor.
	//
	// TWO COMPOSITIONS, one surface. `error` is a modal system dialog parked on a
	// dark desktop field; `bsod` IS the field — full-bleed, no chrome, and the
	// component renders the iconic `:(` itself. They are not one layout with a new
	// accent: the dialog is a window on a desktop, the crash screen is the screen.
	// The narrator supplies the words (`title` = dialog caption / stop code,
	// `lines[]` render verbatim — "0% complete", "For more information…"); the
	// component adds only what the machine owns: the sad face, the dead OK
	// button, the cursor.
	//
	// Colour (plan §1.4, the enumerated A4 exception list): `digital` is neutral
	// tokens — `--bg-panel` desktop, dialog edged in `--tone-1-line` under a
	// `--tone-1` caption and a dead ✕ close glyph (the reference's red is NOT
	// reproduced; the glyph is derived from `--tone-1`). `crt` is phosphor green
	// `hsl(130 90% 60%)` on near-black (`--bg`) with a scanline overlay, a phosphor
	// glow and a curved-glass edge vignette, under a 500 ms warm-up. `bsod` is
	// inherent blue `hsl(222 62% 38%)` with neutral `--text`, REGARDLESS of theme;
	// its modern layout pairs the `:(` and progress lines with a purely decorative
	// CSS QR block (a felt module grid — no encoding, no image). Those two literals
	// are the complete hardcoded-colour list in this component.
	//
	// Motion: the dialog pops (scale .96→1, 180 ms); the BSOD cuts instantly and
	// blinks exactly one cursor; CRT warms in 500 ms plus a persistent scanline
	// shimmer (infinite loop, matching the `iface-signal` precedent). Everything
	// is readable inside 600 ms, and all of it is off under
	// `prefers-reduced-motion: reduce`.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	// Local prop types — Wave 2 replaces these with `MonitorProps` imported from
	// `../schemas/flavors` (zod key order = DSL positional order).
	type MonitorOptions = { theme?: string };
	type MonitorProps = { mode: string; title: string; lines: string[]; options?: MonitorOptions };

	let { props }: ComponentRenderProps<MonitorProps> = $props();

	const MODES = ['error', 'bsod'] as const;
	type Mode = (typeof MODES)[number];
	const THEMES = ['digital', 'crt'] as const;
	type Theme = (typeof THEMES)[number];

	// An unrecognised mode still has to render: fall back to the error dialog
	// rather than to a frame with no variant styles at all (Interface convention).
	const mode = $derived<Mode>(
		MODES.includes((props.mode ?? '').toLowerCase() as Mode)
			? ((props.mode ?? '').toLowerCase() as Mode)
			: 'error'
	);
	// Ditto the theme: an unknown word is the neutral desktop, never the phosphor.
	const theme = $derived<Theme>(
		THEMES.includes((props.options?.theme ?? '').toLowerCase() as Theme)
			? ((props.options?.theme ?? '').toLowerCase() as Theme)
			: 'digital'
	);
	// The gate caps `lines` at 12 in zod; the renderer clamps too, so a
	// hand-edited log can never blow the screen up (plan §1.4 caps, §4.4).
	const lines = $derived((props.lines ?? []).slice(0, 12));
</script>

<section class="vtk-monitor ui-frame-diegetic" data-openui="Monitor" data-mode={mode} data-theme={theme}>
	{#if theme === 'crt'}
		<span class="scanlines" data-crt="scanlines" aria-hidden="true"></span>
		<span class="vignette" data-crt="vignette" aria-hidden="true"></span>
	{/if}

	{#if mode === 'bsod'}
		<div class="crash">
			<p class="sad" aria-hidden="true">:(</p>
			<p class="stop">{props.title}</p>
			<ul class="progress">
				{#each lines as line, i (i)}
					<li class="progress-line">{line}</li>
				{/each}
			</ul>
			<div class="info">
				<span class="qr" data-bsod="qr" aria-hidden="true"></span>
				<span class="cursor" aria-hidden="true"></span>
			</div>
		</div>
	{:else}
		<div class="desktop ui-texture">
			<div class="dialog" role="dialog" aria-label={props.title}>
				<div class="titlebar">
					<span class="dialog-title">{props.title}</span>
					<span class="dialog-close" data-error="close" aria-hidden="true">✕</span>
				</div>
				<div class="dialog-body">
					<ul class="lines">
						{#each lines as line, i (i)}
							<li class="line">{line}</li>
						{/each}
					</ul>
				</div>
				<div class="actions">
					<button class="ok" type="button" disabled>OK</button>
				</div>
			</div>
		</div>
	{/if}
</section>

<style>
	.vtk-monitor {
		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		padding: 0; /* the screen runs edge to edge; the kit frame keeps border/radius */
		overflow: hidden;
		/* The machine register: monospace, never the prose face. */
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 15px; /* the 14px floor, with room for the crash register */
		line-height: 1.45;
		color: var(--text);
	}

	/* ── error: a dialog on a dark desktop field ─────────────────────────── */
	.desktop {
		position: relative;
		z-index: 1;
		display: grid;
		place-items: center;
		min-height: 9.5rem;
		padding: 1.7rem 1.2rem;
		background-color: var(--bg-panel);
	}
	.dialog {
		width: min(22rem, 100%);
		border: 1px solid var(--tone-1-line);
		border-radius: 4px;
		background-color: var(--bg-raised);
		box-shadow: 0 12px 22px -14px var(--shadow); /* static — never animated */
		animation: monitor-pop 180ms ease-out both;
	}
	.titlebar {
		display: flex;
		align-items: center;
		padding: 0.4rem 0.6rem;
		border-bottom: 1px solid var(--tone-1-line);
		background-color: var(--tone-1-soft);
	}
	.dialog-title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--font-ui);
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--tone-1);
	}
	/* The title-bar close glyph: the reference's red square is NOT reproduced —
	   it is chip-styled from the tone tokens. Decorative and dead, like the OK. */
	.dialog-close {
		flex: none;
		display: grid;
		place-items: center;
		width: 1.15rem;
		height: 1.15rem;
		border: 1px solid var(--tone-1-line);
		border-radius: 2px;
		background-color: var(--tone-1-soft);
		color: var(--tone-1);
		font-size: var(--font-ui);
		line-height: 1;
	}
	.dialog-body {
		padding: 0.7rem 0.7rem 0.25rem;
	}
	/* Verbatim: the narrator's fault lines keep their own punctuation and spacing. */
	.lines {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.line {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		padding: 0.5rem 0.65rem 0.6rem;
	}
	/* The dialog's one affordance, and it is dead: no handler, `disabled`. */
	.ok {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.25rem 1.15rem;
		border: 1px solid var(--line-strong);
		border-radius: 3px;
		background-color: var(--bg-inset);
		color: var(--text-dim);
	}
	.ok:disabled {
		opacity: 1;
		cursor: default;
	}

	/* ── bsod: the field itself, full bleed ──────────────────────────────── */
	.crash {
		position: relative;
		z-index: 1;
		display: flex;
		flex: 1;
		flex-direction: column;
		gap: 0.55rem;
		padding: 1.8rem 1.4rem 2rem;
		background-color: var(--bsod);
		color: var(--text);
	}
	.sad {
		margin: 0;
		font-size: 3.6rem;
		line-height: 1;
	}
	.stop {
		margin: 0;
		font-size: var(--font-lg);
		letter-spacing: 0.02em;
		overflow-wrap: anywhere;
	}
	.progress {
		margin: 0.3rem 0 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		font-size: var(--font-ui);
		color: color-mix(in srgb, var(--text) 82%, transparent);
	}
	.progress-line {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	/* The modern crash floor: the decorative code block and the alive cursor sit
	   together, pushed to the bottom the way monitor-20 lays out its small print. */
	.info {
		display: flex;
		align-items: flex-end;
		justify-content: space-between;
		margin-top: auto;
		padding-top: 0.7rem;
	}
	/* A QR suggestion only: a felt module grid, pure CSS, never an encoding and
	   never an image (plan §1.4). Drawn in the crash's own blue on a white field. */
	.qr {
		flex: none;
		width: 3rem;
		height: 3rem;
		background-color: var(--text);
		background-image: repeating-conic-gradient(var(--bsod) 0% 25%, transparent 0% 50%);
		background-size: 0.4rem 0.4rem;
		opacity: 0.92;
	}
	.cursor::after {
		content: '▋';
		animation: monitor-blink 560ms steps(2, end) 1; /* one blink, then it holds */
	}

	/* ── bsod: inherent blue, neutral text, under EVERY theme ────────────── */
	.vtk-monitor[data-mode='bsod'] {
		--bsod: hsl(222 62% 38%);
		background-color: var(--bsod);
		color: var(--text);
	}

	/* ── theme: crt — phosphor on near-black, one literal ────────────────── */
	.vtk-monitor[data-theme='crt'] {
		--phosphor: hsl(130 90% 60%);
	}
	/* The warm-up belongs to the screen, not the crash: a BSOD cuts instantly. */
	.vtk-monitor[data-mode='error'][data-theme='crt'] {
		background-color: var(--bg);
		color: var(--phosphor);
		/* Two-layer phosphor bloom: a tight core plus a wide, dimmer halo. */
		text-shadow:
			0 0 6px color-mix(in srgb, var(--phosphor) 46%, transparent),
			0 0 16px color-mix(in srgb, var(--phosphor) 22%, transparent);
		animation: monitor-warm 500ms ease-out both;
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .desktop {
		background-color: var(--bg);
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .dialog {
		border-color: color-mix(in srgb, var(--phosphor) 55%, transparent);
		background-color: color-mix(in srgb, var(--phosphor) 6%, var(--bg));
		box-shadow: none;
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .titlebar {
		border-bottom-color: color-mix(in srgb, var(--phosphor) 45%, transparent);
		background-color: color-mix(in srgb, var(--phosphor) 10%, transparent);
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .dialog-title,
	.vtk-monitor[data-mode='error'][data-theme='crt'] .line {
		color: var(--phosphor);
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .ok {
		border-color: color-mix(in srgb, var(--phosphor) 50%, transparent);
		background-color: transparent;
		color: var(--phosphor);
	}
	.vtk-monitor[data-mode='error'][data-theme='crt'] .dialog-close {
		border-color: color-mix(in srgb, var(--phosphor) 50%, transparent);
		background-color: color-mix(in srgb, var(--phosphor) 12%, transparent);
		color: var(--phosphor);
	}
	/* The scanlines sit over everything, on both modes, whenever the machine is
	   the glass kind — a BSOD on a CRT still has scanlines. */
	.scanlines {
		position: absolute;
		inset: 0;
		z-index: 3;
		pointer-events: none;
		/* Richer tube treatment (monitor-05): visible horizontal scanlines plus a
		   faint vertical aperture grille, both cut from the near-black field. */
		background-image:
			repeating-linear-gradient(
				0deg,
				color-mix(in srgb, var(--bg) 74%, transparent) 0 1px,
				transparent 1px 3px
			),
			repeating-linear-gradient(
				90deg,
				color-mix(in srgb, var(--bg) 90%, transparent) 0 1px,
				transparent 1px 3px
			);
		animation: monitor-shimmer 3.4s ease-in-out infinite;
	}
	/* Edge curvature: the glass darkens and pinches toward the corners so the flat
	   panel reads as a curved tube. Sits above the scanlines; both are decorative. */
	.vignette {
		position: absolute;
		inset: 0;
		z-index: 4;
		pointer-events: none;
		box-shadow:
			inset 0 0 22px 4px color-mix(in srgb, var(--bg) 72%, transparent),
			inset 0 0 60px 12px color-mix(in srgb, var(--bg) 58%, transparent);
	}

	@keyframes monitor-pop {
		from {
			opacity: 0.5;
			transform: scale(0.96);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@keyframes monitor-warm {
		from {
			opacity: 0.12;
		}
		to {
			opacity: 1;
		}
	}
	@keyframes monitor-shimmer {
		0%,
		100% {
			opacity: 0.8;
		}
		50% {
			opacity: 1;
		}
	}
	@keyframes monitor-blink {
		0%,
		100% {
			opacity: 1;
		}
		50% {
			opacity: 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		/* The theme rule above out-specifies a bare `.vtk-monitor`, so the warm-up is
		   cancelled on its own selector shape. */
		.vtk-monitor,
		.vtk-monitor[data-mode='error'][data-theme='crt'],
		.vtk-monitor .dialog,
		.vtk-monitor .scanlines,
		.vtk-monitor .cursor::after {
			animation: none;
		}
	}
</style>
