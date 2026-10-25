<script lang="ts">
	// Assistant panel (D38): the assistant's own area, docked directly on top of
	// the composer inside the thread column. It is NOT a transcript aside and NOT
	// a floating drawer — it belongs to the composer.
	//
	// States
	//   (a) No assistant enabled -> a slim rail with a settings affordance.
	//       Opening it lists the available assistants (GET /api/assistants), each
	//       with one toggle; at most one is on. It collapses on focus loss.
	//   (b) Enabled -> PINNED open. The engine's latest panel statement
	//       (`asst = AssistantCard(...)`, GET /api/sessions/{id}/assistant-panel)
	//       renders through `panelLibrary`; a shimmer shows while the per-turn
	//       consult is in flight; the latest replies are listed as history.
	//       Toggling off unpins and the collapse behavior resumes.
	//
	// Data path
	//   Enablement is GET/PATCH /api/sessions/{id}/assistant (a single slot). The
	//   per-turn statement/refresh is keyed to the `facts` prop exactly like
	//   StateSidebar: the parent replaces that object after every kept turn, which
	//   re-runs the effect. The consult is fire-and-forget and lands ~15-25s after
	//   the turn, so a single fetch at turn end reads the PRE-consult state; after a
	//   kept turn we therefore open a BOUNDED REFRESH WINDOW that polls the panel +
	//   replies until the statement changes or the reply count grows (or ~120s
	//   elapses). A missing statement (404/empty) is NORMAL before the first
	//   consult, so the panel stays quiet instead of showing an error.
	//
	// Interaction seam (docs/UI-LANGUAGE.md §6)
	//   Clicking an AssistantCard option fires a `continue_conversation` action;
	//   the host FILLS the composer and never auto-sends.
	import { get } from 'svelte/store';
	import { Renderer } from '@openuidev/svelte-lang';
	import type { ActionEvent, ParseResult } from '@openuidev/svelte-lang';
	import { panelLibrary } from '#lib/openui/panelLibrary';
	import { stripAssistantStatement } from '#lib/openui/panelText';
	import {
		getAssistantPanel,
		getSessionAssistant,
		listAssistantReplies,
		listAssistants,
		listPersonas,
		setSessionAssistant,
		USE_MOCK
	} from '#lib/api';
	import type { AssistantCatalog, AssistantReplyRecord, Persona } from '#lib/api';
	import {
		assistantReplies,
		fillComposer,
		resetAssistantReplies,
		sessionId,
		setAssistantReplies
	} from '#lib/stores';
	import type { AssistantReplyEntry } from '#lib/stores';
	import AssistantReplyBlock from './AssistantReplyBlock.svelte';

	let {
		facts = {},
		streaming = false,
		onOpenStudio,
		pickerOpen = $bindable(false),
		enabled = $bindable(false)
	}: {
		facts?: Record<string, string>;
		streaming?: boolean;
		/** Open the Design Studio on the Assistants tab (D51). The definitions
		 *  editor moved out of the retired AssistantDrawer; optional so an unwired
		 *  mount keeps the rest of the panel working. */
		onOpenStudio?: () => void;
		/**
		 * The settings picker is open (issue #22). Lifted to the HOST because the
		 * disabled strip no longer exists at rest: the open door is now the ✦
		 * action in the composer's own row, so the host owns the flag and either
		 * end can close it (focus loss does, the composer button toggles it).
		 */
		pickerOpen?: boolean;
		/** Mirrors "an assistant is pinned for this session" to the host, so the
		 *  composer knows whether to offer the ✦ door at all. */
		enabled?: boolean;
	} = $props();

	/** The enabled assistant id for the current session, or `null`. */
	let enabledId = $state<string | null>(null);
	/** The history disclosure is open. Independent of the settings picker state on
	 * purpose: the panel is PINNED when an assistant is enabled, so the focus-out
	 * collapse belongs to the settings picker only. Keeping history on its own
	 * flag means a focus-loss close of the picker cannot swallow the toggle (the
	 * `saving`-exempt focusout path stays untouched).
	 */
	let historyOpen = $state(false);
	let saving = $state(false);
	let enableError = $state<string | null>(null);

	let catalog = $state<AssistantCatalog | null>(null);
	/** Persona catalog (Bug 4): a persona can be used DIRECTLY as the assistant —
	 *  the picker lists it alongside the definitions and enabling one writes its
	 *  id into the same slot (the engine synthesizes the assistant, no clone). */
	let personas = $state<Persona[]>([]);
	/** Non-reactive mirror: async name lookups must not create effect deps. */
	let catalogCache: AssistantCatalog | null = null;
	let personaCache: Persona[] = [];
	let catalogLoading = false;
	let catalogError = $state<string | null>(null);

	let statement = $state<string | null>(null);
	let panelError = $state<string | null>(null);
	/** False when the Renderer's parser produced no usable root. */
	let panelRenderable = $state(true);
	/**
	 * True for the whole bounded refresh window that follows a kept turn. Unlike a
	 * per-fetch busy flag this does not flicker between polls, so the header's
	 * "consulting…" and the empty-card placeholder both read off it.
	 */
	let consulting = $state(false);

	let panelEl: HTMLElement | null = $state(null);

	const enabledName = $derived(
		(catalog?.assistants.find((a) => a.id === enabledId)?.name ??
			personas.find((p) => p.persona_id === enabledId)?.name ??
			enabledId) || ''
	);
	/** The per-turn consult runs during/after the turn; consulting covers after. */
	const pending = $derived(streaming || consulting);
	const recentReplies = $derived($assistantReplies.slice(-3));

	// -- bounded post-turn refresh ------------------------------------------
	// Plain (non-reactive) bookkeeping: reading these inside the effect must not
	// create dependencies, and the poll token guards against overlapping loops.
	let syncedId: string | null = null;
	let wasStreaming = false;
	let turnArmed = false;
	let lastFacts: Record<string, string> | null = null;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let pollToken = 0;

	const CONSULT_POLL_MS = 3000;
	const CONSULT_WINDOW_MS = 120_000;

	// Session changes reset the pane. A kept turn is detected as: `streaming`
	// dropped (the parent sets `streaming = false` first), then `facts` was
	// replaced (the parent re-projects after the turn) — so a session open (facts
	// set without any streaming) never opens a window. Reads are limited to the
	// props/store so the async writes below cannot feed back into the effect.
	$effect(() => {
		const id = $sessionId;
		const isStreaming = streaming;
		const nextFacts = facts;
		const factsChanged = nextFacts !== lastFacts;
		lastFacts = nextFacts;

		if (!id) {
			syncedId = null;
			wasStreaming = false;
			turnArmed = false;
			cancelConsult();
			enabledId = null;
			statement = null;
			panelError = null;
			resetAssistantReplies();
			return;
		}

		if (id !== syncedId) {
			// Session switch: reset the pane and load its state once.
			syncedId = id;
			wasStreaming = isStreaming;
			turnArmed = false;
			cancelConsult();
			void syncSession(id);
			return;
		}

		if (wasStreaming && !isStreaming) turnArmed = true;
		wasStreaming = isStreaming;

		if (turnArmed && factsChanged && !isStreaming) {
			turnArmed = false;
			if (enabledId) startConsultWindow(id);
		}
	});

	// Never leave a poll loop (or the consulting flag) behind on teardown.
	$effect(() => () => cancelConsult());

	async function syncSession(id: string): Promise<void> {
		await ensureCatalog();
		if (get(sessionId) !== id) return;
		let enabled: string | null = null;
		try {
			enabled = await getSessionAssistant(id);
		} catch (err) {
			enableError = err instanceof Error ? err.message : String(err);
			return;
		}
		if (get(sessionId) !== id) return;
		enabledId = enabled;
		if (!enabled) {
			statement = null;
			resetAssistantReplies();
			return;
		}
		await refreshPanel(id);
		await refreshReplies(id);
	}

	/** Open/close the settings list. The catalog is re-read on every open: the
	 *  definitions editor is the Design Studio now, so nothing tells this panel
	 *  when an assistant changed (the retired AssistantDrawer's `onChanged` hook
	 *  was the only invalidator). */
	function toggleExpanded(): void {
		// The catalog warm-up rides the effect below, so the composer's ✦ door and
		// this button take the SAME path (one fetch, one place to change).
		pickerOpen = !pickerOpen;
	}

	/** Open the Design Studio on Assistants (the head's ✎ door). */
	function openDefinitions(): void {
		pickerOpen = false;
		onOpenStudio?.();
	}

	// The host's composer ✦ door opens the same list; reading the bound flag here
	// keeps the catalog warm-up in ONE place (the door only flips the flag).
	$effect(() => {
		if (pickerOpen) void ensureCatalog(true);
	});

	/**
	 * Close the picker when the pointer leaves the panel.
	 *
	 * The section's own `focusout` covers focus moving INSIDE the panel, but it
	 * cannot cover the composer's ✦ door, which is NOT a descendant (issue #22
	 * moved the disabled state's door into the composer's action row). So while
	 * the list is open, a document-level pointerdown closes it from anywhere —
	 * except the panel's own controls and the door itself (the door is a TOGGLE:
	 * closing on its own pointerdown would just reopen on the click that follows).
	 */
	$effect(() => {
		if (!pickerOpen) return;
		const onPointerDown = (event: PointerEvent) => {
			const target = event.target;
			if (!(target instanceof Element)) return;
			if (panelEl?.contains(target)) return;
			if (target.closest('[data-assistant-door]')) return;
			pickerOpen = false;
		};
		const onFocusIn = (event: FocusEvent) => {
			const target = event.target;
			if (!(target instanceof Element)) return;
			if (panelEl?.contains(target)) return;
			if (target.closest('[data-assistant-door]')) return;
			pickerOpen = false;
		};
		window.addEventListener('pointerdown', onPointerDown, true);
		window.addEventListener('focusin', onFocusIn, true);
		return () => {
			window.removeEventListener('pointerdown', onPointerDown, true);
			window.removeEventListener('focusin', onFocusIn, true);
		};
	});

	// "Is an assistant pinned" is the host's business too: the ✦ door only exists
	// while nothing is pinned (a pinned panel shows its own ⚙).
	$effect(() => {
		enabled = !!enabledId;
	});

	/** Open/close the history disclosure (independent of the settings picker). */
	function toggleHistory(): void {
		historyOpen = !historyOpen;
	}

	async function ensureCatalog(force = false): Promise<void> {
		if (catalogLoading) return;
		if (catalogCache && !force) return;
		catalogLoading = true;
		catalogError = null;
		try {
			const next = await listAssistants();
			catalogCache = next;
			catalog = next;
		} catch (err) {
			catalogError = err instanceof Error ? err.message : String(err);
		}
		// Personas are best-effort: a persona-catalog failure must not take the
		// assistant list down with it (the picker just omits the persona group).
		try {
			const rows = await listPersonas();
			personaCache = rows;
			personas = rows;
		} catch {
			/* keep the assistant catalog usable */
		}
		catalogLoading = false;
	}

	function nameFor(assistantId: string): string {
		return (
			catalogCache?.assistants.find((a) => a.id === assistantId)?.name ??
			personaCache.find((p) => p.persona_id === assistantId)?.name ??
			assistantId
		);
	}

	/** Latest-wins statement fetch; a miss is normal, not an error. */
	async function refreshPanel(id: string): Promise<void> {
		panelError = null;
		try {
			const next = await getAssistantPanel(id);
			if (get(sessionId) !== id) return;
			statement = next;
			panelRenderable = true;
		} catch (err) {
			if (get(sessionId) !== id) return;
			panelError = err instanceof Error ? err.message : String(err);
		}
	}

	/**
	 * Persisted `assistant_reply` rows as panel history entries.
	 *
	 * Rows logged before the engine split the card out of the reply still carry
	 * the raw `asst = AssistantCard(...)` statement (the log is append-only, so
	 * the display strips it — see `#lib/openui/panelText`). A row left with no
	 * prose at all (statement-only legacy row) is dropped: there is nothing to
	 * show and an empty aside would be noise.
	 */
	function toEntries(records: AssistantReplyRecord[]): AssistantReplyEntry[] {
		const entries: AssistantReplyEntry[] = [];
		for (const record of records) {
			const text = stripAssistantStatement(record.text);
			if (!text.trim()) continue;
			entries.push({
				id: `history-assistant-${record.id}`,
				assistant: record.assistant,
				assistantName: nameFor(record.assistant),
				text,
				agent: record.agent,
				at: record.ts ? Date.parse(record.ts) || Date.now() : Date.now(),
				user_msg: record.user_msg
			});
		}
		return entries;
	}

	/** Re-hydrate the persisted `assistant_reply` rows (log hydration). */
	async function refreshReplies(id: string): Promise<void> {
		try {
			const records = await listAssistantReplies(id);
			if (get(sessionId) !== id) return;
			setAssistantReplies(toEntries(records));
		} catch {
			// Hydration is best-effort; the panel itself still works.
		}
	}

	/** Kill any in-flight poll loop (the token makes a stale tick a no-op). */
	function stopPoll(): void {
		pollToken += 1;
		if (pollTimer !== null) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
	}

	/** Stop polling and drop the consulting state (session change / teardown). */
	function cancelConsult(): void {
		stopPoll();
		consulting = false;
	}

	/**
	 * Bounded refresh window opened after a kept turn: the fire-and-forget consult
	 * lands ~15-25s later, so poll the panel + replies until the statement changes
	 * or the reply count grows, then stop. A window that ends with no new statement
	 * is normal (the reply carried no card) and is cleared silently.
	 */
	function startConsultWindow(id: string): void {
		stopPoll();
		if (!enabledId) {
			consulting = false;
			return;
		}
		const token = ++pollToken;
		const baseStatement = statement;
		const baseCount = get(assistantReplies).length;
		const deadline = Date.now() + CONSULT_WINDOW_MS;
		consulting = true;

		const tick = async (): Promise<void> => {
			pollTimer = null;
			if (token !== pollToken || get(sessionId) !== id) return;

			let nextStatement = baseStatement;
			try {
				nextStatement = await getAssistantPanel(id);
			} catch {
				// Transient read miss: keep polling inside the window.
			}
			if (token !== pollToken || get(sessionId) !== id) return;

			let nextCount = baseCount;
			try {
				const records = await listAssistantReplies(id);
				if (token !== pollToken || get(sessionId) !== id) return;
				nextCount = records.length;
				setAssistantReplies(toEntries(records));
			} catch {
				// Hydration is best-effort; the statement check below still runs.
			}
			if (token !== pollToken || get(sessionId) !== id) return;

			const statementChanged = nextStatement !== null && nextStatement !== baseStatement;
			if (statementChanged) statement = nextStatement;
			if (statementChanged || nextCount > baseCount) {
				consulting = false;
				return;
			}
			if (Date.now() >= deadline) {
				consulting = false;
				return;
			}
			pollTimer = setTimeout(() => void tick(), CONSULT_POLL_MS);
		};

		void tick();
	}

	/** Enable `nextId`, or clear the slot with `null` (unpins the panel). */
	async function setEnabled(nextId: string | null): Promise<void> {
		const id = get(sessionId);
		if (!id || saving) return;
		saving = true;
		enableError = null;
		try {
			const result = await setSessionAssistant(id, nextId);
			enabledId = result;
			pickerOpen = false;
			if (result) {
				await refreshPanel(id);
				await refreshReplies(id);
			} else {
				cancelConsult();
				statement = null;
				resetAssistantReplies();
			}
		} catch (err) {
			enableError = err instanceof Error ? err.message : String(err);
		} finally {
			saving = false;
		}
	}

	function toggleEnabled(assistantId: string): void {
		void setEnabled(enabledId === assistantId ? null : assistantId);
	}

	/**
	 * Collapse the settings list once focus leaves the panel. The event bubbles
	 * from any descendant, so a click on a row/option stays open; a click
	 * elsewhere (the composer, the transcript) closes it. An in-flight toggle is
	 * exempt: disabling the focused row blurs it with a null `relatedTarget`,
	 * which must not be read as focus loss.
	 */
	function onFocusOut(event: FocusEvent): void {
		if (!pickerOpen || saving) return;
		const next = event.relatedTarget;
		if (next instanceof Node && panelEl?.contains(next)) return;
		pickerOpen = false;
	}

	/** Actionable options report clicks here; the event FILLS the composer only. */
	function onAction(event: ActionEvent): void {
		const label = event.humanFriendlyMessage?.trim();
		const param = event.params?.message;
		const fallback = typeof param === 'string' ? param.trim() : '';
		fillComposer(label || fallback);
	}

	function onParseResult(result: ParseResult | null): void {
		panelRenderable = !!result?.root;
	}
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section
	class="assistant-panel"
	class:pinned={!!enabledId}
	aria-label="Assistant"
	bind:this={panelEl}
	onfocusout={onFocusOut}
>
	{#if enabledId}
		<header class="panel-head">
			<button
				type="button"
				class="panel-settings"
				aria-expanded={pickerOpen}
				aria-label="Choose the assistant"
				title="Choose the assistant"
				onclick={toggleExpanded}
			>
				⚙
			</button>
			<button
				type="button"
				class="panel-manage"
				aria-label="Edit assistant definitions"
				title="Edit assistant definitions"
				onclick={openDefinitions}
			>
				✎
			</button>
			<span class="panel-title">
				<span class="panel-glyph" aria-hidden="true">✦</span>
				{enabledName}
				{#if USE_MOCK}<span class="mock">MOCK</span>{/if}
			</span>
			{#if pending}
				<span class="panel-state" aria-live="polite">consulting…</span>
			{/if}
			<!-- HISTORY DISCLOSURE: collapsed by default; the toggle + the bounded
			     list below are the whole feature, removable together. -->
			{#if recentReplies.length}
				<button
					type="button"
					class="panel-history-toggle"
					aria-expanded={historyOpen}
					aria-controls="assistant-history-list"
					onclick={toggleHistory}
				>
					History ({recentReplies.length})
				</button>
			{/if}
			<button
				type="button"
				class="panel-off"
				title="Turn this assistant off"
				disabled={saving}
				onclick={() => void setEnabled(null)}
			>
				Turn off
			</button>
		</header>

		<div class="panel-card">
			{#if statement}
				<Renderer
					response={statement}
					library={panelLibrary}
					{onAction}
					onParseResult={onParseResult}
				/>
				{#if !panelRenderable}
					<pre class="raw">{statement}</pre>
				{/if}
			{:else if panelError}
				<p class="error" role="alert">Panel unavailable: {panelError}</p>
			{:else}
				<p class="panel-empty">
					{pending
						? 'Waiting for this turn…'
						: 'No panel yet — it appears after the next turn.'}
				</p>
			{/if}
		</div>

		{#if recentReplies.length && historyOpen}
			<section class="panel-history" id="assistant-history-list" aria-label="Assistant history">
				{#each recentReplies as reply (reply.id)}
					<AssistantReplyBlock {reply} />
				{/each}
			</section>
		{/if}
	{:else if pickerOpen}
		<!-- Issue #22: with NO assistant pinned this strip is not chrome — UI-LANGUAGE
		     §6 says the disabled state collapses. It renders only while the picker is
		     open (the composer's ✦ action opens it), so the thread pays 31px for it
		     exactly when the reader asked for it. The row keeps the list's own close
		     affordance so the picker always has a visible owner. -->
		<div class="rail-row">
			<button
				type="button"
				class="rail"
				aria-expanded={pickerOpen}
				aria-label="Close the assistant list"
				onclick={toggleExpanded}
			>
				<span class="rail-glyph" aria-hidden="true">✦</span>
				<span class="rail-label">Assistant</span>
				<span class="rail-hint">none enabled</span>
			</button>
			<button
				type="button"
				class="panel-manage"
				aria-label="Edit assistant definitions"
				title="Edit assistant definitions"
				onclick={openDefinitions}
			>
				✎
			</button>
		</div>
	{/if}

	{#if pickerOpen}
		<div class="picker" role="group" aria-label="Available assistants and personas">
			<p class="picker-head">
				Assistants and personas — at most one is on. {#if !$sessionId}<span>Start a chat first.</span>{/if}
			</p>
			{#if catalogError}
				<p class="error" role="alert">Could not load assistants: {catalogError}</p>
			{:else if !catalog}
				<p class="panel-empty">Loading assistants…</p>
			{:else if !catalog.assistants.length && !personas.length}
				<p class="panel-empty">No assistants configured.</p>
			{:else}
				{#if catalog.assistants.length}
					<ul class="picker-list">
						{#each catalog.assistants as assistant (assistant.id)}
							<li>
								<button
									type="button"
									class="pick"
									class:on={enabledId === assistant.id}
									aria-pressed={enabledId === assistant.id}
									disabled={saving || !$sessionId}
									onclick={() => toggleEnabled(assistant.id)}
								>
									<span class="pick-row">
										<span class="pick-name">{assistant.name}</span>
										{#if assistant.demo}<span class="demo">DEMO</span>{/if}
										<span class="pick-state">
											{enabledId === assistant.id ? 'On' : 'Off'}
										</span>
									</span>
									<span class="pick-persona">{assistant.persona}</span>
								</button>
							</li>
						{/each}
					</ul>
				{/if}
				{#if personas.length}
					<!-- PERSONA GROUP (Bug 4): enabling one writes the persona id into the
					     SAME slot; the engine synthesizes the assistant from the persona, so
					     no definition is created and no content is cloned. -->
					<p class="picker-group">Personas — use one directly as the assistant</p>
					<ul class="picker-list">
						{#each personas as persona (`persona:${persona.persona_id}`)}
							<li>
								<button
									type="button"
									class="pick"
									class:on={enabledId === persona.persona_id}
									aria-pressed={enabledId === persona.persona_id}
									disabled={saving || !$sessionId}
									onclick={() => toggleEnabled(persona.persona_id)}
								>
									<span class="pick-row">
										<span class="pick-name">{persona.name || 'Unnamed persona'}</span>
										<span class="demo">PERSONA</span>
										<span class="pick-state">
											{enabledId === persona.persona_id ? 'On' : 'Off'}
										</span>
									</span>
									{#if persona.description}
										<span class="pick-persona">{persona.description}</span>
									{/if}
								</button>
							</li>
						{/each}
					</ul>
				{/if}
			{/if}
		</div>
	{/if}

	{#if enableError}
		<p class="error" role="alert">Assistant error: {enableError}</p>
	{/if}
</section>

<!-- The definitions editor is the Design Studio (D51); this panel's head ✎
     opens it via the `onOpenStudio` prop owned by the page. -->

<style>
	.assistant-panel {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		margin: 0 0.75rem 0.35rem;
		color: var(--text);
	}
	.pinned {
		border-top: 1px solid var(--line);
		padding-top: 0.5rem;
	}
	/* ── enabled: pinned panel ─────────────────────────────────────────── */
	.panel-head {
		display: flex;
		align-items: center;
		gap: 0.45rem;
	}
	.panel-settings {
		font: inherit;
		line-height: 1;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		padding: 0.15rem 0.4rem;
		cursor: pointer;
	}
	.panel-settings[aria-expanded='true'] {
		border-color: var(--assistant-accent);
		color: var(--text);
	}
	/* The definitions door (D47): inline icon button, same band as the picker
	   gear, so the default panel height does not change. */
	.panel-manage {
		font: inherit;
		line-height: 1;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		padding: 0.15rem 0.4rem;
		cursor: pointer;
	}
	.panel-manage:hover {
		color: var(--text);
		border-color: var(--line-strong);
	}
	.panel-title {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.panel-glyph {
		color: var(--assistant-accent);
	}
	.panel-state {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.panel-off {
		margin-left: auto;
		font: inherit;
		font-size: var(--font-small);
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		padding: 0.15rem 0.5rem;
		cursor: pointer;
	}
	.panel-off:hover {
		color: var(--text);
		border-color: var(--line-strong);
	}
	.panel-history-toggle {
		font: inherit;
		font-size: var(--font-small);
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		padding: 0.15rem 0.5rem;
		cursor: pointer;
	}
	.panel-history-toggle:hover,
	.panel-history-toggle[aria-expanded='true'] {
		color: var(--text);
		border-color: var(--line-strong);
	}
	.panel-card {
		border: 1px solid var(--line);
		border-left: 3px solid var(--assistant-accent);
		border-radius: 10px;
		background: var(--bg-raised);
		padding: 0.5rem 0.6rem;
	}
	.panel-history {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		/* Bounded disclosure: ~160px of content (box ≈ 168px with rule+padding),
		   rows scroll once the kept replies exceed it. */
		max-height: 10rem;
		overflow-y: auto;
		border-top: 1px solid var(--line);
		padding-top: 0.45rem;
	}
	.raw {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--text-muted);
	}
	/* ── disabled: slim rail ───────────────────────────────────────────── */
	/* The rail + its definitions door share one row (the door is inline, so the
	   rail's collapsed height is unchanged). */
	.rail-row {
		display: flex;
		align-items: stretch;
		gap: 0.35rem;
	}
	.rail {
		display: flex;
		align-items: center;
		gap: 0.45rem;
		flex: 1;
		min-width: 0;
		font: inherit;
		font-size: var(--font-small);
		border: 1px dashed var(--line-strong);
		border-radius: 8px;
		background: transparent;
		color: var(--text-muted);
		padding: 0.3rem 0.6rem;
		cursor: pointer;
	}
	.rail:hover,
	.rail[aria-expanded='true'] {
		color: var(--text);
		border-color: var(--assistant-accent);
	}
	.rail-glyph {
		color: var(--assistant-accent);
	}
	.rail-label {
		font-weight: 600;
	}
	.rail-hint {
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	/* ── the settings list (both states) ───────────────────────────────── */
	.picker {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		border: 1px solid var(--line);
		border-radius: 10px;
		background: var(--bg-panel);
		padding: 0.5rem 0.6rem;
	}
	.picker-head {
		margin: 0;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.picker-head span {
		color: var(--text-dim);
	}
	/* Persona group heading (Bug 4): visually separates the definitions from the
	   personas that can be used directly as the assistant. */
	.picker-group {
		margin: 0.2rem 0 0;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}
	.picker-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		max-height: 14rem;
		overflow-y: auto;
	}
	.pick {
		width: 100%;
		text-align: left;
		font: inherit;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		padding: 0.4rem 0.55rem;
		cursor: pointer;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.pick.on {
		border-color: var(--assistant-accent);
		background: var(--assistant-accent-soft);
	}
	.pick:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.pick-row {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
	}
	.pick-name {
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.pick-state {
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.pick-persona {
		font-size: var(--font-small);
		color: var(--text-muted);
		white-space: pre-wrap;
		max-height: 3.6rem;
		overflow: hidden;
	}
	.demo {
		font-size: 0.55rem;
		font-weight: 700;
		letter-spacing: 0.06em;
		color: var(--text-muted);
		background: var(--bg-hover);
		border-radius: 4px;
		padding: 0.05rem 0.28rem;
	}
	.mock {
		background: var(--danger);
		color: var(--on-accent);
		font-size: 0.58rem;
		font-weight: 700;
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
	}
	.panel-empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.error {
		margin: 0;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
</style>
