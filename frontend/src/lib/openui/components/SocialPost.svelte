<script lang="ts">
	// VTK artifact: SocialPost — one feed post, its stats, and the first reply only.
	// OpenUI Lang call: SocialPost(author, handle, body, stats{likes,reposts,comments}, reply{author,handle,text}?, options?).
	// Flavor: artifact reproduction (artifact-components §1.6). Likeness is
	// STRUCTURAL, not 1:1 (A1): our own feed composition and our own neutral
	// action row — no brand blue, no verified badge, no platform chrome copied.
	//
	// Front-loaded design (owner's north star: "front load design and modulate it
	// at runtime"): the FEED CARD IS THE DESIGN DECISION and it is made here — a
	// resolved avatar, name over `@handle`, the body verbatim, one action row with
	// the three engagement glyphs, a rule, and the single nested reply. The
	// narrator supplies the words, the counts and (optionally) the first reply;
	// everything structural is authored, so the pane reads as a post and not as a
	// paragraph with a handle stuck on it.
	//
	// ONE REPLY, EVER: `reply` is the first comment and the component renders
	// exactly that one row. A beat with a hundred comments still shows one — the
	// cut is structural (a feed card is a glimpse, not a thread), not a clamp of
	// whatever the model sent.
	//
	// COLOUR (§2, docs/UI-LANGUAGE.md §2/§4): tone tokens and the neutral scale
	// only. The three action glyphs carry the `--tone-1` action register — never a
	// platform accent — calmed toward `--text-muted` (see REGISTER), while counts
	// and meta stay neutral (`--text-muted` / `--text-dim`). The avatar emblem is
	// the kit's tone-1 chip, as in Phone's contact header.
	//
	// REGISTER (polish pass): the card reads QUIET. One neutral hairline all round
	// with NO diegetic accent cap ("less framing chrome"), a roomier radius, a
	// tighter internal rhythm, a softened divider before the reply, and the action
	// glyphs pulled from full `--tone-1` toward `--text-muted` — the row stays
	// (C12) without shouting. The reply row's muted register is untouched.
	//
	// MOTION (§4/D61): CSS-timed only. The card `ui-rise-in` 300ms; the reply
	// `ui-fade-in` trailing it by 0.6 × the shared `--reveal-stagger` (= 150ms at
	// the 250ms default, read WITH its literal fallback per the reveal contract),
	// so the whole card is readable well inside the 3.5s budget. Both animations
	// are off under `prefers-reduced-motion: reduce`.
	//
	// DEGRADATION: a missing field never breaks the pane — a missing stat key
	// renders the bare glyph with no count, a missing/blank reply renders
	// post-only, a missing handle renders the name alone. Counts are clamped
	// 0..9,999,999 and formatted 1.2k / 3.4M; nothing is invented to fill a gap.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	// Read the Wave 0 store DIRECTLY (project import convention: relative within
	// `lib/`). `$avatarIndex.get(name) ?? initialsFor(name)` is the only idiom — a
	// `get(store)` helper would not track in a `$derived` (AGENTS.md).
	import { avatarIndex, initialsFor } from '../avatars';

	// Local prop types — mirror §1.6 exactly (names, order and types). Wave 2
	// swaps these for the `../schemas/flavors` imports; keep them identical.
	// (Not exported: no other module reads them — `export` is disallowed in a
	// Svelte instance script and would only ever be a second source of truth.)
	type SocialStats = { likes: number; reposts: number; comments: number };
	type SocialReply = { author: string; handle: string; text: string };
	type SocialPostProps = {
		author: string;
		handle: string;
		body: string;
		stats: SocialStats;
		reply?: SocialReply;
		options?: Record<string, unknown>;
	};

	let { props }: ComponentRenderProps<SocialPostProps> = $props();

	// Counts live in 0..9,999,999 and read as `1.2k` / `3.4M` (plan §1.6):
	// thousands step at 1,000 with one decimal and the trailing `.0` dropped; a
	// value that rounds up to four digits in thousands promotes to millions
	// (999,999 -> "1M") rather than printing "1000k".
	const MAX_COUNT = 9_999_999;
	function formatCount(value: number): string {
		const n = Math.max(0, Math.min(MAX_COUNT, Math.round(value)));
		if (n < 1000) return String(n);
		const thousands = Math.round(n / 100) / 10;
		return thousands >= 1000
			? `${Number((thousands / 1000).toFixed(1))}M`
			: `${Number(thousands.toFixed(1))}k`;
	}

	// A missing key is a bare glyph, never a fabricated zero: the model may omit a
	// count it does not know, and "0 likes" would be an assertion it never made.
	function statCount(value: number | undefined): number | null {
		return typeof value === 'number' && Number.isFinite(value) ? value : null;
	}

	// The action row is FIXED — three glyphs, always in this order. `stats` fills
	// counts in; it never decides which actions exist.
	const ACTIONS = [
		{ key: 'likes', label: 'likes' },
		{ key: 'reposts', label: 'reposts' },
		{ key: 'comments', label: 'comments' }
	] as const;

	// Our own line-icon geometry (no platform glyph set is copied): 24px box,
	// stroked with `currentColor`.
	const ICON: Record<(typeof ACTIONS)[number]['key'], string[]> = {
		likes: ['M12 20.3 5.4 13.8a4.3 4.3 0 0 1 6.1-6.1l.5.5.5-.5a4.3 4.3 0 0 1 6.1 6.1Z'],
		reposts: ['M7.2 8.6h9.6l-2.7-2.7', 'M16.8 15.4H7.2l2.7 2.7'],
		comments: ['M4.8 6.4h14.4v9.1h-8.1l-4.4 3.4v-3.4H4.8Z']
	};

	const stats = $derived(
		ACTIONS.map((action) => {
			const raw = statCount(props.stats?.[action.key]);
			const count = raw === null ? null : formatCount(raw);
			return {
				key: action.key,
				count,
				aria: count === null ? action.label : `${count} ${action.label}`
			};
		})
	);

	const author = $derived((props.author ?? '').trim());

	// `@` is presentation, not content: the narrator writes the bare handle and
	// this adds the prefix — an already-prefixed handle is left alone.
	function atHandle(value: string | undefined): string {
		const raw = (value ?? '').trim();
		if (raw === '') return '';
		return raw.startsWith('@') ? raw : `@${raw}`;
	}
	const handle = $derived(atHandle(props.handle));

	// The reply row exists only when the reply has something in it; an
	// author-less reply still renders (the emblem falls back to "?").
	const reply = $derived.by(() => {
		const r = props.reply;
		if (!r) return null;
		const name = (r.author ?? '').trim();
		const text = r.text ?? '';
		const replyHandle = atHandle(r.handle);
		if (name === '' && replyHandle === '' && text.trim() === '') return null;
		return { name, handle: replyHandle, text };
	});
</script>

<article
	class="vtk-socialpost ui-frame-diegetic"
	data-openui="SocialPost"
	data-has-reply={reply ? 'true' : 'false'}
>
	<header class="who">
		<span class="avatar">
			{#if $avatarIndex.get(author)}
				<img src={$avatarIndex.get(author)} alt="" />
			{:else}
				<span class="initials">{initialsFor(author)}</span>
			{/if}
		</span>
		<span class="ident">
			<span class="name">{author}</span>
			{#if handle}<span class="handle">{handle}</span>{/if}
		</span>
	</header>

	<p class="body">{props.body}</p>

	<ul class="actions">
		{#each stats as stat (stat.key)}
			<li class="act" data-stat={stat.key} aria-label={stat.aria}>
				<svg
					class="icon"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="1.6"
					stroke-linecap="round"
					stroke-linejoin="round"
					aria-hidden="true"
				>
					{#each ICON[stat.key] as d (d)}
						<path {d} />
					{/each}
				</svg>
				{#if stat.count !== null}<span class="count" data-count={stat.count}>{stat.count}</span>{/if}
			</li>
		{/each}
	</ul>

	{#if reply}
		<hr class="ui-rule" />
		<div class="reply" data-reply="true">
			<span class="avatar small">
				{#if $avatarIndex.get(reply.name)}
					<img src={$avatarIndex.get(reply.name)} alt="" />
				{:else}
					<span class="initials">{initialsFor(reply.name)}</span>
				{/if}
			</span>
			<span class="reply-body">
				<span class="reply-who">
					<span class="name">{reply.name}</span>
					{#if reply.handle}<span class="handle">{reply.handle}</span>{/if}
				</span>
				<span class="reply-text">{reply.text}</span>
			</span>
		</div>
	{/if}
</article>

<style>
	.vtk-socialpost {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.75rem 0.85rem 0.65rem;
		/* Quieter than the stock diegetic frame: a feed card is a calm surface,
		   not a VTK panel — one neutral hairline all round, no accent cap. */
		border-color: var(--line);
		border-top: 1px solid var(--line);
		border-radius: 10px;
		animation: ui-rise-in 300ms ease-out both;
	}

	/* ── byline ─────────────────────────────────────────────────────────── */
	.who {
		display: flex;
		align-items: center;
		gap: 0.6rem;
	}
	.avatar {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 2.25rem;
		height: 2.25rem;
		overflow: hidden;
		border: 1px solid var(--tone-1-line);
		border-radius: 999px;
		background-color: var(--tone-1-soft);
		color: var(--tone-1);
	}
	.avatar.small {
		width: 1.6rem;
		height: 1.6rem;
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
	.ident {
		display: flex;
		flex-direction: column;
		min-width: 0;
	}
	.name {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: var(--font-body);
		font-weight: 600;
		color: var(--text);
	}
	.handle {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.02em;
		color: var(--text-dim);
	}

	/* ── body: verbatim, line breaks preserved ──────────────────────────── */
	.body {
		margin: 0;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: var(--font-body);
		line-height: 1.45;
		color: var(--text);
	}

	/* ── action row: glyphs tint the tone, counts stay neutral ──────────── */
	.actions {
		display: flex;
		align-items: center;
		gap: 0.95rem;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	.act {
		display: inline-flex;
		align-items: center;
		gap: 0.32rem;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.03em;
	}
	.icon {
		width: 1rem;
		height: 1rem;
		/* Calmed from the full accent: the action register stays tone-1 (C12 keeps
		   the row) but reads quiet against the body. */
		color: color-mix(in srgb, var(--tone-1) 55%, var(--text-muted));
	}
	.count {
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
	}

	/* ── the single reply row ───────────────────────────────────────────── */
	.ui-rule {
		margin: 0.05rem 0;
		/* Softer divider than the kit default: a quieter break before the reply. */
		border-top-color: color-mix(in srgb, var(--line) 60%, transparent);
	}
	.reply {
		display: flex;
		align-items: flex-start;
		gap: 0.5rem;
		animation: ui-fade-in 300ms ease-out both;
		/* 0.6 × the shared 250 ms stagger = the spec's +150 ms (reveal contract) */
		animation-delay: calc(var(--reveal-stagger, 250ms) * 0.6);
	}
	.reply-body {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		min-width: 0;
	}
	.reply-who {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		min-width: 0;
	}
	.reply-who .name {
		font-size: var(--font-ui);
	}
	.reply-text {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		font-size: var(--font-ui);
		line-height: 1.45;
		color: var(--text-muted);
	}

	@media (prefers-reduced-motion: reduce) {
		.vtk-socialpost,
		.reply {
			animation: none;
		}
	}
</style>
