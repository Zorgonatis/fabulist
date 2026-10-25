<script lang="ts">
	// Planner pane (W9.20 §8) --------------------------------------------------
	//
	// The planner is a turn step that runs INFREQUENTLY — turn 1 is mandatory,
	// then every `cadence` committed turns — and writes the plan the narration
	// steers by: story strings, the scene's date/time, weather + forecast, and
	// the facts it holds established. This pane is its control surface, in two
	// halves:
	//
	//  · the CONFIG (cadence · steer · fields) is preset data and needs NO
	//    session — the field list is the plan's dynamic schema, so a GM/RPG
	//    preset adds its own slots (currency, reputation) here;
	//  · the CURRENT PLAN (strings · established · extras) is SESSION state, so
	//    it renders only when the shell hands a `sessionId` down (the same prop
	//    pattern `GraphHomePane` uses). Saving sends the WHOLE plan — a plan event
	//    is a complete plan, not a diff — which the engine validates against the
	//    active field list and appends as a `manual` plan. Extras are therefore
	//    restricted to the CONFIGURED field ids: an invented key is a 422.
	//
	// Two owner doors onto the same planner, both claiming the session's turn slot
	// like a turn does (409 while one streams):
	//
	//  · "Run planner now" refreshes the plan and nothing else (W9.23 §3.6.7) —
	//    safe on any chat, so it needs no confirm;
	//  · "Regenerate Scenario" does the same and, on a zero-turn session, writes
	//    the opening message too (plan §1.5) — hence the confirm.
	//
	// The API is the authority on bad rows: a 422's `detail` list is rendered
	// verbatim (`plannerErrorDetails`), the same way `NodeTemplatesPane` shows a
	// validation verdict. Numeric drafts stay TEXT so a cleared or half-typed
	// value remains visible instead of being silently coerced.
	import { onMount } from 'svelte';
	import {
		DEFAULT_CADENCE,
		defaultPlannerConfig,
		getPlannerConfig,
		getSessionPlan,
		plannerConfigError,
		plannerErrorDetails,
		putPlannerConfig,
		putSessionPlan,
		regenerateScenario,
		runPlanNow,
		type PlannerConfig,
		type PlannerField,
		type PlannerFieldType,
		type ScenarioPlan,
		type SessionPlanPayload
	} from '#lib/api/planner';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty,
		sessionId = null
	}: {
		/** Inert: the pane has no addressable row yet (contract is visible here). */
		focusId?: string | null;
		/** Inert: field rows are added by the pane's own button. */
		startNew?: boolean;
		/** Inert: no `focusId` to re-apply. */
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
		/** The open chat; absent = definition-only editing (no plan section). */
		sessionId?: string | null;
	} = $props();

	/** A field row with its type kept as the select's value. */
	interface FieldDraft {
		id: string;
		title: string;
		type: PlannerFieldType;
		hint: string;
		required: boolean;
	}

	/** A plan extra with its value as text (see the header note). */
	interface ExtraDraft {
		key: string;
		type: PlannerFieldType;
		value: string;
	}

	// --- config draft ---------------------------------------------------------
	let cadenceText = $state(String(DEFAULT_CADENCE));
	let steer = $state('');
	let fieldDrafts = $state<FieldDraft[]>([]);
	let configBaseline = $state('');
	let loading = $state(true);
	let savingConfig = $state(false);
	let error = $state('');

	// --- current plan ---------------------------------------------------------
	let plan = $state<ScenarioPlan | null>(null);
	let dueIn = $state<number | null>(null);
	let planLoading = $state(false);
	let planError = $state('');
	let savingPlan = $state(false);
	let regenerating = $state(false);
	let runningPlan = $state(false);
	let stringDrafts = $state<string[]>([]);
	let extraDrafts = $state<ExtraDraft[]>([]);
	let planBaseline = $state('');

	const configDirty = $derived(
		configBaseline !== '' &&
			configBaseline !== JSON.stringify({ cadenceText, steer, fields: fieldDrafts })
	);
	const planDirty = $derived(
		planBaseline !== '' &&
			planBaseline !== JSON.stringify({ strings: stringDrafts, extras: extraDrafts })
	);
	const busy = $derived(savingConfig || savingPlan || regenerating || runningPlan);

	$effect(() => {
		onDirty?.(configDirty || planDirty);
	});

	const dueText = $derived(
		dueIn === null
			? ''
			: dueIn <= 0
				? 'plan due on the next turn'
				: `next plan in ${dueIn} turn${dueIn === 1 ? '' : 's'}`
	);

	function message(err: unknown): string {
		return plannerErrorDetails(err).join('; ');
	}

	// --- config load/save -----------------------------------------------------

	function applyConfig(config: PlannerConfig): void {
		cadenceText = String(config.cadence);
		steer = config.steer;
		fieldDrafts = config.fields.map((field) => ({ ...field }));
		const baseline = JSON.stringify({ cadenceText, steer, fields: fieldDrafts });
		configBaseline = baseline;
	}

	/** Draft -> wire. A blank cadence becomes NaN, which the check rejects. */
	function toConfig(): PlannerConfig {
		const text = cadenceText.trim();
		return {
			cadence: text === '' ? NaN : Number(text),
			steer,
			fields: fieldDrafts.map(
				(field): PlannerField => ({
					id: field.id.trim(),
					title: field.title,
					type: field.type,
					hint: field.hint,
					required: field.required
				})
			)
		};
	}

	async function loadConfig(): Promise<void> {
		loading = true;
		error = '';
		try {
			applyConfig(await getPlannerConfig());
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void loadConfig();
	});

	async function saveConfig(): Promise<void> {
		if (busy) return;
		const config = toConfig();
		const problem = plannerConfigError(config);
		if (problem) {
			error = problem;
			return;
		}
		savingConfig = true;
		error = '';
		try {
			applyConfig(await putPlannerConfig(config));
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			savingConfig = false;
		}
	}

	function addField(): void {
		fieldDrafts = [
			...fieldDrafts,
			{ id: '', title: '', type: 'string', hint: '', required: false }
		];
	}

	function removeField(target: FieldDraft): void {
		fieldDrafts = fieldDrafts.filter((field) => field !== target);
	}

	function discardConfig(): void {
		const saved = JSON.parse(configBaseline) as {
			cadenceText: string;
			steer: string;
			fields: FieldDraft[];
		};
		cadenceText = saved.cadenceText;
		steer = saved.steer;
		fieldDrafts = saved.fields;
		error = '';
	}

	// --- current plan load/save ----------------------------------------------
	//
	// The field list is the plan's schema, so an extra's control follows the
	// configured TYPE for its key; a key the config does not declare keeps the
	// type of the value it arrived with (extras are a free-form bag on the wire).

	function typeForKey(key: string, value: unknown): PlannerFieldType {
		const declared = fieldDrafts.find((field) => field.id === key);
		if (declared) return declared.type;
		if (typeof value === 'number') return 'number';
		if (typeof value === 'boolean') return 'boolean';
		return 'string';
	}

	function applyPlan(response: { plan: ScenarioPlan | null; due_in: number | null }): void {
		plan = response.plan;
		dueIn = response.due_in;
		stringDrafts = response.plan ? [...response.plan.strings] : [];
		extraDrafts = response.plan
			? Object.entries(response.plan.extras).map(([key, value]) => ({
					key,
					type: typeForKey(key, value),
					value: value === null || value === undefined ? '' : String(value)
				}))
			: [];
		planBaseline = JSON.stringify({ strings: stringDrafts, extras: extraDrafts });
	}

	async function loadPlan(id: string): Promise<void> {
		planLoading = true;
		planError = '';
		try {
			applyPlan(await getSessionPlan(id));
		} catch (err) {
			planError = message(err);
		} finally {
			planLoading = false;
		}
	}

	// Keyed on the id: a session switch re-reads the plan, closing clears it.
	let loadedFor: string | null = null;
	$effect(() => {
		const id = sessionId;
		if (id === loadedFor) return;
		loadedFor = id;
		plan = null;
		dueIn = null;
		stringDrafts = [];
		extraDrafts = [];
		planBaseline = '';
		planError = '';
		if (id) void loadPlan(id);
	});

	/** Text draft -> the value the wire carries, per the row's declared type. */
	function extraValue(draft: ExtraDraft): unknown {
		if (draft.type === 'boolean') return draft.value === 'true';
		if (draft.type === 'number') {
			const text = draft.value.trim();
			const value = Number(text);
			// A blank or half-typed number stays as typed: the server is the
			// authority on whether that is acceptable for the field.
			return text !== '' && Number.isFinite(value) ? value : draft.value;
		}
		return draft.value;
	}

	async function savePlan(): Promise<void> {
		if (busy || !sessionId || !plan) return;
		const extras: Record<string, unknown> = {};
		for (const draft of extraDrafts) {
			const key = draft.key.trim();
			if (key) extras[key] = extraValue(draft);
		}
		const payload: SessionPlanPayload = {
			strings: stringDrafts.map((line) => line.trim()).filter((line) => line !== ''),
			established: [...plan.established],
			datetime: plan.datetime,
			weather: plan.weather,
			forecast: plan.forecast,
			extras
		};
		savingPlan = true;
		planError = '';
		try {
			applyPlan(await putSessionPlan(sessionId, payload));
			onChanged?.();
		} catch (err) {
			planError = message(err);
		} finally {
			savingPlan = false;
		}
	}

	/** Run planner now: the plain refresh — no confirm, it can never write prose. */
	async function runNow(): Promise<void> {
		if (busy || !sessionId) return;
		runningPlan = true;
		planError = '';
		try {
			applyPlan(await runPlanNow(sessionId));
			onChanged?.();
		} catch (err) {
			planError = message(err);
		} finally {
			runningPlan = false;
		}
	}

	async function regenerate(): Promise<void> {
		if (busy || !sessionId) return;
		if (typeof window !== 'undefined') {
			const confirmed = window.confirm(
				'Regenerate the scenario? The planner replaces the story strings and the plan, and on a chat with no turns yet it also writes the opening message.'
			);
			if (!confirmed) return;
		}
		regenerating = true;
		planError = '';
		try {
			applyPlan(await regenerateScenario(sessionId));
			onChanged?.();
		} catch (err) {
			planError = message(err);
		} finally {
			regenerating = false;
		}
	}

	function addString(): void {
		stringDrafts = [...stringDrafts, ''];
	}

	function removeString(index: number): void {
		stringDrafts = stringDrafts.filter((_, i) => i !== index);
	}

	/** The configured field waiting to be added to the plan, or `null`. */
	const nextExtraField = $derived(
		fieldDrafts.find(
			(field) => field.id.trim() !== '' && !extraDrafts.some((draft) => draft.key === field.id)
		) ?? null
	);

	/** A configured field's display label (its id when the title is blank). */
	function fieldTitle(id: string): string {
		const title = fieldDrafts.find((field) => field.id === id)?.title.trim();
		return title || id;
	}

	function addExtra(): void {
		const field = nextExtraField;
		if (!field) return;
		extraDrafts = [
			...extraDrafts,
			{
				key: field.id,
				type: field.type,
				value: field.type === 'boolean' ? 'false' : ''
			}
		];
	}

	function removeExtra(target: ExtraDraft): void {
		extraDrafts = extraDrafts.filter((draft) => draft !== target);
	}

	function discardPlan(): void {
		const saved = JSON.parse(planBaseline) as { strings: string[]; extras: ExtraDraft[] };
		stringDrafts = saved.strings;
		extraDrafts = saved.extras;
		planError = '';
	}
</script>

<div class="planner-pane">
	<header class="pane-head">
		<h2 class="pane-title">Planner</h2>
		<span class="pane-meta">
			every {cadenceText || '—'} turn{cadenceText === '1' ? '' : 's'} · {fieldDrafts.length} extra
			field{fieldDrafts.length === 1 ? '' : 's'}{dueText ? ` · ${dueText}` : ''}
		</span>
	</header>

	<p class="hint">
		The planner runs before the narration — on the first turn, then every N committed turns —
		and writes the story strings, the scene's date and weather, and the facts the plan holds
		established. The narration steers by it until the next plan. Configure it here; the
		current plan of an open chat is below.
	</p>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading planner config…</p>
	{:else}
		<section class="group" aria-labelledby="planner-cadence-heading">
			<header class="group-head">
				<h3 id="planner-cadence-heading">Cadence</h3>
			</header>
			<div class="row">
				<label class="field-inline">
					<span>Every</span>
					<input
						class="cadence"
						type="number"
						min="1"
						step="1"
						value={cadenceText}
						oninput={(event) => (cadenceText = event.currentTarget.value)}
					/>
					<span>committed turns</span>
				</label>
			</div>
			<p class="hint">
				Turn 1 always plans, whatever this says — a chat with no plan has nothing to steer by.
			</p>
		</section>

		<section class="group" aria-labelledby="planner-steer-heading">
			<header class="group-head">
				<h3 id="planner-steer-heading">Steer</h3>
			</header>
			<label class="field">
				<span>Standing direction for every planning pass</span>
				<textarea
					rows="3"
					value={steer}
					placeholder="e.g. keep the scenes grounded; favour consequences over new mysteries"
					oninput={(event) => (steer = event.currentTarget.value)}
				></textarea>
			</label>
		</section>

		<section class="group" aria-labelledby="planner-fields-heading">
			<header class="group-head">
				<h3 id="planner-fields-heading">Extra fields</h3>
				<button type="button" class="ghost" onclick={addField}>+ Add field</button>
			</header>
			<p class="hint">
				Extra details the plan can carry beyond its core values (text, date/time, weather,
				forecast, established). Each row is something your setup cares about — add currency and
				reputation for a money-minded game; the planner fills them in and you edit them here.
			</p>
			{#if fieldDrafts.length === 0}
				<p class="hint">No extra fields — the plan carries only its core values.</p>
			{:else}
				<ul class="rows">
					{#each fieldDrafts as field, index (index)}
						<li class="row">
							<input
								class="field-id"
								placeholder="id"
								aria-label="Field id"
								value={field.id}
								oninput={(event) =>
									(field.id = event.currentTarget.value
										.toLowerCase()
										.replace(/[^a-z0-9_]+/g, '_'))}
							/>
							<input
								class="field-title"
								placeholder="title"
								aria-label="Field title"
								value={field.title}
								oninput={(event) => (field.title = event.currentTarget.value)}
							/>
							<select
								aria-label="Field type"
								value={field.type}
								onchange={(event) =>
									(field.type = event.currentTarget.value as PlannerFieldType)}
							>
								<option value="string">string</option>
								<option value="number">number</option>
								<option value="boolean">boolean</option>
							</select>
							<input
								class="field-hint"
								placeholder="hint for the planner"
								aria-label="Field hint"
								value={field.hint}
								oninput={(event) => (field.hint = event.currentTarget.value)}
							/>
							<label class="toggle">
								<input type="checkbox" bind:checked={field.required} />
								<span>required</span>
							</label>
							<button type="button" class="danger" onclick={() => removeField(field)}>
								Remove
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		{#if sessionId}
			<section class="group" aria-labelledby="planner-plan-heading">
				<header class="group-head">
					<h3 id="planner-plan-heading">Current plan</h3>
					<div class="head-actions">
						<button type="button" class="ghost" onclick={() => void runNow()} disabled={busy}>
							{runningPlan ? 'Planning…' : 'Run planner now'}
						</button>
						<button
							type="button"
							class="ghost"
							onclick={() => void regenerate()}
							disabled={busy}
						>
							{regenerating ? 'Regenerating…' : 'Regenerate Scenario'}
						</button>
					</div>
				</header>

				{#if planError}
					<p class="error" role="alert">{planError}</p>
				{/if}

				{#if planLoading}
					<p class="hint">Loading the plan…</p>
				{:else if !plan}
					<p class="hint">
						This chat has no plan yet — the next turn plans before it narrates, or press Run
						planner now to write one now. (On a chat with no turns yet, Regenerate Scenario
						also writes the opening message.)
					</p>
				{:else}
					<p class="hint">
						Planned at turn {plan.turn_seq} ({plan.created_from || 'planner'}){dueText
							? ` · ${dueText}`
							: ''}. Edits are saved as a manual plan revision.
					</p>

					<div class="view">
						<div class="view-row"><span>Date / time</span><code>{plan.datetime ?? '—'}</code></div>
						<div class="view-row"><span>Weather</span><code>{plan.weather ?? '—'}</code></div>
						<div class="view-row"><span>Forecast</span><code>{plan.forecast ?? '—'}</code></div>
					</div>

					<div class="field">
						<header class="group-head">
							<span class="sub-head">Story strings</span>
							<button type="button" class="ghost" onclick={addString}>+ Add string</button>
						</header>
						{#if stringDrafts.length === 0}
							<p class="hint">No story strings — the planner has nothing scripted to steer toward.</p>
						{:else}
							<ul class="rows">
								{#each stringDrafts as _, index (index)}
									<li class="row">
										<input
											class="plan-string"
											aria-label={`Story string ${index + 1}`}
											value={stringDrafts[index]}
											oninput={(event) => (stringDrafts[index] = event.currentTarget.value)}
										/>
										<button
											type="button"
											class="danger"
											aria-label={`Remove story string ${index + 1}`}
											onclick={() => removeString(index)}>Remove</button
										>
									</li>
								{/each}
							</ul>
						{/if}
					</div>

					<div class="field">
						<span class="sub-head">Established</span>
						{#if plan.established.length === 0}
							<p class="hint">Nothing recorded as already true.</p>
						{:else}
							<ul class="view-list">
								{#each plan.established as line, index (index)}
									<li>{line}</li>
								{/each}
							</ul>
						{/if}
					</div>

					<div class="field">
						<header class="group-head">
							<span class="sub-head">Extras</span>
							{#if fieldDrafts.length > 0}
								<button
									type="button"
									class="ghost"
									onclick={addExtra}
									disabled={!nextExtraField}
								>
									+ Add extra
								</button>
							{/if}
						</header>
						{#if extraDrafts.length === 0}
							<p class="hint">
								{fieldDrafts.length > 0
									? 'No extras in this plan yet.'
									: 'No extras — add a field above and the plan can carry it.'}
							</p>
						{:else}
							<ul class="rows">
								{#each extraDrafts as draft, index (index)}
									<li class="row">
										<code class="extra-key" title={draft.key}>{fieldTitle(draft.key)}</code>
										{#if draft.type === 'boolean'}
											<label class="toggle">
												<input
													type="checkbox"
													checked={draft.value === 'true'}
													onchange={(event) =>
														(draft.value = event.currentTarget.checked ? 'true' : 'false')}
												/>
												<span>{draft.value === 'true' ? 'true' : 'false'}</span>
											</label>
										{:else if draft.type === 'number'}
											<input
												class="extra-value"
												type="number"
												aria-label="Extra value"
												value={draft.value}
												oninput={(event) => (draft.value = event.currentTarget.value)}
											/>
										{:else}
											<input
												class="extra-value"
												aria-label="Extra value"
												value={draft.value}
												oninput={(event) => (draft.value = event.currentTarget.value)}
											/>
										{/if}
										<button
											type="button"
											class="danger"
											aria-label={`Remove extra ${draft.key || index + 1}`}
											onclick={() => removeExtra(draft)}>Remove</button
										>
									</li>
								{/each}
							</ul>
						{/if}
					</div>

					<div class="actions">
						<span class="spacer"></span>
						<button
							type="button"
							class="ghost"
							onclick={discardPlan}
							disabled={busy || !planDirty}>Discard plan edits</button
						>
						<button
							type="button"
							class="primary"
							onclick={() => void savePlan()}
							disabled={busy || !planDirty}
						>
							{savingPlan ? 'Saving…' : 'Save plan'}
						</button>
					</div>
				{/if}
			</section>
		{/if}

		<div class="actions">
			<span class="spacer"></span>
			<button
				type="button"
				class="ghost"
				onclick={discardConfig}
				disabled={busy || !configDirty}>Discard config changes</button
			>
			<button
				type="button"
				class="primary"
				onclick={() => void saveConfig()}
				disabled={busy || !configDirty}
			>
				{savingConfig ? 'Saving…' : 'Save config'}
			</button>
		</div>
	{/if}
</div>

<style>
	.planner-pane {
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
	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.group-head h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.sub-head {
		font-size: var(--font-small);
		color: var(--text-dim);
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
		align-items: center;
		gap: 0.35rem;
		padding: 0.2rem 0.3rem;
		border: 1px solid transparent;
		border-radius: 6px;
	}
	.field-id {
		flex: 0 0 7rem;
	}
	.field-title {
		flex: 1 1 7rem;
		min-width: 0;
	}
	.field-hint {
		flex: 1 1 8rem;
		min-width: 0;
	}
	.cadence {
		width: 4.6rem;
	}
	.plan-string,
	.extra-value {
		flex: 1;
		min-width: 0;
	}
	.extra-key {
		flex: 0 0 8rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
		padding: 0.1rem 0.25rem;
		border: 1px solid var(--line);
		border-radius: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.view {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.view-row {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		font-size: var(--font-small);
	}
	.view-row > span {
		flex: 0 0 6.5rem;
		color: var(--text-dim);
	}
	.view-list {
		margin: 0;
		padding-left: 1.1rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.field,
	.field-inline {
		display: flex;
		gap: 0.3rem;
	}
	.field {
		flex-direction: column;
	}
	.field-inline {
		align-items: center;
		flex-wrap: wrap;
	}
	.field > span,
	.field-inline > span {
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	textarea {
		resize: vertical;
		min-height: 2.4rem;
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.spacer {
		flex: 1;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding-top: 0.5rem;
		border-top: 1px solid var(--line);
	}
	.hint {
		margin: 0.15rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.error {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	button:disabled {
		opacity: 0.55;
		cursor: default;
	}
	.primary {
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.primary:hover:not(:disabled) {
		background: var(--accent);
		color: var(--on-accent);
	}
	.ghost {
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text);
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
	}
	.danger {
		border: 1px solid var(--line);
		background: none;
		color: var(--text-muted);
	}
	.danger:hover:not(:disabled) {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
</style>
