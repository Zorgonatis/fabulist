<script lang="ts">
	// Proposals drawer — the CHAT half of the review home (W9.22 §4, W9.23 §3.6.7).
	//
	// A first-class floating surface like the Cast pane (W9.22 Wave 4): the header
	// carries one toggle and this file owns the pane's geometry, so `+page.svelte`
	// adds a button and a mount and nothing else. It is SESSION-scoped — the body
	// reads GET /api/proposals?session= through `#lib/api/proposals` and shows
	// committed items (with UNDO) above the live queue for THIS chat.
	//
	// W9.23 §3.6.7 adds the HISTORIAN control plane to this surface: the agent that
	// files the world/actor/book amendments now runs on a cadence (like the planner)
	// or on demand, OUTSIDE the turn flow. The header therefore carries
	// "Run historian now" (+ a spinner while it works) and the last-run/due line
	// ("ran 4 turns ago — 3 filed · due in 6 turns"). A completed run re-reads BOTH
	// the status and the queue body (the run files rows this drawer must show).
	// A run whose gate answer was below the confidence floor filed nothing and
	// judged nothing: its line says "historian unsure, run now?" rather than
	// "0 filed", because a quiet zero would read as a window with nothing in it.
	//
	// The DEGRADED PATH is a first-class state, not an afterthought: the historian
	// endpoints land in a later lane, so an absent/unreachable endpoint must leave
	// the drawer QUIET — no run line, a disabled run button whose reason rides its
	// label/title, and never an error banner. The proposals queue below keeps
	// working regardless; only the historian strip degrades.
	//
	// The archive partition (§3.2) lives in a SEPARATE drawer (linked here), because
	// archived rows are never mass-selectable: keeping the archive out of this
	// surface's selection tree is what makes "archived items are never affected by
	// mass selections" a property of the layout, not a runtime check.
	//
	// The cross-session queue is the studio's half (`ProposalsHome`); the two share
	// `ProposalReview.svelte`, which is what keeps one grouping/action vocabulary.
	import DraggablePane from '#lib/components/DraggablePane.svelte';
	import ProposalReview from '#lib/proposals/ProposalReview.svelte';
	import {
		historianErrorDetails,
		historianStatus,
		runHistorian,
		type HistorianStatus
	} from '#lib/api/historian';

	let {
		sessionId = null,
		onclose,
		onOpenArchive
	}: {
		/** The open chat; `null` renders the body's empty state, never a stale queue. */
		sessionId?: string | null;
		onclose?: () => void;
		/** Opens the separate archive drawer (aged-out rows; no mass selection). */
		onOpenArchive?: () => void;
	} = $props();

	// `window` at render crashes SSR (AGENTS.md gotchas): the pane is only mounted
	// from a client toggle, but the guard keeps a future SSR mount safe.
	const x = typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 980) : 300;

	// --- historian control plane (§3.6.7) ------------------------------------
	/** The latest status read; `null` until it lands (or when it is unavailable). */
	let status = $state<HistorianStatus | null>(null);
	/** The status read is in flight — the run button stays inert (never a flash of
	 *  an actionable button whose endpoint may not exist yet). Starts true: the
	 *  strip renders only with a session, and that session's read is imminent. */
	let statusPending = $state(true);
	/** The status read failed (no endpoint yet / engine down) — the quiet state. */
	let statusUnavailable = $state(false);
	/** A run-now request issued from THIS surface is in flight (the spinner). */
	let runBusy = $state(false);
	/** The last run's short outcome line (kept quiet — a note, never an alert). */
	let runNote = $state('');
	/** Bumped after a run so the queue body re-reads (a run files new rows). */
	let queueToken = $state(0);

	$effect(() => {
		const id = sessionId;
		status = null;
		statusPending = Boolean(id);
		statusUnavailable = false;
		runBusy = false;
		runNote = '';
		if (!id) return;
		void readStatus(id);
	});

	async function readStatus(id: string): Promise<void> {
		statusPending = true;
		try {
			status = await historianStatus(id);
			statusUnavailable = false;
		} catch {
			// The historian endpoints are a later lane and the engine may be
			// unreachable: stay quiet — no run line, a disabled run button whose
			// reason is on its label/title. Never an error dump in the drawer.
			status = null;
			statusUnavailable = true;
		} finally {
			statusPending = false;
		}
	}

	async function runNow(): Promise<void> {
		const id = sessionId;
		if (!id || busy || statusPending || statusUnavailable) return;
		runBusy = true;
		runNote = '';
		try {
			const result = await runHistorian(id);
			const filed = `${result.filed} filed`;
			runNote = result.dropped > 0 ? `${filed}, ${result.dropped} dropped` : filed;
		} catch (err) {
			runNote = historianErrorDetails(err)[0] ?? 'historian run failed';
		} finally {
			runBusy = false;
			await readStatus(id);
			// The run may have filed rows: re-mount the queue body so it re-reads.
			queueToken += 1;
		}
	}

	/** A run is working (ours, or one the engine reports in flight). */
	const busy = $derived(runBusy || status?.running === true);

	/** The run button is inert while a run works and while no status is readable. */
	const runDisabled = $derived(busy || statusPending || statusUnavailable);

	/** The strip's one readable state (the aria-label text changes with it). */
	const runState = $derived(
		busy ? 'running' : statusUnavailable ? 'unavailable' : statusPending ? 'pending' : 'ready'
	);

	/** The last-run line: "ran 4 turns ago — 3 filed" (§3.6.7). */
	const runLine = $derived.by(() => {
		const current = status;
		if (!current) return '';
		if (busy) return 'historian running…';
		const last = current.last_run;
		if (!last) return 'not run yet';
		const ago = Math.max(0, current.committed_turns - last.turn_seq);
		const ran = `ran ${ago} turn${ago === 1 ? '' : 's'} ago`;
		// `unsure` = the gate's answer was below the confidence floor, so the window
		// was never really judged and nothing was filed. It is NOT a quiet window:
		// say so and offer the door the owner still holds (run now).
		if (last.gate === 'unsure') return `${ran} — historian unsure, run now?`;
		const filed = `${last.filed} filed`;
		return `${ran} — ${filed}`;
	});

	/** The due line: "due in 6 turns" (or "due now" once the cadence has elapsed). */
	const dueLine = $derived.by(() => {
		const current = status;
		if (!current || busy) return '';
		if (current.due_in <= 0) return 'due now';
		return `due in ${current.due_in} turn${current.due_in === 1 ? '' : 's'}`;
	});

	const unavailableReason = 'the engine has no historian endpoint yet';
	const runTitle = $derived(
		statusUnavailable
			? `Run historian now — unavailable: ${unavailableReason}`
			: 'File amendments from the recent turns now, without waiting for the cadence'
	);
	const runLabel = $derived(
		statusUnavailable ? `Run historian now — unavailable, ${unavailableReason}` : 'Run historian now'
	);
</script>

<DraggablePane title="Proposals" {x} y={160} w={470} {onclose}>
	{#snippet children()}
		{#if sessionId}
			<div class="historian" role="group" aria-label="Historian">
				<button
					type="button"
					class="run-now"
					data-state={runState}
					disabled={runDisabled}
					title={runTitle}
					aria-label={runLabel}
					onclick={() => void runNow()}
				>
					{#if busy}<span class="spinner" aria-hidden="true"></span>{/if}
					{busy ? 'Running…' : 'Run historian now'}
				</button>
				<span class="historian-status">
					{#if statusUnavailable}
						<span class="historian-note">historian unavailable</span>
					{:else if runLine}
						<span class="run-line">{runLine}</span>
						{#if dueLine}<span class="due-line">· {dueLine}</span>{/if}
					{/if}
				</span>
				{#if onOpenArchive}
					<button
						type="button"
						class="archive-link"
						title="Archived amendments — older than 10 turns, never mass-selected"
						aria-label="Open the proposal archive"
						onclick={() => onOpenArchive?.()}
					>
						Archive
					</button>
				{/if}
			</div>
			{#if runNote}<p class="run-note" role="status">{runNote}</p>{/if}
			<!-- `queueToken` remounts the body after a run so a freshly-filed batch
			     shows without a manual reopen (the body owns its own read). -->
			{#key queueToken}
				<ProposalReview {sessionId} />
			{/key}
		{:else}
			<!-- No chat open: a falsy sessionId is the body's INBOX mode (the studio's),
			     so the drawer says what it needs instead of showing the wrong queue. -->
			<p class="note">
				Open a chat to review its proposals. The studio's Proposals tab reads the queue
				across every chat.
			</p>
		{/if}
	{/snippet}
</DraggablePane>

<style>
	.note {
		margin: 0;
		color: var(--text-muted);
		font-size: var(--font-micro);
		line-height: 1.4;
	}
	.historian {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
		margin-bottom: 0.5rem;
		padding-bottom: 0.45rem;
		border-bottom: 1px solid var(--line);
	}
	.run-now {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		padding: 0.1rem 0.5rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.run-now:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.historian-status {
		display: inline-flex;
		align-items: baseline;
		gap: 0.25rem;
		flex-wrap: wrap;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.run-line {
		color: var(--text-muted);
	}
	.due-line {
		color: var(--text-dim);
	}
	.historian-note {
		color: var(--text-dim);
	}
	.archive-link {
		margin-left: auto;
		padding: 0.1rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.run-note {
		margin: 0 0 0.45rem;
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	.spinner {
		width: 0.65rem;
		height: 0.65rem;
		border: 2px solid var(--line-strong);
		border-top-color: var(--text);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
</style>
