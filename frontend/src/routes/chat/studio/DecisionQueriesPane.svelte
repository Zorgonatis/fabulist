<script lang="ts">
	// Decision-queries pane (R25) ---------------------------------------------
	//
	// The owner-curated question bank the per-turn decision gate asks (see
	// `#lib/api/decisionQueries`): the anti-slop phrase rows plus custom
	// questions. It is PROPS-ONLY (`StudioPaneProps`) — no page store, no page
	// fetch — so the studio shell can re-host it in a popout later (D51 §2.3).
	//
	// Two groups, one asset:
	//  · "Anti-slop phrases" — each row IS a noul question BOUND to its phrase:
	//    editing the phrase re-derives `id` (slugify) and the validated
	//    instruction wording, and the row always fires when the answer is true.
	//  · "Custom questions" — owner-written noul/choice questions with an
	//    instruction, criteria, threshold and polarity.
	//
	// Nothing is protected (D67): every row has an enable toggle and can be
	// removed; there is deliberately no lock UI.
	//
	// The numeric threshold is held as TEXT in the draft so a cleared or
	// half-typed value stays visible instead of being coerced, and the pane's
	// save-time check (`questionSetError`) reports it like any other bad row.
	// The API is the authority — its 400 reason is displayed verbatim.
	import { onMount, tick } from 'svelte';
	import {
		DEFAULT_THRESHOLD,
		getDecisionQueries,
		putDecisionQueries,
		questionSetError,
		slugifyPhrase,
		slopInstructions,
		type DecisionPolarity,
		type DecisionQuestion,
		type DecisionQuestionType
	} from '#lib/api/decisionQueries';

	let {
		focusId = null,
		startNew = false,
		requestSeq = null,
		onChanged,
		onDirty
	}: {
		focusId?: string | null;
		startNew?: boolean;
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
	} = $props();

	/** A wire question with the threshold kept as text (see the header note). */
	interface QuestionDraft extends Omit<DecisionQuestion, 'threshold'> {
		threshold: string;
	}

	let drafts = $state<QuestionDraft[]>([]);
	/** True while the asset is empty and the engine's built-in default is shown. */
	let seeded = $state(false);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	/** Row highlighted by a host request (`focusId`). */
	let focusedId = $state<string | null>(null);
	/** JSON snapshot of the loaded/saved draft set; drift means unsaved edits. */
	let baseline = $state('');

	const slopRows = $derived(drafts.filter((row) => row.source === 'slop'));
	const customRows = $derived(drafts.filter((row) => row.source === 'custom'));
	const dirty = $derived(baseline !== '' && JSON.stringify(drafts) !== baseline);

	$effect(() => {
		onDirty?.(dirty);
	});

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** Draft text -> the number the wire carries; blank/garbage -> NaN (rejected). */
	function thresholdNumber(text: string): number {
		const trimmed = text.trim();
		return trimmed === '' ? NaN : Number(trimmed);
	}

	function toDraft(question: DecisionQuestion): QuestionDraft {
		return { ...question, criteria: { ...question.criteria }, threshold: String(question.threshold) };
	}

	/** Draft -> wire. A slop row's polarity and phrase binding are structural. */
	function toWire(draft: QuestionDraft): DecisionQuestion {
		const criteria: Record<string, string> = {};
		for (const [key, value] of Object.entries(draft.criteria)) {
			// A blank description is UNSET, not a criterion: pruning it here keeps a
			// cleared option visible while the owner types (the row is keyed by its
			// label, so deleting the key would unmount the input under the caret).
			if (value.trim() !== '') criteria[key] = value;
		}
		const question: DecisionQuestion = {
			id: draft.id,
			source: draft.source,
			type: draft.type,
			instructions: draft.instructions,
			criteria,
			threshold: thresholdNumber(draft.threshold),
			enabled: draft.enabled,
			polarity: draft.source === 'slop' ? 'true_is_defect' : draft.polarity
		};
		if (draft.source === 'slop') question.phrase = draft.phrase ?? '';
		// `section` names the GATE that consumes the row (W9.22 §4a). The bank is PUT
		// whole, so dropping it here would erase the proposal rows' routing on the
		// next save — the per-turn gate would start asking them and the proposal gate
		// would silently fall back to its code defaults.
		if (draft.section) question.section = draft.section;
		return question;
	}

	function replaceFromCatalog(questions: readonly DecisionQuestion[], isSeeded: boolean): void {
		drafts = questions.map(toDraft);
		seeded = isSeeded;
		baseline = JSON.stringify(drafts);
		focusedId = null;
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try {
			const catalog = await getDecisionQueries();
			replaceFromCatalog(catalog.questions, catalog.seeded);
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void load();
	});

	/** Throw the draft away and go back to the last loaded/saved set. */
	function discard(): void {
		drafts = JSON.parse(baseline) as QuestionDraft[];
		focusedId = null;
		error = '';
	}

	async function save(): Promise<void> {
		if (busy) return;
		const problem = questionSetError(drafts.map(toWire));
		if (problem) {
			error = problem;
			return;
		}
		busy = true;
		error = '';
		try {
			const catalog = await putDecisionQueries(drafts.map(toWire));
			replaceFromCatalog(catalog.questions, catalog.seeded);
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	// --- row editing -----------------------------------------------------------
	//
	// Rows are rendered with positional `{#each}` keys on purpose: a slop row's
	// identity is its phrase (and a custom row's is its id), both of which the
	// owner edits in place — keying on them would re-mount the focused input on
	// every keystroke.

	function newSlopRow(): QuestionDraft {
		return {
			id: '',
			source: 'slop',
			type: 'noul',
			phrase: '',
			instructions: '',
			criteria: {},
			threshold: String(DEFAULT_THRESHOLD),
			enabled: true,
			polarity: 'true_is_defect'
		};
	}

	/** A phrase row's id + instruction are DERIVED; `decision.py` owns the wording. */
	function setPhrase(row: QuestionDraft, value: string): void {
		const phrase = value.trim();
		row.phrase = value;
		row.id = slugifyPhrase(phrase);
		row.instructions = phrase ? slopInstructions(phrase) : '';
	}

	/** First unused `question_N` id for a new custom row. */
	function nextCustomId(): string {
		const used = new Set(drafts.map((row) => row.id));
		let index = 1;
		while (used.has(`question_${index}`)) index += 1;
		return `question_${index}`;
	}

	function newCustomRow(): QuestionDraft {
		return {
			id: nextCustomId(),
			source: 'custom',
			type: 'noul',
			instructions: '',
			criteria: {},
			threshold: String(DEFAULT_THRESHOLD),
			enabled: true,
			polarity: 'true_is_defect'
		};
	}

	async function addSlopRow(): Promise<void> {
		drafts = [...drafts, newSlopRow()];
		await tick();
		if (typeof document === 'undefined') return;
		document.getElementById(`slop-phrase-${slopRows.length - 1}`)?.focus();
	}

	async function addCustomRow(): Promise<void> {
		drafts = [...drafts, newCustomRow()];
		await tick();
		if (typeof document === 'undefined') return;
		document.getElementById(`custom-id-${customRows.length - 1}`)?.focus();
	}

	function removeRow(target: QuestionDraft): void {
		const index = drafts.findIndex((row) => row === target);
		if (index >= 0) drafts.splice(index, 1);
		if (focusedId === target.id) focusedId = null;
	}

	/** Sanitize a custom id as it is typed (`[a-z0-9_]`), then re-seat the caret. */
	function onIdInput(row: QuestionDraft, input: HTMLInputElement): void {
		row.id = input.value.toLowerCase().replace(/[^a-z0-9_]+/g, '_');
		input.value = row.id;
	}

	/** Tidy edge underscores on blur (mirrors `decision.slugify`). */
	function tidyId(row: QuestionDraft): void {
		row.id = slugifyPhrase(row.id);
	}

	/**
	 * Switch question type. The criteria SHAPE changes with it, so the old
	 * entries are replaced by an empty rubric of the new shape (this is a
	 * deliberate, visible reset, not a silent merge).
	 */
	function setType(row: QuestionDraft, type: DecisionQuestionType): void {
		if (row.type === type) return;
		row.type = type;
		row.criteria = type === 'noul' ? {} : { option_1: '' };
	}

	/** Criteria text edit. A blank value is pruned at save time (`toWire`). */
	function setCriterion(row: QuestionDraft, key: string, value: string): void {
		row.criteria[key] = value;
	}

	/** First unused `option_N` label for a choice question. */
	function addCriterion(row: QuestionDraft): void {
		let index = 1;
		while (`option_${index}` in row.criteria) index += 1;
		row.criteria[`option_${index}`] = '';
	}

	function removeCriterion(row: QuestionDraft, key: string): void {
		delete row.criteria[key];
	}

	/**
	 * Rename one choice option. Returns the key the row now has: a rejected
	 * rename (blank/duplicate) keeps the old key so the input can be re-seated.
	 */
	function renameCriterion(row: QuestionDraft, from: string, to: string): string {
		const key = to.trim();
		if (!key || key === from || key in row.criteria) return from;
		const next: Record<string, string> = {};
		for (const [label, description] of Object.entries(row.criteria)) {
			next[label === from ? key : label] = description;
		}
		row.criteria = next;
		return key;
	}

	function setPolarity(row: QuestionDraft, polarity: DecisionPolarity): void {
		row.polarity = polarity;
	}

	// --- host requests (`focusId` scrolls to a row, `startNew` adds one) ------
	// Keyed on `requestSeq` when present so a repeated door click re-fires; a
	// plain prop change still applies once per value (standalone pane).
	let appliedKey: string | null = null;

	$effect(() => {
		if (loading) return;
		const key =
			requestSeq != null
				? `seq:${requestSeq}`
				: startNew
					? 'new'
					: focusId
						? `focus:${focusId}`
						: null;
		if (!key || key === appliedKey) return;
		if (startNew) {
			appliedKey = key;
			void addCustomRow();
			return;
		}
		if (!focusId) return;
		const row = drafts.find((candidate) => candidate.id === focusId);
		if (!row) return;
		appliedKey = key;
		focusedId = row.id;
		if (typeof document === 'undefined') return;
		document.getElementById(`decision-question-${row.id}`)?.scrollIntoView({ block: 'nearest' });
	});
</script>

<div class="queries-pane">
	<header class="pane-head">
		<h2 class="pane-title">Decision queries</h2>
		<span class="pane-meta"
			>{slopRows.length} phrases · {customRows.length} custom</span
		>
	</header>

	<p class="hint">
		The checks the engine runs on every turn before the critic reviews the prose. A check that
		fires is handed to the critic as a specific issue, so add the ones you keep catching yourself.
		Every row here can be turned off or removed.
	</p>

	{#if seeded}
		<p class="notice">
			Showing the built-in set (18 anti-slop phrases plus the movement question). It edits like
			any other asset — the first save stores it as yours.
		</p>
	{/if}

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading decision queries…</p>
	{:else}
		<section class="group" aria-labelledby="slop-heading">
			<header class="group-head">
				<h3 id="slop-heading">Anti-slop phrases</h3>
				<button type="button" class="ghost" onclick={() => void addSlopRow()}>+ Add phrase</button>
			</header>
			<p class="hint">
				Each row is a yes/no question — “does the text contain this phrase, or a trivial
				variant of it?” — and fires when the answer is TRUE. The id is the phrase slug the
				critic's flag carries.
			</p>
			{#if slopRows.length === 0}
				<p class="hint">No phrase rows — the anti-slop check is off for every phrase.</p>
			{:else}
				<ul class="rows">
					{#each slopRows as row, index (index)}
						<li
							class="row"
							class:focused={focusedId !== null && focusedId === row.id}
							class:disabled={!row.enabled}
							id={row.id ? `decision-question-${row.id}` : undefined}
						>
							<input
								type="checkbox"
								aria-label="Enabled"
								bind:checked={row.enabled}
							/>
							<input
								class="phrase"
								id={`slop-phrase-${index}`}
								value={row.phrase}
								placeholder="phrase to catch"
								oninput={(event) => setPhrase(row, event.currentTarget.value)}
							/>
							<code class="question-id" title="Question id used in the turn log"
								>{row.id || '—'}</code
							>
							<button type="button" class="danger" onclick={() => removeRow(row)}>
								Remove
							</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<section class="group" aria-labelledby="custom-heading">
			<header class="group-head">
				<h3 id="custom-heading">Custom questions</h3>
				<button type="button" class="ghost" onclick={() => void addCustomRow()}
					>+ Add question</button
				>
			</header>
			<p class="hint">
				Any check the decision model can answer in one pass. “Defect when” decides which
				answer becomes the critic's issue: slop-style checks fire on yes, movement-style
				checks fire on no.
			</p>
			{#if customRows.length === 0}
				<p class="hint">No custom questions yet.</p>
			{:else}
				<div class="cards">
					{#each customRows as row, index (index)}
						<article
							class="card"
							class:focused={focusedId !== null && focusedId === row.id}
							class:disabled={!row.enabled}
							id={row.id ? `decision-question-${row.id}` : undefined}
						>
							<header class="card-head">
								<label class="toggle">
									<input type="checkbox" bind:checked={row.enabled} />
									<span>Enabled</span>
								</label>
								<label class="field-inline">
									<span>id</span>
									<input
										class="question-id-input"
										id={`custom-id-${index}`}
										value={row.id}
										oninput={(event) => onIdInput(row, event.currentTarget)}
										onblur={() => tidyId(row)}
									/>
								</label>
								<label class="field-inline">
									<span>type</span>
									<select
										value={row.type}
										onchange={(event) =>
											setType(row, event.currentTarget.value as DecisionQuestionType)}
									>
										<option value="noul">noul (yes/no)</option>
										<option value="choice">choice</option>
									</select>
								</label>
								<span class="spacer"></span>
								<button type="button" class="danger" onclick={() => removeRow(row)}>
									Remove
								</button>
							</header>

							<label class="field">
								<span>Instruction</span>
								<textarea
									rows="2"
									value={row.instructions}
									placeholder="The question the decision model answers"
									oninput={(event) => (row.instructions = event.currentTarget.value)}
								></textarea>
							</label>

							<div class="field">
								<span>Criteria</span>
								{#if row.type === 'noul'}
									<div class="criterion">
										<code class="crit-key">true</code>
										<input
											value={row.criteria['true'] ?? ''}
											placeholder="what “yes” means (optional)"
											oninput={(event) =>
												setCriterion(row, 'true', event.currentTarget.value)}
										/>
									</div>
									<div class="criterion">
										<code class="crit-key">false</code>
										<input
											value={row.criteria['false'] ?? ''}
											placeholder="what “no” means (optional)"
											oninput={(event) =>
												setCriterion(row, 'false', event.currentTarget.value)}
										/>
									</div>
									<p class="hint">
										Optional — fill both sides, or leave both empty; a plain yes/no check
										needs no rubric.
									</p>
								{:else}
									{#each Object.entries(row.criteria) as [key, description] (key)}
										<div class="criterion">
											<input
												class="crit-key-input"
												value={key}
												aria-label="Option label"
												onchange={(event) => {
													const input = event.currentTarget;
													input.value = renameCriterion(row, key, input.value);
												}}
											/>
											<input
												value={description}
												placeholder="when this option applies"
												oninput={(event) =>
													setCriterion(row, key, event.currentTarget.value)}
											/>
											<button
												type="button"
												class="danger"
												aria-label={`Remove option ${key}`}
												onclick={() => removeCriterion(row, key)}>×</button
											>
										</div>
									{/each}
									<button type="button" class="ghost" onclick={() => addCriterion(row)}>
										+ Add option
									</button>
								{/if}
							</div>

							<div class="card-foot">
								<label class="field-inline">
									<span>Defect when</span>
									<select
										value={row.polarity}
										onchange={(event) =>
											setPolarity(row, event.currentTarget.value as DecisionPolarity)}
									>
										<option value="true_is_defect">the answer is true</option>
										<option value="false_is_defect">the answer is false</option>
									</select>
								</label>
								<label class="field-inline">
									<span>Threshold</span>
									<input
										class="threshold"
										type="number"
										min="0"
										max="1"
										step="0.01"
										value={row.threshold}
										oninput={(event) => (row.threshold = event.currentTarget.value)}
									/>
								</label>
								<span class="hint">0..1 confidence needed to fire</span>
							</div>
						</article>
					{/each}
				</div>
			{/if}
		</section>
	{/if}

	<div class="actions">
		<span class="spacer"></span>
		<button type="button" class="ghost" onclick={discard} disabled={busy || !dirty}>
			Discard changes
		</button>
		<button
			type="button"
			class="primary"
			onclick={() => void save()}
			disabled={busy || !(dirty || seeded)}
		>
			{busy ? 'Saving…' : 'Save queries'}
		</button>
	</div>
</div>

<style>
	.queries-pane {
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
	.group-head h3 {
		margin: 0;
		font-size: var(--font-ui);
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
		gap: 0.4rem;
		padding: 0.2rem 0.3rem;
		border: 1px solid transparent;
		border-radius: 6px;
	}
	.row.focused,
	.card.focused {
		border-color: var(--accent-line);
		background: var(--accent-soft);
	}
	.row.disabled,
	.card.disabled {
		opacity: 0.6;
	}
	.phrase {
		flex: 1;
		min-width: 0;
	}
	.question-id {
		flex: 0 0 auto;
		font-size: var(--font-micro);
		color: var(--text-dim);
		padding: 0.1rem 0.25rem;
		border: 1px solid var(--line);
		border-radius: 4px;
	}
	.cards {
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 0.45rem;
		padding: 0.5rem 0.55rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.card-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.card-foot {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		flex-wrap: wrap;
	}
	.criterion {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	.criterion > input:not(.crit-key-input) {
		flex: 1;
		min-width: 0;
	}
	.crit-key {
		flex: 0 0 3rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.crit-key-input {
		flex: 0 0 7rem;
		font-size: var(--font-small);
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
	.threshold {
		width: 4.6rem;
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
	.notice {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
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
