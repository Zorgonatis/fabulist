<script lang="ts">
	// Composer: input box, in-character / OOC toggle, send + stop,
	// mic (W3.8 voice channel): record -> POST /api/stt -> transcript
	// drops into the textarea for review before sending (you stay in charge
	// of what actually goes into the log).
	//
	// Image Gen (COMFY-PLAN Wave 4, lane V3 + W1a): the button opens the adjustables
	// strip ONLY. It never changes the composer's turn kind or placeholder, and it
	// never copies the box text. The prompt is typed in the strip (bound two-way so
	// the parent can read it); the generator agent is asked only when the user presses
	// the strip's explicit "Prompt Enhance" button. The button shows only when
	// `GET /api/imagegen/config` reports enabled + reachable, and stays highlighted
	// while the strip is open.
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import { API_BASE, createSession } from '#lib/api';
	import type { ImageGenDefinition, ImageGenOverrides } from '#lib/api/imagegen';
	import {
		getImageGenConfig,
		listImageGenDefinitions,
		renderImageGenPrompts,
		runImageGen
	} from '#lib/api/imagegen';
	import { bumpInspectorRefresh } from '#lib/inspectorRefresh';
	import { composerFill, sessionId, sessions, type TurnKind } from '#lib/stores';
	import type { StudioKind } from '#lib/studio/kinds';
	import ImageGenStrip from './ImageGenStrip.svelte';
	import PersonaControl from './PersonaControl.svelte';

	let {
		disabled = false,
		streaming = false,
		onSend,
		onStop,
		onImageGenerated,
		pendingName = null,
		attachBusy = false,
		attachError = '',
		onAttach,
		onClearAttach,
		assistantEnabled = false,
		assistantPickerOpen = false,
		onToggleAssistant,
		onOpenStudio,
		onPersonaChanged,
		personaRefreshToken = 0
	}: {
		disabled?: boolean;
		streaming?: boolean;
		onSend: (content: string, kind: TurnKind) => void;
		onStop: () => void;
		/** After a successful image run the engine has appended a canon image
		 *  narrative row; the page re-reads the projection to show it. */
		onImageGenerated?: () => void;
		/**
		 * Media attachment, in the ACTIONS row (issue #22).
		 *
		 * The attach control used to live in its own 35px row under the composer —
		 * a permanent row for a control that is empty most of the time. It is one
		 * action now, beside the mic and send, and the pending file rides the same
		 * row as a chip (the page owns the File: it prefixes the next send). The
		 * error line is the only thing that ever adds a row, and only when there
		 * IS an error.
		 */
		pendingName?: string | null;
		attachBusy?: boolean;
		attachError?: string;
		onAttach?: () => void;
		onClearAttach?: () => void;
		/** An assistant is pinned for this session (its panel shows above us). */
		assistantEnabled?: boolean;
		/** The assistant picker is open (host-owned; UI-LANGUAGE §6 collapse). */
		assistantPickerOpen?: boolean;
		/** Open/close the assistant picker — the door the collapsed strip lost. */
		onToggleAssistant?: () => void;
		/** The studio's one definition door — the persona chip's Edit opens the
		 *  studio's Persona pane through it (#94). */
		onOpenStudio: (
			kind: StudioKind,
			opts?: { focusId?: string | null; startNew?: boolean }
		) => void;
		/** Called after the persona chip writes, so the page refreshes its session
		 *  list (the row the chip reads its state from). */
		onPersonaChanged?: () => void;
		/** Bumped by the page after a studio edit: the chip re-reads the catalog. */
		personaRefreshToken?: number;
	} = $props();

	/** The open chat's row — the persona chip's own/inherited/explicit-none input. */
	const personaSession = $derived($sessions.find((row) => row.id === $sessionId) ?? null);

	let text = $state('');
	let kind = $state<TurnKind>('in_character');

	// --- image mode (lane V3) ---------------------------------------------------
	let imageEnabled = $state(false);
	let imageDefinitions = $state<ImageGenDefinition[]>([]);
	let imageStripOpen = $state(false);
	let imageDefinitionId = $state('');
	let imagePositive = $state('');
	let imageNegative = $state('');
	// Bumped whenever the strip is (re)seeded — open, Prompt Enhance, or a
	// definition switch — so the strip remounts and re-seeds its editables.
	let imageRenderSeq = $state(0);
	let imageRendering = $state(false);
	let imageRunning = $state(false);
	let imageError = $state('');

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** Poll the feature flag on mount (the engine caches reachability 60 s). */
	onMount(() => {
		void refreshImageConfig();
	});

	async function refreshImageConfig(): Promise<void> {
		try {
			imageEnabled = (await getImageGenConfig()).enabled;
		} catch {
			// A failed probe means "not usable" — never surface a probe error.
			imageEnabled = false;
		}
	}

	async function loadImageDefinitions(): Promise<void> {
		try {
			imageDefinitions = await listImageGenDefinitions();
			if (!imageDefinitionId && imageDefinitions.length) {
				imageDefinitionId = imageDefinitions[0].imagegen_id;
			}
		} catch (err) {
			imageError = message(err);
		}
	}

	/**
	 * The "Image Gen" toggle: re-poll the flag, load definitions on demand, and open
	 * the strip. It never touches the turn kind, the placeholder, or the box text —
	 * the strip's positive prompt is the only input surface for a render.
	 */
	async function openImageStrip(): Promise<void> {
		imageError = '';
		await refreshImageConfig();
		if (!imageEnabled) return; // flag flipped off — no strip, no error surface
		await seedAndOpenStrip();
	}

	/**
	 * The session a render belongs to: the active session id, lazily creating one
	 * for a fresh chat (the same rule `runCurrentImage` uses for the run). The
	 * session-scoped render route injects that chat's cast + recent scene.
	 */
	async function activeSessionId(): Promise<string> {
		let sid = get(sessionId);
		if (!sid) {
			sid = await createSession();
			sessionId.set(sid);
		}
		return sid;
	}

	/**
	 * Open the adjustables strip empty — the prompt is typed there. No agent call
	 * here: the strip's explicit "Prompt Enhance" button is the only path that asks
	 * the generator agent for prompt text. Returns whether the strip actually
	 * opened. Refuses while a render or a run is in flight, so a second open can
	 * never clobber it.
	 */
	async function seedAndOpenStrip(): Promise<boolean> {
		if (imageRendering || imageRunning) return false;
		if (!imageEnabled) {
			imageError = 'Image generation is unavailable.';
			return false;
		}
		if (!imageDefinitions.length) await loadImageDefinitions();
		if (!imageDefinitions.length) {
			imageError = 'No image gen definitions yet — add one in the Design Studio.';
			return false;
		}
		const definition =
			imageDefinitions.find((item) => item.imagegen_id === imageDefinitionId) ??
			imageDefinitions[0];
		imageDefinitionId = definition.imagegen_id;
		imagePositive = '';
		imageNegative = '';
		imageRenderSeq += 1; // remount the strip so its fields start fresh
		imageRendering = false; // nothing is rendering until the user asks for it
		imageError = '';
		imageStripOpen = true;
		return true;
	}

	/**
	 * Explicit agent render (the strip's "Prompt Enhance" button): ask the generator
	 * agent to turn the typed prompt into positive/negative prompt text. Fires ONLY
	 * when pressed — never on mount, strip open, or definition switch.
	 */
	async function aiGenerateImage(): Promise<void> {
		if (imageRendering || imageRunning || !imageDefinitionId) return;
		imageRendering = true;
		imageError = '';
		try {
			const sid = await activeSessionId();
			const prompts = await renderImageGenPrompts(imagePositive, imageDefinitionId, sid);
			imagePositive = prompts.positive;
			imageNegative = prompts.negative;
			imageRenderSeq += 1;
			// A UI-invoked model call: have an open Inspector re-read its calls.
			bumpInspectorRefresh();
		} catch (err) {
			imageError = message(err);
		} finally {
			imageRendering = false;
		}
	}

	/**
	 * Switching definition re-seeds the strip's seed/resolution fields for the new
	 * workflow. It does NOT render prompts — that stays behind "Prompt Enhance".
	 */
	function changeImageDefinition(id: string): void {
		if (id === imageDefinitionId || imageRendering) return;
		imageDefinitionId = id;
		imageError = '';
		imageRenderSeq += 1; // remount so the param fields follow the definition
	}

	/** Submit the run with the user's explicit overrides; the engine appends the row. */
	async function runCurrentImage(overrides: ImageGenOverrides): Promise<void> {
		if (imageRunning || imageRendering) return;
		imageRunning = true;
		imageError = '';
		try {
			let sid = get(sessionId);
			if (!sid) {
				// A fresh chat has no session yet; create one lazily (like send()).
				sid = await createSession();
				sessionId.set(sid);
			}
			const result = await runImageGen(sid, {
				// The prompt lives in the strip, so its (bound) positive is the run's
				// request; `overrides.positive` is the same value for an adjustable
				// workflow and the fallback when the positive input is not adjustable.
				request: imagePositive || overrides.positive || '',
				imagegen_id: imageDefinitionId,
				overrides
			});
			if (result.error) {
				imageError = `Generation failed (${result.error.stage}): ${result.error.message}`;
				return;
			}
			// The engine appended a canon image narrative row; have the page re-read
			// the projection so the bubble comes from the log, not a fabricated entry.
			onImageGenerated?.();
			// A UI-invoked model call: have an open Inspector re-read its calls.
			bumpInspectorRefresh();
			imageStripOpen = false;
		} catch (err) {
			imageError = message(err);
		} finally {
			imageRunning = false;
		}
	}

	function cancelImageStrip(): void {
		imageStripOpen = false;
		imageError = '';
	}

	// An Assistant option is a suggestion, not a turn: it fills the composer and
	// waits for the user to send (docs/UI-LANGUAGE.md §6 — never auto-send). The
	// request arrives as a store because this component owns `text` locally;
	// assign into that same state and clear the store — no second input path.
	$effect(() => {
		const pending = $composerFill;
		if (pending === null) return;
		text = pending;
		composerFill.set(null);
	});

	function submit(): void {
		const content = text.trim();
		if (!content || streaming || disabled) return;
		onSend(content, kind);
		text = '';
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Enter' && !event.shiftKey) {
			event.preventDefault();
			submit();
		}
		if (recording && event.key === 'Escape') {
			event.preventDefault();
			cancelRecording();
		}
	}

	// --- voice channel (v1: review-before-send) -----------------------------
	let recording = $state(false);
	let transcribing = $state(false);
	let micError = $state('');
	let recorder: MediaRecorder | null = null;
	let chunks: Blob[] = [];

	function beginRecording(): void {
		micError = '';
		if (transcribing || disabled || streaming) return;
		if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
			micError = 'mic unsupported in this browser';
			return;
		}
		navigator.mediaDevices
			.getUserMedia({ audio: true })
			.then((stream) => {
				// Force a REAL container mime: some chromium builds leave
				// MediaRecorder defaulting to an empty string type, and an
				// empty blob.type uploads as application/octet-stream — which
				// the sniff defers to for webm and 415s. Pick the best
				// supported audio mime explicitly.
				const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
				const mimeType = candidates.find((t) => MediaRecorder.isTypeSupported(t));
				recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
				chunks = [];
				recorder.ondataavailable = (e) => {
					if (e.data.size > 0) chunks.push(e.data);
				};
				recorder.onstop = () => {
					stream.getTracks().forEach((t) => t.stop());
				};
				recorder.start();
				recording = true;
			})
			.catch(() => {
				micError = 'microphone unavailable (permission?)';
			});
	}

	function stopRecording(): void {
		if (!recorder) return;
		const finish = async () => {
			recording = false;
			const blob = new Blob(chunks, { type: recorder?.mimeType || 'audio/webm' });
			if (blob.size === 0) return;
			transcribing = true;
			try {
				const form = new FormData();
				form.append('file', new File([blob], 'clip.webm', { type: blob.type }));
				const res = await fetch(`${API_BASE}/api/stt`, { method: 'POST', body: form });
				if (!res.ok) throw new Error(`stt ${res.status}`);
				const body = (await res.json()) as { text: string };
				const said = body.text.trim();
				if (said) text = text ? `${text} ${said}` : said;
			} catch {
				micError = 'transcription failed';
			} finally {
				transcribing = false;
			}
		};
		recorder?.addEventListener('stop', () => void finish(), { once: true });
		recorder?.stop();
	}

	function cancelRecording(): void {
		if (!recorder) return;
		const r = recorder;
		r.addEventListener('stop', () => {
			r.stream.getTracks().forEach((t) => t.stop());
		});
		chunks = [];
		r.stop();
		recording = false;
	}
</script>

{#if imageStripOpen}
	{#key imageRenderSeq}
		<ImageGenStrip
			definitions={imageDefinitions}
			definitionId={imageDefinitionId}
			bind:positive={imagePositive}
			negative={imageNegative}
			rendering={imageRendering}
			running={imageRunning}
			error={imageError}
			onDefinition={changeImageDefinition}
			onGenerate={(overrides) => void runCurrentImage(overrides)}
			onAiGenerate={() => void aiGenerateImage()}
			onCancel={cancelImageStrip}
		/>
	{/key}
{/if}

<form class="composer" onsubmit={(event) => { event.preventDefault(); submit(); }}>
	<div class="toggle" role="group" aria-label="Turn kind">
		<button
			type="button"
			class:active={kind === 'in_character'}
			title="In character — speak as your character inside the scene"
			onclick={() => (kind = 'in_character')}
		>
			In-character
		</button>
		<button
			type="button"
			class:active={kind === 'ooc'}
			title="Out-of-character — talk to the narrator directly"
			onclick={() => (kind = 'ooc')}
		>
			OOC
		</button>
		{#if imageEnabled}
			<button
				type="button"
				class:active={imageStripOpen}
				title="Image Gen — opens the image drawer; you review and edit the prompts before it runs"
				onclick={() => void openImageStrip()}
			>
				Image Gen
			</button>
		{/if}
	</div>
	<textarea
		rows="2"
		placeholder={kind === 'ooc' ? 'Out-of-character note…' : 'What do you do?'}
		bind:value={text}
		onkeydown={onKeydown}
		disabled={disabled}
	></textarea>
	<div class="actions">
		<!-- Issue #94: the always-visible persona chip. It sits at the input line —
		     the moment a player wonders "who am I in this chat?" — and opens the
		     SAME panel the rail's first block renders (one component, one state).
		     It shows the pinned default when no chat is open, so the control is
		     reachable before the first turn. -->
		<div class="persona-slot">
			<PersonaControl
				variant="chip"
				sessionId={$sessionId}
				personaId={personaSession?.persona_id ?? null}
				personaCleared={personaSession?.persona_cleared === true}
				refreshToken={personaRefreshToken}
				{onOpenStudio}
				onChanged={onPersonaChanged}
			/>
		</div>
		<!-- Issue #22: the assistant door. With nothing pinned the strip above the
		     composer is gone (UI-LANGUAGE §6: the disabled state collapses), so the
		     door lives here, in the band the assistant actually belongs to. -->
		{#if !assistantEnabled && onToggleAssistant}
			<button
				type="button"
				class="assistant-door"
				data-assistant-door="1"
				class:active={assistantPickerOpen}
				aria-expanded={assistantPickerOpen}
				aria-label={assistantPickerOpen ? 'Close the assistant list' : 'Choose an assistant'}
				title="Assistant — pin a voice above the composer"
				onclick={onToggleAssistant}
				>✦</button
			>
		{/if}
		{#if pendingName}
			<span class="attach-chip" title={pendingName}>
				<span class="attach-chip-name">{pendingName}</span>
				<button
					type="button"
					class="attach-clear"
					aria-label="clear the attached media"
					title="Clear the attachment"
					onclick={onClearAttach}>×</button
				>
			</span>
		{/if}
		{#if onAttach}
			<button
				type="button"
				class="attach"
				disabled={streaming || attachBusy}
				aria-label="attach media"
				title="Attach media — it rides your next message"
				onclick={onAttach}>{attachBusy ? '⏳' : '📎'}</button
			>
		{/if}
		{#if recording}
			<button
				type="button"
				class="mic active"
				onclick={stopRecording}
				aria-label="stop recording and transcribe"
				title="click to transcribe · Esc to discard"
				>⏹</button
			>
		{:else}
			<button
				type="button"
				class="mic"
				onclick={beginRecording}
				disabled={transcribing || disabled || streaming}
				aria-label="record voice message"
				>{transcribing ? '…' : '🎤'}</button
			>
		{/if}
		{#if streaming}
			<button type="button" class="stop" onclick={onStop}>Stop</button>
		{:else}
			<button type="submit" class="send" disabled={disabled || !text.trim()}>Send</button>
		{/if}
	</div>
	{#if micError}
		<p class="mic-error" role="alert">{micError}</p>
	{/if}
	{#if attachError}
		<p class="attach-error" role="alert">{attachError}</p>
	{/if}
</form>

<style>
	.composer {
		display: grid;
		grid-template-columns: auto 1fr auto;
		align-items: end;
		gap: 0.55rem;
		padding: 0.75rem 1rem;
		border-top: 1px solid var(--line);
		background: var(--bg-panel);
	}
	.toggle {
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.toggle button {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.25rem 0.6rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
		white-space: nowrap;
	}
	.toggle button.active {
		background: var(--accent-hover);
		border-color: var(--accent-hover);
		color: var(--on-accent);
	}
	textarea {
		font: inherit;
		font-size: var(--font-body);
		resize: vertical;
		padding: 0.5rem 0.65rem;
		border: 1px solid var(--line-strong);
		border-radius: 8px;
		color: var(--text);
		background: var(--bg-inset);
	}
	textarea:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.actions {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	/* The persona chip is the row's first item; `margin-right: auto` keeps the
	   turn's verbs (attach, mic, send) pinned right while it stays left. */
	.persona-slot {
		display: flex;
		margin-right: auto;
		min-width: 0;
	}
	.actions button {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.45rem 0.9rem;
		border-radius: 8px;
		border: 1px solid transparent;
		cursor: pointer;
	}
	/* The two icon actions in this row (assistant door, attach) are square and
	   quiet: they are affordances, not the turn's verbs. */
	.assistant-door,
	.attach {
		padding: 0.45rem 0.55rem;
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text-muted);
		line-height: 1;
	}
	.assistant-door:hover,
	.attach:hover {
		border-color: var(--line-strong);
		color: var(--text);
	}
	.assistant-door.active {
		border-color: var(--assistant-accent);
		color: var(--assistant-accent);
	}
	.attach:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	/* The pending attachment, inline: one line, ellipsized, never a second row. */
	.attach-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		max-width: 12rem;
		min-width: 0;
		padding: 0.2rem 0.25rem 0.2rem 0.5rem;
		border-radius: 999px;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.attach-chip-name {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.attach-clear {
		padding: 0 0.2rem;
		border: 0;
		background: none;
		color: inherit;
		cursor: pointer;
		font-size: var(--font-ui);
		line-height: 1;
	}
	.attach-error {
		margin: 0;
		grid-column: 2 / -1;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
	.send {
		background: var(--accent-hover);
		color: var(--on-accent);
	}
	.send:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.stop {
		background: var(--bg-raised);
		border-color: var(--danger) !important;
		color: var(--danger-text);
	}
	.mic {
		background: var(--bg-raised);
		border-color: var(--line-strong);
		color: var(--text-muted);
	}
	.mic.active {
		border-color: var(--danger);
		color: var(--danger-text);
		animation: none; /* VTK: no attention-grabbing motion */
	}
	.mic:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.mic-error {
		margin: 0;
		grid-column: 2 / -1;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
</style>
