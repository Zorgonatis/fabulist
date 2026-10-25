<script lang="ts">
	// Proposal archive drawer — the AGED-OUT partition of the review home (W9.23 §3.2/§3.3).
	//
	// The owner's rule (plan §1 item 3): proposals auto-archive 10 chat turns after
	// they are filed, and "ARCHIVED ITEMS ARE NEVER AFFECTED BY MASS SELECTIONS".
	// Archived-ness is a DERIVED partition of the fold (never a log event, never a
	// deletion), so this surface reads the SAME session queue the queue drawer does
	// and renders the same tree fold — the shared `ProposalReview.svelte` in its
	// `archive` mode.
	//
	// Why a SEPARATE drawer instead of a toggle in the queue drawer: the exclusion
	// has to be structural. The archive body renders no checkboxes and no mass
	// buttons at all (`ProposalReview`'s `archiving` flag gates every selection
	// affordance), so there is no selection state an aged row could ever join —
	// the layout, not a runtime check, is what keeps mass selection off the
	// archive. Per-row Accept/Reject/Dismiss stay, because a single-id decide on an
	// archived row is legal (§3.2 disposition rules); only a MULTI-id body refuses
	// an archived id, and this surface never sends one.
	//
	// Geometry is offset from the queue drawer's own pane so the two sit side by
	// side when both are open (the queue drawer spans innerWidth-980 … innerWidth-510).
	import DraggablePane from '#lib/components/DraggablePane.svelte';
	import ProposalReview from '#lib/proposals/ProposalReview.svelte';

	let {
		sessionId = null,
		onclose
	}: {
		/** The open chat whose archive this reads; `null` renders a quiet empty state. */
		sessionId?: string | null;
		onclose?: () => void;
	} = $props();

	// `window` at render crashes SSR (AGENTS.md gotchas): guard the seed even
	// though the page only mounts this pane from a client toggle.
	const x = typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 500) : 380;
</script>

<DraggablePane title="Proposal archive" {x} y={200} w={440} {onclose}>
	{#snippet children()}
		{#if sessionId}
			<ProposalReview {sessionId} mode="archive" />
		{:else}
			<p class="note">
				Open a chat to review its archived proposals. Archived items are older than 10 turns;
				nothing is deleted, and a single item can still be accepted on its own.
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
</style>
