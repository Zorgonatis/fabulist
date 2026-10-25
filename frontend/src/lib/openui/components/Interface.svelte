<script lang="ts">
	// VTK flavor: Interface — a device surface, not a list.
	// OpenUI Lang call: Interface(mode, title, lines, {glyph?, art?}).
	// Preset: "Visual Toolkit (VTK)" → Interface.
	//
	// Front-loaded design (owner's north star: "front load design and modulate it
	// at runtime"): the MODE is the design decision and it is made here. Each
	// variant is a different composition — terminal chrome, bubble rows, a scan
	// sweep, a feed column — never the same list with a new accent. The narrator
	// supplies words, a mode, and an optional device glyph.
	//
	// `lines[]` render VERBATIM in monospace with `white-space: pre-wrap`, because
	// dotted leaders and status tags are content the model wrote
	// (`[ OK ] coolant loop ....... stable`) and runs of spaces must survive. Rows
	// carry a mode-appropriate gutter marker; there is deliberately NO row
	// numbering — the old `<ol>` made every mode read as the same numbered list
	// (owner review 2026-10-11: "It's literally just a terminal … the number
	// ordering is bizarre").
	//
	// Colour: the shared tone tokens plus the neutral scale only
	// (docs/UI-LANGUAGE.md §2/§4). No per-mode hue and no hue offsets — the
	// variants differ by structure, register and composition.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { InterfaceProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<InterfaceProps> = $props();

	const MODES = ['terminal', 'phone', 'chat', 'hologram', 'social'] as const;
	type Mode = (typeof MODES)[number];

	// An unrecognised mode still has to render: fall back to the terminal rather
	// than to a frame with no variant styles at all.
	const mode = $derived<Mode>(
		MODES.includes((props.mode ?? '').toLowerCase() as Mode)
			? ((props.mode ?? '').toLowerCase() as Mode)
			: 'terminal'
	);

	// Row gutter markers — the replacement for list numbering.
	const MARKERS: Record<Mode, string> = {
		terminal: '>', // shell prompt
		phone: '▢', // app / notification tile
		chat: '·', // speech dot
		hologram: '◇', // readout delimiter
		social: '@' // handle gutter
	};
	// Header device badge, used when the narrator passed no glyph.
	const BADGES: Record<Mode, string> = {
		terminal: '>_',
		phone: '▢',
		chat: '✦',
		hologram: '◇',
		social: '@'
	};

	const badge = $derived(props.options?.glyph?.trim() || BADGES[mode]);
	const art = $derived((props.options?.art ?? '').trim());
	const bubbles = $derived(mode === 'phone' || mode === 'chat');

	// Social rows are handle-prefixed and timestamped, but the schema gives the
	// model no field for either — it writes them inside the line
	// ("@kess 09:14 handshake ok"). Social mode lifts those two tokens into the
	// row's gutter and stamp so the feed keeps its columns: repositioned, never
	// invented, and nothing else is taken out of the line.
	const HANDLE_RE = /^@[A-Za-z0-9_.-]+\s*/;
	const TIME_RE = /^\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*/;
	type Row = { text: string; handle: string; stamp: string };
	const rows = $derived(
		props.lines.map((line): Row => {
			if (mode !== 'social') return { text: line, handle: '', stamp: '' };
			let text = line;
			let handle = '';
			let stamp = '';
			const handleMatch = HANDLE_RE.exec(text);
			if (handleMatch) {
				handle = handleMatch[0].trim();
				text = text.slice(handleMatch[0].length);
			}
			const timeMatch = TIME_RE.exec(text);
			if (timeMatch) {
				stamp = timeMatch[0].trim();
				text = text.slice(timeMatch[0].length);
			}
			return { text, handle, stamp };
		})
	);
</script>

<section class="vtk-interface ui-frame-diegetic" data-openui="Interface" data-mode={mode}>
	{#if mode === 'hologram'}
		<span class="sweep" aria-hidden="true"></span>
	{/if}

	<header class="bar">
		<span class="badge" aria-hidden="true">{badge}</span>
		<span class="title">{props.title}</span>
		{#if mode === 'terminal' || mode === 'phone'}
			<span class="signal" aria-hidden="true"></span>
		{/if}
	</header>

	{#if mode === 'hologram'}
		<span class="glow-line ui-glow" aria-hidden="true"></span>
	{/if}

	<ul class="lines">
		{#each rows as row, i (i)}
			<li
				class="row {bubbles || mode === 'social' ? 'ui-alt' : ''}"
				data-side={i % 2 === 0 ? 'in' : 'out'}
				style="--i: {i}"
			>
				<span class="marker" aria-hidden={mode !== 'social' || !row.handle}>
					{row.handle || MARKERS[mode]}
				</span>
				<span class="text">{row.text}</span>
				{#if mode === 'terminal' && i === rows.length - 1}
					<span class="cursor" aria-hidden="true"></span>
				{/if}
				{#if row.stamp}
					<span class="stamp">{row.stamp}</span>
				{/if}
			</li>
		{/each}
	</ul>

	{#if art}
		<figure class="art">
			<pre>{art}</pre>
		</figure>
	{/if}
</section>

<style>
	.vtk-interface {
		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		padding: 0; /* the chrome runs edge to edge; the kit frame keeps border/radius */
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 15px;
		line-height: 1.45;
		overflow: hidden;
	}

	/* ── header ─────────────────────────────────────────────────────────── */
	.bar {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.45rem 0.7rem;
		font-size: 15px;
		border-bottom: 1px solid var(--line);
		background-color: color-mix(in srgb, var(--text) 5%, transparent);
	}
	/* The device badge: options.glyph in the top corner, or the mode's own mark. */
	.badge {
		flex: none;
		padding: 0.05rem 0.35rem;
		border: 1px solid var(--tone-1-line);
		border-radius: 5px;
		background-color: var(--tone-1-soft);
		line-height: 1.3;
		letter-spacing: 0.02em;
	}
	.title {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
	}
	.signal {
		flex: none;
		margin-left: auto;
		width: 8px;
		height: 8px;
		border-radius: 999px;
		background-color: var(--tone-1);
		animation: iface-signal 1.6s steps(2, end) infinite;
	}

	/* ── lines ──────────────────────────────────────────────────────────── */
	.lines {
		margin: 0;
		padding: 0.55rem 0.7rem;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.row {
		display: flex;
		gap: 0.55rem;
		min-width: 0;
		font-size: 15px;
		animation: iface-row 0.3s ease-out both;
		animation-delay: calc(var(--i) * 55ms);
	}
	.marker {
		flex: none;
		min-width: 1.1em;
		color: var(--tone, var(--tone-1));
		user-select: none;
	}
	/* Verbatim: pre-wrap keeps the model's dotted leaders and status tags intact. */
	.text {
		min-width: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.stamp {
		flex: none;
		margin-left: auto;
		padding-left: 0.6rem;
		font-size: var(--font-micro);
		letter-spacing: 0.06em;
		font-variant-numeric: tabular-nums;
		color: var(--text-dim);
	}

	/* ── terminal: a dark well, scanlines, a cursor on the last line ─────── */
	.vtk-interface[data-mode='terminal'] {
		background-color: var(--bg-inset);
		color: color-mix(in srgb, var(--text) 84%, var(--tone-1));
	}
	.vtk-interface[data-mode='terminal']::before {
		content: '';
		position: absolute;
		inset: 0;
		z-index: 0;
		pointer-events: none;
		/* CRT scanlines — horizontal only, so the well reads as a screen rather
		   than the kit's two-axis grain. */
		background-image: repeating-linear-gradient(
			0deg,
			var(--line) 0 1px,
			transparent 1px 3px
		);
		opacity: 0.55;
	}
	.vtk-interface[data-mode='terminal'] .bar,
	.vtk-interface[data-mode='terminal'] .lines,
	.vtk-interface[data-mode='terminal'] .art {
		position: relative;
		z-index: 1;
	}
	.vtk-interface[data-mode='terminal'] .bar {
		border-bottom-color: var(--tone-1-line);
	}
	.cursor::after {
		content: '▋';
		margin-left: 0.15em;
		color: var(--tone-1);
		animation: iface-blink 1.1s steps(2, end) infinite;
	}

	/* ── hologram: scan sweep + a glowing readout line ───────────────────── */
	.vtk-interface[data-mode='hologram'] {
		background-color: var(--bg-inset);
		color: color-mix(in srgb, var(--text) 80%, var(--tone-1));
	}
	.sweep {
		position: absolute;
		inset: -60% 0;
		z-index: 0;
		pointer-events: none;
		background: linear-gradient(
			180deg,
			transparent 0%,
			var(--tone-1-soft) 46%,
			var(--tone-1-line) 50%,
			var(--tone-1-soft) 54%,
			transparent 100%
		);
		animation: iface-sweep 5.6s linear infinite;
	}
	.glow-line {
		display: block;
		height: 1px;
		background: linear-gradient(90deg, transparent, var(--tone-1), transparent);
	}
	.vtk-interface[data-mode='hologram'] .bar,
	.vtk-interface[data-mode='hologram'] .lines,
	.vtk-interface[data-mode='hologram'] .art,
	.vtk-interface[data-mode='hologram'] .glow-line {
		position: relative;
		z-index: 1;
	}
	.vtk-interface[data-mode='hologram'] .text {
		text-shadow: 0 0 12px var(--tone-1-soft);
	}

	/* ── phone / chat: bubble rows, alternating alignment ────────────────── */
	.vtk-interface[data-mode='chat'] {
		background-color: var(--bg-panel);
	}
	.vtk-interface[data-mode='phone'] .row,
	.vtk-interface[data-mode='chat'] .row {
		align-self: flex-start;
		max-width: 78%;
		padding: 0.32rem 0.6rem;
		border: 1px solid var(--tone-line, var(--tone-1-line));
		border-radius: 12px 12px 12px 4px;
		background-color: var(--tone-soft, var(--tone-1-soft));
	}
	.vtk-interface[data-mode='phone'] .row[data-side='out'],
	.vtk-interface[data-mode='chat'] .row[data-side='out'] {
		align-self: flex-end;
		border-radius: 12px 12px 4px 12px;
	}
	/* The phone adds device furniture: a heavier shell (2px, 20px radius) around a
	   dark screen that bleeds into the shell padding, so the bar + rows read as
	   one inset display rather than a panel with a border. */
	.vtk-interface[data-mode='phone'] {
		gap: 0;
		padding: 0.4rem;
		border-width: 2px;
		border-radius: 20px;
		background-color: var(--bg-inset);
	}
	.vtk-interface[data-mode='phone'] .bar {
		border-bottom-color: var(--line);
		border-radius: 14px 14px 0 0;
	}
	.vtk-interface[data-mode='phone'] .lines:last-child {
		border-radius: 0 0 14px 14px;
	}

	/* ── social: a feed column — handle gutter, body, timestamp ──────────── */
	.vtk-interface[data-mode='social'] {
		background-color: var(--bg-panel);
	}
	.vtk-interface[data-mode='social'] .lines {
		gap: 0;
	}
	.vtk-interface[data-mode='social'] .row {
		display: grid;
		grid-template-columns: minmax(4.5rem, auto) minmax(0, 1fr) auto;
		align-items: baseline;
		column-gap: 0.6rem;
		padding: 0.45rem 0.15rem;
		border-bottom: 1px solid var(--line);
	}
	.vtk-interface[data-mode='social'] .row:last-child {
		border-bottom: 0;
	}
	.vtk-interface[data-mode='social'] .marker {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
	}

	/* ── art: the machine's punchline, framed like art on a wall ─────────── */
	.art {
		margin: 0.9rem 0.85rem 1rem;
		padding: 1.1rem 0.9rem;
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		background-color: var(--bg-inset);
		text-align: center;
	}
	.art pre {
		margin: 0;
		font-family: inherit;
		font-size: 15px;
		line-height: 1.35;
		white-space: pre-wrap;
		color: var(--tone-1);
	}

	@keyframes iface-signal {
		50% {
			opacity: 0.2;
		}
	}
	@keyframes iface-blink {
		50% {
			opacity: 0;
		}
	}
	@keyframes iface-row {
		from {
			opacity: 0;
			transform: translateY(3px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@keyframes iface-sweep {
		0% {
			transform: translateY(-30%);
			opacity: 0.25;
		}
		50% {
			opacity: 0.9;
		}
		100% {
			transform: translateY(30%);
			opacity: 0.25;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.sweep,
		.signal,
		.row,
		.cursor::after {
			animation: none;
		}
	}
</style>
