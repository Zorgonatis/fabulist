<script lang="ts">
	// NPC lanes pane (issue #56 owner ruling) --------------------------------
	//
	// Lanes are configured where NPCs are managed, so this page is their home in
	// the studio: the on/off switch and the in-flight width, the effective
	// values read back from the stored flow, and — for an open chat — the lane
	// set the last turn actually ran.
	//
	// Both controls are TOPOLOGY data on the fan-out row (`data.enabled`,
	// `data.max_lanes`): the running graph is compiled from that row, so the pane
	// writes the whole topology back through the validated door
	// (`PUT /api/prompts/topology`) through `#lib/api/npcLanes`. That module is
	// also what the cast pane's quick options use, so the two surfaces can never
	// drift: both read the same store, and a save in one lands in the other.
	//
	// A refused save is not silently swallowed: the door names the violation, and
	// that sentence is what the owner sees (issue #56.4 — an off switch the engine
	// cannot honour is refused rather than ignored).
	import { onMount } from 'svelte';
	import { getLanes, type FlowLanes } from '#lib/api/flow';
	import {
		LANE_WIDTHS,
		laneConfigStore,
		laneRefusal,
		readLaneConfig,
		writeLaneConfig,
		type LaneConfig
	} from '#lib/api/npcLanes';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty,
		sessionId = null
	}: {
		/** Inert: this page edits the flow's lane step, not one addressable row. */
		focusId?: string | null;
		/** Inert: there is nothing to create here. */
		startNew?: boolean;
		/** Inert: no `focusId` to re-apply. */
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
		/** The open chat: its last turn's lane set is shown when there is one. */
		sessionId?: string | null;
	} = $props();

	/** `loading` until the first read lands; `no-step` = the flow has no lane step. */
	type LaneState = 'loading' | 'ready' | 'unreachable' | 'no-step';

	let laneState = $state<LaneState>('loading');
	let config = $state<LaneConfig | null>(null);
	//: The two controls' own state. They are NOT derived from `config`: a refused
	//: save restores the stored value, and a `checked={config.enabled}` binding
	//: would look unchanged to Svelte and leave the clicked box ticked anyway.
	let lanesOn = $state(true);
	let laneWidth = $state('4');
	let saving = $state(false);
	let error = $state('');
	/** The door's confirmation, kept visible so a save is not a flash. */
	let note = $state('');

	//: The last turn's lane set for the open chat (a session fact, not config).
	let laneSet = $state<FlowLanes | null>(null);
	let lanesLoading = $state(false);

	const busy = $derived(saving);
	const disabled = $derived(laneState !== 'ready' || saving);

	const stateLabel = $derived(
		laneState === 'loading'
			? 'reading the flow…'
			: laneState === 'unreachable'
				? 'the engine is not answering'
				: laneState === 'no-step'
					? 'this flow has no NPC lane step'
					: !config
						? 'not set'
						: config.enabled
							? `on — ${config.maxLanes} at once`
							: 'off — the narrator writes this scene alone'
	);

	const switchTitle = $derived(
		laneState === 'loading'
			? 'Reading the lane settings…'
			: laneState === 'unreachable'
				? 'Engine unreachable — the lane settings cannot be changed'
				: laneState === 'no-step'
					? 'This flow has no NPC lane step to switch on or off'
					: 'Switch lanes off: the narrator writes this scene alone'
	);

	const widthTitle = $derived(
		laneState === 'ready' && !lanesOn
			? 'Lanes are off — switch them on to choose how many run at once'
			: 'How many lanes may run at once (every lane still runs, just not all together)'
	);

	/** A stored config's identity — the guard that keeps the shared store quiet. */
	let appliedKey = '';

	/** Apply a stored config without touching the door (a read, or a save's echo). */
	function apply(next: LaneConfig | null): void {
		config = next;
		appliedKey = next ? `${next.enabled}:${next.maxLanes}` : '';
		laneState = next ? 'ready' : 'no-step';
		lanesOn = next?.enabled ?? true;
		laneWidth = String(next?.maxLanes ?? 4);
	}

	async function load(): Promise<void> {
		laneState = 'loading';
		error = '';
		note = '';
		try {
			apply(await readLaneConfig());
		} catch {
			// Not a refusal, just no engine: the state line and the hint say so.
			laneState = 'unreachable';
		}
	}

	onMount(() => {
		void load();
	});

	// The shared store: a save in the cast pane's quick options (or a read from
	// any other surface) re-renders this page from the same stored value. The key
	// guard is what keeps this from re-applying its own write (Svelte 5: read the
	// `$store` directly — `get(store)` inside an effect does not track).
	$effect(() => {
		const published = $laneConfigStore;
		if (!published || saving) return;
		const key = `${published.enabled}:${published.maxLanes}`;
		if (key === appliedKey) return;
		apply(published);
		if (laneState !== 'unreachable') laneState = 'ready';
		note = '';
	});

	/** One write, one read-back: the controls show what the flow now says. */
	async function save(patch: { enabled?: boolean; maxLanes?: number }): Promise<void> {
		if (disabled) return;
		saving = true;
		error = '';
		note = '';
		try {
			apply(await writeLaneConfig(patch));
			note = 'Saved — applies to the next turn.';
			onChanged?.();
		} catch (err) {
			const refused = laneRefusal(err);
			// A refused save changed nothing: re-read so the controls fall back to
			// the stored values, then keep the door's own words on screen.
			await load();
			error = refused;
		} finally {
			saving = false;
		}
	}

	function toggle(next: boolean): void {
		lanesOn = next;
		void save({ enabled: next });
	}

	function setWidth(value: string): void {
		laneWidth = value;
		const width = Number(value);
		if (!Number.isFinite(width)) return;
		if (config && width === config.maxLanes) return;
		void save({ maxLanes: width });
	}

	// --- the open chat's own lane set (bonus: the existing lanes read) --------
	//
	// `GET /api/sessions/{id}/lanes` already serves the plan/finished set of the
	// last turn, so the page shows what the setting actually produced. It is a
	// session fact, not configuration — with no chat open there is nothing to
	// read, and the section simply does not render.
	let loadedFor: string | null = null;

	async function loadLanes(id: string): Promise<void> {
		lanesLoading = true;
		try {
			laneSet = await getLanes(id);
		} finally {
			lanesLoading = false;
		}
	}

	$effect(() => {
		const id = sessionId;
		if (id === loadedFor) return;
		loadedFor = id;
		laneSet = null;
		if (id) void loadLanes(id);
	});

	const laneSetLabel = $derived.by(() => {
		if (!laneSet) return '';
		const count = laneSet.lanes.length;
		if (count === 0) return 'No lanes yet — they appear on the next turn.';
		const names = laneSet.lanes.map((lane) => lane.name ?? lane.member_id ?? 'unnamed');
		return `${count} lane${count === 1 ? '' : 's'} on turn ${laneSet.turn_seq ?? '—'}: ${names.join(', ')}`;
	});

	// The pane edits settings, not a draft: nothing here is pending a save button.
	$effect(() => {
		onDirty?.(false);
	});
</script>

<div class="lanes-pane">
	<header class="pane-head">
		<h2 class="pane-title">NPC lanes</h2>
		<div class="head-actions">
			<span class="pane-meta">{stateLabel}</span>
			<button
				type="button"
				class="ghost"
				disabled={saving || laneState === 'loading'}
				onclick={() => void load()}
			>
				Reload
			</button>
		</div>
	</header>

	<p class="hint">
		Every character in the scene gets a lane: they work out what they want and act on it
		before the narrator writes the turn. Lanes run a few at a time — the rest wait their
		turn, and every lane still runs. With lanes off, the narrator writes this scene alone.
	</p>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if laneState === 'loading'}
		<p class="hint">Reading the lane settings…</p>
	{:else if laneState === 'unreachable'}
		<p class="hint">
			The engine is not answering, so these settings can't be changed right now. Start it, then
			press Reload.
		</p>
	{:else if laneState === 'no-step'}
		<p class="hint">
			This flow has no NPC lane step, so there is nothing to switch on or off. Put the lane step
			back on the Flow page to give characters their own lanes again.
		</p>
	{:else if config}
		<section class="group" aria-labelledby="lanes-switch-heading">
			<header class="group-head">
				<h3 id="lanes-switch-heading">Lanes</h3>
			</header>
			<label class="switch" title={switchTitle}>
				<input
					type="checkbox"
					checked={lanesOn}
					disabled={disabled}
					aria-label="NPC lanes on or off"
					data-lane-enabled={lanesOn ? 'on' : 'off'}
					onchange={(event) => toggle(event.currentTarget.checked)}
				/>
				<span>NPC lanes</span>
				<span class="switch-state">{lanesOn ? 'on' : 'off'}</span>
			</label>
			<p class="hint">
				{#if lanesOn}
					Each character acts in their own lane before the narrator writes the turn.
				{:else}
					Off: the narrator writes this scene alone, and no character gets their own lane.
				{/if}
			</p>
		</section>

		<section class="group" aria-labelledby="lanes-width-heading">
			<header class="group-head">
				<h3 id="lanes-width-heading">At once</h3>
			</header>
			<label class="field-inline" title={widthTitle}>
				<span>Lanes running</span>
				<select
					value={laneWidth}
					disabled={disabled || !lanesOn}
					aria-label="Lanes running at once"
					data-lane-width={laneWidth}
					onchange={(event) => setWidth(event.currentTarget.value)}
				>
					{#each LANE_WIDTHS as width (width)}
						<option value={String(width)}>{width}</option>
					{/each}
				</select>
				<span>at once</span>
			</label>
			<p class="hint">
				How many characters work at the same time. Every character in the scene still gets a
				lane — a narrower pool just makes them take turns.
			</p>
		</section>

		{#if note}
			<p class="note" role="status">{note}</p>
		{/if}

		{#if sessionId}
			<section class="group" aria-labelledby="lanes-live-heading">
				<header class="group-head">
					<h3 id="lanes-live-heading">This chat</h3>
					{#if laneSet?.in_flight}<span class="pane-meta">in flight…</span>{/if}
				</header>
				{#if lanesLoading}
					<p class="hint">Reading the last turn…</p>
				{:else if laneSetLabel}
					<p class="hint">{laneSetLabel}</p>
				{/if}
			</section>
		{/if}
	{:else}
		<p class="hint">Nothing to show.</p>
	{/if}

	<p class="hint footnote">
		These settings belong to the turn flow, not to one chat: they apply to every chat from the
		next turn on. The cast pane carries the same two controls while you manage characters, and
		the turn flow panel shows them beside a running turn.
	</p>

	{#if busy}
		<p class="hint" role="status">Saving…</p>
	{/if}
</div>

<style>
	.lanes-pane {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		min-height: 0;
	}
	.pane-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
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
	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.group {
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding-top: 0.45rem;
		border-top: 1px solid var(--line);
	}
	.group-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.group-head h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.hint {
		margin: 0.15rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.footnote {
		padding-top: 0.45rem;
		border-top: 1px solid var(--line);
		color: var(--text-muted);
	}
	.note {
		margin: 0;
		font-size: var(--font-small);
		color: var(--ok);
	}
	.error {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.switch,
	.field-inline {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.switch-state {
		color: var(--text-dim);
	}
	.field-inline select {
		font: inherit;
		font-size: var(--font-small);
		color: var(--text);
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.1rem 0.3rem;
	}
	.field-inline select:disabled {
		opacity: 0.55;
		cursor: not-allowed;
	}
	.switch input:disabled {
		cursor: not-allowed;
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.2rem 0.55rem;
	}
	button:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.ghost {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text);
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
	}
</style>
