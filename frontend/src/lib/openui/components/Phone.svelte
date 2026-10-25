<script lang="ts">
	// VTK artifact: Phone — a group chat drawn as a messaging device.
	// OpenUI Lang call: Phone(thread, messages[], {glyph?, art?, me?, typing?, clock?, battery?}).
	// Flavor: artifact reproduction (artifact-components §1.2). Likeness is
	// STRUCTURAL, not 1:1 (A1): our own palettes, our own neutral E2EE line,
	// no brand colour anywhere.
	//
	// Front-loaded design (owner's north star: "front load design and modulate it
	// at runtime"). The DEVICE IS THE DESIGN DECISION and it is made here: a
	// status bar, a contact header with a resolved avatar, an encryption notice,
	// a scrolling bubble thread with day separators, read ticks and reaction
	// pills — the whole grammar of a chat at a glance. The narrator supplies a
	// thread name, the messages, and a few small choices (which clock, who is
	// typing); everything structural is authored, so the pane reads as a phone
	// and not as a list with a chat label.
	//
	// COLOUR (§2, docs/UI-LANGUAGE.md): tone tokens and the neutral scale only.
	// Sender names alternate `--tone-2`/`--tone-3` by SENDER INDEX — structural
	// differentiation, never per-entity hue identity. `me` bubbles take the
	// `--tone-1-soft` fill + `--tone-1-line` border and read ticks tint
	// `--tone-1`; every other bubble is neutral `--bg-panel` + `--line`.
	//
	// MOTION (§4/D61): CSS-timed only. Bubbles `ui-rise-in` at 260ms, staggered
	// `--i * min(var(--reveal-stagger), 3000ms / --n)` so all content is fully
	// readable <=3.5s (16 messages => 15 x 187.5ms + 260ms = 3.07s). Typing dots
	// pulse `steps(2)`. `prefers-reduced-motion: reduce` disables all of it.
	//
	// DEGRADATION: a missing field never breaks the pane — no status => no ticks,
	// no time => no stamp, no reactions => no pill, no `me` => everything reads
	// as incoming, no `typing` => an "online" dot instead. `glyph`/`art` ride the
	// uniform trailing options object (OptionsSchema) but have no anchor in a
	// fixed device chrome, so they are accepted and ignored to keep the call
	// shape uniform with every other flavor.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	// Read the Wave 0 store DIRECTLY (project import convention: relative within
	// `lib/`). `$avatarIndex.get(name) ?? initialsFor(name)` is the only idiom —
	// a `get(store)` helper would not track in a `$derived` (AGENTS.md).
	import { avatarIndex, initialsFor } from '../avatars';

	// Local prop types — mirror §1.2 exactly (names, order and types). Wave 2
	// swaps these for the `../schemas/flavors` imports; keep them identical.
	// (Not exported: no other module reads them — `export` is disallowed in a
	// Svelte instance script and would only ever be a second source of truth.)
	type PhoneReaction = { emoji: string; count?: number };
	type PhoneMessage = {
		from: string;
		text: string;
		time: string;
		status: string;
		reactions?: PhoneReaction[];
		day?: string;
	};
	type PhoneOptions = {
		glyph?: string;
		art?: string;
		me?: string;
		typing?: string;
		clock?: string;
		battery?: number;
	};
	interface PhoneProps {
		thread: string;
		messages: PhoneMessage[];
		options?: PhoneOptions;
	}

	let { props }: ComponentRenderProps<PhoneProps> = $props();

	const MAX_MESSAGES = 16;
	const DEFAULT_BATTERY = 87;
	// OWN neutral copy — not WhatsApp's notice (A1: likeness is structural only).
	const E2EE = 'End-to-end encrypted · messages stay inside this chat';
	const TYPES = new Set(['sent', 'delivered', 'read']);

	type Row = {
		key: number;
		mine: boolean;
		from: string;
		text: string;
		time: string;
		ticks: string;
		reactions: PhoneReaction[];
		day: string;
		label: boolean;
		tone: string;
	};

	const threadName = $derived((props.thread ?? '').trim() || 'Unknown');
	const me = $derived((props.options?.me ?? '').trim());
	const typingName = $derived((props.options?.typing ?? '').trim());
	const clock = $derived((props.options?.clock ?? '').trim());
	const battery = $derived(
		Math.max(0, Math.min(100, Math.round(props.options?.battery ?? DEFAULT_BATTERY)))
	);

	// Rows are the render model: each message gains its side, its sender-label
	// tone, its clamped ticks and its reactions, plus a date separator when its
	// `day` differs from the last day marker seen.
	const rows = $derived.by<Row[]>(() => {
		const src = (props.messages ?? []).slice(0, MAX_MESSAGES);
		const senderIndex = new Map<string, number>();
		let lastDay = '';
		return src.map((m, i): Row => {
			const from = (m?.from ?? '').trim();
			const mine = me !== '' && from === me;
			if (!mine && from !== '' && !senderIndex.has(from)) {
				senderIndex.set(from, senderIndex.size);
			}
			const index = senderIndex.get(from) ?? 0;
			const day = (m?.day ?? '').trim();
			const separator = day !== '' && day !== lastDay ? day : '';
			if (day !== '') lastDay = day;
			const status = (m?.status ?? '').trim();
			return {
				key: i,
				mine,
				from,
				text: m?.text ?? '',
				time: (m?.time ?? '').trim(),
				ticks: mine && TYPES.has(status) ? status : '',
				reactions: (m?.reactions ?? []).filter(
					(r): r is PhoneReaction => Boolean(r && r.emoji)
				),
				day: separator,
				label: !mine && from !== '',
				tone: index % 2 === 0 ? 'var(--tone-2)' : 'var(--tone-3)'
			};
		});
	});

	// `--n` drives the reveal budget; never zero (division lives in the CSS).
	const n = $derived(Math.max(1, rows.length));

	// Count is clamped to the owner's 1..999 range; an absent count is a bare pill.
	function pillCount(reaction: PhoneReaction): number | null {
		if (typeof reaction.count !== 'number' || !Number.isFinite(reaction.count)) return null;
		return Math.max(1, Math.min(999, Math.round(reaction.count)));
	}
</script>

<section class="vtk-phone ui-frame-diegetic" data-openui="Phone">
	<!-- status bar: narrative clock left; signal + battery right (B7) -->
	<div class="status" aria-hidden="true">
		{#if clock}<span class="clock">{clock}</span>{/if}
		<span class="indicators">
			<span class="bars"><i></i><i></i><i></i><i></i></span>
			<span class="battery" style="--charge: {battery}%"><i></i></span>
		</span>
	</div>

	<!-- contact header: resolved avatar (initials fallback), name, presence -->
	<header class="contact">
		<span class="avatar">
			{#if $avatarIndex.get(threadName)}
				<img src={$avatarIndex.get(threadName)} alt="" />
			{:else}
				<span class="initials">{initialsFor(threadName)}</span>
			{/if}
		</span>
		<span class="who">
			<span class="name">{threadName}</span>
			{#if typingName}
				<span class="sub sub-typing">{typingName} is typing…</span>
			{:else}
				<span class="sub"><span class="dot"></span>online</span>
			{/if}
		</span>
	</header>

	<!-- encryption notice: our own neutral line, under the header -->
	<p class="e2ee">{E2EE}</p>

	<div class="thread" style="--n: {n}">
		{#each rows as row (row.key)}
			{#if row.day}
				<div class="day"><span>{row.day}</span></div>
			{/if}
			<div class="row" data-side={row.mine ? 'out' : 'in'} style="--i: {row.key}">
				<div class="stack">
					<div class="bubble">
						{#if row.label}
							<span class="sender" style="--sender-tone: {row.tone}">{row.from}</span>
						{/if}
						<span class="text">{row.text}</span>
						{#if row.time || row.ticks}
							<span class="meta">
								{#if row.time}<span class="stamp">{row.time}</span>{/if}
								{#if row.ticks}
									<span class="ticks" class:read={row.ticks === 'read'}>
										{row.ticks === 'sent' ? '✓' : '✓✓'}
									</span>
								{/if}
							</span>
						{/if}
					</div>
					{#if row.reactions.length}
						<span class="reactions" aria-hidden="true">
							{#each row.reactions as reaction, ri (ri)}
								<span class="pill">
									{reaction.emoji}{#if pillCount(reaction) !== null}&nbsp;{pillCount(reaction)}{/if}
								</span>
							{/each}
						</span>
					{/if}
				</div>
			</div>
		{/each}

		{#if typingName}
			<div class="row typing" data-side="in" style="--i: {n}">
				<div class="stack">
					<div class="bubble">
						<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
					</div>
				</div>
			</div>
		{/if}
	</div>
</section>

<style>
	.vtk-phone {
		display: flex;
		flex-direction: column;
		padding: 0;
		overflow: hidden;
		border-radius: 14px;
		background: var(--bg-inset);
		font-size: var(--font-ui);
		color: var(--text);
	}

	/* ── status bar ─────────────────────────────────────────────────────── */
	.status {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.35rem 0.7rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.06em;
		color: var(--text-muted);
	}
	.clock {
		font-variant-numeric: tabular-nums;
	}
	.indicators {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		margin-left: auto;
	}
	.bars {
		display: inline-flex;
		align-items: flex-end;
		gap: 1px;
		height: 0.7rem;
	}
	.bars i {
		display: block;
		width: 2px;
		border-radius: 1px;
		background-color: var(--text-dim);
	}
	.bars i:nth-child(1) {
		height: 32%;
	}
	.bars i:nth-child(2) {
		height: 55%;
	}
	.bars i:nth-child(3) {
		height: 78%;
	}
	.bars i:nth-child(4) {
		height: 100%;
	}
	.battery {
		position: relative;
		width: 1.5rem;
		height: 0.68rem;
		padding: 1px;
		border: 1px solid var(--text-dim);
		border-radius: 3px;
	}
	.battery::after {
		content: '';
		position: absolute;
		top: 50%;
		right: -3px;
		width: 2px;
		height: 4px;
		border-radius: 0 2px 2px 0;
		background-color: var(--text-dim);
		transform: translateY(-50%);
	}
	.battery > i {
		display: block;
		width: var(--charge, 87%);
		height: 100%;
		border-radius: 1px;
		background-color: var(--text);
	}

	/* ── contact header ─────────────────────────────────────────────────── */
	.contact {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		padding: 0.5rem 0.7rem 0.45rem;
		border-top: 1px solid var(--line);
		background-color: color-mix(in srgb, var(--text) 4%, transparent);
	}
	.avatar {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2rem;
		height: 2rem;
		overflow: hidden;
		border: 1px solid var(--tone-1-line);
		border-radius: 999px;
		background-color: var(--tone-1-soft);
		color: var(--tone-1);
	}
	.avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.initials {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
	}
	.who {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
		font-size: var(--font-body);
		color: var(--text);
	}
	.sub {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--font-micro);
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.sub-typing {
		color: var(--tone-1);
	}
	.dot {
		width: 0.35rem;
		height: 0.35rem;
		border-radius: 999px;
		background-color: var(--ok);
	}

	/* ── encryption notice ──────────────────────────────────────────────── */
	.e2ee {
		margin: 0;
		padding: 0.3rem 0.7rem;
		border-top: 1px solid var(--line);
		border-bottom: 1px solid var(--line);
		text-align: center;
		font-size: var(--font-micro);
		letter-spacing: 0.02em;
		color: var(--text-dim);
	}

	/* ── thread ─────────────────────────────────────────────────────────── */
	.thread {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.6rem 0.7rem 0.8rem;
	}
	.day {
		align-self: center;
		margin: 0.2rem 0;
	}
	.day > span {
		display: inline-block;
		padding: 0.12rem 0.6rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		background-color: color-mix(in srgb, var(--text) 7%, transparent);
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.1em;
		text-transform: uppercase;
		color: var(--text-muted);
	}

	.row {
		display: flex;
		animation: ui-rise-in 260ms ease-out both;
		animation-delay: calc(
			var(--i) * min(var(--reveal-stagger, 250ms), 3000ms / var(--n, 1))
		);
	}
	.row[data-side='out'] {
		justify-content: flex-end;
	}
	.stack {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		max-width: 80%;
	}

	.bubble {
		display: flex;
		flex-direction: column;
		padding: 0.4rem 0.62rem 0.28rem;
		border: 1px solid var(--line);
		border-radius: 12px 12px 12px 4px;
		background-color: var(--bg-panel);
		color: var(--text);
	}
	.row[data-side='out'] .bubble {
		border-color: var(--tone-1-line);
		border-radius: 12px 12px 4px 12px;
		background-color: var(--tone-1-soft);
	}

	/* Sender labels: alternated tone-2 / tone-3 by sender index (no per-sender hue). */
	.sender {
		margin-bottom: 0.12rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--sender-tone, var(--tone-2));
	}
	.text {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: var(--font-body);
		line-height: 1.4;
	}
	.meta {
		display: inline-flex;
		align-items: center;
		gap: 0.28rem;
		align-self: flex-end;
		margin-top: 0.15rem;
		padding-left: 0.7rem;
		font-size: var(--font-micro);
		font-variant-numeric: tabular-nums;
		color: var(--text-dim);
	}
	.ticks {
		letter-spacing: -0.12em;
	}
	.ticks.read {
		color: var(--tone-1);
	}

	/* Reaction pills pinned to the bubble's bottom edge (owner: count on group
	   reaction pills; count clamped 1..999, omitted count = bare pill). */
	.reactions {
		position: absolute;
		bottom: -0.42rem;
		display: inline-flex;
		gap: 0.15rem;
		z-index: 1;
	}
	.row[data-side='out'] .reactions {
		right: 0.6rem;
	}
	.row[data-side='in'] .reactions {
		left: 0.6rem;
	}
	.pill {
		display: inline-flex;
		align-items: center;
		padding: 0.02rem 0.34rem;
		border: 1px solid var(--line-strong);
		border-radius: 999px;
		background-color: var(--bg-raised);
		font-size: var(--font-micro);
		line-height: 1.5;
		color: var(--text);
	}

	/* Typing indicator: three dots, steps(2) pulse. */
	.typing .bubble {
		padding: 0.5rem 0.7rem;
	}
	.dots {
		display: inline-flex;
		gap: 0.22rem;
	}
	.dots i {
		width: 0.34rem;
		height: 0.34rem;
		border-radius: 999px;
		background-color: var(--text-muted);
		animation: phone-dot 1.2s steps(2, end) infinite;
	}
	.dots i:nth-child(2) {
		animation-delay: 0.2s;
	}
	.dots i:nth-child(3) {
		animation-delay: 0.4s;
	}

	@keyframes phone-dot {
		0%,
		100% {
			opacity: 0.25;
		}
		50% {
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.row,
		.dots i {
			animation: none;
		}
	}
</style>
