<script lang="ts">
	// Assistant card — the side-channel register (docs/UI-LANGUAGE.md §6).
	// OpenUI Lang call: AssistantCard(avatar, name, role, mood, commentary, options, footer).
	//
	// OUT OF FICTION: this is a side conversation with <USER> about the scene, so
	// it wears the side-channel frame (`.ui-frame-sidechannel`: soft corners, tone
	// rail down the left margin) and never the diegetic frame — no paper, no
	// terminal chrome, no in-world document furniture.
	//
	// Emphasis: exactly one option is emphasised, and only STRUCTURALLY — heavier
	// rule, bolder text, `▶` marker. Colour is never the signal: every row draws
	// from the same shared tone ramp (docs/UI-LANGUAGE.md §2).
	//
	// Interaction: clicking an option fires a `continue_conversation` action whose
	// humanFriendlyMessage is the option text. The host FILLS the composer with it
	// and never auto-sends — see routes/chat/DslFallback.svelte + lib/stores.ts.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import { getIsStreaming, getTriggerAction } from '@openuidev/svelte-lang';
	import type { AssistantCardProps } from '../schemas/assistant';

	let { props }: ComponentRenderProps<AssistantCardProps> = $props();

	// Context getters must be called during component init (they read context).
	const triggerAction = getTriggerAction();
	const isStreaming = getIsStreaming();

	// §6 says one option is emphasised. If the narrator flagged none, the first
	// takes the emphasis so the list always shows a hierarchy instead of a flat
	// menu.
	const emphasisIndex = $derived.by(() => {
		const flagged = (props.options ?? []).findIndex((option) => option.emphasis === true);
		return flagged === -1 ? 0 : flagged;
	});

	// No image -> initials from the name (first letter of up to two words).
	const initials = $derived(
		(props.name || '?')
			.trim()
			.split(/\s+/)
			.slice(0, 2)
			.map((word) => Array.from(word)[0] ?? '')
			.join('')
			.toUpperCase()
	);

	// The model drifts into putting its NAME in the positional avatar slot; a
	// browser would then request it as a URL (live defect: `GET /The%20Oracle`
	// -> 404 + a broken image). Only an addressable value — http(s)://, a data:
	// URI, or a path (`/` segment or a `.ext` tail) — becomes an `<img src>`;
	// anything else falls back to the initials. The engine blanks the same
	// shapes on the canonical statement; this is the display-side guard.
	const AVATAR_SCHEME = /^(?:https?:\/\/|data:)/;
	const AVATAR_EXTENSION = /\.[A-Za-z0-9]+$/;

	function isAvatarUrl(value: string): boolean {
		return AVATAR_SCHEME.test(value) || value.includes('/') || AVATAR_EXTENSION.test(value);
	}

	const avatarUrl = $derived.by(() => {
		const value = (props.avatar ?? '').trim();
		return value && isAvatarUrl(value) ? value : '';
	});
	const mood = $derived((props.mood ?? '').trim());

	function choose(text: string): void {
		triggerAction(text, undefined, {
			type: 'continue_conversation',
			params: { message: text }
		});
	}
</script>

<article class="assistant-card ui-frame-sidechannel" data-openui="AssistantCard">
	<aside class="rail">
		<span class="avatar ui-glyph" aria-hidden="true">
			{#if avatarUrl}
				<img src={avatarUrl} alt="" />
			{:else}
				{initials}
			{/if}
		</span>
		<p class="name">{props.name}</p>
		{#if props.role}
			<p class="role">{props.role}</p>
		{/if}
		{#if mood}
			<p class="mood">{mood}</p>
		{/if}
	</aside>

	<div class="body">
		<p class="commentary">{props.commentary}</p>

		<ol class="options">
			{#each props.options ?? [] as option, i (i)}
				<li class="option ui-alt" class:emphasised={i === emphasisIndex}>
					<button type="button" onclick={() => choose(option.text)} disabled={isStreaming()}>
						<span class="index" aria-hidden="true">{i + 1}</span>
						<span class="text">{option.text}</span>
						{#if option.tag}
							<span class="sep" aria-hidden="true">·</span>
							<span class="tag ui-tag">{option.tag}</span>
						{/if}
						{#if option.note}
							<span class="sep" aria-hidden="true">·</span>
							<span class="note">{option.note}</span>
						{/if}
					</button>
				</li>
			{/each}
		</ol>

		{#if props.footer}
			<p class="footer ui-foot">{props.footer}</p>
		{/if}
	</div>
</article>

<style>
	.assistant-card {
		display: grid;
		grid-template-columns: 5.5rem minmax(0, 1fr);
		gap: 0.85rem;
		font-size: 15px; /* §3: Assistant body floor */
		line-height: 1.4;
	}

	/* Surface fallback for a build without chrome.css: `.ui-frame-sidechannel`
	   owns these once the kit is loaded — `:where()` holds this at zero
	   specificity so the primitive always wins. */
	:where(.assistant-card) {
		padding: 0.6rem 0.75rem;
		border: 1px solid var(--line);
		border-left: 3px solid var(--tone, var(--tone-1, var(--accent)));
		border-radius: 10px;
		background: var(--bg-raised);
		color: var(--text);
	}

	/* The kit's `.ui-frame-sidechannel` padding (0.75rem / 0.85rem) is tuned for
	   free-standing cards; inside the pinned panel the card is the panel body, so
	   this register tightens it. Compound selector beats the primitive's single
	   class regardless of stylesheet order. */
	.assistant-card.ui-frame-sidechannel {
		padding: 0.6rem 0.75rem;
	}

	/* ── rail ─────────────────────────────────────────────────────────── */
	.rail {
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.35rem;
		padding-right: 0.7rem;
		border-right: 1px solid var(--line);
		text-align: center;
	}
	.avatar {
		width: 2.75rem;
		height: 2.75rem;
		border-radius: 50%;
		border: 1px solid var(--tone-line, var(--tone-1-line, var(--accent-line)));
		background: var(--bg-inset);
		color: var(--tone, var(--tone-1, var(--accent)));
		font-size: 15px;
		font-weight: 600;
		letter-spacing: 0.04em;
	}
	.avatar img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.name {
		margin: 0;
		font-size: var(--font-ui);
		font-weight: 600;
		color: var(--text);
		overflow-wrap: anywhere;
	}
	/* Rail labels are the system register: mono, uppercase, micro (§3). */
	.role {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-dim);
	}
	.mood {
		margin: 0;
		font-size: var(--font-ui);
	}

	/* ── commentary + options ─────────────────────────────────────────── */
	.body {
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
		min-width: 0;
	}
	.commentary {
		margin: 0;
		font-size: 15px;
		line-height: 1.35;
		color: var(--text);
		white-space: pre-line;
	}
	.options {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.option {
		min-width: 0;
	}
	/* One compact line: `1 suggestion · TAG · note`. `.text` is the substance and
	   wraps (never ellipsised); the small tag/note ride the same baseline and
	   fall to a second line only when the text runs long. */
	.option button {
		box-sizing: border-box;
		display: flex;
		flex-wrap: wrap;
		align-items: baseline;
		column-gap: 0.4rem;
		row-gap: 0.1rem;
		width: 100%;
		padding: 0.35rem 0.5rem;
		text-align: left;
		font: inherit;
		font-size: 15px;
		line-height: 1.4;
		border: 1px solid var(--tone-line, var(--tone-1-line, var(--accent-line)));
		border-left: 2px solid var(--tone, var(--tone-1, var(--accent)));
		border-radius: 6px;
		background: var(--bg-inset);
		color: var(--text);
		cursor: pointer;
	}
	.option button:hover {
		background: var(--bg-hover);
	}
	.option button:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.option button:disabled {
		opacity: 0.55;
		cursor: progress;
	}
	.index {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.text {
		min-width: 0;
		white-space: pre-line;
	}
	.sep {
		color: var(--text-dim);
	}
	.tag {
		font-size: var(--font-micro);
		white-space: nowrap;
	}
	.note {
		font-size: var(--font-small);
		font-style: italic;
		color: var(--text-muted);
		white-space: pre-line;
	}

	/* Emphasis is structural only: heavier frame, bolder text, `▶` marker. */
	.option.emphasised button {
		border-width: 2px;
		border-left-width: 4px;
		background: var(--bg-raised);
	}
	.option.emphasised .text {
		font-weight: 600;
	}
	.option.emphasised .index::before {
		content: '▶';
		margin-right: 0.25rem;
	}

	/* §3/§6: the Assistant footer floor is 11px — the chrome kit's `.ui-foot`
	   sits at the 14px chrome floor, so this register restores its own size. */
	.footer {
		margin: 0;
		font-size: var(--font-micro);
		white-space: pre-line;
	}
</style>
