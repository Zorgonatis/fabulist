<script lang="ts">
	// Proposal review — the ONE body both review surfaces render (W9.23 §3.1/§3.3).
	//
	// The owner's core rule (plan §1.7): "I am an AI and want to update this card
	// due to discovered information… the user approves old -> new directly, with
	// the 'new' path obviously not showing any prior content." So this body is a
	// TREE of DELTAS, never a list of regenerated blobs:
	//
	//   kind header → collapsible object → field section → operation rows
	//
	// An operation row renders `old -> new` (modify), `new` alone (add) or the
	// struck-through `old` (delete). A LEGACY whole-value row (pre-op patches, and
	// the shape the server hydrates at read) renders the same `old -> new` with a
	// `legacy` tag. Greeting fields never appear (owner ruling Q1).
	//
	// Selection CASCADES at every layer (§3.3): entry → field section → object →
	// kind, with each node's checkbox tri-state derived from ONE flat id set.
	// "Accept selected (N)" renders at every layer and sends exactly that layer's
	// selected ids — the mass op the owner named.
	//
	// ARCHIVE is a derived partition (§3.2), rendered by the same body in a second
	// mode: no checkboxes, no mass buttons, single-row actions only. The teeth are
	// server-side (a multi-id decide refuses archived ids), so the UI's job is to
	// never OFFER a mass action over an aged item — "archived items are never
	// affected by mass selections".
	//
	// Why the per-item in-flight set instead of a pane-wide flag: a decide request
	// is per item or per layer, and the OWNER keeps working the rest of the queue
	// meanwhile. A row that is in flight is disabled (`aria-busy`) and cannot be
	// submitted twice; every other row stays live.
	//
	// Why undo drops the row from the response instead of re-reading: the store's
	// `committed` read already excludes an undone decision (the compensating
	// `proposal_undo` row wins), so the response IS the new truth — one fewer round
	// trip on the surface's most-repeated action. A failed undo keeps the row.
	//
	// The honesty states are the Cast pane's: loading is "reading…", empty says
	// nothing is proposed, and an unreachable engine renders the message with NO
	// action controls plus a Retry — a queue the body could not read must never
	// look like a queue with items you cannot decide.
	import {
		buildReviewTree,
		decideProposals,
		decisionLabel,
		formatValue,
		getProposals,
		isPromotion,
		opLabel,
		proposalLabel,
		proposalTarget,
		sectionLabel,
		signalLabel,
		treeIds,
		undoDecision,
		type Decision,
		type FieldSection,
		type InboxState,
		type KindSection,
		type ObjectGroup,
		type Proposal,
		type ProposalDecision,
		type ReviewRow,
		type SessionQueue
	} from '#lib/api/proposals';

	/** Which partition of the fold this body renders. */
	type Mode = 'queue' | 'archive';

	let {
		sessionId = null,
		inbox = null,
		mode = 'queue',
		onRefresh
	}: {
		/** Drawer mode: the open chat — the body reads its queue itself. */
		sessionId?: string | null;
		/** Studio mode: the cross-session inbox, read (and owned) by `ProposalsHome`. */
		inbox?: InboxState | null;
		/** `archive` renders the aged-out partition with no selection affordances. */
		mode?: Mode;
		/** Studio mode: re-read the inbox after a decision changed the queue. */
		onRefresh?: () => void;
	} = $props();

	/** One review block: a session's tree (the studio's collapsible unit). */
	interface Block {
		sessionId: string;
		title: string;
		tree: KindSection[];
	}

	// --- session-scoped state (drawer mode) ---------------------------------
	let queue = $state<SessionQueue>({ pending: [], archived: [], committed: [] });
	let sessionLoading = $state(false);
	let sessionError = $state('');

	// --- shared state (both modes) ------------------------------------------
	/** The call-level failure: the engine refused or could not be reached. */
	let actionError = $state('');
	/** Per-id refusals (a stale op or an archived id in a mass body) — on the row. */
	let itemErrors = $state<Record<string, string>>({});
	/** Ids decided on THIS surface, hidden until the next read (optimistic). */
	let decidedIds = $state<string[]>([]);
	let selected = $state<string[]>([]);
	/** Proposal ids with a decide request in flight (disabled rows). */
	let busyIds = $state<string[]>([]);
	/** Decision ids with an undo request in flight. */
	let busyDecisions = $state<string[]>([]);

	const scoped = $derived(Boolean(sessionId));
	const archiving = $derived(mode === 'archive');
	const loading = $derived(scoped ? sessionLoading : (inbox?.loading ?? false));
	const error = $derived(scoped ? sessionError : (inbox?.error ?? ''));
	const committedRows = $derived<Decision[]>(scoped && !archiving ? queue.committed : []);

	function liveRows(items: Proposal[]): Proposal[] {
		return items.filter((item) => !decidedIds.includes(item.id));
	}

	const blocks = $derived<Block[]>(
		(scoped
			? [
					{
						sessionId: sessionId ?? '',
						title: '',
						tree: buildReviewTree(
							liveRows(archiving ? queue.archived : queue.pending)
						)
					}
				]
			: (inbox?.sessions ?? []).map((session) => ({
					sessionId: session.sessionId,
					title: session.title,
					tree: buildReviewTree(liveRows(archiving ? session.archived : session.pending))
				}))
		).filter((block) => block.tree.length > 0)
	);
	const pendingCount = $derived(
		blocks.reduce(
			(total, block) => total + block.tree.reduce((sum, kind) => sum + kind.items.length, 0),
			0
		)
	);
	/** The live queue's size, so the queue's empty state can point at the archive. */
	const archivedCount = $derived(
		scoped
			? queue.archived.length
			: (inbox?.sessions ?? []).reduce((total, session) => total + session.archived.length, 0)
	);

	$effect(() => {
		const id = sessionId;
		queue = { pending: [], archived: [], committed: [] };
		sessionLoading = false;
		sessionError = '';
		actionError = '';
		itemErrors = {};
		decidedIds = [];
		selected = [];
		busyIds = [];
		busyDecisions = [];
		if (!id) return;
		void loadSession(id);
	});

	async function loadSession(id: string): Promise<void> {
		sessionLoading = true;
		sessionError = '';
		try {
			const result = await getProposals(id);
			if ('error' in result) {
				queue = { pending: [], archived: [], committed: [] };
				sessionError = result.error;
				return;
			}
			queue = result;
			decidedIds = [];
			selected = [];
		} finally {
			sessionLoading = false;
		}
	}

	/** Re-read whichever queue this mode owns (scoped read / parent's inbox). */
	async function refresh(): Promise<void> {
		if (scoped && sessionId) {
			await loadSession(sessionId);
			return;
		}
		onRefresh?.();
	}

	/**
	 * Decide `ids` in ONE call, in the session that owns them.
	 *
	 * A layer's mass accept and a row's single action are the same door: the log
	 * writes one decision row per id, so both stay granularly undoable. Archive
	 * mode never reaches the mass path — it passes exactly one id.
	 */
	async function decide(
		owner: string,
		ids: string[],
		decision: ProposalDecision
	): Promise<void> {
		const inFlight = ids.filter((id) => !busyIds.includes(id));
		if (!inFlight.length) return;
		busyIds = [...busyIds, ...inFlight];
		actionError = '';
		itemErrors = Object.fromEntries(
			Object.entries(itemErrors).filter(([id]) => !inFlight.includes(id))
		);
		try {
			const outcome = await decideProposals(owner, inFlight, decision);
			if (outcome.error) actionError = outcome.error;
			for (const refused of outcome.rejected) {
				itemErrors = { ...itemErrors, [refused.id]: refused.error };
			}
			if (!outcome.error) {
				const refused = new Set(outcome.rejected.map((row) => row.id));
				const gone = inFlight.filter((id) => !refused.has(id));
				decidedIds = [...decidedIds, ...gone];
				selected = selected.filter((id) => !gone.includes(id));
				if (scoped && !archiving) {
					// The response carries the decision rows themselves, so the
					// committed list is right without a second read.
					const accepted = outcome.decisions.filter((row) => row.decision === 'accept');
					queue = {
						pending: queue.pending,
						archived: queue.archived,
						committed: [...accepted, ...queue.committed]
					};
				}
			}
			if (!scoped || outcome.error) await refresh();
		} finally {
			busyIds = busyIds.filter((id) => !inFlight.includes(id));
		}
	}

	/** Undo one accepted decision — a compensating append, never a rewrite. */
	async function undo(decisionId: string): Promise<void> {
		const owner = sessionId;
		if (!owner || busyDecisions.includes(decisionId)) return;
		busyDecisions = [...busyDecisions, decisionId];
		actionError = '';
		try {
			const result = await undoDecision(owner, decisionId);
			if ('error' in result) {
				actionError = result.error;
				return;
			}
			queue = {
				pending: queue.pending,
				archived: queue.archived,
				committed: queue.committed.filter((row) => row.decision_id !== decisionId)
			};
		} finally {
			busyDecisions = busyDecisions.filter((id) => id !== decisionId);
		}
	}

	// --- cascade selection (plan §3.3) --------------------------------------
	//
	// ONE flat id set is the source of truth; every layer's checkbox state is
	// DERIVED from it, so a layer can never disagree with its children.

	/** The selected ids of one layer, in tree order. */
	function selectedIn(ids: string[]): string[] {
		return ids.filter((id) => selected.includes(id));
	}

	/** Tri-state for one layer: all, none, or some of its ids selected. */
	function checkState(ids: string[]): 'all' | 'none' | 'some' {
		if (ids.length === 0) return 'none';
		const count = selectedIn(ids).length;
		if (count === 0) return 'none';
		return count === ids.length ? 'all' : 'some';
	}

	/** Select or clear a whole layer (the cascade: every descendant id moves). */
	function toggleLayer(ids: string[], on: boolean): void {
		selected = on
			? [...selected, ...ids.filter((id) => !selected.includes(id))]
			: selected.filter((id) => !ids.includes(id));
	}

	function isBusy(id: string): boolean {
		return busyIds.includes(id);
	}

	/** The mass-accept mask for one layer: selectable ids, busy rows excluded. */
	function actionable(ids: string[]): string[] {
		return selectedIn(ids).filter((id) => !busyIds.includes(id));
	}

	function rowOpText(row: ReviewRow): string {
		return row.op ? opLabel(row.op) : row.proposal.action;
	}

	/**
	 * The object is a promotion (W9.30 §6): the persona card the cast ladder mints
	 * from a session-cast member. It renders as an ordinary actor add; the ONE
	 * addition is the origin line below, so the owner knows an accept mints an
	 * asset that outlives this chat (req 5/9 — still inside the existing area).
	 */
	function promotesObject(object: ObjectGroup): boolean {
		return object.items.some((row) => isPromotion(row.proposal));
	}

	/** The op verbs as the owner reads them (`add` / `change` / `remove`). */
	function opVerb(op: string): string {
		if (op === 'add') return 'add';
		if (op === 'delete') return 'remove';
		return 'change';
	}
</script>

{#snippet layerCheckbox(ids: string[], label: string)}
	{#if !archiving}
		<input
			type="checkbox"
			class="pick"
			checked={checkState(ids) === 'all'}
			indeterminate={checkState(ids) === 'some'}
			aria-label="Select {label}"
			onchange={(event) => toggleLayer(ids, event.currentTarget.checked)}
		/>
	{/if}
{/snippet}

{#snippet massAccept(owner: string, ids: string[], label: string)}
	{#if !archiving}
		<button
			type="button"
			class="primary"
			disabled={actionable(ids).length === 0}
			aria-label="Accept selected {label}"
			onclick={() => void decide(owner, actionable(ids), 'accept')}
		>
			Accept selected ({actionable(ids).length})
		</button>
	{/if}
{/snippet}

{#snippet committedBody(rows: Decision[])}
	<section class="group group-committed" aria-label="Committed proposals">
		<header class="group-head">
			<span class="group-label">Committed</span>
			<span class="count">{rows.length}</span>
		</header>
		<p class="hint">accepted into your assets · undo puts back what the accept replaced</p>
		<ul class="rows">
			{#each rows as row (row.decision_id)}
				<li class="row" aria-busy={busyDecisions.includes(row.decision_id)}>
					<div class="row-body">
						<div class="row-line">
							<span class="row-target">{decisionLabel(row)}</span>
							<span class="chip chip-decision">{row.decision}</span>
							{#if row.kind}<span class="chip">{row.kind}</span>{/if}
						</div>
						<div class="row-actions">
							<button
								type="button"
								class="ghost"
								disabled={busyDecisions.includes(row.decision_id)}
								aria-label="Undo: {decisionLabel(row)}"
								onclick={() => void undo(row.decision_id)}
							>
								{busyDecisions.includes(row.decision_id) ? 'undoing…' : 'Undo'}
							</button>
						</div>
					</div>
				</li>
			{/each}
		</ul>
	</section>
{/snippet}

{#snippet opValue(op: { op: string; old: unknown; hasOld: boolean; new: unknown; hasNew: boolean; path: string })}
	{#if op.op === 'add'}
		<code class="val val-new">{formatValue(op.new)}</code>
	{:else if op.op === 'delete'}
		<code class="val val-old val-struck">{formatValue(op.old)}</code>
	{:else}
		{#if op.hasOld}
			<code class="val val-old">{formatValue(op.old)}</code>
		{:else}
			<span class="val val-old val-missing" title="An older proposal that did not record the previous value">
				prior value not shown
			</span>
		{/if}
		<span class="arrow" aria-hidden="true">→</span>
		<code class="val val-new">{formatValue(op.new)}</code>
	{/if}
{/snippet}

{#snippet reviewRow(row: ReviewRow, owner: string)}
	<li class="row" aria-busy={isBusy(row.id)}>
		{#if !archiving}
			<input
				type="checkbox"
				class="pick"
				checked={selected.includes(row.id)}
				disabled={isBusy(row.id)}
				aria-label="Select: {proposalLabel(row.proposal)}"
				onchange={(event) => toggleLayer([row.id], event.currentTarget.checked)}
			/>
		{/if}
		<div class="row-body">
			<div class="row-line">
				<span class="row-op" title={row.op ? sectionLabel(row.op.path) : ''}
					>{rowOpText(row)}</span
				>
				<span class="row-target">{proposalTarget(row.proposal)}</span>
				{#if row.op?.legacy}
					<span class="chip" title="Filed before amendments carried operations — applies newest-wins">
						legacy
					</span>
				{/if}
				{#if row.stale}
					<span
						class="chip chip-stale"
						title="'{row.op?.path}' changed since this was filed — the accept will refuse it"
					>
						card changed
					</span>
				{/if}
				{#if row.proposal.supersedes.length > 0}
					<span
						class="chip"
						title="Older proposals for the same asset are folded into this one (the newest wins)"
					>
						{row.proposal.supersedes.length} collapsed
					</span>
				{/if}
				<span class="chip chip-origin">{row.proposal.origin || 'unknown'}</span>
				{#if row.proposal.signal !== null}
					<span class="chip" title="The decision model's probability that this should be proposed">
						{signalLabel(row.proposal.signal)}
					</span>
				{/if}
			</div>
			<div class="diff" class:diff-stale={row.stale}>
				{#if row.op}
					{@render opValue(row.op)}
				{:else if row.proposal.action === 'add'}
					<p class="new-def">
						{#each Object.entries(row.proposal.patch) as [key, value] (key)}
							<span class="new-field"
								><span class="new-key">{key}</span> = {formatValue(value)}</span
							>
						{/each}
					</p>
				{:else if row.proposal.action === 'remove'}
					<span class="removal-note">removes the whole asset</span>
				{/if}
			</div>
			{#if row.proposal.evidence}<p class="evidence">{row.proposal.evidence}</p>{/if}
			{#if itemErrors[row.id]}<p class="row-error" role="alert">{itemErrors[row.id]}</p>{/if}
			<div class="row-actions">
				<button
					type="button"
					class="primary"
					disabled={isBusy(row.id)}
					aria-label="Accept: {proposalLabel(row.proposal)}"
					onclick={() => void decide(owner, [row.id], 'accept')}
				>
					{row.stale ? 'Accept (may refuse)' : 'Accept'}
				</button>
				<button
					type="button"
					class="ghost"
					disabled={isBusy(row.id)}
					aria-label="Reject: {proposalLabel(row.proposal)}"
					onclick={() => void decide(owner, [row.id], 'reject')}
				>
					Reject
				</button>
				<button
					type="button"
					class="ghost"
					disabled={isBusy(row.id)}
					aria-label="Dismiss: {proposalLabel(row.proposal)}"
					onclick={() => void decide(owner, [row.id], 'dismiss')}
				>
					Dismiss
				</button>
				{#if isBusy(row.id)}<span class="working">working…</span>{/if}
			</div>
		</div>
	</li>
{/snippet}

{#snippet sectionBody(owner: string, section: FieldSection)}
	<div class="section" aria-label={section.label}>
		<header class="section-head">
			{@render layerCheckbox(section.ids, section.label)}
			<span class="section-label">{section.label}</span>
			<span class="count">{section.items.length}</span>
			{@render massAccept(owner, section.ids, section.label)}
		</header>
		{#if section.hint}<p class="hint">{section.hint}</p>{/if}
		<ul class="rows">
			{#each section.items as row (`${row.id}:${row.op?.path ?? 'whole'}`)}
				{@render reviewRow(row, owner)}
			{/each}
		</ul>
	</div>
{/snippet}

{#snippet objectBody(owner: string, object: ObjectGroup)}
	<details class="object" open>
		<summary aria-label="Object: {object.label}">
			{@render layerCheckbox(object.ids, object.label)}
			<span class="object-label">{object.label}</span>
			{#if object.isNew}<span class="chip chip-new">new</span>{/if}
			{#if object.createHint}<span class="object-hint">{object.createHint}</span>{/if}
			{#if object.change}<span class="object-change">{object.change}</span>{/if}
			<span class="object-head-actions">
				{@render massAccept(owner, object.ids, object.label)}
			</span>
		</summary>
		{#if promotesObject(object)}
			<p class="hint promotion-origin">
				promoted from this chat’s cast — accepting mints this actor persona card
			</p>
		{/if}
		{#each object.sections as section (section.field)}
			{@render sectionBody(owner, section)}
		{/each}
	</details>
{/snippet}

{#snippet castRows(owner: string, kind: KindSection)}
	<ul class="rows">
		{#each kind.items as row (row.id)}
			{@render reviewRow(row, owner)}
		{/each}
	</ul>
{/snippet}

{#snippet kindBody(owner: string, kind: KindSection)}
	<section class="group" aria-label="{kind.label} proposals">
		<header class="group-head">
			{@render layerCheckbox(kind.ids, kind.label)}
			<span class="group-label">{kind.label}</span>
			<span class="count">{kind.items.length}</span>
			{@render massAccept(owner, kind.ids, kind.label)}
		</header>
		<p class="hint">{kind.hint}</p>
		{#if kind.objects.length === 0}
			{@render castRows(owner, kind)}
		{:else}
			{#each kind.objects as object (object.key)}
				{@render objectBody(owner, object)}
			{/each}
		{/if}
	</section>
{/snippet}

{#snippet blockBody(block: Block)}
	{@const ids = treeIds(block.tree)}
	<!-- The outermost cascade layer (§3.3 'global select-all'): the session's
	     whole live tree. Rendered OUTSIDE the session <details> so a click on it
	     never also toggles the disclosure. -->
	{#if !archiving && ids.length > 1}
		<div class="block-mask">
			{@render layerCheckbox(ids, `every proposal in ${block.title || 'this queue'}`)}
			<span class="hint">select all {ids.length}</span>
			{@render massAccept(block.sessionId, ids, `every proposal in ${block.title || 'this queue'}`)}
		</div>
	{/if}
	{#each block.tree as kind (kind.kind)}
		{@render kindBody(block.sessionId, kind)}
	{/each}
{/snippet}

<div class="review">
	{#if actionError}
		<p class="alert" role="alert">{actionError}</p>
	{/if}

	{#if loading}
		<p class="note">reading proposals…</p>
	{:else if error}
		<div class="blocked" role="group" aria-label="Proposals unavailable">
			<p class="alert" role="alert">Proposals unavailable — {error}</p>
			<button
				type="button"
				class="ghost"
				aria-label="Retry reading proposals"
				onclick={() => void refresh()}
			>
				Retry
			</button>
		</div>
	{:else}
		{#if archiving}
			<p class="hint archive-hint">
				Archive — items older than 10 turns. Nothing is deleted; each can still be accepted
				on its own. Mass selection never reaches here.
			</p>
		{:else if committedRows.length > 0}
			{@render committedBody(committedRows)}
		{/if}

		{#if pendingCount === 0}
			{#if archiving}
				<p class="note">
					Nothing archived — items reach here 10 turns after they are filed, and stay
					revisitable until you decide them.
				</p>
			{:else if scoped}
				<p class="note">
					Nothing proposed — no lane filed an amendment. The historian files here after a
					turn settles when assets drifted; the committed list above keeps what you
					accepted.{#if archivedCount > 0}
						{' '}{archivedCount} older item{archivedCount === 1 ? '' : 's'} sit in the
						archive.{/if}
				</p>
			{:else}
				<p class="note">
					Nothing proposed — no lane filed an amendment. Committed items and their undo live
					in a chat’s Proposals drawer.
				</p>
			{/if}
		{/if}

		{#each blocks as block (block.sessionId)}
			{#if scoped}
				{@render blockBody(block)}
			{:else}
				<details class="session" open>
					<summary aria-label="Session: {block.title}">
						<span class="session-title">{block.title}</span>
						<span class="count"
							>{block.tree.reduce((sum, kind) => sum + kind.items.length, 0)}</span
						>
					</summary>
					{@render blockBody(block)}
				</details>
			{/if}
		{/each}
	{/if}
</div>

<style>
	.review {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		font-size: var(--font-small);
	}
	.note {
		margin: 0;
		color: var(--text-muted);
		font-size: var(--font-micro);
		line-height: 1.4;
	}
	.archive-hint {
		color: var(--text-muted);
	}
	.alert {
		margin: 0;
		padding: 0.35rem 0.5rem;
		border: 1px solid var(--danger, #a33);
		border-radius: 6px;
		color: var(--danger-text, #f2b8b8);
		background: var(--danger-bg, rgba(170, 51, 51, 0.12));
		font-size: var(--font-micro);
	}
	.blocked {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.4rem;
	}
	.group {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		padding: 0.4rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	/* Committed sits ABOVE the queue and reads as settled, not as work to do. */
	.group-committed {
		border-style: solid;
		border-color: var(--line-strong);
		background: var(--bg-panel);
	}
	.group-head,
	.section-head {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
	}
	.group-head button.primary,
	.section-head button.primary {
		margin-left: auto;
	}
	.group-label {
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	.count {
		font-size: var(--font-micro);
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}
	.hint {
		margin: 0;
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	.object {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.3rem 0.4rem;
		background: var(--bg-panel);
	}
	.object + .object {
		margin-top: 0.35rem;
	}
	.object > summary {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		cursor: pointer;
		flex-wrap: wrap;
	}
	.object-label {
		font-weight: 600;
		color: var(--text);
	}
	.object-change {
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	/* The create hint (W9.30 §6) takes the place of a stale badge: a row that
	   mints its target can never be stale. */
	.object-hint {
		color: var(--text-muted);
		font-size: var(--font-micro);
	}
	.promotion-origin {
		margin: 0.15rem 0 0 0.9rem;
		font-style: italic;
	}
	.object-head-actions {
		margin-left: auto;
	}
	.block-mask {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		flex-wrap: wrap;
		padding: 0.2rem 0.1rem;
	}
	.block-mask button.primary {
		margin-left: auto;
	}
	.section {
		margin: 0.3rem 0 0 0.9rem;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.section-label {
		font-size: var(--font-micro);
		font-weight: 600;
		color: var(--text-muted);
	}
	.rows {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.row {
		display: flex;
		align-items: flex-start;
		gap: 0.4rem;
		padding: 0.3rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg);
	}
	.row[aria-busy='true'] {
		opacity: 0.7;
	}
	.pick {
		margin: 0.15rem 0 0;
		flex: 0 0 auto;
	}
	.row-body {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	.row-line {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		flex-wrap: wrap;
	}
	.row-op {
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--text-dim);
	}
	.row-target {
		font-weight: 600;
		color: var(--text);
	}
	.diff {
		display: flex;
		align-items: flex-start;
		gap: 0.3rem;
		flex-wrap: wrap;
		font-size: var(--font-micro);
	}
	.val {
		white-space: pre-wrap;
		word-break: break-word;
		padding: 0.1rem 0.3rem;
		border-radius: 5px;
		border: 1px solid var(--line);
		max-height: 9rem;
		overflow: auto;
	}
	.val-old {
		color: var(--text-muted);
		background: var(--bg-panel);
	}
	.val-new {
		color: var(--text);
		background: var(--bg-raised);
	}
	.val-struck {
		text-decoration: line-through;
	}
	.val-missing {
		font-style: italic;
		color: var(--text-dim);
	}
	.diff-stale .val-old {
		border-color: var(--danger, #a33);
	}
	.arrow {
		color: var(--text-dim);
	}
	.new-def {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.12rem;
	}
	.new-field {
		color: var(--text);
		white-space: pre-wrap;
		word-break: break-word;
	}
	.new-key {
		color: var(--text-dim);
		margin-right: 0.25rem;
	}
	.removal-note {
		color: var(--danger-text, #f2b8b8);
	}
	.chip {
		padding: 0 0.3rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		font-size: var(--font-micro);
		color: var(--text-dim);
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
	.chip-origin {
		color: var(--text-muted);
	}
	.chip-decision {
		color: var(--text-muted);
	}
	.chip-stale {
		color: var(--danger-text, #f2b8b8);
		border-color: var(--danger, #a33);
	}
	.chip-new {
		color: var(--text);
		border-color: var(--line-strong);
	}
	.evidence {
		margin: 0;
		color: var(--text-dim);
		font-size: var(--font-micro);
		font-style: italic;
	}
	.row-error {
		margin: 0;
		color: var(--danger-text, #f2b8b8);
		font-size: var(--font-micro);
	}
	.row-actions {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		flex-wrap: wrap;
	}
	.row-actions button,
	.blocked button,
	.group-head button,
	.section-head button {
		padding: 0.1rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.row-actions button.primary,
	.group-head button.primary,
	.section-head button.primary {
		color: var(--text);
		border-color: var(--accent, var(--line-strong));
	}
	.row-actions button:disabled,
	.blocked button:disabled,
	.group-head button:disabled,
	.section-head button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.working {
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	.session {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.3rem 0.45rem;
	}
	.session + .session {
		margin-top: 0.4rem;
	}
	.session > summary {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
		cursor: pointer;
	}
	.session-title {
		font-weight: 600;
		color: var(--text);
	}
</style>
