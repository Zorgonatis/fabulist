<script lang="ts">
	// Proposals home — the STUDIO half of the review home (W9.22 §4, W9.23 §3.2/§3.6.7).
	//
	// The plan makes the review home two first-class surfaces over one component. The
	// chat drawer is SESSION-scoped (its queue, its committed list, its undo); this
	// pane is the CROSS-SESSION INBOX: every chat's live proposals in one queue,
	// each session collapsible, so the owner can clear amendments filed while she was
	// reading something else.
	//
	// TWO PARTITIONS over the SAME body: the queue is the live fold, the archive is
	// the aged-out one (an item moves there 10 turns after it was filed — never
	// deleted). The shared body renders the archive with no checkboxes and no mass
	// buttons; the server refuses archived ids inside a multi-id decide, so the UI
	// simply never offers them. The toggle is here because the studio owns the read.
	//
	// The HISTORIAN control plane lands here too (§3.6.7): the world/actor/book lane
	// system runs OUTSIDE the turn flow, on cadence (preset data, editable with NO
	// chat open) or on demand from a chat. The strip below the header carries the
	// run-now action and, beside it, the cadence input in the PlannerPane row's
	// shape — cadence should read the same in both panes — plus the due-in readout.
	//
	// DEGRADED BY DESIGN: the historian endpoints are newer than this pane. When the
	// config or status read fails, the cadence input and the run action go disabled
	// behind ONE quiet reason line — never an error dump with a stack of detail. A
	// surface that cannot reach the control plane must not look like a broken queue.
	//
	// Why the read lives here and not in the shared body: the inbox is asset-scoped
	// (an accepted proposal writes a catalog asset every session shares, so a
	// decision recorded anywhere retires the item everywhere), and the studio is the
	// surface that knows the whole catalog — it also has to tell the shell when a
	// decision changed a definition (`onChanged`, the rail re-reads its catalogs).
	// The body receives the data + its honesty state (`InboxState`) and renders both
	// surfaces' queue with one grouping and one action vocabulary.
	//
	// Props it does not use are the studio's shared pane contract (D51 §2.3), declared
	// so the shell can pass them to every pane: this pane has no addressable row and
	// no draft of its own.
	import { onMount } from 'svelte';
	import {
		DEFAULT_HISTORIAN_CADENCE,
		getHistorianConfig,
		historianConfigError,
		historianErrorDetails,
		historianStatus,
		putHistorianConfig,
		runHistorian,
		type HistorianConfig,
		type HistorianStatus
	} from '#lib/api/historian';
	import { getProposalInbox, type InboxState } from '#lib/api/proposals';
	import { StreamError } from '#lib/api/shared';
	import ProposalReview from '#lib/proposals/ProposalReview.svelte';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty,
		sessionId = null
	}: {
		/** Inert: the inbox has no addressable row. */
		focusId?: string | null;
		/** Inert: proposals are filed by gates, not created in a pane. */
		startNew?: boolean;
		/** Inert: no `focusId` to re-apply. */
		requestSeq?: number | null;
		/** A decision can write a catalog asset — the rail re-reads its catalogs. */
		onChanged?: () => void;
		/** The cadence draft is unsaved work; the studio's discard guard reads this. */
		onDirty?: (dirty: boolean) => void;
		/** The open chat — the run-now door and the due-in readout are session-scoped. */
		sessionId?: string | null;
	} = $props();

	/** Which partition of the fold the shared body renders (§3.2). */
	type Mode = 'queue' | 'archive';

	let inbox = $state<InboxState>({ sessions: [], loading: true, error: '' });
	let mode = $state<Mode>('queue');

	// --- historian control plane (§3.6.7) ------------------------------------
	/** The cadence draft stays TEXT so a cleared or half-typed value stays visible. */
	let cadenceText = $state(String(DEFAULT_HISTORIAN_CADENCE));
	let cadenceBaseline = $state('');
	let configLoading = $state(true);
	let savingCadence = $state(false);
	/** True once the config read succeeded — a failed read leaves the row disabled. */
	let configLoaded = $state(false);
	let configUnavailable = $state('');
	let cadenceError = $state('');

	let status = $state<HistorianStatus | null>(null);
	/** True once the status read for the open chat succeeded. */
	let statusLoaded = $state(false);
	let statusUnavailable = $state('');
	let running = $state(false);
	/** The last run's outcome line (the runner's own counts). */
	let runNotice = $state('');
	/** A run the engine refused (409 in flight / 503 no runner / lane error). */
	let runError = $state('');
	let loadedFor: string | null = null;

	const pendingCount = $derived(
		inbox.sessions.reduce((total, session) => total + session.pending.length, 0)
	);
	const archivedCount = $derived(
		inbox.sessions.reduce((total, session) => total + session.archived.length, 0)
	);

	const cadenceDirty = $derived(cadenceBaseline !== '' && cadenceBaseline !== cadenceText);
	const busy = $derived(savingCadence || running);

	$effect(() => {
		onDirty?.(cadenceDirty);
	});

	/** The due-in readout: what the cadence clock says for the open chat. */
	const dueText = $derived(
		status === null
			? ''
			: status.due_in <= 0
				? 'historian due now'
				: `historian due in ${status.due_in} turn${status.due_in === 1 ? '' : 's'}`
	);

	/** The one quiet line for a control plane the engine does not serve (either read). */
	const unavailable = $derived(
		configUnavailable !== ''
			? configUnavailable
			: sessionId !== null && statusUnavailable !== ''
				? statusUnavailable
				: ''
	);

	/** Why the run action cannot be pressed right now ('' = it can). */
	const runBlocked = $derived(
		running || status?.running === true
			? 'a historian run is already in flight'
			: !configLoaded
				? 'the historian settings read is unavailable'
				: !sessionId
					? 'open a chat to run the historian on it'
					: !statusLoaded
						? 'the historian status read is unavailable'
						: ''
	);

	onMount(() => {
		void load();
		void loadConfig();
	});

	async function load(): Promise<void> {
		inbox = { sessions: inbox.sessions, loading: true, error: '' };
		const result = await getProposalInbox();
		if ('error' in result) {
			inbox = { sessions: [], loading: false, error: result.error };
			return;
		}
		inbox = { sessions: result.sessions, loading: false, error: '' };
	}

	/** A decision landed: tell the shell (catalogs changed), then re-read the inbox. */
	async function refreshAfterDecision(): Promise<void> {
		onChanged?.();
		await load();
	}

	function message(err: unknown): string {
		const detail = historianErrorDetails(err)[0] ?? String(err);
		const http = err instanceof StreamError ? err.meta?.status : undefined;
		return typeof http === 'number' ? `${detail} (HTTP ${http})` : detail;
	}

	// --- cadence -------------------------------------------------------------

	async function loadConfig(): Promise<void> {
		configLoading = true;
		cadenceError = '';
		try {
			applyConfig(await getHistorianConfig());
			configLoaded = true;
			configUnavailable = '';
		} catch (err) {
			configLoaded = false;
			configUnavailable = message(err);
		} finally {
			configLoading = false;
		}
	}

	function applyConfig(config: HistorianConfig): void {
		cadenceText = String(config.cadence);
		cadenceBaseline = cadenceText;
	}

	/** Draft -> wire. A blank cadence becomes NaN, which the shared check rejects. */
	function toConfig(): HistorianConfig {
		const text = cadenceText.trim();
		return { cadence: text === '' ? NaN : Number(text) };
	}

	/** The owner's cadence is preset data — saving needs no chat open. */
	async function saveCadence(): Promise<void> {
		if (busy || !configLoaded) return;
		const config = toConfig();
		const problem = historianConfigError(config);
		if (problem) {
			// Nothing is written on a rejection — the draft stays for correction.
			cadenceError = problem;
			return;
		}
		savingCadence = true;
		cadenceError = '';
		try {
			applyConfig(await putHistorianConfig(config));
			// The status read serves `due_in` derived from this cadence — refresh it,
			// or the readout contradicts the value the owner just saved.
			if (sessionId) void loadStatus(sessionId);
			onChanged?.();
		} catch (err) {
			cadenceError = message(err);
		} finally {
			savingCadence = false;
		}
	}

	// --- run now / due-in ----------------------------------------------------

	/** Keyed on the id: a chat switch re-reads the clock, closing the chat clears it. */
	$effect(() => {
		const id = sessionId;
		if (id === loadedFor) return;
		loadedFor = id;
		status = null;
		statusLoaded = false;
		statusUnavailable = '';
		runNotice = '';
		runError = '';
		if (id) void loadStatus(id);
	});

	async function loadStatus(id: string): Promise<void> {
		try {
			const read = await historianStatus(id);
			// A chat switch during the read wins: the stale answer is dropped.
			if (loadedFor !== id) return;
			status = read;
			statusLoaded = true;
			statusUnavailable = '';
		} catch (err) {
			if (loadedFor !== id) return;
			status = null;
			statusLoaded = false;
			statusUnavailable = message(err);
		}
	}

	/**
	 * Run the historian NOW on the open chat. It does NOT claim the session turn
	 * slot (§3.6.7), so a send is never blocked behind it; afterwards both reads
	 * are refreshed — the run filed rows and moved the clock.
	 */
	async function runNow(): Promise<void> {
		const id = sessionId;
		if (busy || !id || runBlocked !== '') return;
		running = true;
		runNotice = '';
		runError = '';
		try {
			const result = await runHistorian(id);
			runNotice =
				result.gate === 'proceeded'
					? `ran at turn ${result.turn_seq} — ${result.filed} filed`
					: `ran at turn ${result.turn_seq} — ${result.gate.replace(/_/g, ' ')}`;
			await Promise.all([load(), loadStatus(id)]);
		} catch (err) {
			runError = message(err);
		} finally {
			running = false;
		}
	}
</script>

<div class="proposals-home">
	<header class="pane-head">
		<h2 class="pane-title">Proposals</h2>
		<span class="pane-meta">
			{inbox.sessions.length} chat{inbox.sessions.length === 1 ? '' : 's'} · {pendingCount} pending
			· {archivedCount} archived{dueText ? ` · ${dueText}` : ''}
		</span>
		<div class="segmented" role="group" aria-label="Proposal partition">
			<button
				type="button"
				class="segment"
				aria-pressed={mode === 'queue'}
				onclick={() => (mode = 'queue')}
			>
				Queue ({pendingCount})
			</button>
			<button
				type="button"
				class="segment"
				aria-pressed={mode === 'archive'}
				onclick={() => (mode = 'archive')}
			>
				Archive ({archivedCount})
			</button>
		</div>
		<button
			type="button"
			class="ghost"
			aria-label="Refresh the proposal inbox"
			disabled={inbox.loading}
			onclick={() => void load()}
		>
			Refresh
		</button>
	</header>

	<section class="historian" aria-labelledby="historian-heading">
		<h3 id="historian-heading" class="historian-title">Historian</h3>
		<button
			type="button"
			class="ghost"
			aria-label="Run historian now"
			aria-busy={running || status?.running === true}
			disabled={runBlocked !== '' || busy}
			title={runBlocked}
			onclick={() => void runNow()}
		>
			{#if running || status?.running === true}
				<span class="spinner" aria-hidden="true"></span>
				Running…
			{:else}
				Run historian now
			{/if}
		</button>
		<label class="field-inline">
			<span>Every</span>
			<input
				class="cadence"
				type="number"
				min="1"
				step="1"
				aria-label="Historian cadence (committed turns)"
				value={cadenceText}
				disabled={!configLoaded || configLoading || busy}
				title={configLoaded ? '' : 'the historian cadence read is unavailable'}
				oninput={(event) => {
					cadenceText = event.currentTarget.value;
					cadenceError = '';
				}}
			/>
			<span>committed turns</span>
		</label>
		<button
			type="button"
			class="ghost"
			disabled={!configLoaded || busy || !cadenceDirty}
			onclick={() => void saveCadence()}
		>
			{savingCadence ? 'Saving…' : 'Save cadence'}
		</button>
		{#if runNotice}<span class="historian-note">{runNotice}</span>{/if}
		{#if cadenceError || runError}
			<p class="historian-error" role="alert">{cadenceError || runError}</p>
		{/if}
		{#if unavailable}
			<p class="historian-reason">
				Historian unavailable — {unavailable}. Cadence editing and Run historian now stay
				disabled until the engine serves them.
			</p>
		{:else}
			<p class="historian-reason">
				The historian files the world/actor/book proposals outside the turn flow — on cadence
				or on demand. It never touches the cast door, and an archived item can still be
				accepted on its own.{#if !sessionId}
					A run is always for one chat (it files into that chat’s chain), so open a chat to run
					it now.{/if}
			</p>
		{/if}
	</section>

	<p class="hint">
		Agents propose; you dispose. Every chat’s live amendments are queued here — accept,
		reject, or dismiss them, singly or by the group mask. Nothing is written until you
		accept, and an accept is undoable from the chat’s Proposals drawer (the committed
		list is per chat). Nothing here needs a chat open: proposals name catalog assets.
	</p>

	<ProposalReview {inbox} {mode} onRefresh={() => void refreshAfterDecision()} />
</div>

<style>
	.proposals-home {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		font-size: var(--font-small);
		color: var(--text);
	}
	.pane-head {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.pane-title {
		margin: 0;
		font-size: var(--font-ui);
	}
	.pane-meta {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	/* The Queue|Archive partition (§3.2): one segmented control, one shared body. */
	.segmented {
		display: flex;
		gap: 0.15rem;
		margin-left: auto;
		padding: 0.1rem;
		border: 1px solid var(--line-strong);
		border-radius: 7px;
	}
	.segment {
		padding: 0.1rem 0.45rem;
		border: 1px solid transparent;
		border-radius: 5px;
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.segment[aria-pressed='true'] {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	/* The control-plane strip — PlannerPane's cadence row, one line. */
	.historian {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
		padding: 0.3rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.historian-title {
		margin: 0;
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	.field-inline {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}
	.field-inline > span {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.cadence {
		width: 4.6rem;
	}
	.historian-note {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.historian-error {
		flex-basis: 100%;
		margin: 0;
		font-size: var(--font-micro);
		color: var(--danger-text);
	}
	.historian-reason {
		flex-basis: 100%;
		margin: 0;
		font-size: var(--font-micro);
		color: var(--text-muted);
		line-height: 1.4;
	}
	.hint {
		margin: 0;
		color: var(--text-muted);
		font-size: var(--font-micro);
		line-height: 1.45;
	}
	/* A run in flight is ~40-60 s of three lane calls: say so, don't stall. */
	.spinner {
		display: inline-block;
		width: 0.5rem;
		height: 0.5rem;
		margin-right: 0.3rem;
		vertical-align: -0.05rem;
		border: 2px solid var(--line-strong);
		border-top-color: var(--accent-strong);
		border-radius: 50%;
		animation: historian-spin 0.9s linear infinite;
	}
	@keyframes historian-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spinner {
			animation: none;
		}
	}
	.ghost {
		padding: 0.1rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.ghost:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
</style>
