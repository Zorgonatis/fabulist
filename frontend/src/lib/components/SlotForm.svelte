<script lang="ts">
	// Generic slot-driven form renderer (D51 §2.2) -----------------------------
	//
	// Contract (props-only, no fetching):
	//   slots    — the slot table for one kind (`ENTITY_SLOTS[kind]`).
	//   value    — the draft body object (a Record). SlotForm MUTATES it in place,
	//              one property per slot; the parent owns the object and must pass
	//              reactive state (`$state`) so edits re-render there. Swapping in
	//              a new object (e.g. selecting another entity) re-seeds the form.
	//   catalogs — parent-supplied idlist catalogs, `{ id, label }[]`, keyed by the
	//              slot's `source` ('books' | 'personas'). SlotForm does no
	//              fetching; a missing catalog renders an empty picker.
	//
	// Submission: the parent renders the submit button and calls `validate()`
	// (bound via `bind:this`). `validate()` sets the submit-attempt flag, renders
	// per-slot inline errors, and returns the FIRST visible message (e.g.
	// "Name is required") or `null` when valid. No silent coercion.
	//
	// Passthrough slots (`extensions`, `provenance`) are rendered read-only as a
	// JSON block and never contribute an editable input.
	//
	// 'absent stays absent' (D47) is a SAVE-PATH rule, not a form rule: for the
	// `idlist-single` control (assistant `actor`) SlotForm carries `''` for
	// "— none —"; the consuming pane omits the key on save when `''`.
	import type { SlotSpec } from '#lib/entitySlots';

	let {
		slots,
		value,
		catalogs = {}
	}: {
		slots: SlotSpec[];
		value: Record<string, unknown>;
		catalogs?: Partial<Record<'books' | 'personas', { id: string; label: string }[]>>;
	} = $props();

	// Unique per instance so multiple SlotForms on one page keep distinct ids.
	const uid = $props.id();

	/** True once a submit has been attempted (drives inline error visibility). */
	let submitted = $state(false);

	function linesToText(raw: unknown): string {
		return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string').join('\n') : '';
	}

	function seedLines(source: Record<string, unknown>): Record<string, string> {
		const next: Record<string, string> = {};
		for (const spec of slots) next[spec.key] = linesToText(source[spec.key]);
		return next;
	}

	/** Raw line text per `lines` slot, kept separate so typing is not normalized away.
	 * Seeded by the effect below; until then the template falls back to the joined
	 * value (which is what renders during SSR). */
	let lineDrafts = $state<Record<string, string>>({});
	let seededFrom: Record<string, unknown> | null = null;

	$effect(() => {
		if (value === seededFrom) return;
		seededFrom = value;
		lineDrafts = seedLines(value);
	});

	/** The editable text for a `lines` slot (buffer once seeded, else the joined value). */
	function lineText(spec: SlotSpec): string {
		return lineDrafts[spec.key] ?? linesToText(value[spec.key]);
	}

	function splitLines(text: string): string[] {
		return text
			.split('\n')
			.map((line) => line.trim())
			.filter((line) => line.length > 0);
	}

	function stringOf(raw: unknown): string {
		return typeof raw === 'string' ? raw : raw == null ? '' : String(raw);
	}

	function idsOf(raw: unknown): string[] {
		return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : [];
	}

	function jsonText(raw: unknown): string {
		return JSON.stringify(raw ?? {}, null, 2);
	}

	/** A 1:1 control id for `spec`. */
	function controlId(spec: SlotSpec): string {
		return `${uid}-${spec.key}`;
	}

	function helpId(spec: SlotSpec): string {
		return `${uid}-${spec.key}-help`;
	}

	function errorId(spec: SlotSpec): string {
		return `${uid}-${spec.key}-error`;
	}

	/** Both help and the (latent) error node, so the described-by set is stable. */
	function describedBy(spec: SlotSpec): string | undefined {
		const parts: string[] = [];
		if (spec.help) parts.push(helpId(spec));
		parts.push(errorId(spec));
		return parts.join(' ');
	}

	/** A required slot's emptiness, per the shape its control carries. */
	function isEmpty(spec: SlotSpec): boolean {
		const raw = value[spec.key];
		switch (spec.control) {
			case 'lines':
				return idsOf(raw).length === 0;
			case 'idlist':
				return idsOf(raw).length === 0;
			case 'number':
				return raw == null || raw === '' || Number.isNaN(Number(raw));
			case 'boolean':
				return raw !== true;
			default:
				return stringOf(raw).trim().length === 0;
		}
	}

	function errorFor(spec: SlotSpec): string | null {
		return spec.required && isEmpty(spec) ? `${spec.label} is required` : null;
	}

	/**
	 * Validate the draft: sets the submit-attempt flag (revealing inline errors)
	 * and returns the first visible message, or `null` when valid.
	 */
	export function validate(): string | null {
		submitted = true;
		for (const spec of slots) {
			const message = errorFor(spec);
			if (message) return message;
		}
		return null;
	}

	function setText(spec: SlotSpec, raw: string): void {
		value[spec.key] = raw;
	}

	function setLines(spec: SlotSpec, raw: string): void {
		lineDrafts[spec.key] = raw;
		value[spec.key] = splitLines(raw);
	}

	function setNumber(spec: SlotSpec, raw: string): void {
		if (raw.trim() === '') value[spec.key] = null;
		else value[spec.key] = Number(raw);
	}

	function setBoolean(spec: SlotSpec, checked: boolean): void {
		value[spec.key] = checked;
	}

	/**
	 * Checkbox ids in catalog order. Catalog rows come first (in catalog order);
	 * any checked id outside the catalog (should not occur) is appended so it is
	 * never dropped silently.
	 */
	function orderedIds(spec: SlotSpec, checked: string, on: boolean): string[] {
		const cat = catalogs[spec.source ?? 'books'] ?? [];
		const set = new Set(idsOf(value[spec.key]));
		if (on) set.add(checked);
		else set.delete(checked);
		const ordered = cat.filter((row) => set.has(row.id)).map((row) => row.id);
		for (const id of set) if (!ordered.includes(id)) ordered.push(id);
		return ordered;
	}

	function toggleId(spec: SlotSpec, id: string, on: boolean): void {
		value[spec.key] = orderedIds(spec, id, on);
	}

	function setSingle(spec: SlotSpec, raw: string): void {
		value[spec.key] = raw;
	}
</script>

<div class="slot-form">
	{#each slots as spec (`slot:${spec.key}`)}
		{#if spec.passthrough || spec.control === 'json'}
			<div class="slot passthrough">
				<span class="slot-label">{spec.label}</span>
				{#if spec.help}
					<p class="help" id={helpId(spec)}>{spec.help}</p>
				{/if}
				<pre class="json" aria-readonly="true" data-slot={spec.key}>{jsonText(value[spec.key])}</pre>
			</div>
		{:else if spec.control === 'textarea' || spec.control === 'lines'}
			<div class="slot"
				><label class="slot-label" for={controlId(spec)}>{spec.label}{#if spec.required}<span
						class="req"
						aria-hidden="true">*</span
					>{/if}</label
				>
				<textarea
					id={controlId(spec)}
					rows={spec.rows ?? 3}
					placeholder={spec.placeholder ?? ''}
					aria-describedby={describedBy(spec)}
					aria-invalid={submitted && errorFor(spec) ? 'true' : 'false'}
					value={spec.control === 'lines' ? lineText(spec) : stringOf(value[spec.key])}
					oninput={(event) =>
						spec.control === 'lines'
							? setLines(spec, event.currentTarget.value)
							: setText(spec, event.currentTarget.value)}
				></textarea>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{:else if spec.control === 'select'}
			<div class="slot"
				><label class="slot-label" for={controlId(spec)}>{spec.label}{#if spec.required}<span
						class="req"
						aria-hidden="true">*</span
					>{/if}</label
				>
				<select
					id={controlId(spec)}
					aria-describedby={describedBy(spec)}
					value={stringOf(value[spec.key])}
					onchange={(event) => setText(spec, event.currentTarget.value)}
				>
					{#each spec.options ?? [] as option (option)}
						<option value={option}>{option}</option>
					{/each}
				</select>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{:else if spec.control === 'idlist-single'}
			<div class="slot"
				><label class="slot-label" for={controlId(spec)}>{spec.label}{#if spec.required}<span
						class="req"
						aria-hidden="true">*</span
					>{/if}</label
				>
				<select
					id={controlId(spec)}
					aria-describedby={describedBy(spec)}
					value={stringOf(value[spec.key])}
					onchange={(event) => setSingle(spec, event.currentTarget.value)}
				>
					<option value="">— none —</option>
					{#each catalogs[spec.source ?? 'personas'] ?? [] as row (row.id)}
						<option value={row.id}>{row.label}</option>
					{/each}
				</select>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{:else if spec.control === 'idlist'}
			<fieldset class="slot" aria-describedby={describedBy(spec)}>
				<legend class="slot-label"
					>{spec.label}{#if spec.required}<span class="req" aria-hidden="true">*</span>{/if}</legend
				>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if (catalogs[spec.source ?? 'books'] ?? []).length === 0}
					<p class="empty">No catalog rows.</p>
				{:else}
					{#each catalogs[spec.source ?? 'books'] ?? [] as row (row.id)}
						<label class="check" for={`${uid}-${spec.key}-${row.id}`}>
							<input
								id={`${uid}-${spec.key}-${row.id}`}
								type="checkbox"
								checked={idsOf(value[spec.key]).includes(row.id)}
								onchange={(event) => toggleId(spec, row.id, event.currentTarget.checked)}
							/>
							<span>{row.label}</span>
						</label>
					{/each}
				{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</fieldset>
		{:else if spec.control === 'boolean'}
			<div class="slot"
				><label class="check" for={controlId(spec)}>
					<input
						id={controlId(spec)}
						type="checkbox"
						checked={value[spec.key] === true}
						aria-describedby={describedBy(spec)}
						onchange={(event) => setBoolean(spec, event.currentTarget.checked)}
					/>
					<span>{spec.label}{#if spec.required}<span class="req" aria-hidden="true">*</span>{/if}</span>
				</label>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{:else if spec.control === 'number'}
			<div class="slot"
				><label class="slot-label" for={controlId(spec)}>{spec.label}{#if spec.required}<span
						class="req"
						aria-hidden="true">*</span
					>{/if}</label
				>
				<input
					id={controlId(spec)}
					type="number"
					placeholder={spec.placeholder ?? ''}
					aria-describedby={describedBy(spec)}
					aria-invalid={submitted && errorFor(spec) ? 'true' : 'false'}
					value={value[spec.key] == null ? '' : String(value[spec.key])}
					oninput={(event) => setNumber(spec, event.currentTarget.value)}
				/>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{:else}
			<div class="slot"
				><label class="slot-label" for={controlId(spec)}>{spec.label}{#if spec.required}<span
						class="req"
						aria-hidden="true">*</span
					>{/if}</label
				>
				<input
					id={controlId(spec)}
					type="text"
					placeholder={spec.placeholder ?? ''}
					autocomplete="off"
					aria-describedby={describedBy(spec)}
					aria-invalid={submitted && errorFor(spec) ? 'true' : 'false'}
					value={stringOf(value[spec.key])}
					oninput={(event) => setText(spec, event.currentTarget.value)}
				/>
				{#if spec.help}<p class="help" id={helpId(spec)}>{spec.help}</p>{/if}
				{#if submitted && errorFor(spec)}<p class="error" id={errorId(spec)} role="alert"
						>{errorFor(spec)}</p
					>{/if}
			</div>
		{/if}
	{/each}
</div>

<style>
	.slot-form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.slot {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		margin: 0;
	}
	.slot-label {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.req {
		color: var(--danger-text);
		margin-left: 0.15rem;
	}
	.slot input,
	.slot textarea,
	.slot select {
		font: inherit;
		font-size: var(--font-ui);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.35rem 0.5rem;
		resize: vertical;
	}
	.slot input[aria-invalid='true'],
	.slot textarea[aria-invalid='true'] {
		border-color: var(--danger-text);
	}
	fieldset.slot {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.4rem 0.55rem 0.5rem;
		gap: 0.15rem;
	}
	fieldset.slot legend {
		font-size: var(--font-micro);
		color: var(--text-dim);
		padding: 0 0.25rem;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--font-ui);
		color: var(--text);
		padding: 0.15rem 0.1rem;
		border-radius: 6px;
		cursor: pointer;
	}
	.check:hover {
		background: var(--bg-hover);
	}
	.check input {
		font-size: var(--font-ui);
	}
	.help {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.error {
		margin: 0;
		font-size: var(--font-small);
		color: var(--danger-text);
	}
	.passthrough .slot-label {
		color: var(--text-dim);
	}
	.json {
		margin: 0;
		padding: 0.4rem 0.5rem;
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: var(--font-small);
		color: var(--text-muted);
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		overflow: auto;
		max-height: 14rem;
		white-space: pre-wrap;
	}
</style>
