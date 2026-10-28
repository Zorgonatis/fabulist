<script lang="ts">
	// PersonaControl (#94): the ONE user-persona control.
	//
	// Issue #94: the capability existed but no surface named it the way a
	// SillyTavern user looks for it ("my own description"), and the only picker
	// was 11px, three layers down, hidden by the auto-collapsed rail, and gated
	// on an already-created chat. This component is the shared answer:
	//
	//   * `variant="chip"` — the always-visible chip at the composer's input row
	//     (and in the COLLAPSED rail, so a narrow window still exposes it). It
	//     shows the effective persona's picture + name ("You: not set" when
	//     there is none) and opens a small panel on click.
	//   * `variant="block"` — the rail's FIRST layer, above Worlds. Same body.
	//
	// The body is ONE snippet rendered by both variants, and its state comes
	// from ONE store (`#lib/userPersonaStore`), so the two surfaces cannot
	// disagree. It copies the three assignment states exactly:
	//
	//   OWN           the chat's own assignment;
	//   INHERITED     no own assignment, no clear -> the pinned default is in
	//                 effect (the engine's `persona_for_chat` fallback);
	//   EXPLICIT NONE the chat was cleared -> no persona, even with a pin.
	//
	// With NO chat open the control is not gated (the old "no chat" line is
	// gone): it shows the pinned default — what a new chat will use — and its
	// list sets that pin. The pin is the "Use for new chats" switch; the match
	// is `PATCH /api/settings`, the assignment `POST/DELETE .../persona`.
	//
	// The description renders in full — the same `description` text the
	// narrator receives in its `### User (the player)` block — never the
	// scenario the old rail previewed.
	import type { Persona } from '#lib/api';
	import type { StudioKind } from '#lib/studio/kinds';
	import {
		assignUserPersona,
		dropUserPersonaOverride,
		loadUserPersona,
		pinUserPersona,
		userPersona
	} from '#lib/userPersonaStore';
	import UserBadge from './UserBadge.svelte';

	let {
		sessionId = null,
		personaId = null,
		personaCleared = false,
		variant = 'block',
		chipSide = 'up',
		compact = false,
		refreshToken = 0,
		onOpenStudio,
		onChanged
	}: {
		/** The open chat's id, or `null` when no chat is open. */
		sessionId?: string | null;
		/** The chat's OWN assignment (never the inherited default); `null` = none. */
		personaId?: string | null;
		/** True when the chat carries an explicit "no persona" clear. */
		personaCleared?: boolean;
		/** `block` = the rail's first layer; `chip` = the composer/collapsed-rail trigger. */
		variant?: 'block' | 'chip';
		/** Which way the chip's panel opens: `up` at the composer, `right` in the collapsed rail. */
		chipSide?: 'up' | 'right';
		/** Compact chip: picture only; the name moves to the tooltip/aria label. */
		compact?: boolean;
		/** Bumped by the host after a studio edit: re-read the catalog. */
		refreshToken?: number;
		/** The studio's single definition door (`openStudio(kind, opts)`). */
		onOpenStudio: (
			kind: StudioKind,
			opts?: { focusId?: string | null; startNew?: boolean }
		) => void;
		/** Called after a successful write so the host can refresh its session list. */
		onChanged?: () => void;
	} = $props();

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	let busy = $state(false);
	let pinBusy = $state(false);
	let error = $state('');
	/** The chip's panel is open (ignored by the block variant, which is inline). */
	let open = $state(false);
	let wrapEl = $state<HTMLElement | null>(null);

	// The catalog + pin read is shared by every mounted control (the store
	// dedupes a concurrent read). `refreshToken` re-reads after a studio edit so
	// a persona created there shows up without a page reload; a remount (the rail
	// collapsing/expanding) reuses what the store already has.
	let seenRefresh: number | null = null;
	$effect(() => {
		const token = refreshToken;
		const force = seenRefresh !== null && seenRefresh !== token;
		seenRefresh = token;
		void loadUserPersona(force);
	});

	const catalog = $derived($userPersona);
	const override = $derived(sessionId ? catalog.overrides[sessionId] : undefined);
	/** The chat's own assignment, with a just-written value winning over the host's
	 *  (possibly not-yet-refreshed) list row. */
	const ownId = $derived(override ? override.personaId : personaId);
	/** The chat's explicit-clear flag, same precedence as {@link ownId}. */
	const ownCleared = $derived(override ? override.cleared : personaCleared);
	const hasChat = $derived(sessionId !== null);

	const ownPersona = $derived(catalog.personas.find((p) => p.persona_id === ownId) ?? null);
	const defaultPersona = $derived(
		catalog.personas.find((p) => p.persona_id === catalog.defaultPersonaId) ?? null
	);

	/** No own assignment and no clear: the pinned default is in effect. */
	const inherited = $derived(hasChat && !ownId && !ownCleared && defaultPersona !== null);
	/** The chat carries an explicit "no persona" (a pin does not apply). */
	const explicitNone = $derived(hasChat && !ownId && ownCleared);

	/**
	 * The persona the narrator receives: the chat's own assignment, else the
	 * inherited pin. With NO chat open it is the pin — what a new chat will use.
	 */
	const effectivePersona = $derived(
		hasChat ? (ownPersona ?? (inherited ? defaultPersona : null)) : defaultPersona
	);
	const effectiveId = $derived(effectivePersona?.persona_id ?? null);
	const effectiveName = $derived(effectivePersona?.name || 'Unnamed persona');
	/** Does a default exist for new chats? (The switch's checked state — the pin
	 *  is global, so it stays readable/clearable from any chat, including a
	 *  cleared one that has no persona of its own to pin.) */
	const pinned = $derived(catalog.defaultPersonaId !== null);
	/** Is the persona in effect also the pinned default? (The block's head tag.) */
	const effectiveIsDefault = $derived(
		effectiveId !== null && effectiveId === catalog.defaultPersonaId
	);
	const defaultName = $derived(
		defaultPersona?.name || catalog.defaultPersonaLabel || 'your default persona'
	);

	/** One honest line per state; the explicit-clear case never claims a persona. */
	const stateLine = $derived.by(() => {
		if (!hasChat) {
			return defaultPersona
				? `New chats start as ${defaultName}.`
				: 'No default yet — new chats start with no persona.';
		}
		if (ownPersona) return `This chat plays as ${effectiveName}.`;
		if (inherited) return `No persona set for this chat — using your default, ${effectiveName}.`;
		return explicitNone
			? 'No persona for this chat (cleared — your default does not apply).'
			: 'No persona for this chat.';
	});

	/** The switch's explanation: always names the pin that actually applies. */
	const pinNote = $derived.by(() => {
		if (catalog.defaultPersonaId === null)
			return 'No default — new chats start with no persona.';
		if (effectiveIsDefault) return `New chats start as ${effectiveName}.`;
		return `New chats start as ${defaultName}.`;
	});

	// Once the host's refreshed list carries the value a write put in the
	// override, the override has done its job: drop it so the row is the truth
	// again (a different surface editing the same chat must win next).
	$effect(() => {
		const id = sessionId;
		if (!id) return;
		const pending = $userPersona.overrides[id];
		if (!pending) return;
		if (pending.personaId === personaId && pending.cleared === personaCleared) {
			dropUserPersonaOverride(id);
		}
	});

	// The chip's panel closes on an outside press or Escape (Escape is
	// preventDefaulted so the rail's scope-pop does not also fire).
	$effect(() => {
		if (!open) return;
		const onDown = (event: PointerEvent): void => {
			const target = event.target as Node | null;
			if (target && wrapEl?.contains(target)) return;
			open = false;
		};
		const onKey = (event: KeyboardEvent): void => {
			if (event.key !== 'Escape') return;
			event.preventDefault();
			open = false;
		};
		document.addEventListener('pointerdown', onDown, true);
		document.addEventListener('keydown', onKey);
		return () => {
			document.removeEventListener('pointerdown', onDown, true);
			document.removeEventListener('keydown', onKey);
		};
	});

	/**
	 * Pick a persona (or "No persona").
	 *
	 * With a chat open this assigns the chat (an empty pick is the explicit
	 * clear). With no chat open it sets the pinned default only — a new chat
	 * then uses it.
	 */
	async function choose(id: string | null): Promise<void> {
		if (busy || pinBusy) return;
		error = '';
		if (!hasChat || !sessionId) {
			if (id === catalog.defaultPersonaId) return;
			pinBusy = true;
			try {
				await pinUserPersona(id);
				onChanged?.();
			} catch (err) {
				error = message(err);
			} finally {
				pinBusy = false;
			}
			return;
		}
		// Re-picking the current value is a no-op; a never-set chat CAN record an
		// explicit clear, so the empty pick is only a no-op for an explicit none.
		if (id === null ? !ownId && ownCleared : id === ownId) return;
		busy = true;
		try {
			await assignUserPersona(sessionId, id);
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** The "Use for new chats" switch pins the effective persona / unpins. */
	async function toggleDefault(event: Event): Promise<void> {
		if (pinBusy) return;
		const input = event.currentTarget as HTMLInputElement;
		const next = input.checked;
		error = '';
		pinBusy = true;
		try {
			await pinUserPersona(next ? effectiveId : null);
			onChanged?.();
		} catch (err) {
			// The write failed: put the box back where the server still is.
			input.checked = pinned;
			error = message(err);
		} finally {
			pinBusy = false;
		}
	}

	/** Edit opens the studio's Persona pane (the single definition door, D51). */
	function editPersona(): void {
		onOpenStudio('persona', effectiveId ? { focusId: effectiveId } : {});
	}

	/** A row's right-hand tag: which of the states this row currently carries. */
	function rowTag(persona: Persona): string {
		if (hasChat && ownId === persona.persona_id) return 'this chat';
		if (catalog.defaultPersonaId === persona.persona_id) return 'default';
		return '';
	}
</script>

{#snippet body()}
	<div class="pc-state">
		{#if effectivePersona}
			<UserBadge
				name={effectiveName}
				id={effectivePersona.persona_id}
				imageAsset={effectivePersona.image_asset ?? null}
				kind="persona"
				size="1.6rem"
			/>
		{:else}
			<span class="pc-avatar-none" aria-hidden="true">?</span>
		{/if}
		<div class="pc-state-text">
			<span class="pc-you">You: {effectivePersona ? effectiveName : 'not set'}</span>
			<p class="pc-state-line">{stateLine}</p>
		</div>
	</div>

	{#if effectivePersona}
		<div class="pc-desc-block">
			<span class="pc-label">Description</span>
			{#if effectivePersona.description.trim()}
				<!-- The description itself, unedited and unclamped: the same text the
				     narrator receives in its "User (the player)" block. -->
				<p class="pc-desc">{effectivePersona.description}</p>
			{:else}
				<p class="pc-desc pc-desc-empty">
					No description yet — the narrator receives only your name.
				</p>
			{/if}
		</div>
	{/if}

	<div class="pc-list-block">
		<span class="pc-label">Personas</span>
		<ul class="pc-list">
			<li>
				<button
					type="button"
					class="pc-row"
					class:current={hasChat ? !effectivePersona : catalog.defaultPersonaId === null}
					aria-pressed={hasChat ? !effectivePersona : catalog.defaultPersonaId === null}
					title={hasChat
						? 'No persona — the narrator receives no description of you'
						: 'No default — new chats start with no persona'}
					onclick={() => void choose(null)}
				>
					<span class="pc-row-none" aria-hidden="true">—</span>
					<span class="pc-row-name">No persona</span>
					{#if hasChat && explicitNone}<span class="pc-row-tag">cleared</span>{/if}
				</button>
			</li>
			{#each catalog.personas as persona (persona.persona_id)}
				{@const tag = rowTag(persona)}
				<li>
					<button
						type="button"
						class="pc-row"
						class:current={hasChat
							? ownId === persona.persona_id
							: persona.persona_id === effectiveId}						aria-pressed={hasChat
							? ownId === persona.persona_id
							: persona.persona_id === effectiveId}
						onclick={() => void choose(persona.persona_id)}
					>
						<UserBadge
							name={persona.name || 'Unnamed persona'}
							id={persona.persona_id}
							imageAsset={persona.image_asset ?? null}
							kind="persona"
							size="1.3rem"
						/>
						<span class="pc-row-name">{persona.name || 'Unnamed persona'}</span>
						{#if tag}<span class="pc-row-tag">{tag}</span>{/if}
					</button>
				</li>
			{:else}
				<li class="pc-list-empty">
					{catalog.error
						? 'Could not load your personas.'
						: 'No personas yet — create one in the studio.'}
				</li>
			{/each}
		</ul>
	</div>

	<label class="pc-switch">
		<input
			type="checkbox"
			checked={pinned}
			disabled={pinBusy || (!pinned && !effectiveId)}
			onchange={(event) => void toggleDefault(event)}
		/>
		<span class="pc-switch-text">
			Use for new chats
			<span class="pc-switch-note">{pinNote}</span>
		</span>
	</label>

	<div class="pc-actions">
		<button
			type="button"
			class="pc-btn"
			title={effectiveId ? 'Edit this persona in the studio' : 'Open the persona studio'}
			onclick={editPersona}
		>
			Edit
		</button>
	</div>

	{#if error}<p class="pc-error" role="alert">{error}</p>{/if}
{/snippet}

{#if variant === 'chip'}
	<div class="pc pc-chip-wrap" class:right={chipSide === 'right'} bind:this={wrapEl}>
		<button
			type="button"
			class="pc-chip"
			class:compact
			aria-haspopup="dialog"
			aria-expanded={open}
			aria-label={effectivePersona
				? `You: ${effectiveName} — choose or edit your persona`
				: 'You: not set — choose your persona'}
			title={effectivePersona ? `You: ${effectiveName}` : 'You: not set'}
			onclick={() => (open = !open)}
		>
			{#if effectivePersona}
				<UserBadge
					name={effectiveName}
					id={effectivePersona.persona_id}
					imageAsset={effectivePersona.image_asset ?? null}
					kind="persona"
					size="1.35rem"
				/>
			{:else}
				<span class="pc-chip-none" aria-hidden="true">?</span>
			{/if}
			{#if !compact}
				<span class="pc-chip-label">You: {effectivePersona ? effectiveName : 'not set'}</span>
			{/if}
		</button>
		{#if open}
			<div class="pc-pop" role="dialog" aria-label="User persona">
				{@render body()}
			</div>
		{/if}
	</div>
{:else}
	<section class="pc pc-block" aria-label="User persona">
		<div class="pc-head-row">
			<h3 class="pc-head">User persona</h3>
			{#if effectiveIsDefault}<span class="pc-head-tag">default</span>{/if}
		</div>
		{@render body()}
	</section>
{/if}

<style>
	/* --- shared body --------------------------------------------------------- */
	.pc-state {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		min-width: 0;
	}
	.pc-state-text {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		min-width: 0;
	}
	/* 14px: the identity line is not a micro-label. */
	.pc-you {
		font-size: var(--font-ui);
		font-weight: 600;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pc-state-line {
		margin: 0;
		font-size: var(--font-small);
		line-height: 1.35;
		color: var(--text-muted);
	}
	.pc-avatar-none {
		display: grid;
		place-items: center;
		flex: none;
		width: 1.6rem;
		height: 1.6rem;
		border-radius: 50%;
		border: 1px dashed var(--line-strong);
		color: var(--text-dim);
		font-size: var(--font-small);
	}
	.pc-label {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
	}
	.pc-desc-block {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	/* The description in full: line breaks preserved, nothing clamped — this is
	   the text the narrator reads back. */
	.pc-desc {
		margin: 0;
		font-size: var(--font-small);
		line-height: 1.4;
		color: var(--text-muted);
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	.pc-desc-empty {
		color: var(--text-dim);
		font-style: italic;
	}
	.pc-list-block {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.pc-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		max-height: 12rem;
		overflow-y: auto;
	}
	.pc-row {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		width: 100%;
		min-width: 0;
		padding: 0.3rem 0.35rem;
		border: 1px solid transparent;
		border-radius: 6px;
		background: none;
		font: inherit;
		/* 13px rows: the old 11px select was part of the defect. */
		font-size: var(--font-small);
		color: var(--text-muted);
		text-align: left;
		cursor: pointer;
	}
	.pc-row:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.pc-row.current {
		background: var(--bg-active);
		border-color: var(--accent-line);
		color: var(--text);
	}
	.pc-row-none {
		flex: none;
		width: 1.3rem;
		text-align: center;
		color: var(--text-dim);
	}
	.pc-row-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pc-row-tag {
		flex: none;
		font-size: var(--font-micro);
		color: var(--accent-strong);
	}
	.pc-list-empty {
		font-size: var(--font-small);
		color: var(--text-dim);
		padding: 0.15rem 0.35rem;
	}
	.pc-switch {
		display: flex;
		align-items: flex-start;
		gap: 0.4rem;
		cursor: pointer;
	}
	.pc-switch input {
		margin: 0.15rem 0 0;
		accent-color: var(--accent-hover);
	}
	.pc-switch input:disabled {
		cursor: default;
	}
	.pc-switch-text {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		font-size: var(--font-small);
		color: var(--text);
	}
	.pc-switch-note {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.pc-actions {
		display: flex;
		gap: 0.3rem;
	}
	.pc-btn {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.2rem 0.6rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.pc-btn:hover {
		border-color: var(--accent-line);
		background: var(--bg-hover);
		color: var(--accent-strong);
	}
	.pc-error {
		margin: 0;
		font-size: var(--font-small);
		color: var(--danger-text);
	}

	/* --- block (the rail's first layer) ------------------------------------- */
	.pc-block {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.35rem 0.3rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.pc-head-row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.3rem;
	}
	.pc-head {
		margin: 0;
		font-size: var(--font-micro);
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
	}
	.pc-head-tag {
		font-size: var(--font-micro);
		color: var(--accent-strong);
	}

	/* --- chip (the composer row + the collapsed rail) ----------------------- */
	.pc-chip-wrap {
		position: relative;
		display: inline-flex;
		min-width: 0;
	}
	.pc-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		max-width: 15rem;
		min-width: 0;
		padding: 0.25rem 0.6rem 0.25rem 0.3rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--text-muted);
		font: inherit;
		font-size: var(--font-small);
		cursor: pointer;
	}
	.pc-chip:hover {
		border-color: var(--accent-line);
		color: var(--text);
	}
	.pc-chip.compact {
		padding: 0.15rem;
		gap: 0;
	}
	.pc-chip-label {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pc-chip-none {
		display: grid;
		place-items: center;
		flex: none;
		width: 1.35rem;
		height: 1.35rem;
		border-radius: 50%;
		border: 1px dashed var(--line-strong);
		color: var(--text-dim);
		font-size: var(--font-small);
	}
	.pc-pop {
		position: absolute;
		/* Opens upward, its RIGHT edge on the chip's: the composer chip sits at the
		   right end of the input row, so growing left keeps the panel on screen. */
		right: 0;
		bottom: calc(100% + 0.4rem);
		z-index: var(--z-pane, 10);
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
		width: min(20rem, calc(100vw - 2rem));
		max-height: min(26rem, 70vh);
		overflow-y: auto;
		padding: 0.6rem;
		border: 1px solid var(--line-strong);
		border-radius: 10px;
		background: var(--bg-panel);
		box-shadow: 0 10px 30px var(--shadow);
	}
	.pc-chip-wrap.right .pc-pop {
		right: auto;
		bottom: auto;
		top: 0;
		left: calc(100% + 0.4rem);
		width: min(20rem, calc(100vw - 4rem));
	}
</style>
