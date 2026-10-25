<script lang="ts">
	// VTK artifact: SMS — a text message, as a lock-screen banner or a thread.
	// OpenUI Lang call: SMS(mode, title, messages[], {glyph?, art?, me?}).
	// Flavor: artifact reproduction (artifact-components §1.3). Likeness is
	// STRUCTURAL, not 1:1 (A1): our own palettes and neutral chrome, no brand
	// colour, no lock-screen wallpaper.
	//
	// Front-loaded design. `mode` is the design decision and it is made here
	// (§1.3, C10): the two variants are different COMPOSITIONS, not one card
	// with a flag. `banner` is a notification drop — rounded app tile, bold
	// title, two-line clamped preview, "now" right-aligned, on a translucent
	// dark scrim — and it HOLDS (a static artifact, no auto-dismiss, A2).
	// `thread` is the SIMPLER sibling of Phone: a plain title line, then
	// bubbles with timestamps, read ticks and reaction pills. It deliberately
	// carries NO contact header, NO avatar, NO E2EE line — that chrome is what
	// makes Phone read as a device, and its absence is what keeps SMS a text.
	// An unrecognised or empty mode falls back to `thread` (Interface's
	// convention).
	//
	// COLOUR (§2, docs/UI-LANGUAGE.md): tone tokens and the neutral scale only.
	// Bubbles reuse Phone's grammar — `me` bubbles are `--tone-1-soft` /
	// `--tone-1-line`, everything else `--bg-panel` / `--line`; the banner sits
	// on the neutral `--bg` with a `--line-strong` hairline.
	//
	// RECEIPT (polish pass): the thread carries iMessage's receipt convention —
	// a quiet CENTERED line under the newest OUTGOING bubble reporting that
	// bubble's own delivery state ("Delivered" / "Read"). A "sent"-only or
	// absent status shows nothing: the receipt speaks for the newest me-side
	// message and never for an older one, so it stays truthful. The label is
	// derived from the status (token-styled, day-separator register), and only
	// the receipt convention is borrowed — no "Edited" marker is reproduced
	// (likeness is structural, A1).
	//
	// MOTION (§4/D61): CSS-timed only. The banner drops 300ms then holds; thread
	// bubbles `ui-rise-in` 260ms staggered per the shared reveal contract so all
	// content is fully readable <=3.5s. The receipt is static (readable on
	// arrival, like the day separator). `prefers-reduced-motion: reduce`
	// disables all of it.
	//
	// DEGRADATION: absent `time` in the banner becomes the literal "now"; a
	// missing status => no ticks; a missing timestamp => no stamp; no reactions
	// => no pill; no `me` => everything reads as incoming (and so => no receipt);
	// a newest me-bubble still only "sent" => no receipt either. `glyph` presses
	// the banner's app tile (default 💬); `art` rides the uniform trailing
	// options object (OptionsSchema) but has no anchor in this chrome.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	// Local prop types — mirror §1.3 exactly (names, order and types). Wave 2
	// swaps these for the `../schemas/flavors` imports; keep them identical.
	// (Not exported: no other module reads them; `export` is disallowed here.)
	type SmsReaction = { emoji: string; count?: number };
	type SmsMessage = {
		from: string;
		text: string;
		time: string;
		status: string;
		reactions?: SmsReaction[];
		day?: string;
	};
	type SmsOptions = {
		glyph?: string;
		art?: string;
		me?: string;
	};
	interface SmsProps {
		mode: string;
		title: string;
		messages: SmsMessage[];
		options?: SmsOptions;
	}

	let { props }: ComponentRenderProps<SmsProps> = $props();

	const BANNER_MAX = 1;
	const THREAD_MAX = 12;
	const TYPES = new Set(['sent', 'delivered', 'read']);

	type Row = {
		key: number;
		mine: boolean;
		text: string;
		time: string;
		ticks: string;
		reactions: SmsReaction[];
		day: string;
	};

	// Unknown/empty mode -> thread (Interface's graceful-fallback convention).
	const mode = $derived(props.mode === 'banner' ? 'banner' : 'thread');
	const title = $derived((props.title ?? '').trim());
	const me = $derived((props.options?.me ?? '').trim());
	const glyph = $derived((props.options?.glyph ?? '').trim() || '💬');

	// The banner shows exactly ONE message; extras are ignored (§1.3 cap).
	const banner = $derived((props.messages ?? [])[0]);

	// Thread rows: same render model as Phone (side, clamped ticks, reactions,
	// date separators), minus the sender labels Phone carries.
	const rows = $derived.by<Row[]>(() => {
		const src = (props.messages ?? []).slice(0, THREAD_MAX);
		let lastDay = '';
		return src.map((m, i): Row => {
			const from = (m?.from ?? '').trim();
			const mine = me !== '' && from === me;
			const day = (m?.day ?? '').trim();
			const separator = day !== '' && day !== lastDay ? day : '';
			if (day !== '') lastDay = day;
			const status = (m?.status ?? '').trim();
			return {
				key: i,
				mine,
				text: m?.text ?? '',
				time: (m?.time ?? '').trim(),
				ticks: mine && TYPES.has(status) ? status : '',
				reactions: (m?.reactions ?? []).filter(
					(r): r is SmsReaction => Boolean(r && r.emoji)
				),
				day: separator
			};
		});
	});

	const n = $derived(Math.max(1, rows.length));

	// The newest outgoing bubble — where the receipt hangs. -1 when the thread
	// has no me-side message at all.
	const lastMineKey = $derived.by(() => {
		let key = -1;
		for (const row of rows) if (row.mine) key = row.key;
		return key;
	});

	// The receipt's label is DERIVED from that bubble's own status; "sent" (or a
	// blank status) names nothing. Only delivered/read are acknowledged states.
	const RECEIPT: Record<string, string> = { delivered: 'Delivered', read: 'Read' };
	const receipt = $derived(
		lastMineKey < 0 ? '' : (RECEIPT[rows[lastMineKey].ticks] ?? '')
	);

	// Banner "now" is the literal fallback for an absent time (§1.3).
	const bannerTime = $derived((banner?.time ?? '').trim() || 'now');

	function pillCount(reaction: SmsReaction): number | null {
		if (typeof reaction.count !== 'number' || !Number.isFinite(reaction.count)) return null;
		return Math.max(1, Math.min(999, Math.round(reaction.count)));
	}
</script>

<section class="vtk-sms ui-frame-diegetic" data-openui="SMS" data-mode={mode}>
	{#if mode === 'banner'}
		<!-- lock-screen notification: icon, title, two-line preview, "now" -->
		<div class="banner">
			<span class="icon" aria-hidden="true">{glyph}</span>
			<div class="banner-body">
				<div class="banner-top">
					<span class="banner-title">{title}</span>
					<span class="banner-time">{bannerTime}</span>
				</div>
				{#if (banner?.from ?? '').trim()}
					<span class="banner-origin">{banner?.from}</span>
				{/if}
				<p class="banner-preview">{banner?.text ?? ''}</p>
			</div>
		</div>
	{:else}
		<!-- thread: a plain title line (no contact header, no E2EE) + bubbles -->
		<header class="thread-head">{title}</header>
		<div class="thread" style="--n: {n}">
			{#each rows as row (row.key)}
				{#if row.day}
					<div class="day"><span>{row.day}</span></div>
				{/if}
				<div class="row" data-side={row.mine ? 'out' : 'in'} style="--i: {row.key}">
					<div class="stack">
						<div class="bubble">
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
				{#if row.key === lastMineKey && receipt}
					<div class="receipt" data-receipt="true">{receipt}</div>
				{/if}
			{/each}
		</div>
	{/if}
</section>

<style>
	.vtk-sms {
		display: flex;
		flex-direction: column;
		padding: 0;
		overflow: hidden;
		border-radius: 14px;
		background: var(--bg-inset);
		font-size: var(--font-ui);
		color: var(--text);
	}

	/* ── banner: a notification card on a translucent dark scrim ────────── */
	.banner {
		display: flex;
		align-items: flex-start;
		gap: 0.7rem;
		margin: 0.7rem;
		padding: 0.7rem 0.8rem;
		border: 1px solid var(--line-strong);
		border-radius: 16px;
		background-color: color-mix(in srgb, var(--bg) 74%, transparent);
		animation: sms-drop 300ms cubic-bezier(0.2, 0.9, 0.3, 1.1) both;
	}
	.icon {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.35rem;
		height: 2.35rem;
		border: 1px solid var(--line-strong);
		border-radius: 10px;
		background-color: var(--bg-raised);
		font-size: 1.15rem;
		line-height: 1;
	}
	.banner-body {
		display: flex;
		flex-direction: column;
		gap: 0.12rem;
		min-width: 0;
	}
	.banner-top {
		display: flex;
		align-items: baseline;
		gap: 0.6rem;
	}
	.banner-title {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 700;
		font-size: var(--font-body);
		color: var(--text);
	}
	.banner-time {
		flex: none;
		margin-left: auto;
		font-size: var(--font-micro);
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.banner-origin {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.04em;
		color: var(--text-dim);
	}
	.banner-preview {
		margin: 0.1rem 0 0;
		overflow: hidden;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		font-size: var(--font-ui);
		line-height: 1.4;
		color: var(--text-muted);
	}

	/* ── thread: simpler than Phone — title line, then bubbles ──────────── */
	.thread-head {
		padding: 0.5rem 0.8rem;
		border-bottom: 1px solid var(--line);
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.thread {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.7rem 0.8rem 0.9rem;
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

	/* iMessage's receipt convention, in the day-separator register: a quiet
	   centered line under the newest outgoing bubble. Quieter than a day pill —
	   plain text, no chrome — because it annotates a bubble, not the thread. */
	.receipt {
		align-self: center;
		margin: 0.02rem 0 0.08rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.06em;
		color: var(--text-dim);
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
		max-width: 82%;
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

	@keyframes sms-drop {
		from {
			opacity: 0;
			transform: translateY(-14px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.banner,
		.row {
			animation: none;
		}
	}
</style>
