<script lang="ts">
	// Inspector pane v2 (W3.6 + I2/I3): per-turn "what the model saw and
	// produced" over GET /api/sessions/{sid}/inspect/turns, plus log-backed
	// usage from GET /api/usage?session_id=. Read-only over the event log (D9),
	// plus two writes: the session-scoped agent-debug capture toggle (W1b) and
	// the per-session component-set picker (#16).
	import { untrack } from 'svelte';
	import {
		API_BASE,
		listComponentSets,
		selectComponentSet,
		type ComponentSetOption
	} from '#lib/api';
	import { getContextLedger, type ContextLedgerRow } from '#lib/api/hud';
	import { inspectorRefresh } from '#lib/inspectorRefresh';
	// Issue #34: the ONE notice surface. The chat page loads it (it already reads
	// the inspector endpoint every turn end); the pane renders the rows and marks
	// them seen when its notices tab is showing. Pending proposals ride the SAME
	// badge, read through the review drawer's own client (owner ruling).
	import {
		acknowledgeNotices,
		degradationLabel,
		loadPendingProposals,
		noticeBadge
	} from '#lib/notices';

	let {
		sessionId = '',
		onclose,
		onopenproposals
	}: {
		sessionId?: string | null;
		onclose: () => void;
		/** Opens the existing proposals review drawer (the notices tab's action). */
		onopenproposals?: () => void;
	} = $props();

	type TurnCall = {
		agent: string;
		model: string;
		seq: number;
		prompt_tokens: number;
		completion_tokens: number;
		total_tokens: number;
		latency_s: number;
	};
	type TokenTotals = {
		calls?: number;
		prompt_tokens: number;
		completion_tokens: number;
		total_tokens: number;
	};
	type RerankSetting = boolean | null;
	type RagPanel = {
		query: string;
		queries: string[];
		recall_text: string | null;
		k: number;
		rerank_enabled: RerankSetting;
		query_mode: string;
		memory_enabled: boolean;
	};
	type TurnRow = {
		user_seq: number | null;
		user_ts: string | null;
		user_text: string | null;
		narrative_seq: number | null;
		narrative_ts: string | null;
		narrative_text: string | null;
		narrative_dsl: string | null;
		latency_s: number | null;
		model: string | null;
		model_raw: string | null;
		sampler: Record<string, unknown> | null;
		prompt_version: number | null;
		facts_hash: string | null;
		facts_text: string | null;
		regions: Record<string, number> | null;
		recall_text: string | null;
		tokens: TokenTotals;
		calls: TurnCall[];
		delta_count: number;
		validation_flags: number;
		validation_details: Record<string, unknown>[];
		revision_count: number;
		revision_refs: Record<string, unknown>[];
		window: { low: number; high: number } | null;
		rag: RagPanel;
	};
	type AgentAgg = {
		calls: number;
		prompt_tokens: number;
		completion_tokens: number;
		total_tokens: number;
		avg_latency_s?: number;
	};
	type ModelAgg = { calls: number; total_tokens: number };
	// Two producers, two shapes, one view:
	// * GET /inspect/turns -> usage: {models:{m:turnCount}, agents:{a:eventCount},
	//   calls:{calls,prompt..}, by_agent, by_model}
	// * GET /api/usage -> aggregate: {calls:int, prompt_tokens.., agents:{a:agg},
	//   models:{m:agg}, by_agent.., sessions}
	type Usage = {
		source?: string;
		// aggregate top-level totals (int) vs inspect ``calls`` object
		calls?: number | TokenTotals;
		prompt_tokens?: number;
		completion_tokens?: number;
		total_tokens?: number;
		models?: Record<string, number> | Record<string, ModelAgg>;
		agents?: Record<string, number> | Record<string, AgentAgg>;
		by_agent?: Record<string, AgentAgg>;
		by_model?: Record<string, ModelAgg>;
	};
	type UsageView = {
		source: string;
		totals: TokenTotals;
		byAgent: Record<string, AgentAgg>;
		byModel: Record<string, ModelAgg>;
		turnModels: Record<string, number>;
		eventAgents: Record<string, number>;
	};
	type AgentCall = {
		agent: string;
		seq: number;
		response_text: string;
		reasoning_text: string;
		finish: string | null;
		latency_s: number;
		facts_hash: string;
	};

	let turns = $state<TurnRow[]>([]);
	let usage = $state<Usage | null>(null);
	let logUsage = $state<Usage | null>(null);
	let agentCalls = $state<AgentCall[]>([]);
	// W9.26 §2.7: the per-task context ledger (read-only; the pane's own read).
	let ledger = $state<ContextLedgerRow[]>([]);
	let ledgerError = $state('');
	let loadError = $state('');
	let expanded = $state<string | null>(null);
	// W5-S2: capture chokepoint records every agent (narrator/ooc/summarizer/
	// captioner/assistant); this narrows the capture lists to one agent.
	let captureAgent = $state('');
	// W1b: the per-session capture toggle moved here from the Narrator settings
	// drawer — it sits next to the captures it produces.
	let agentDebug = $state(false);
	let agentDebugBusy = $state(false);
	let agentDebugError = $state('');

	// Issue #16: the NARRATOR COMPONENT SET this chat is taught. Selection is a
	// per-session log fact (`set_swapped`); the POST takes effect on the NEXT
	// turn, so the control says so instead of implying a live switch. A set the
	// client cannot render is disabled (ADR-025: set ⊆ union).
	let componentSets = $state<ComponentSetOption[]>([]);
	let activeSetId = $state('');
	let setBusy = $state(false);
	let setError = $state('');

	// Issue #34: two views over one pane — the per-turn trace (the pane's old
	// body) and the notices the engine used to swallow. The tab opens ON the
	// notices when something is waiting, which is the ruling's "prompting" half
	// applied at the destination as well as on the header button.
	let tab = $state<'turns' | 'notices'>('turns');
	let prompted = $state(false);

	const badge = $derived($noticeBadge);

	$effect(() => {
		// Track the badge so a notice landing while the tab is open is acknowledged
		// too (the store update is idempotent, so this cannot loop).
		const waiting = badge.degradations.length;
		if (!prompted && waiting > 0) {
			prompted = true;
			tab = 'notices';
		}
	});

	$effect(() => {
		// Same tracking rule for the acknowledgement below.
		void badge.count;
		if (tab !== 'notices') return;
		const id = sessionId;
		if (id) acknowledgeNotices(id);
	});

	// I4: clear on session switch / New chat. The previous version returned
	// early on a null id, leaving the previous chat's turns on screen.
	$effect(() => {
		const id = sessionId;
		turns = [];
		usage = null;
		logUsage = null;
		agentCalls = [];
		ledger = [];
		ledgerError = '';
		loadError = '';
		expanded = null;
		captureAgent = '';
		if (!id) return;
		void load(id);
	});

	// W1b live refresh: producers outside the pane (turn end in the page, a
	// UI-invoked model call) bump the shared counter; re-read when it moves.
	// `$inspectorRefresh` is read directly so this effect tracks the store
	// (`get()` would not register). `sessionId` is read untracked: the reset
	// effect above already owns session-switch loads, so tracking it here would
	// only re-read the same session twice.
	$effect(() => {
		if ($inspectorRefresh === 0) return;
		const id = untrack(() => sessionId);
		if (id) void load(id);
	});

	// W1b: session-scoped capture toggle (same endpoint the settings drawer used).
	// A failed read just leaves the switch off — captures are optional surface.
	$effect(() => {
		const id = sessionId;
		agentDebugError = '';
		if (!id) {
			agentDebug = false;
			return;
		}
		void loadAgentDebug(id);
	});

	// #16: the picker's catalog + this session's current set. A failed read
	// leaves the control hidden — the vocabulary surface is optional.
	$effect(() => {
		const id = sessionId;
		setError = '';
		if (!id) {
			componentSets = [];
			activeSetId = '';
			return;
		}
		void loadComponentSets(id);
	});

	async function loadComponentSets(id: string): Promise<void> {
		try {
			const catalog = await listComponentSets(id);
			if (id !== sessionId) return;
			componentSets = catalog.sets;
			activeSetId = catalog.active_set_id;
		} catch {
			/* optional surface — a failed catalog hides the picker */
		}
	}

	async function changeComponentSet(nextId: string): Promise<void> {
		const id = sessionId;
		if (!id || setBusy || nextId === activeSetId) return;
		setBusy = true;
		setError = '';
		try {
			const result = await selectComponentSet(id, nextId);
			activeSetId = result.set_id;
		} catch (err) {
			setError = err instanceof Error ? err.message : 'Could not select that component set.';
		} finally {
			setBusy = false;
		}
	}

	async function load(id: string) {
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${id}/inspect/turns`);
			if (!res.ok) throw new Error(String(res.status));
			const body = (await res.json()) as { turns: TurnRow[]; usage: Usage };
			turns = body.turns ?? [];
			usage = body.usage ?? null;
		} catch {
			loadError = 'inspector unavailable';
		}
		// Log-backed usage (survives restart) — the old /api/usage read process RAM.
		try {
			const res = await fetch(
				`${API_BASE}/api/usage?session_id=${encodeURIComponent(id)}`
			);
			if (res.ok) logUsage = (await res.json()) as Usage;
		} catch {
			/* fall back to the inspect-turn usage block (same log rows) */
		}
		// D17: agent captures (only exist when agent_debug was on for that turn)
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${id}/agent-calls`);
			if (res.ok) {
				const body = (await res.json()) as { calls: AgentCall[] };
				agentCalls = body.calls ?? [];
			}
		} catch {
			/* captures are optional surface — absence is normal (off by default) */
		}
		// W9.26 §2.7: the per-task context ledger rides the same refresh (session
		// switch, the ↻ button, a turn-end bump). Its failure is INLINE: a ledger
		// the engine cannot answer never takes the turns table down with it.
		await loadLedger(id);
		// Issue #34: the ↻ door re-reads the pending proposal queue too, so the
		// notices tab never contradicts the review drawer after a manual refresh.
		void loadPendingProposals(id);
	}

	async function loadLedger(id: string): Promise<void> {
		const result = await getContextLedger(id);
		// A stale response must not overwrite the next session's rows.
		if (id !== sessionId) return;
		if ('error' in result) {
			ledger = [];
			ledgerError = result.error;
			return;
		}
		ledger = result.tasks;
		ledgerError = '';
	}

	async function loadAgentDebug(id: string): Promise<void> {
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(id)}/agent-debug`);
			if (!res.ok) return;
			const body = (await res.json()) as { enabled?: unknown };
			agentDebug = body.enabled === true;
		} catch {
			/* optional surface — a failed read just leaves the switch off */
		}
	}

	async function toggleAgentDebug(): Promise<void> {
		const id = sessionId;
		if (!id || agentDebugBusy) return;
		const next = !agentDebug;
		agentDebugBusy = true;
		agentDebugError = '';
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(id)}/agent-debug`, {
				method: 'PATCH',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ enabled: next })
			});
			if (!res.ok) throw new Error(String(res.status));
			agentDebug = next;
		} catch {
			agentDebugError = 'Could not save the debug toggle.';
		} finally {
			agentDebugBusy = false;
		}
	}

	function isAggMap(value: unknown): boolean {
		return (
			!!value &&
			typeof value === 'object' &&
			Object.values(value).every((v) => v !== null && typeof v === 'object')
		);
	}

	function isCountMap(value: unknown): boolean {
		return (
			!!value &&
			typeof value === 'object' &&
			Object.values(value).every((v) => typeof v === 'number')
		);
	}

	const usageView = $derived.by<UsageView | null>(() => {
		const raw = (logUsage ?? usage) as Usage | null;
		if (!raw) return null;
		const callsRaw = raw.calls;
		const totals: TokenTotals =
			typeof callsRaw === 'number'
				? {
						calls: callsRaw,
						prompt_tokens: raw.prompt_tokens ?? 0,
						completion_tokens: raw.completion_tokens ?? 0,
						total_tokens: raw.total_tokens ?? 0
					}
				: ((callsRaw as TokenTotals) ?? {
						calls: 0,
						prompt_tokens: 0,
						completion_tokens: 0,
						total_tokens: 0
					});
		return {
			source: raw.source ?? 'log',
			totals,
			byAgent:
				raw.by_agent ?? (isAggMap(raw.agents) ? (raw.agents as Record<string, AgentAgg>) : {}),
			byModel:
				raw.by_model ?? (isAggMap(raw.models) ? (raw.models as Record<string, ModelAgg>) : {}),
			turnModels: isCountMap(raw.models) ? (raw.models as Record<string, number>) : {},
			eventAgents: isCountMap(raw.agents) ? (raw.agents as Record<string, number>) : {}
		};
	});

	function rowKey(t: TurnRow): string {
		return `${t.user_seq}-${t.narrative_seq}`;
	}

	function toggle(t: TurnRow): void {
		const key = rowKey(t);
		expanded = expanded === key ? null : key;
	}

	// W5-S2: every captured agent that appears in this session's history, for
	// the capture filter. Empty until at least one capture exists.
	const captureAgents = $derived.by<string[]>(() =>
		[...new Set(agentCalls.map((c) => c.agent))].sort()
	);

	function capturesFor(t: TurnRow): AgentCall[] {
		const w = t.window;
		if (!w) return [];
		const rows = agentCalls.filter((c) => c.seq >= w.low && c.seq < w.high);
		return captureAgent ? rows.filter((c) => c.agent === captureAgent) : rows;
	}

	function regionEntries(regions: Record<string, number> | null): [string, number][] {
		if (!regions) return [];
		return Object.entries(regions).filter(([, v]) => typeof v === 'number');
	}

	function regionPct(value: number, regions: Record<string, number> | null): number {
		if (!regions) return 0;
		const max = Math.max(1, ...Object.values(regions));
		return Math.round((value / max) * 100);
	}

	function fmtLatency(s: number | null): string {
		return s == null ? '—' : `${s.toFixed(2)}s`;
	}

	function fmtHash(h: string | null): string {
		return h ? h : '—';
	}

	function fmtTokens(n: number | null | undefined): string {
		return n == null ? '—' : n.toLocaleString();
	}

	/** Ratio bar width — the bar is a proportion of the USABLE budget, capped
	 *  at 100% so an over-budget measurement still reads as "full", not "wider". */
	function ratioPct(ratio: number | null): number {
		if (ratio == null || !Number.isFinite(ratio)) return 0;
		return Math.min(100, Math.max(0, Math.round(ratio * 100)));
	}

	/** §2.7 thresholds: the gauge warns at 0.8 and compacts at 0.9. */
	function ratioTone(ratio: number | null): 'ok' | 'warn' | 'hot' {
		if (ratio == null) return 'ok';
		if (ratio >= 0.9) return 'hot';
		if (ratio >= 0.8) return 'warn';
		return 'ok';
	}

	function fmtRatio(ratio: number | null): string {
		return ratio == null ? '—' : `${Math.round(ratio * 100)}%`;
	}

	function fmtRevision(rev: Record<string, unknown>): string {
		const id = rev.id == null ? '—' : String(rev.id);
		const seq = rev.seq == null ? '—' : String(rev.seq);
		const actor = rev.actor == null ? '' : String(rev.actor);
		const reason = rev.reason == null ? '' : String(rev.reason);
		return `#${id} seq ${seq}${actor ? ` · ${actor}` : ''}${reason ? ` · ${reason}` : ''}`;
	}

	/** #23: copy one turn's provenance JSON for an external editor. */
	async function copyTurn(t: TurnRow): Promise<void> {
		const payload = {
			user_seq: t.user_seq,
			narrative_seq: t.narrative_seq,
			user_text: t.user_text,
			narrative_text: t.narrative_text,
			model: t.model,
			model_raw: t.model_raw,
			sampler: t.sampler,
			prompt_version: t.prompt_version,
			facts_hash: t.facts_hash,
			facts_text: t.facts_text,
			regions: t.regions,
			recall_text: t.recall_text,
			tokens: t.tokens,
			calls: t.calls,
			rag: t.rag,
			latency_s: t.latency_s
		};
		try {
			await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
		} catch {
			/* clipboard may be denied — cosmetic, never surface */
		}
	}
</script>

<div class="inspector">
	<header>
		<h2>Inspector</h2>
		<label
			class="agent-debug"
			class:busy={agentDebugBusy}
			title="Record what each agent sees and says for this chat"
		>
			<input
				type="checkbox"
				checked={agentDebug}
				disabled={agentDebugBusy || !sessionId}
				onchange={() => void toggleAgentDebug()}
				aria-label="Agent debug capture"
			/>
			<span>Agent debug</span>
		</label>
		{#if componentSets.length > 0}
			<label
				class="component-set"
				class:busy={setBusy}
				title="The component set the narrator is taught — a swap applies on the next turn"
			>
				<span>Set</span>
				<select
					value={activeSetId}
					disabled={setBusy || !sessionId}
					onchange={(event) => void changeComponentSet(event.currentTarget.value)}
					aria-label="Narrator component set"
				>
					{#each componentSets as set (set.id)}
						<option value={set.id} disabled={!set.loadable} title={set.reason}>
							{set.name}{set.version ? ` · v${set.version}` : ''}
						</option>
					{/each}
				</select>
			</label>
		{/if}
		<button type="button" class="refresh" onclick={() => sessionId && void load(sessionId)}
			>↻</button
		>
		<button type="button" class="close" onclick={onclose} aria-label="close inspector">✕</button>
	</header>

	{#if agentDebugError}
		<p class="error" role="alert">{agentDebugError}</p>
	{/if}

	{#if setError}
		<p class="error" role="alert">{setError}</p>
	{/if}

	{#if captureAgents.length > 0}
		<label class="capture-filter">
			<span>Captures</span>
			<select bind:value={captureAgent} aria-label="filter agent captures">
				<option value="">all agents</option>
				{#each captureAgents as a (a)}
					<option value={a}>{a}</option>
				{/each}
			</select>
		</label>
	{/if}

	{#if loadError}
		<p class="error" role="alert">{loadError}</p>
	{/if}

	<!-- Issue #34 (owner ruling): "a tab or something for the actual failures".
	     Two views, one pane: the turn trace this pane always showed, and the
	     notices the engine used to swallow to stderr. The tab label carries the
	     unacknowledged count so the prompt survives closing the header badge. -->
	<div class="pane-tabs" role="tablist" aria-label="Inspector views">
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'turns'}
			class:active={tab === 'turns'}
			onclick={() => (tab = 'turns')}
		>
			Turns
		</button>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'notices'}
			class:active={tab === 'notices'}
			class:alert={badge.count > 0}
			onclick={() => (tab = 'notices')}
		>
			Notices
			{#if badge.count > 0}<span class="tab-count">{badge.count}</span>{/if}
		</button>
	</div>

	{#if tab === 'notices'}
		<h3>
			Things that went wrong <span class="muted">({badge.allDegradations.length})</span>
		</h3>
		{#if badge.allDegradations.length === 0}
			<p class="empty">Nothing went wrong in this chat.</p>
		{:else}
			<ul class="notices" aria-label="degradation notices">
				{#each badge.allDegradations as notice (notice.seq)}
					<li class="notice">
						<span class="notice-what">{degradationLabel(notice.event)}</span>
						<span class="notice-meta">
							<span class="notice-stage">{notice.stage || notice.event}</span>
							<span class="notice-turn"
								>{notice.turn === null ? 'turn —' : `turn @${notice.turn}`}</span
							>
						</span>
						<span class="notice-reason">{notice.reason}</span>
					</li>
				{/each}
			</ul>
		{/if}

		<h3>Proposals waiting <span class="muted">({badge.proposals.length})</span></h3>
		{#if badge.proposals.length === 0}
			<p class="empty">No proposals waiting for a decision.</p>
		{:else}
			<ul class="notices" aria-label="pending proposals">
				{#each badge.proposals as proposal (proposal.id)}
					<li class="notice">
						<span class="notice-what">{proposal.action} · {proposal.label}</span>
						<span class="notice-meta">
							<span class="notice-stage">{proposal.kind}</span>
							<span class="notice-turn">{proposal.age_turns} turns old</span>
						</span>
						<span class="notice-reason">{proposal.evidence}</span>
					</li>
				{/each}
			</ul>
			{#if onopenproposals}
				<button type="button" class="review-proposals" onclick={onopenproposals}>
					Review proposals
				</button>
			{/if}
		{/if}
	{:else}

	<!-- W9.26 §2.7: per-task context ledger — quiet, read-only, one row per
		task surface. Undeclared windows say so instead of inventing a budget. -->
	<h3>
		Window use <span class="muted">(per task · prompt tokens measured by the model)</span>
	</h3>
	{#if ledgerError}
		<p class="error" role="alert">{ledgerError}</p>
	{:else if ledger.length === 0}
		<p class="empty">Nothing measured yet.</p>
	{:else}
		<!-- Issue #49: the ledger is a READOUT, not prose — every cell is one
			logical line, so cells never wrap and the table scrolls sideways when
			the pane is narrower than the columns. Every column keeps its `title`
			for the full value, so clipping is never information loss. -->
		<div class="ctx-scroll">
			<table class="context">
				<thead>
					<tr>
						<th>task</th>
						<th>connection</th>
						<th title="the context size you declared">window</th>
						<th title="response length in tokens">resp</th>
						<th title="room left for the prompt: window − response − reserve">budget</th>
						<th title="prompt tokens the model reported last turn">prompt</th>
						<th title="how full the prompt was — warns at 80%, compacts at 90%">ratio</th>
					</tr>
				</thead>
				<tbody>
					{#each ledger as row (row.task)}
						<tr class:undeclared={!row.window_declared}>
							<td class="ctx-task" title={`interaction: ${row.interaction} · agent: ${row.agent}`}
								>{row.task}</td
							>
							<td class="ctx-conn" title={row.model ?? 'no token report yet'}
								>{#if row.connection}<span class="ctx-model">{row.model ?? '—'}</span> · {row.connection}{:else}<span class="muted">unbound</span>{/if}</td
							>
							<td class="num" title={row.note ?? ''}
								>{#if row.window_declared}{fmtTokens(row.context_window)}{:else}<span class="muted"
										>{row.note ?? 'context size not set'}</span
									>{/if}</td
							>
							<td class="num">{fmtTokens(row.response_length)}</td>
							<td class="num">{fmtTokens(row.budget_tokens)}</td>
							<td class="num">{fmtTokens(row.prompt_tokens)}</td>
							<td class="ratio-cell">
								{#if row.ratio == null}
									<span class="muted">—</span>
								{:else}
									<span class="ratio" title={`${fmtTokens(row.prompt_tokens)} of ${fmtTokens(
										row.budget_tokens
									)} usable`}
										><span class="bar-track"
											><span
												class="bar-fill"
												class:warn={ratioTone(row.ratio) === 'warn'}
												class:hot={ratioTone(row.ratio) === 'hot'}
												style={`width:${ratioPct(row.ratio)}%`}
											></span
										></span
									><span class="ratio-value">{fmtRatio(row.ratio)}</span></span
									>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}

	{#if turns.length === 0 && !loadError}
		<p class="empty">No turns recorded yet.</p>
	{:else if turns.length > 0}
		<!-- composite key: the pairing can attach one narrative to two user
			events (a user turn that failed + its retry share the next narrative);
			narrative_seq alone would repeat each_key_duplicate -->
		<table class="turns">
			<thead>
				<tr>
					<th>#</th>
					<th>latency</th>
					<th>model</th>
					<th>tok</th>
					<th>facts</th>
					<th>Δ</th>
					<th>⚑</th>
					<th>✎</th>
					<th></th>
				</tr>
			</thead>
			<tbody>
				{#each turns as t (`${t.user_seq}-${t.narrative_seq}`)}
					{@const key = rowKey(t)}
					{@const isOpen = expanded === key}
					{@const captures = capturesFor(t)}
					<tr
						class:open={isOpen}
						onclick={() => toggle(t)}
						onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && toggle(t)}
						tabindex="0"
						role="button"
						aria-expanded={isOpen}
					>
						<td>{t.narrative_seq ?? t.user_seq}</td>
						<td class:num={t.latency_s != null}>{fmtLatency(t.latency_s)}</td>
						<td title={t.model_raw && t.model_raw !== t.model ? `raw: ${t.model_raw}` : (t.model ?? 'no config snapshot')}
							>{t.model ?? '—'}</td
						>
						<td class="num" title="prompt + completion tokens">{fmtTokens(t.tokens?.total_tokens)}</td>
						<td class="hash" title={t.facts_hash ?? ''}>{fmtHash(t.facts_hash)}</td>
						<td>{t.delta_count}</td>
						<td class:flagged={t.validation_flags > 0}>{t.validation_flags || ''}</td>
						<td>{t.revision_count || ''}</td>
						<td>
							<button
								type="button"
								class="copy"
								title="copy this turn's raw record (JSON)"
								aria-label={`copy turn ${t.narrative_seq ?? t.user_seq} record`}
								onclick={(e) => {
									e.stopPropagation();
									void copyTurn(t);
								}}>⧉</button
							>
						</td>
					</tr>
					{#if isOpen}
						<tr class="detail">
							<td colspan="9">
								<div class="detail-body">
									<section>
										<h4>User post</h4>
										<pre class="text user-text">{t.user_text ?? '—'}</pre>
									</section>

									<section>
										<h4>Response</h4>
										<pre class="text response">{t.narrative_text ?? '—'}</pre>
										{#if t.narrative_dsl}
											<details>
												<summary>panel markup ({t.narrative_dsl.length} chars)</summary>
												<pre class="text dsl">{t.narrative_dsl}</pre>
											</details>
										{/if}
									</section>

									<section>
										<h4>
											Tokens
											<span class="muted"
												>prompt {fmtTokens(t.tokens?.prompt_tokens)} · completion {fmtTokens(
													t.tokens?.completion_tokens
												)} · total {fmtTokens(t.tokens?.total_tokens)}</span
											>
										</h4>
										{#if t.calls.length > 0}
											<table class="mini">
												<thead>
													<tr>
														<th>agent</th>
														<th>model</th>
														<th>prompt</th>
														<th>completion</th>
														<th>total</th>
														<th>latency</th>
													</tr>
												</thead>
												<tbody>
													{#each t.calls as call (`${call.agent}-${call.seq}`)}
														<tr>
															<td>{call.agent}</td>
															<td>{call.model}</td>
															<td class="num">{fmtTokens(call.prompt_tokens)}</td>
															<td class="num">{fmtTokens(call.completion_tokens)}</td>
															<td class="num">{fmtTokens(call.total_tokens)}</td>
															<td class="num">{call.latency_s.toFixed(1)}s</td>
														</tr>
													{/each}
												</tbody>
											</table>
										{:else}
											<p class="muted">No token report for this turn.</p>
										{/if}
									</section>

									<section>
										<h4>Prompt regions <span class="muted">(chars)</span></h4>
										{#if regionEntries(t.regions).length > 0}
											<ul class="bars">
												{#each regionEntries(t.regions) as [name, value] (name)}
													<li>
														<span class="bar-head">
															<span class="bar-label">{name}</span>
															<span class="bar-value">{value}</span>
														</span>
														<span class="bar-track"
															><span
																class="bar-fill"
																style={`width:${regionPct(value, t.regions)}%`}
															></span></span
														>
													</li>
												{/each}
											</ul>
										{:else}
											<p class="muted">No region breakdown recorded.</p>
										{/if}
									</section>

									<section class="rag">
										<h4>RAG <span class="muted">(what recall fed this turn)</span></h4>
										<dl>
											<dt>query</dt>
											<dd>
												{#each t.rag?.queries ?? [] as q, i (i)}
													<pre class="text query">{q}</pre>
												{/each}
												{#if !t.rag?.queries?.length}
													<pre class="text query">{t.rag?.query ?? '—'}</pre>
												{/if}
											</dd>
											<dt>mode</dt>
											<dd>{t.rag?.query_mode ?? 'message'}</dd>
											<dt>k</dt>
											<dd>{t.rag?.k ?? '—'}</dd>
											<dt>rerank</dt>
											<dd>
												{t.rag?.rerank_enabled == null
													? 'not configured'
													: t.rag.rerank_enabled
														? 'on'
														: 'off'}
											</dd>
											<dt>memory</dt>
											<dd>{t.rag?.memory_enabled ? 'enabled' : 'disabled'}</dd>
											<dt>recall</dt>
											<dd>
												{#if t.rag?.recall_text}
													<pre class="text recall">{t.rag.recall_text}</pre>
												{:else}
													<span class="muted">no recall this turn</span>
												{/if}
											</dd>
										</dl>
									</section>

									{#if t.regions === null && t.facts_text === null}
										<p class="muted">
											facts: <code>{fmtHash(t.facts_hash)}</code> (text/regions not stamped by this
											turn's builder)
										</p>
									{:else if t.facts_text}
										<section>
											<h4>Injected facts</h4>
											<pre class="text facts">{t.facts_text}</pre>
										</section>
									{/if}

									{#if t.validation_details.length > 0}
										<section>
											<h4>Validation flags ({t.validation_details.length})</h4>
											{#each t.validation_details as flag, i (i)}
												<pre class="text flag">{JSON.stringify(flag, null, 2)}</pre>
											{/each}
										</section>
									{/if}

									{#if t.revision_refs.length > 0}
										<section>
											<h4>Revisions ({t.revision_refs.length})</h4>
											{#each t.revision_refs as rev, i (i)}
												<pre class="text rev">{fmtRevision(rev)}</pre>
											{/each}
										</section>
									{/if}

									{#if captures.length > 0}
										<section>
											<h4>
												What each agent saw and said
												<span class="muted">({captures.length}{captureAgent ? ` · ${captureAgent}` : ''})</span>
											</h4>
											<table class="mini">
												<tbody>
													{#each captures as call (`${call.agent}-${call.seq}`)}
														<tr>
															<td>{call.agent}</td>
															<td title={`response (${call.response_text.length} chars)`}
																>{call.response_text.slice(0, 90).replace(/\s+/g, ' ')}…</td
															>
															<td class="num">{call.latency_s.toFixed(1)}s</td>
														</tr>
													{/each}
												</tbody>
											</table>
										</section>
									{/if}
								</div>
							</td>
						</tr>
					{/if}
				{/each}
			</tbody>
		</table>
	{/if}

	{#if usageView}
		<h3>Usage — calls <span class="muted">({usageView.source})</span></h3>
		<p class="totals">
			{usageView.totals.calls ?? 0} calls · prompt
			{fmtTokens(usageView.totals.prompt_tokens)} · completion
			{fmtTokens(usageView.totals.completion_tokens)} · total
			{fmtTokens(usageView.totals.total_tokens)}
		</p>
		<table class="usage">
			<thead>
				<tr>
					<th>agent</th>
					<th>calls</th>
					<th>prompt</th>
					<th>completion</th>
					<th>total</th>
					<th>avg</th>
				</tr>
			</thead>
			<tbody>
				{#each Object.entries(usageView.byAgent) as [agent, agg] (agent)}
					<tr>
						<td>{agent}</td>
						<td class="num">{agg.calls}</td>
						<td class="num">{fmtTokens(agg.prompt_tokens)}</td>
						<td class="num">{fmtTokens(agg.completion_tokens)}</td>
						<td class="num">{fmtTokens(agg.total_tokens)}</td>
						<td class="num">{agg.avg_latency_s == null ? '—' : `${agg.avg_latency_s.toFixed(2)}s`}</td>
					</tr>
				{/each}
			</tbody>
		</table>

		<h3>Usage — models</h3>
		<table class="usage">
			<thead>
				<tr><th>model</th><th>calls</th><th>total tokens</th></tr>
			</thead>
			<tbody>
				{#each Object.entries(usageView.byModel) as [model, agg] (model)}
					<tr>
						<td>{model}</td>
						<td class="num">{agg.calls}</td>
						<td class="num">{fmtTokens(agg.total_tokens)}</td>
					</tr>
				{/each}
			</tbody>
		</table>

		{#if Object.keys(usageView.turnModels).length > 0}
			<h3>Narrative turns per model</h3>
			<table class="usage">
				<tbody>
					{#each Object.entries(usageView.turnModels) as [model, n] (model)}
						<tr>
							<td>{model}</td>
							<td class="num">{n} turns</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}

		{#if Object.keys(usageView.eventAgents).length > 0}
			<h3>Events per agent</h3>
			<table class="usage">
				<tbody>
					{#each Object.entries(usageView.eventAgents) as [agent, n] (agent)}
						<tr>
							<td>{agent}</td>
							<td class="num">{n}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}
	{/if}
</div>

<style>
	.inspector {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		height: 100%;
		overflow-y: auto;
		font-size: 0.78rem;
	}

	header {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}

	h2 {
		flex: 1;
		font-size: 0.85rem;
		margin: 0;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-muted);
	}

	h3 {
		font-size: 0.75rem;
		margin: 0.4rem 0 0;
		text-transform: uppercase;
		letter-spacing: 0.08em;
		color: var(--text-dim);
	}

	h4 {
		font-size: 0.72rem;
		margin: 0 0 0.2rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
		display: flex;
		gap: 0.4rem;
		align-items: baseline;
	}

	.agent-debug {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.72rem;
		color: var(--text-muted);
		cursor: pointer;
		white-space: nowrap;
	}

	.agent-debug.busy {
		cursor: default;
		opacity: 0.85;
	}

	.agent-debug input[type='checkbox'] {
		width: 0.9rem;
		height: 0.9rem;
		accent-color: var(--accent);
	}

	.agent-debug input:disabled {
		opacity: 0.5;
	}

	.component-set {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: 0.72rem;
		color: var(--text-muted);
		white-space: nowrap;
	}

	.component-set.busy {
		cursor: default;
		opacity: 0.85;
	}

	.component-set select {
		max-width: 11rem;
		font-size: 0.72rem;
		color: inherit;
		background: transparent;
		border: 1px solid var(--line);
		border-radius: 4px;
		padding: 0.05rem 0.2rem;
	}

	.component-set select:disabled {
		opacity: 0.5;
	}

	.refresh,
	.close {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text-muted);
		border-radius: 6px;
		cursor: pointer;
		font-size: 0.8rem;
		padding: 0.1rem 0.35rem;
	}

	table {
		border-collapse: collapse;
		width: 100%;
	}

	th {
		text-align: left;
		font-weight: normal;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		font-size: 0.7rem;
		line-height: 1.4;
		white-space: nowrap;
		vertical-align: bottom;
		color: var(--text-dim);
		padding: 0.4rem 0.3rem 0.32rem;
		border-bottom: 1px solid var(--line);
	}

	td {
		padding: 0.22rem 0.3rem;
		border-bottom: 1px solid var(--line);
		color: var(--text);
		overflow-wrap: anywhere;
	}

	td.num {
		text-align: right;
		font-variant-numeric: tabular-nums;
		/* Issue #49: a number is one token — never let it break across lines. */
		white-space: nowrap;
	}

	td.hash {
		font-family: monospace;
		color: var(--text-muted);
	}

	td.flagged {
		color: var(--danger-text);
		font-weight: bold;
	}

	tbody tr[role='button'] {
		cursor: pointer;
	}

	tbody tr[role='button']:hover,
	tbody tr.open {
		background: var(--bg-raised);
	}

	tr.detail td {
		background: var(--bg-sunken, var(--bg-raised));
		padding: 0.4rem 0.5rem;
	}

	.detail-body {
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
	}

	pre.text {
		margin: 0;
		padding: 0.35rem 0.4rem;
		background: var(--bg-sunken, rgba(0, 0, 0, 0.18));
		border: 1px solid var(--line);
		border-radius: 4px;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		max-height: 16rem;
		overflow-y: auto;
		/* keep a long box's wheel scroll inside the box, not the pane */
		overscroll-behavior: contain;
		/* a visible-enough scrollbar so a capped body reads as scrollable */
		scrollbar-width: thin;
		scrollbar-color: var(--line-strong, #3b3850) transparent;
		font-size: 0.72rem;
		line-height: 1.35;
	}

	/* The two prose bodies own their scroll so a long turn never stretches
	   the floating pane (I5b). */
	pre.response {
		max-height: 14rem;
	}

	pre.dsl,
	pre.facts,
	pre.flag,
	pre.rev {
		max-height: 10rem;
	}

	pre.user-text {
		border-left: 3px solid var(--accent, var(--line));
		max-height: 8rem;
	}

	pre.query {
		max-height: 6rem;
	}

	pre.recall {
		max-height: 12rem;
	}

	.muted {
		color: var(--text-dim);
		font-weight: normal;
		text-transform: none;
		letter-spacing: 0;
	}

	.totals {
		margin: 0.15rem 0;
		color: var(--text-muted);
		/* Issue #49: the per-call token readout is ONE line — it wraps between the
		   label and its number otherwise, which reads as broken. Scroll instead. */
		white-space: nowrap;
		overflow-x: auto;
		overflow-y: hidden;
		font-variant-numeric: tabular-nums;
	}

	table.mini {
		margin-top: 0.15rem;
		font-size: 0.7rem;
	}

	.bars {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}

	/* I5a: label sits on its own line ABOVE the track so it can never
	   collide with the fill, however small the region is. */
	.bars li {
		display: flex;
		flex-direction: column;
		gap: 0.12rem;
	}

	.bar-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}

	.bar-label {
		color: var(--text-muted);
	}

	.bar-track {
		width: 100%;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 3px;
		height: 0.5rem;
		overflow: hidden;
	}

	.bar-fill {
		display: block;
		height: 100%;
		background: var(--accent, #6b8afd);
	}

	.bar-value {
		text-align: right;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
	}

	/* W9.26 §2.7: the per-task context ledger. Quiet by construction — it is a
	   read of the engine's own numbers, never a control. */
	/* Issue #49: horizontal scroll, not ellipsis, for the ledger. These cells ARE
	   the value the inspector was opened to read; truncating "200,000" to "20…"
	   would be worse than a scrollbar, and the pane is now resizable so the
	   common case is that nothing overflows at all. */
	.ctx-scroll {
		overflow-x: auto;
		overscroll-behavior-x: contain;
		scrollbar-width: thin;
		scrollbar-color: var(--line-strong, #3b3850) transparent;
	}

	table.context {
		width: max-content;
		min-width: 100%;
	}

	/* One logical line per cell: a readout never breaks mid-number. */
	table.context th,
	table.context td {
		white-space: nowrap;
	}

	table.context td {
		font-size: 0.72rem;
	}

	/* Tabular numerals + monospace so digits line up column-wise and never reflow
	   as a value's width changes (the ugly-wrap half of issue #49). */
	table.context td.num {
		font-family: monospace;
		font-variant-numeric: tabular-nums;
	}

	td.ctx-task {
		white-space: nowrap;
	}

	td.ctx-conn {
		color: var(--text-muted);
		white-space: nowrap;
		/* The one DESCRIPTIVE readout (model id · connection name): ellipsis it
		   rather than let a long model id widen the table — the `title` carries
		   the full string. */
		max-width: 12rem;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.ctx-model {
		font-family: monospace;
		color: var(--text);
	}

	/* Undeclared Context Size (⚑C): the row states it instead of guessing. */
	tr.undeclared td {
		color: var(--text-muted);
	}

	.ratio-cell {
		white-space: nowrap;
	}

	.ratio {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}

	.ratio .bar-track {
		width: 3.2rem;
		flex: none;
	}

	.ratio-value {
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
		font-size: 0.7rem;
	}

	/* The gauge's own thresholds: warn at 80%, compact at 90%. */
	.bar-fill.warn {
		background: var(--warn, #fbbf24);
	}

	.bar-fill.hot {
		background: var(--danger, #b91c1c);
	}

	dl {
		display: grid;
		grid-template-columns: 4.5rem 1fr;
		gap: 0.15rem 0.5rem;
		margin: 0;
	}

	dt {
		color: var(--text-dim);
	}

	dd {
		margin: 0;
	}

	.copy {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text-muted);
		border-radius: 4px;
		cursor: pointer;
		font-size: 0.72rem;
		padding: 0.05rem 0.3rem;
	}

	.capture-filter {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		font-size: 0.72rem;
		color: var(--text-dim);
	}

	.capture-filter select {
		font: inherit;
		background: var(--bg-raised);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 5px;
		padding: 0.05rem 0.25rem;
	}

	.empty,
	.error {
		color: var(--text-dim);
		font-size: 0.78rem;
	}

	.error {
		color: var(--danger-text);
	}
	/* Issue #34: the two-view tab strip + the notices body. The pane stays one
	   surface (owner ruling: "a tab or something for the actual failures"); the
	   alert tone is the same one the header badge uses. */
	.pane-tabs {
		display: flex;
		gap: 0.25rem;
		border-bottom: 1px solid var(--line);
		padding-bottom: 0.25rem;
	}

	.pane-tabs button {
		font: inherit;
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		padding: 0.15rem 0.5rem;
		border: 1px solid transparent;
		border-radius: 6px;
		background: transparent;
		color: var(--text-dim);
		cursor: pointer;
	}

	.pane-tabs button:hover {
		background: var(--bg-hover);
		color: var(--text-muted);
	}

	.pane-tabs button.active {
		border-color: var(--line);
		background: var(--bg-raised);
		color: var(--text);
	}

	.pane-tabs button.alert {
		color: var(--warn);
	}

	.tab-count {
		min-width: 0.9rem;
		padding: 0 0.2rem;
		border-radius: 999px;
		background: var(--warn);
		color: #1c1917;
		font-size: 0.6rem;
		font-weight: 700;
		line-height: 0.9rem;
		text-align: center;
	}

	.notices {
		list-style: none;
		margin: 0.2rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}

	.notice {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		padding: 0.3rem 0.4rem;
		border: 1px solid var(--line);
		border-left: 3px solid var(--warn);
		border-radius: 6px;
		background: var(--bg-raised);
	}

	.notice-what {
		font-weight: 600;
		color: var(--warn);
	}

	.notice-meta {
		display: flex;
		justify-content: space-between;
		gap: 0.5rem;
		font-size: 0.68rem;
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}

	.notice-reason {
		color: var(--text-muted);
		line-height: 1.35;
		word-break: break-word;
	}

	.review-proposals {
		font: inherit;
		font-size: 0.72rem;
		margin-top: 0.35rem;
		padding: 0.2rem 0.5rem;
		border: 1px solid var(--accent-line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
</style>
