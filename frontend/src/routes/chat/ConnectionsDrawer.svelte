<script lang="ts">
	// Connections editor (PLAN R5): the first UI for the per-agent connection
	// registry. Defines each connection (endpoint/model/sampling/extra params),
	// discovers the endpoint's models server-side, and saves with a ping.
	//
	// This component owns its draft + list state and the API calls; the parent
	// owns only whether the drawer is open (`onClose`). Every path is absolute
	// `API_BASE` through `#lib/api` (AGENTS.md gotcha: relative `/api/...` 404s).
	//
	// Key storage: the credential field on this form is an API **key** (paste it,
	// type=password) — never an env-var name. A literal key is draft state: it can
	// be typed at ANY time (before the connection is saved), it is used for the
	// model probe, and `save` sends it as `api_key`. The engine stores it in the
	// project `.env` (mode 600), keeps only the derived `api_key_env` NAME, and
	// answers with that name plus `api_key_set` — the secret never comes back, so
	// the field is cleared after a save and only the resolved name is shown.
	// An env-var NAME stays available under the Advanced disclosure for users who
	// resolve their key that way; it keeps working exactly as before.
	//
	// Numeric fields (timeout + samplers) are `number | null` because Svelte's
	// `bind:value` on `<input type="number">` coerces to a number (`null` when
	// empty); treating them as strings is what silently killed every save.
	//
	// W9.25 owner-steered surface: "Context Size" (the owner-DECLARED prompt +
	// response ceiling — never probed), "Response Length" (the generation cap,
	// `sampling.max_tokens`), the two transport toggles "Thinking"/"Streaming"
	// (checked = no instruction at all, so a checked box writes `null`, NEVER
	// `true`; unchecking writes `false`), the standard sampler vocabulary, and
	// the agents section rendered as TYPE/TASK groups from the engine's `slots`
	// payload with its resolution provenance — ONE row per resolved ROLE (issue
	// #48): a legacy twin or the item slot serving a task-less type is an alias
	// on that row, never a second row, and an alias that loses precedence is
	// marked inert by the ENGINE (`row.inert`), not re-derived here. No
	// llama.cpp-specific knob appears here — `extra_body` stays the escape hatch
	// for server-specific params.
	//
	// "Reasoning effort" (issue #26 R5) is the provider's own top-level
	// `reasoning_effort` body field. It is a per-connection SELECT: the wire value
	// is the raw enum string and "Not set" is `null`, which sends the parameter
	// NOWHERE (that is the default, so an untouched connection is unchanged). A
	// stored value outside the documented vocabulary is never dropped — it
	// renders as its own appended raw option.
	import { onMount } from 'svelte';
	import type { ConnectionEntry, ConnectionsCatalog, ConnectionWrite, ReembedStatus } from '#lib/api';
	import {
		createConnection,
		deleteConnection,
		discoverModels,
		getReembedStatus,
		listConnections,
		resetMemory,
		setAgentConnection,
		startReembed,
		updateConnection
	} from '#lib/api';
	// One entry of the connections client module is imported directly because the
	// `#lib/api` barrel is outside this lane (same pattern as `#lib/api/hud`).
	import type { ConnectionSlot, SlotInertBinding } from '#lib/api/connections';
	// Issue #57: the ComfyUI endpoint is a persisted STRING setting, so this
	// section writes it through the settings door and reads the EFFECTIVE value
	// back from the imagegen config endpoint (the barrel is outside this lane).
	import { COMFY_BASE_URL_KEY, patchSettingValues } from '#lib/api/settings';
	import { getImageGenConfig } from '#lib/api/imagegen';
	import { register } from '#lib/uiLayers';
	import ImageGenPane from './studio/ImageGenPane.svelte';

	let { onClose }: { onClose: () => void } = $props();

	/** One editable extra-body row (dotted keys nest, values parse as JSON). */
	interface ExtraRow {
		key: string;
		value: string;
	}

	/**
	 * The connection form's editable fields. The numeric ones hold real numbers
	 * (`null` = unset) because `<input type="number">` binding coerces.
	 *
	 * The two toggles hold the EFFECTIVE state the owner chose (checked = on);
	 * the wire shape is the inverse of `false` (`save` writes `null` for a
	 * checked box — absence IS the on position). "Return thinking" has THREE
	 * states, so it holds the wire value's own vocabulary (`auto`/`on`/`off`) and
	 * maps to `null`/`true`/`false`.
	 */
	interface Draft {
		name: string;
		base_url: string;
		model: string;
		timeout: number | null;
		/** "Context Size": owner-declared tokens (`null` = unknown). */
		context_window: number | null;
		note: string;
		thinking: boolean;
		streaming: boolean;
		/** "Return thinking": `auto` (null) echoes whatever reasoning arrived. */
		return_thinking: 'auto' | 'on' | 'off';
		/**
		 * "Reasoning effort": a documented enum string, or `''` for NOT SET (the
		 * parameter is not sent at all — `null` on the wire). The empty string is
		 * the select's not-set sentinel, NOT a value: `none` is a real effort.
		 */
		reasoning_effort: string;
		temperature: number | null;
		/** "Response Length": the generation cap (`sampling.max_tokens`). */
		max_tokens: number | null;
		top_p: number | null;
		top_k: number | null;
		repetition_penalty: number | null;
		frequency_penalty: number | null;
		presence_penalty: number | null;
		seed: number | null;
		/** "Stop Sequences": one per line, written as `sampling.stop: string[]`. */
		stop: string;
		api_key_env: string;
		extra: ExtraRow[];
	}

	/** One rendered group of slot rows: a type header plus the rows under it. */
	interface SlotGroup {
		key: string;
		label: string;
		rows: ConnectionSlot[];
	}

	/**
	 * The "Reasoning effort" vocabulary, ASCENDING, not-set first.
	 *
	 * `''` is the NOT-SET option (the parameter is never sent); every other entry
	 * is a raw wire value the engine's `REASONING_EFFORT_VALUES` accepts. The
	 * labels are semantic — the wire value is the string on the right.
	 */
	const REASONING_EFFORT_OPTIONS: { value: string; label: string }[] = [
		{ value: '', label: 'Not set — send no reasoning effort' },
		{ value: 'none', label: 'none' },
		{ value: 'minimal', label: 'minimal' },
		{ value: 'low', label: 'low' },
		{ value: 'medium', label: 'medium' },
		{ value: 'high', label: 'high' },
		{ value: 'xhigh', label: 'xhigh' },
		{ value: 'max', label: 'max' }
	];

	/** True when a stored effort is a real value the option list does not cover. */
	function isUnlistedEffort(value: string): boolean {
		return value !== '' && !REASONING_EFFORT_OPTIONS.some((option) => option.value === value);
	}

	let catalog = $state<ConnectionsCatalog | null>(null);
	let loading = $state(true);
	let busy = $state(false);
	let error = $state('');
	/** Non-fatal save/ping feedback (a saved connection that failed its ping). */
	let status = $state('');

	/**
	 * The literal key the owner pasted, if any. Draft state only: it is sent to the
	 * engine on a probe (transient) and on save (`api_key`), and the browser keeps
	 * no copy once it has been stored (the engine answers with the env NAME).
	 */
	let keyDraft = $state('');
	/** The optional Image Prompter section — collapsed (and unmounted) until opened. */
	let imageGenOpen = $state(false);

	/**
	 * The ComfyUI endpoint (issue #57). `comfyUrl` is the EFFECTIVE URL the engine
	 * reports (the setting, else the shipped default); `comfyDraft` is the field
	 * text. Saving writes the settings value (`comfy.base_url`); an empty field
	 * CLEARS the override, putting the default back. The section then re-reads the
	 * config, so what it shows is what the engine would submit to.
	 */
	let comfyUrl = $state('');
	let comfyDraft = $state('');
	let comfyBusy = $state(false);
	let comfyNote = $state('');

	/** Open form draft, or null when the list is showing. */
	let draft = $state<Draft | null>(null);
	/** Name of the connection being edited (`null` = creating a new one). */
	let editingName = $state<string | null>(null);

	/** Discovered model ids for the current form (empty until "Discover"). */
	let models = $state<string[]>([]);
	let discovering = $state(false);
	let discoveryNote = $state('');

	/**
	 * The pending embedder switch (confirm gate). Switching the embedder wipes
	 * the memory index — no data is retained, and the index re-creates at the
	 * new embedder's dims on the next write. `null` = no gate open.
	 */
	let memoryGate = $state<{ slot: string; connection: string; previous: string } | null>(null);
	let switching = $state(false);
	/** Live re-embed job status (polled while running). */
	let reembed = $state<ReembedStatus | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;

	const connectionNames = $derived(
		catalog ? Object.keys(catalog.connections).sort((a, b) => a.localeCompare(b)) : []
	);
	const slotRows = $derived(catalog?.slots ?? []);
	const slotGroups = $derived(groupSlots(slotRows));

	// --- type-aware form (Q2 ruling) -----------------------------------------

	/**
	 * The slot rows that resolve to the connection being edited.
	 *
	 * The `slots` payload is the ONE place the drawer learns what a connection is
	 * for (`row.type`, null only for authored custom keys), so the form derives
	 * its mode from there instead of guessing from the connection's own fields. A
	 * new connection resolves to nothing yet — that IS the unbound case.
	 */
	const boundRows = $derived.by((): ConnectionSlot[] => {
		if (!editingName) return [];
		return slotRows.filter((row) => row.connection === editingName);
	});

	/** The distinct owning types of {@link boundRows} (`embedding`, `decision`, …). */
	const boundTypes = $derived(
		[...new Set(boundRows.map((row) => row.type).filter((type) => type !== null))].sort()
	);

	/**
	 * Which fields the bound slot(s) actually use. One declared type decides the
	 * form; several types — or none — keep the full form, because a connection is
	 * defined once and may legitimately serve more than one slot.
	 */
	const fieldMode = $derived.by((): 'full' | 'embedding' | 'decision' => {
		if (boundTypes.length !== 1) return 'full';
		const type = boundTypes[0];
		return type === 'embedding' || type === 'decision' ? type : 'full';
	});

	/** The Context Size label; the embedding path never uses the value. */
	const contextLabel = $derived(
		fieldMode === 'embedding'
			? 'Context Size (not used for embeddings)'
			: 'Context Size'
	);

	/** The line above the fields: which mode the form is in and why. */
	const modeNote = $derived.by((): string => {
		if (fieldMode === 'embedding') {
			return 'This connection runs the memory embedder — responses are vectors, so length and sampler settings are never sent.';
		}
		if (fieldMode === 'decision') {
			return 'This connection answers yes/no checks — keep it small (256–512 tokens is plenty).';
		}
		if (!editingName) {
			return 'No agent uses this connection yet — the full form is shown. The agent you assign it to decides which fields matter.';
		}
		if (boundRows.length === 0) {
			return 'No agent uses this connection yet — the full form is shown.';
		}
		const labels = [...new Set(boundRows.map((row) => row.label))].join(', ');
		return `This connection serves ${labels} — the full form is shown.`;
	});

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	onMount(() => {
		void load().then(() => void refreshReembed());
		// Escape is owned by the uiLayers surface stack (D51 §2.4): this drawer is a
		// surface while mounted, so only the TOP surface closes.
		const unregister = register('connections', () => onClose());
		return () => {
			stopPolling();
			unregister();
		};
	});

	async function load(): Promise<void> {
		loading = true;
		try {
			catalog = await listConnections();
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	/** Agent names bound to a connection (read-only view of the registry map). */
	function boundAgents(name: string): string[] {
		if (!catalog) return [];
		return Object.entries(catalog.agents)
			.filter(([, connection]) => connection === name)
			.map(([agent]) => agent)
			.sort();
	}

	/**
	 * Display label for a raw `agents` key: its resolved ROLE's label when known.
	 *
	 * The registry map keeps the authored spelling (``embedder``), while the slot
	 * view carries ONE row per role (``Embedding``). Printing the raw key here
	 * would reintroduce the alias on the connection card — the other half of the
	 * issue #48 confusion — so the card names the role.
	 */
	function slotDisplayLabel(agent: string): string {
		const row = slotRows.find(
			(candidate) => candidate.slot === agent || candidate.aliases.includes(agent)
		);
		return row?.label ?? agent;
	}

	/** The roles bound to one connection, deduped and display-named. */
	function boundRoleLabels(name: string): string[] {
		return [...new Set(boundAgents(name).map(slotDisplayLabel))];
	}

	function sortedEntries(): ConnectionEntry[] {
		return connectionNames.map((name) => catalog!.connections[name]);
	}

	// --- the slot view (type / task / per-item rows, W9.25 §3.6) --------------

	/**
	 * Slice the engine's ordered slot rows into type-headed groups.
	 *
	 * The payload is ALREADY ordered and grouped (type row, its task rows, its
	 * per-item rows) and already carries ONE row per resolved ROLE — a legacy
	 * twin (`narrator`) or the item slot serving a task-less type (`embedder`) is
	 * an alias on that row, never a second row (issue #48). So this only inserts
	 * headers: the hierarchy keeps living in `connections/taxonomy.py`.
	 * Authored custom keys arrive with `type: null` ("per item as today") and
	 * collect under one trailing group.
	 */
	function groupSlots(rows: ConnectionSlot[]): SlotGroup[] {
		const groups: SlotGroup[] = [];
		for (const row of rows) {
			// `group` is the engine's own section name: a row's type, or a declared
			// group of its own (`npc` -> "Npc lanes", issue #55c). A custom key (no
			// type, no group) collects in the trailing "Per item" group.
			const key = row.group ?? row.type ?? 'items';
			let group = groups.find((candidate) => candidate.key === key);
			if (!group) {
				// A group's first row IS its heading: a type group reads the TYPE's
				// label ("Completion"), a lane group its own section name ("Npc lanes").
				group = {
					key,
					label: row.group && row.group !== row.type ? row.group : row.type ? row.label : 'Per item',
					rows: []
				};
				groups.push(group);
			}
			group.rows.push(row);
		}
		// Display order (owner, 2026-10-23): specific before general. Task slots
		// first, then per-item slots, and the type row LAST — it is the fallback
		// every unset slot resolves through, so it must not read as the first
		// thing to set. The payload order (type → tasks → items) is unchanged;
		// this is presentation only, and Array.sort is stable for equal ranks.
		for (const g of groups) {
			g.rows.sort((a, b) => rankSlotRow(a) - rankSlotRow(b));
		}
		return groups;
	}

	/** 0 = the type's fallback row, 1 = task slot, 2 = per-item slot. */
	function rankSlotRow(row: ConnectionSlot): number {
		if (row.type && row.slot === row.type) return 0;
		return row.task ? 1 : 2;
	}

	/** Load the ComfyUI endpoint the engine would submit to (issue #57). */
	async function loadComfyUrl(): Promise<void> {
		try {
			const config = await getImageGenConfig();
			comfyUrl = config.base_url;
			comfyDraft = config.base_url;
		} catch (err) {
			comfyNote = `Could not read the ComfyUI endpoint: ${message(err)}`;
		}
	}

	/** Open/close the Image Prompter section, loading the endpoint on first open. */
	function toggleImageGenSection(): void {
		imageGenOpen = !imageGenOpen;
		if (imageGenOpen && !comfyUrl) void loadComfyUrl();
	}

	/**
	 * Save the endpoint into the settings fold (`comfy.base_url`).
	 *
	 * An empty field sends `""`, which the engine folds as "no override" — the
	 * shipped default applies again. The engine validates the URL (a scheme typo
	 * is a 422 carrying the reason), and the section re-reads the EFFECTIVE value
	 * after the write, so the field can never show a URL the client is not using.
	 */
	async function saveComfyUrl(): Promise<void> {
		if (comfyBusy) return;
		comfyBusy = true;
		comfyNote = '';
		try {
			await patchSettingValues({ [COMFY_BASE_URL_KEY]: comfyDraft.trim() });
			await loadComfyUrl();
			comfyNote = `Saved. The Image Prompter submits to ${comfyUrl}.`;
		} catch (err) {
			comfyNote = message(err);
		} finally {
			comfyBusy = false;
		}
	}

	/** The row's label inside its group; a type row reads as the group default. */
	function rowLabel(row: ConnectionSlot, group: SlotGroup): string {
		// Only a type with rows DEPENDING on it is a "default"; a single-row group
		// (`Embedding`, whose row IS the embedder role) keeps its own name.
		if (row.type === row.slot && group.rows.length > 1) return 'Type default';
		return row.label;
	}

	/** Human form of the payload's resolution provenance. */
	function viaLabel(row: ConnectionSlot): string {
		if (!row.via) return 'unresolved';
		if (row.via === 'authored') return 'set here';
		const [kind, key] = row.via.split(':', 2);
		if (kind === 'legacy') return `via legacy ${key}`;
		// A task-less TYPE served by the item slot declared under it
		// (`embedding` -> `item:embedder`): the item is what actually runs.
		if (kind === 'item') return `via ${key} slot`;
		return key.includes('.') ? `via task ${key}` : 'via type default';
	}

	/**
	 * Authored spellings of this role that LOST precedence (engine-computed).
	 *
	 * Precedence lives in `taxonomy.py`, so the drawer never re-derives it: the
	 * payload's `inert` list is rendered as-is. Marking these is what keeps a
	 * conflicting pair visible instead of silently disappearing (issue #48).
	 */
	function inertBindings(row: ConnectionSlot): SlotInertBinding[] {
		return row.inert;
	}

	/** True when an inert spelling points at a DIFFERENT connection (a conflict). */
	function isConflict(row: ConnectionSlot, entry: SlotInertBinding): boolean {
		return row.connection !== null && entry.connection !== row.connection;
	}

	// --- draft <-> wire conversions -----------------------------------------

	/** JSON-parse a value when possible, else keep the raw string. */
	function parseValue(text: string): unknown {
		const trimmed = text.trim();
		if (trimmed === '') return '';
		try {
			return JSON.parse(trimmed);
		} catch {
			return text;
		}
	}

	/** Assign `value` at a dotted path, creating intermediate objects. */
	function setDeep(target: Record<string, unknown>, path: string, value: unknown): void {
		const parts = path.split('.').filter(Boolean);
		if (parts.length === 0) return;
		let node = target;
		for (let i = 0; i < parts.length - 1; i++) {
			const part = parts[i];
			const existing = node[part];
			if (existing && typeof existing === 'object' && !Array.isArray(existing)) {
				node = existing as Record<string, unknown>;
			} else {
				const child: Record<string, unknown> = {};
				node[part] = child;
				node = child;
			}
		}
		node[parts[parts.length - 1]] = value;
	}

	/** Build the extra_body object from the key/value rows. */
	function buildExtraBody(rows: ExtraRow[]): Record<string, unknown> {
		const out: Record<string, unknown> = {};
		for (const row of rows) {
			const key = row.key.trim();
			if (!key) continue;
			setDeep(out, key, parseValue(row.value));
		}
		return out;
	}

	/** Flatten a nested extra_body back into editable dotted-key rows. */
	function flattenExtra(obj: Record<string, unknown>, prefix = ''): ExtraRow[] {
		const rows: ExtraRow[] = [];
		for (const [key, value] of Object.entries(obj)) {
			const dotted = prefix ? `${prefix}.${key}` : key;
			if (value && typeof value === 'object' && !Array.isArray(value)) {
				rows.push(...flattenExtra(value as Record<string, unknown>, dotted));
			} else {
				rows.push({
					key: dotted,
					value: typeof value === 'string' ? value : JSON.stringify(value)
				});
			}
		}
		return rows;
	}

	/** A stored sampler value as the form shows it (`null` when absent). */
	function samplingNumber(value: unknown): number | null {
		return typeof value === 'number' && Number.isFinite(value) ? value : null;
	}

	/** Stop sequences are authored one per line; blank lines are dropped. */
	function parseStop(text: string): string[] {
		return text
			.split('\n')
			.map((line) => line.trim())
			.filter((line) => line.length > 0);
	}

	/** Render a stored `stop` value back into the one-per-line field. */
	function formatStop(value: unknown): string {
		if (typeof value === 'string') return value;
		if (Array.isArray(value)) {
			return value.filter((item): item is string => typeof item === 'string').join('\n');
		}
		return '';
	}

	/** A form number, or `null` when the field is empty/invalid. */
	function finiteOrNull(value: number | null): number | null {
		return typeof value === 'number' && Number.isFinite(value) ? value : null;
	}

	/** Clear the pasted key: it lives in the input until a save or a probe uses it. */
	function resetKeyFields(): void {
		keyDraft = '';
	}

	/** A new draft starts at REAL values, not hints: 1 / 16000 / 0.95, both toggles on. */
	function startCreate(): void {
		editingName = null;
		models = [];
		discoveryNote = '';
		status = '';
		error = '';
		resetKeyFields();
		draft = {
			name: '',
			base_url: '',
			model: '',
			timeout: null,
			context_window: null,
			note: '',
			thinking: true,
			streaming: true,
			return_thinking: 'auto',
			reasoning_effort: '',
			temperature: 1,
			max_tokens: 16000,
			top_p: 0.95,
			top_k: null,
			repetition_penalty: null,
			frequency_penalty: null,
			presence_penalty: null,
			seed: null,
			stop: '',
			api_key_env: '',
			extra: []
		};
	}

	function startEdit(entry: ConnectionEntry): void {
		editingName = entry.name;
		models = [];
		discoveryNote = '';
		status = '';
		error = '';
		resetKeyFields();
		draft = {
			name: entry.name,
			base_url: entry.base_url,
			model: entry.model,
			timeout: samplingNumber(entry.timeout),
			context_window: samplingNumber(entry.context_window),
			note: entry.note ?? '',
			// null and true BOTH mean on; only `false` carries an instruction.
			thinking: entry.thinking !== false,
			streaming: entry.streaming !== false,
			// Tri-state: only an explicit boolean leaves AUTO.
			return_thinking:
				entry.return_thinking === true
					? 'on'
					: entry.return_thinking === false
						? 'off'
						: 'auto',
			// Verbatim: an effort outside the documented list stays selected (the raw
			// option renders it) instead of being silently reset to not-set.
			reasoning_effort: entry.reasoning_effort ?? '',
			temperature: samplingNumber(entry.sampling.temperature),
			max_tokens: samplingNumber(entry.sampling.max_tokens),
			top_p: samplingNumber(entry.sampling.top_p),
			top_k: samplingNumber(entry.sampling.top_k),
			repetition_penalty: samplingNumber(entry.sampling.repetition_penalty),
			frequency_penalty: samplingNumber(entry.sampling.frequency_penalty),
			presence_penalty: samplingNumber(entry.sampling.presence_penalty),
			seed: samplingNumber(entry.sampling.seed),
			stop: formatStop(entry.sampling.stop),
			api_key_env: entry.api_key_env ?? '',
			extra: flattenExtra(entry.extra_body)
		};
	}

	function cancelForm(): void {
		draft = null;
		editingName = null;
		models = [];
		discoveryNote = '';
		error = '';
		resetKeyFields();
	}

	function addRow(): void {
		if (!draft) return;
		draft.extra = [...draft.extra, { key: '', value: '' }];
	}

	function removeRow(index: number): void {
		if (!draft) return;
		draft.extra = draft.extra.filter((_, i) => i !== index);
	}

	function pickModel(modelId: string): void {
		if (!draft) return;
		draft.model = modelId;
	}

	// --- API actions ---------------------------------------------------------

	async function discover(): Promise<void> {
		if (!draft || discovering) return;
		const probeName = editingName ?? (draft.name.trim() || '__draft__');
		discovering = true;
		discoveryNote = '';
		error = '';
		try {
			const result = await discoverModels(probeName, {
				baseUrl: draft.base_url.trim() || undefined,
				// The pasted key is what the probe authenticates with — that is what makes
				// "base_url → paste key → query models" work with nothing saved yet.
				apiKey: keyDraft.trim() || undefined,
				apiKeyEnv: draft.api_key_env.trim() || undefined
			});
			models = result.models;
			discoveryNote =
				result.count === 0
					? 'Reachable, but the endpoint reported no models.'
					: `Reachable — ${result.count} model${result.count === 1 ? '' : 's'}${
							result.api_key_set ? ' (key sent)' : ''
						}.`;
			if (result.models.length === 1 && !draft.model) {
				draft = { ...draft, model: result.models[0] };
			}
		} catch (err) {
			models = [];
			discoveryNote = `Discovery failed: ${message(err)}`;
		} finally {
			discovering = false;
		}
	}

	/**
	 * Build `sampling` from the standard vocabulary in the form.
	 *
	 * Unset fields are OMITTED (never written as 0/false): the registry replaces
	 * the object wholesale, so an omitted key keeps meaning "not authored".
	 */
	function buildSampling(form: Draft): Record<string, unknown> {
		const sampling: Record<string, unknown> = {};
		const setNumber = (key: string, value: number | null, integer = false): void => {
			const num = finiteOrNull(value);
			if (num === null) return;
			sampling[key] = integer ? Math.trunc(num) : num;
		};
		setNumber('temperature', form.temperature);
		setNumber('max_tokens', form.max_tokens, true); // "Response Length"
		setNumber('top_p', form.top_p);
		setNumber('top_k', form.top_k, true);
		setNumber('repetition_penalty', form.repetition_penalty);
		setNumber('frequency_penalty', form.frequency_penalty);
		setNumber('presence_penalty', form.presence_penalty);
		setNumber('seed', form.seed, true);
		const stop = parseStop(form.stop);
		if (stop.length) sampling.stop = stop;
		return sampling;
	}

	/** Ping a saved connection by reusing discovery (the save validation step). */
	async function ping(name: string): Promise<void> {
		try {
			const result = await discoverModels(name);
			models = result.models;
			status =
				result.count === 0
					? 'Saved. The endpoint is reachable but reported no models.'
					: `Saved. Reachable — ${result.count} model${result.count === 1 ? '' : 's'} available.`;
		} catch (err) {
			status = `Saved, but the ping failed: ${message(err)}`;
		}
	}

	function upsert(saved: ConnectionEntry): void {
		if (!catalog) {
			catalog = { connections: { [saved.name]: saved }, agents: {}, slots: [] };
			return;
		}
		catalog = {
			...catalog,
			connections: { ...catalog.connections, [saved.name]: saved }
		};
	}

	/**
	 * Save the open draft, then ping the endpoint.
	 *
	 * TOTAL: every exit is visible. Validation failures set `error`, request
	 * failures are caught and reported, and the whole body is inside the
	 * try/finally so no rejection can escape into the submit handler unseen —
	 * that silent escape is what made the Save button look dead.
	 */
	async function save(): Promise<void> {
		if (!draft || busy) return; // the button says "Saving…" while busy
		error = '';
		status = '';
		try {
			const form = draft;
			const name = form.name.trim();
			if (!/^[A-Za-z0-9._-]+$/.test(name)) {
				error = 'Name must use letters, digits, dot, dash or underscore.';
				return;
			}
			if (!form.base_url.trim() && !editingName) {
				error = 'Base URL is required.';
				return;
			}
			if (!form.model.trim()) {
				error = 'Model is required — type one or discover the endpoint’s models.';
				return;
			}

			const window = finiteOrNull(form.context_window);
			if (window !== null && Math.trunc(window) < 1) {
				error = 'Context Size must be a whole number of tokens (at least 1).';
				return;
			}
			const body: ConnectionWrite = {
				model: form.model.trim(),
				timeout: finiteOrNull(form.timeout),
				// Context Size: owner-declared; null = unknown (the engine keeps its own
				// defaults — no invented number).
				context_window: window === null ? null : Math.trunc(window),
				// Absence IS on: a checked toggle writes null (never `true`); only the off
				// position carries an instruction, translated per provider engine-side.
				thinking: form.thinking ? null : false,
				streaming: form.streaming ? null : false,
				// auto IS null: the engine echoes whatever reasoning arrived.
				return_thinking:
					form.return_thinking === 'auto'
						? null
						: form.return_thinking === 'on',
				// Always written (like the toggles): not-set is `null`, which CLEARS a
				// stored effort — omitting the key would leave it in place. An unlisted
				// value rides back verbatim; the engine names the vocabulary if it is
				// genuinely invalid.
				reasoning_effort: form.reasoning_effort.trim() ? form.reasoning_effort.trim() : null,
				api_key_env: form.api_key_env.trim() ? form.api_key_env.trim() : null,
				// The pasted key rides the SAME request as the rest of the draft: the engine
				// writes it to `.env` and answers with the derived env NAME. An empty field
				// sends nothing, so an existing key is never cleared by saving.
				...(keyDraft.trim() ? { api_key: keyDraft.trim() } : {}),
				note: form.note.trim(),
				sampling: buildSampling(form),
				extra_body: buildExtraBody(form.extra)
			};
			if (!editingName) body.base_url = form.base_url.trim();

			busy = true;
			const saved = editingName
				? await updateConnection(editingName, body)
				: await createConnection(name, body);
			upsert(saved);
			editingName = saved.name;
			draft = null;
			discoveryNote = '';
			resetKeyFields();
			await ping(saved.name);
			if (saved.api_key_set) {
				status = `${status} Key set — resolved as ${saved.api_key_env}.`;
			}
		} catch (err) {
			error = `Save failed: ${message(err)}`;
		} finally {
			busy = false;
		}
	}

	/** Form submit: `save()` reports every failure itself, so nothing is swallowed. */
	function handleSubmit(event: SubmitEvent): void {
		event.preventDefault();
		void save();
	}

	async function remove(entry: ConnectionEntry): Promise<void> {
		const bound = boundAgents(entry.name);
		const warning = bound.length
			? `“${entry.name}” is still used by ${bound.join(', ')} — rebind those agents first.`
			: `Delete connection “${entry.name}”?`;
		if (bound.length) {
			error = warning;
			return;
		}
		if (!window.confirm(warning)) return;
		try {
			await deleteConnection(entry.name);
			if (catalog) {
				const next = { ...catalog.connections };
				delete next[entry.name];
				catalog = { ...catalog, connections: next };
			}
			if (editingName === entry.name) cancelForm();
			error = '';
			status = `Deleted “${entry.name}”.`;
		} catch (err) {
			error = message(err);
		}
	}

	/**
	 * Re-read the catalog after a write. Slot rows are SERVER-derived (their
	 * `connection`/`via` come from the resolution rule), so a rebind refreshes the
	 * whole read instead of patching the agent map locally — that is what keeps the
	 * provenance lines honest and guarantees no duplicate legacy row appears.
	 */
	async function refresh(): Promise<void> {
		try {
			catalog = await listConnections();
		} catch (err) {
			error = message(err);
		}
	}

	/**
	 * Rebind one slot row to another existing connection (hot-reload PATCH).
	 *
	 * The row's `write_slot` is the spelling the engine's own call sites resolve
	 * (`embedding` -> `embedder`; every other row is already canonical), so a
	 * rebind writes exactly that key and always takes effect — writing the
	 * canonical spelling where an alias outranks it would report success and
	 * change nothing. On failure the DOM control is restored from
	 * `row.connection` — the catalog is the source of truth.
	 */
	async function rebindSlot(connection: string, row: ConnectionSlot): Promise<void> {
		if (!catalog || busy) return;
		busy = true;
		error = '';
		status = '';
		try {
			await setAgentConnection(row.write_slot, connection);
			await refresh();
			status = `Rebound “${row.label}” → “${connection}”.`;
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/**
	 * Does rebinding `row` move the EMBEDDER's resolved connection?
	 *
	 * The memory index is dimensionality-bound, so a new embedder must wipe it.
	 * There is ONE embedding row (issue #48: `embedder` is its alias, not a row of
	 * its own) and its `write_slot` is the embedder key, so a rebind of it always
	 * moves the embedder.
	 */
	function embedderImpact(row: ConnectionSlot): boolean {
		return row.type === 'embedding';
	}

	/**
	 * Change handler for a slot row's select.
	 *
	 * Anything that would move the embedder is held behind the confirm gate (that
	 * switch wipes the single memory index — no data is retained across it); every
	 * other slot rebinds live.
	 */
	function onSlotSelect(event: Event, row: ConnectionSlot): void {
		if (!catalog) return;
		const select = event.currentTarget as HTMLSelectElement;
		const connection = select.value;
		if (!connection || connection === row.connection) {
			select.value = row.connection ?? '';
			return;
		}
		if (!embedderImpact(row)) {
			void rebindSlot(connection, row);
			return;
		}
		select.value = row.connection ?? ''; // hold: the modal applies (or cancels) it
		error = '';
		status = '';
		// The WRITE key, not the row's display key: `embedding` rebinds through
		// `embedder` (the spelling the embedding call path resolves).
		memoryGate = { slot: row.write_slot, connection, previous: row.connection ?? '' };
	}

	/** Cancel the embedder-switch gate: no rebind, no reset. */
	function cancelGate(): void {
		memoryGate = null;
	}

	/**
	 * Confirm the embedder switch: PATCH the agent THEN wipe the index.
	 *
	 * Wiping is destructive for the DERIVED index only (D5: the event log owns all
	 * data); the store re-initializes at the new embedder's dims on the next write.
	 * The user re-embeds explicitly with the button (a separate action).
	 */
	async function switchEmbedder(): Promise<void> {
		if (!memoryGate || !catalog || switching) return;
		const { slot, connection } = memoryGate;
		switching = true;
		error = '';
		status = '';
		try {
			await setAgentConnection(slot, connection);
			await refresh();
			const result = await resetMemory();
			memoryGate = null;
			const count = result.previous.length;
			status = `Switched “${slot}” → “${connection}”. Memory index wiped (${count} collection${
				count === 1 ? '' : 's'
			}) — re-embed all sessions to rebuild it.`;
		} catch (err) {
			error = message(err);
		} finally {
			switching = false;
		}
	}

	/** Poll the re-embed status every 1.5s while a job is running. */
	function startPolling(): void {
		if (pollTimer) return;
		pollTimer = setInterval(() => void refreshReembed(), 1500);
	}

	function stopPolling(): void {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	async function refreshReembed(): Promise<void> {
		try {
			reembed = await getReembedStatus();
		} catch {
			return; // status is advisory — a failed poll never blocks the drawer
		}
		if (reembed.state === 'running') startPolling();
		else stopPolling();
	}

	/** Start the background re-embed (the repair path; re-runnable). */
	async function runReembed(): Promise<void> {
		error = '';
		try {
			const started = await startReembed();
			reembed = started;
			if (started.state === 'running') startPolling();
		} catch (err) {
			error = message(err);
		}
	}

	/** Human status line for the embedder row. */
	function reembedLabel(): string {
		if (!reembed) return '';
		if (reembed.state === 'running') return `re-embedding… ${reembed.done}/${reembed.total}`;
		if (reembed.state === 'done') return 'ready';
		if (reembed.state === 'failed') return `re-embed failed: ${reembed.error ?? 'unknown error'}`;
		return '';
	}

	function extraSummary(entry: ConnectionEntry): string {
		return flattenExtra(entry.extra_body)
			.map((row) => `${row.key}=${row.value}`)
			.join(', ');
	}

	function samplingSummary(entry: ConnectionEntry): string {
		return Object.entries(entry.sampling)
			.map(([key, value]) => `${key} ${String(value)}`)
			.join(' · ');
	}

	/** One connection's "Return thinking" state, spelled the way the control shows it. */
	function returnThinkingLabel(value: boolean | null): string {
		return value === true ? 'on' : value === false ? 'off' : 'auto';
	}

	/** The W9.25 runtime state of a connection: Context Size + every toggle. */
	function transportSummary(entry: ConnectionEntry): string {
		const context = contextDisplay(entry);
		return `context: ${context} · thinking: ${entry.thinking === false ? 'off' : 'on'} · streaming: ${
			entry.streaming === false ? 'off' : 'on'
		} · return thinking: ${returnThinkingLabel(entry.return_thinking)} · reasoning effort: ${
			entry.reasoning_effort ?? 'not set'
		}`;
	}

	// --- item flavor (what a connection is for) ------------------------------

	/**
	 * What kind of item a connection serves, as far as CONTEXT means anything.
	 *
	 * Issue #38: an embedding or rerank endpoint does not consume a prompt window
	 * the way a completion model does, so an undeclared window there is NOT
	 * "unknown" (a number we failed to learn) — it is N/A. A completion-style
	 * entry keeps "unknown" when the owner has not declared one (ADR-008: Context
	 * Size is declared, never probed).
	 */
	type ContextFlavor = 'completion' | 'embedding' | 'rerank';

	/** Slot names that mean "embeddings" / "rerank" — the engine's own vocabulary. */
	const EMBEDDING_SLOTS = new Set(['embedder', 'embedding']);
	const RERANK_SLOTS = new Set(['reranker', 'rerank']);

	/**
	 * Infer a connection's flavor, boring and local.
	 *
	 * Order: the agent slots it is BOUND to (the registry's own map + the slot
	 * view's declared type — `embedder`/`embedding` is an embedding slot), then
	 * the model id, which is the only signal a not-yet-bound entry carries
	 * (`embeddinggemma-2`, `qwen3-reranker`). Rerank is tested first so a hybrid id
	 * mentioning both reads as rerank. Anything else is completion-style.
	 */
	function contextFlavor(entry: ConnectionEntry): ContextFlavor {
		const slots = boundAgents(entry.name);
		if (slots.some((slot) => EMBEDDING_SLOTS.has(slot))) return 'embedding';
		if (slots.some((slot) => RERANK_SLOTS.has(slot))) return 'rerank';
		// A typed bind (`embedding` authored as the type key) shows up on the slot row.
		const boundType = slotRows.find((row) => row.connection === entry.name)?.type;
		if (boundType === 'embedding') return 'embedding';
		const model = entry.model.toLowerCase();
		if (model.includes('rerank')) return 'rerank';
		if (model.includes('embed')) return 'embedding';
		return 'completion';
	}

	/** The Context Size cell: the declared number, else N/A or "unknown". */
	function contextDisplay(entry: ConnectionEntry): string {
		const declared = entry.context_window ?? null;
		if (declared !== null) return String(declared);
		return contextFlavor(entry) === 'completion' ? 'unknown' : 'N/A';
	}
</script>

<div class="scrim" role="presentation" onclick={onClose}></div>

<div class="conn-drawer" role="dialog" aria-modal="true" aria-label="Connections">
	<header>
		<div>
			<h2>Connections</h2>
			<p class="sub">
				Where each agent's model lives. Edits take effect on the next turn — no restart.
			</p>
		</div>
		<button type="button" class="close" onclick={onClose} aria-label="Close connections">×</button>
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}
	{#if status}
		<p class="status" role="status">{status}</p>
	{/if}

	<div class="body">
		{#if draft}
			<div class="draft-view">
				<form class="form" onsubmit={handleSubmit}>
					<h3>{editingName ? `Edit “${editingName}”` : 'New connection'}</h3>
					<p class="hint" data-field-mode={fieldMode}>{modeNote}</p>

					<label class="field">
						<span>Name</span>
						<input
							bind:value={draft.name}
							autocomplete="off"
							disabled={editingName !== null}
							data-field="name"
						/>
						{#if editingName}
							<small>Names are stable keys — create a new connection to rename.</small>
						{/if}
					</label>

					<label class="field">
						<span>Base URL</span>
						<input
							bind:value={draft.base_url}
							autocomplete="off"
							placeholder="http://0.0.0.0:8080"
							disabled={editingName !== null}
							data-field="base_url"
						/>
						<small>host:port of any OpenAI-compatible server; a bare host:port is treated as http://…/v1 — add a path to override</small>
						{#if editingName}
							<small
								>The endpoint is fixed once a connection exists — create a new one to change it.</small
							>
						{/if}
					</label>

					<!-- The credential field is a KEY, not an env-var name: it sits above the
					     model picker because the probe below authenticates with it, and it is
					     usable before the connection is saved. -->
					<label class="field">
						<span>API key</span>
						<input
							bind:value={keyDraft}
							type="password"
							autocomplete="off"
							placeholder="paste the key — stored server-side"
							data-field="api_key"
						/>
						<small>stored in .env, mode 600; never shown again</small>
						{#if editingName && catalog?.connections[editingName]}
							{@const editing = catalog.connections[editingName]}
							{#if editing.api_key_env}
								<small class="key-state" data-key-state>
									{editing.api_key_env}: {editing.api_key_set ? 'set' : 'not set'}.
								</small>
							{/if}
						{/if}
					</label>

					<details class="advanced" data-advanced-key>
						<summary>Advanced — use an env var for the key</summary>
						<label class="field">
							<span>API key env var</span>
							<input
								bind:value={draft.api_key_env}
								autocomplete="off"
								placeholder="MY_PROVIDER_KEY"
								data-field="api_key_env"
							/>
							<small>
								Name of an env var the engine reads at runtime — for a key you already export (a
								service EnvironmentFile, a shell export). A pasted key above wins and takes
								over this.
							</small>
						</label>
					</details>

					<label class="field">
						<span>Model</span>
						<input bind:value={draft.model} autocomplete="off" data-field="model" />
					</label>

					<div class="discover-row">
						<button type="button" class="ghost" onclick={() => void discover()} disabled={discovering}>
							{discovering ? 'Probing…' : 'Discover models'}
						</button>
						{#if discoveryNote}<span class="hint">{discoveryNote}</span>{/if}
					</div>

					{#if models.length}
						<ul class="models">
							{#each models as modelId (modelId)}
								<li>
									<button
										type="button"
										class="chip"
										class:active={draft.model === modelId}
										onclick={() => pickModel(modelId)}
									>
										{modelId}
									</button>
								</li>
							{/each}
						</ul>
					{/if}

					<label class="field">
						<span>Timeout (seconds)</span>
						<input
							bind:value={draft.timeout}
							type="number"
							min="0"
							step="1"
							placeholder="300"
						/>
						<small>seconds — empty means 300</small>
					</label>

					<label class="field">
						<span>{contextLabel}</span>
						<input
							bind:value={draft.context_window}
							type="number"
							min="1"
							step="1"
							placeholder="undeclared"
							data-field="context_window"
						/>
						{#if fieldMode === 'embedding'}
							<small>
								tokens — for information only: the embedder never shortens what it reads.
							</small>
						{:else}
							<small>
								tokens. Leave it empty and the engine cannot warn you before the chat runs long, or
								show you what it sent. Set it on the model that narrates.
							</small>
						{/if}
					</label>

					{#if fieldMode !== 'embedding'}
						<label class="field">
							<span>Response Length</span>
							<input
								bind:value={draft.max_tokens}
								type="number"
								min="1"
								step="1"
								placeholder="provider default"
								data-field="max_tokens"
							/>
							<small>
								tokens — part of this is kept free so the engine can compact the chat later
							</small>
						</label>
					{/if}

					<fieldset>
						<legend>Runtime</legend>
						<label class="toggle">
							<input type="checkbox" bind:checked={draft.thinking} data-field="thinking" />
							<span>Thinking</span>
						</label>
						<small>
							On by default. Unchecked sends the provider's own disable instruction — a provider
							without a measured one is unchanged.
						</small>
						<label class="toggle">
							<input type="checkbox" bind:checked={draft.streaming} data-field="streaming" />
							<span>Streaming</span>
						</label>
						<small>
							On by default: tokens arrive as they are produced. Unchecked sends one request and
							delivers the whole draft at once.
						</small>
						<label class="field">
							<span>Return thinking</span>
							<select bind:value={draft.return_thinking} data-field="return_thinking">
								<option value="auto">Auto — echo reasoning whenever the provider returns it</option>
								<option value="on">On — always echo reasoning that arrived</option>
								<option value="off">Off — never send the field</option>
							</select>
							<small>
								echo <code>reasoning_content</code> back inside tool loops — required by DeepSeek
								thinking models; AUTO does this whenever the provider returns reasoning.
							</small>
						</label>
						<label class="field">
							<span>Reasoning effort</span>
							<select bind:value={draft.reasoning_effort} data-field="reasoning_effort">
								{#each REASONING_EFFORT_OPTIONS as option (option.value)}
									<option value={option.value}>{option.label}</option>
								{/each}
								{#if isUnlistedEffort(draft.reasoning_effort)}
									<!-- A stored value the list does not cover keeps its own option: it
									     stays visible and selected, never silently reset. -->
									<option value={draft.reasoning_effort}>
										{draft.reasoning_effort} (not in this list — saved as-is)
									</option>
								{/if}
							</select>
							<small>
								How hard a thinking model reasons before it answers. <strong>Not set</strong> sends no
								effort parameter at all. GLM-5.3 accepts low / high / max.
							</small>
						</label>
					</fieldset>

					{#if fieldMode !== 'embedding'}
						<fieldset data-sampling>
							<legend>Sampling</legend>
							<div class="grid">
								<!-- Float fields use step="any": a fixed step makes the browser's own
								     constraint validation silently BLOCK Save for any value off the grid
								     (0.66 on a 0.05 step) — the "dead Save button" class of bug. Integer
								     fields keep step="1" and their minimums. -->
								<label class="field">
									<span>Temperature</span>
									<input bind:value={draft.temperature} type="number" step="any" />
								</label>
								<label class="field">
									<span>Top P</span>
									<input bind:value={draft.top_p} type="number" step="any" />
								</label>
								<label class="field">
									<span>Top K</span>
									<input bind:value={draft.top_k} type="number" min="0" step="1" />
								</label>
								<label class="field">
									<span>Repetition Penalty</span>
									<input bind:value={draft.repetition_penalty} type="number" step="any" />
								</label>
								<label class="field">
									<span>Frequency Penalty</span>
									<input bind:value={draft.frequency_penalty} type="number" step="any" />
								</label>
								<label class="field">
									<span>Presence Penalty</span>
									<input bind:value={draft.presence_penalty} type="number" step="any" />
								</label>
								<label class="field">
									<span>Seed</span>
									<input bind:value={draft.seed} type="number" step="1" />
								</label>
								<label class="field">
									<span>Stop Sequences</span>
									<textarea bind:value={draft.stop} rows="2" data-field="stop"></textarea>
								</label>
							</div>
							<small>One stop sequence per line.</small>
						</fieldset>
					{/if}

					<fieldset>
						<legend>Extra params (extra_body)</legend>
						<p class="hint">
							Provider/llama.cpp-specific options that are not standard OpenAI samplers — e.g.
							<code>top_k</code>, <code>repeat_penalty</code>. Dot a key to nest it
							(<code>thinking.type</code>); values parse as JSON when possible.
						</p>
						{#each draft.extra as row, index (index)}
							<div class="extra-row">
								<input bind:value={row.key} placeholder="key" aria-label="Extra param key" />
								<input bind:value={row.value} placeholder="value" aria-label="Extra param value" />
								<button
									type="button"
									class="ghost row-remove"
									onclick={() => removeRow(index)}
									aria-label="Remove extra param"
								>
									×
								</button>
							</div>
						{/each}
						<button type="button" class="ghost add-param" onclick={addRow}>+ Add param</button>
					</fieldset>

					<label class="field">
						<span>Note</span>
						<textarea bind:value={draft.note} rows="2"></textarea>
					</label>

					<div class="form-actions">
						<button type="button" class="ghost" onclick={cancelForm} disabled={busy}>Cancel</button>
						<button type="submit" class="primary" disabled={busy}>
							{busy ? 'Saving…' : 'Save & ping'}
						</button>
					</div>
				</form>
			</div>
		{:else}
			<div class="list-head">
				<span class="count"
					>{connectionNames.length}
					{connectionNames.length === 1 ? 'connection' : 'connections'}</span
				>
				<button type="button" class="primary" onclick={startCreate}>+ New connection</button>
			</div>

			{#if loading}
				<p class="hint">Loading connections…</p>
			{:else if connectionNames.length === 0}
				<p class="hint">No connections yet — create one above.</p>
			{:else}
				<ul class="connections">
					{#each connectionNames as name (name)}
						{@const entry = catalog!.connections[name]}
						{@const bound = boundAgents(name)}
						<li class="connection">
							<div class="conn-head">
								<span class="conn-name">{entry.name}</span>
								<span class="conn-model">{entry.model || '⚠ no model'}</span>
							</div>
							<p class="conn-url">{entry.base_url}</p>
							{#if samplingSummary(entry)}
								<p class="conn-meta">sampling: {samplingSummary(entry)}</p>
							{/if}
							{#if extraSummary(entry)}
								<p class="conn-meta">extra: {extraSummary(entry)}</p>
							{/if}
							<p class="conn-meta">{transportSummary(entry)}</p>
							<p class="conn-meta">
								timeout: {entry.timeout ?? 'default'} · key:
								{#if entry.api_key_env}
									{entry.api_key_env} ({entry.api_key_set ? 'set' : 'not set'})
								{:else}
									none
								{/if}
							</p>
							{#if entry.note}
								<p class="conn-note">{entry.note}</p>
							{/if}
							<p class="conn-agents">
								{bound.length
									? `used by: ${boundRoleLabels(entry.name).join(', ')}`
									: 'not bound to any agent'}
							</p>
							<div class="conn-actions">
								<button type="button" class="ghost" onclick={() => startEdit(entry)}>Edit</button>
								<button type="button" class="danger" onclick={() => void remove(entry)}>Delete</button>
							</div>
						</li>
					{/each}
				</ul>
			{/if}

			<section class="agents">
				<h3>Agents → connections</h3>
				{#if slotRows.length === 0}
					<p class="hint">
						The engine did not send its agent list — restart it to load the agents.
					</p>
				{:else}
					{#each slotGroups as group (group.key)}
						<div class="slot-group">
							<h4 class="slot-type">{group.label}</h4>
							<ul class="agent-list">
								{#each group.rows as row (row.slot)}
									<li
										class="slot-row"
										class:is-type-default={!!row.type && row.slot === row.type}
										data-slot={row.slot}
									>
										<span class="slot-main">
											<span class="agent-name" title={rowLabel(row, group)}>{rowLabel(row, group)}</span>
											<span class="arrow">→</span>
											<select
												class="agent-conn-select"
												value={row.connection ?? ''}
												disabled={busy || switching}
												aria-label={`Connection for ${row.label}`}
												onchange={(event) => onSlotSelect(event, row)}
											>
												{#if !row.connection}
													<option value="">— not set —</option>
												{/if}
												{#each connectionNames as name (name)}
													<option value={name}>{name}</option>
												{/each}
											</select>
										</span>
										<span class="slot-via" class:unresolved={!row.via}>{viaLabel(row)}</span>
										{#each inertBindings(row) as entry (entry.slot)}
											<span
												class="slot-inert"
												class:conflict={isConflict(row, entry)}
												title={isConflict(row, entry)
													? `The ${entry.slot} entry is ignored: this agent goes through the connection above, which names a different one (“${entry.connection}” vs “${row.connection}”).`
													: `The ${entry.slot} entry is ignored — this agent goes through the connection above.`}
											>
												{isConflict(row, entry) ? '⚠ ' : ''}{entry.slot} ignored → {entry.connection}
											</span>
										{/each}
										{#if row.type === 'embedding'}
											<div class="memory-controls">
												{#if reembedLabel()}
													<span class="reembed-status" role="status">{reembedLabel()}</span>
												{/if}
												<button
													type="button"
													class="ghost reembed-btn"
													onclick={() => void runReembed()}
													disabled={reembed?.state === 'running'}
												>
													Re-embed all sessions
												</button>
											</div>
										{/if}
									</li>
								{/each}
							</ul>
							{#if group.key === 'npc'}
								<p class="hint">
									Every NPC lane shares this one connection, so pointing it at a cheaper or
									stronger model moves all of them at once.
								</p>
							{/if}
						</div>
					{/each}
					<p class="hint">
						One row per agent, showing the connection it actually uses and why (its own setting, an
						older name for the same role, its task, the default for its kind, or the model served
						by another role). An entry that is not the one in force says <em>ignored</em> rather
						than being hidden. Changes save immediately and apply on the next turn — no restart.
					</p>
				{/if}
			</section>

			<!-- Optional Image Prompter section (issue #55a): collapsed by default, so
			     the pane is only mounted (and only fetches its definitions) once the
			     user opens it. -->
			<section class="imagegen">
				<button
					type="button"
					class="imagegen-toggle"
					aria-expanded={imageGenOpen}
					onclick={toggleImageGenSection}
				>
					<span class="chevron" aria-hidden="true">{imageGenOpen ? '▾' : '▸'}</span>
					Image Prompter
				</button>
				{#if imageGenOpen}
					<div class="imagegen-body">
						<!-- Issue #57: the ComfyUI endpoint is a persisted SETTING, editable
						     here — not a constant in code. -->
						<div class="field comfy-endpoint">
							<label for="comfy-base-url">ComfyUI URL</label>
							<div class="comfy-row">
								<input
									id="comfy-base-url"
									type="text"
									class="comfy-input"
									spellcheck="false"
									autocomplete="off"
									placeholder="http://127.0.0.1:8188"
									bind:value={comfyDraft}
									disabled={comfyBusy}
									onkeydown={(event) => {
										if (event.key === 'Enter') void saveComfyUrl();
									}}
								/>
								<button
									type="button"
									class="ghost comfy-save"
									onclick={() => void saveComfyUrl()}
									disabled={comfyBusy}
								>
									{comfyBusy ? 'Saving…' : 'Save'}
								</button>
							</div>
							<small>
								Where the Image Prompter submits its workflows. Empty = the built-in
								default (<code>http://127.0.0.1:8188</code>). Saves immediately.
							</small>
							{#if comfyNote}
								<small class="key-state" role="status">{comfyNote}</small>
							{/if}
						</div>
						<ImageGenPane />
					</div>
				{/if}
			</section>
		{/if}
	</div>

	{#if memoryGate}
		<div class="gate-scrim" role="presentation" onclick={cancelGate}></div>
		<div class="gate" role="alertdialog" aria-modal="true" aria-label="Switch embedder">
			<h3>Switch embedder?</h3>
			<p>
				Switching the <strong>embedder</strong> to <strong>{memoryGate.connection}</strong> will
				<strong>wipe the memory index</strong> and rebuild it to fit the new model's output size.
				Nothing indexed is kept — re-embed all sessions afterwards to rebuild it from your chat
				history.
			</p>
			<div class="form-actions">
				<button type="button" class="ghost" onclick={cancelGate} disabled={switching}>
					Cancel
				</button>
				<button
					type="button"
					class="danger gate-switch"
					onclick={() => void switchEmbedder()}
					disabled={switching}
				>
					{switching ? 'Switching…' : 'Switch'}
				</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 22;
	}
	.conn-drawer {
		position: fixed;
		top: 0;
		right: 0;
		height: 100vh;
		width: 28rem;
		max-width: 96vw;
		z-index: 23;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border-left: 1px solid var(--line);
		box-shadow: -8px 0 24px var(--shadow);
		color: var(--text);
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.85rem 1rem;
		border-bottom: 1px solid var(--line);
	}
	h2 {
		margin: 0;
		font-size: var(--font-lg);
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.sub {
		margin: 0.2rem 0 0;
		font-size: var(--font-small);
		line-height: 1.4;
		color: var(--text-muted);
	}
	.close {
		font: inherit;
		font-size: 1.1rem;
		line-height: 1;
		width: 1.6rem;
		height: 1.6rem;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.close:hover {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.error,
	.status {
		margin: 0.6rem 1rem 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
	}
	.error {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.status {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0.75rem 1rem 1.75rem;
		display: flex;
		flex-direction: column;
		gap: 0.7rem;
	}
	.list-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.count {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.hint {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		line-height: 1.4;
		color: var(--text-dim);
	}
	code {
		font-size: var(--font-micro);
		background: var(--bg-inset);
		padding: 0 0.2rem;
		border-radius: 4px;
	}
	.connections,
	.models,
	.agent-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.connections {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.connection {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		padding: 0.55rem 0.65rem;
	}
	.conn-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.conn-name {
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.conn-model {
		flex: none;
		font-size: var(--font-small);
		color: var(--accent-strong);
	}
	.conn-url,
	.conn-meta,
	.conn-note,
	.conn-agents {
		margin: 0;
		font-size: var(--font-micro);
		line-height: 1.4;
		color: var(--text-muted);
		overflow-wrap: anywhere;
	}
	.conn-note {
		color: var(--text-dim);
		font-style: italic;
	}
	.conn-actions {
		display: flex;
		gap: 0.35rem;
		margin-top: 0.2rem;
	}
	.agents {
		border-top: 1px solid var(--line);
		padding-top: 0.6rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.agent-list {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.slot-group {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.slot-type {
		margin: 0.35rem 0 0;
		font-size: var(--font-small);
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text-dim);
	}
	.slot-via,
	.slot-inert {
		grid-column: 3;
		font-size: var(--font-micro);
		color: var(--text-dim);
		line-height: 1.2;
	}
	.slot-inert {
		font-style: italic;
		color: var(--text-muted);
	}
	/* an inert spelling that names a DIFFERENT connection than the effective one:
	   a real conflict, not merely dead weight (issue #48) */
	.slot-inert.conflict {
		color: var(--danger-text);
	}
	.slot-via.unresolved {
		color: var(--danger-text);
	}
	.agent-list li {
		display: grid;
		grid-template-columns: minmax(8.75rem, 11rem) 0.9rem minmax(0, 1fr);
		column-gap: 0.4rem;
		row-gap: 0.15rem;
		align-items: center;
		font-size: var(--font-small);
	}
	/* the label/arrow/select share the row's grid cells; the via line lands in
	   the control column on the second row, so provenance nests under the box */
	.slot-main {
		display: contents;
	}
	/* the type row leads its group as the fallback every unset slot inherits;
	   a hairline BELOW it separates the fallback from the specific slots */
	.slot-row.is-type-default {
		border-bottom: 1px solid var(--line);
		padding-bottom: 0.35rem;
		margin-bottom: 0.35rem;
	}
	.agent-name {
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.arrow {
		color: var(--text-dim);
	}
	.agent-conn-select {
		font: inherit;
		font-size: var(--font-small);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.2rem 0.35rem;
		min-width: 0;
	}
	.agent-conn-select:disabled {
		opacity: 0.6;
	}
	.memory-controls {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		grid-column: 1 / -1;
		margin-left: 0;
	}
	.reembed-status {
		font-size: var(--font-micro);
		color: var(--accent-strong);
	}
	.reembed-btn {
		font-size: var(--font-micro);
		padding: 0.2rem 0.5rem;
	}
	.gate-scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 40;
	}
	.gate {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		width: min(24rem, 92vw);
		z-index: 41;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		background: var(--bg-panel);
		border: 1px solid var(--line);
		border-radius: 10px;
		box-shadow: 0 12px 40px var(--shadow);
		padding: 1rem;
		color: var(--text);
	}
	.gate h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.gate p {
		margin: 0;
		font-size: var(--font-small);
		line-height: 1.45;
		color: var(--text-muted);
	}
	.gate-switch {
		border-color: var(--accent-line);
	}
	.imagegen {
		border-top: 1px solid var(--line);
		padding-top: 0.6rem;
	}
	.imagegen-toggle {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		width: 100%;
		border: none;
		border-radius: 0;
		background: none;
		color: var(--text);
		font-size: var(--font-ui);
		font-weight: 600;
		text-align: left;
		padding: 0.2rem 0;
	}
	.imagegen-toggle:hover:not(:disabled) {
		background: none;
		color: var(--accent-strong);
	}
	.chevron {
		color: var(--text-dim);
	}
	.imagegen-body {
		margin-top: 0.5rem;
	}
	.comfy-endpoint {
		margin-bottom: 0.6rem;
	}
	.comfy-row {
		display: flex;
		gap: 0.4rem;
		align-items: stretch;
	}
	.comfy-input {
		flex: 1;
		min-width: 0;
	}
	.comfy-save {
		min-width: 3.6rem;
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.field small {
		font-size: var(--font-micro);
		line-height: 1.35;
		color: var(--text-dim);
	}
	.field small.key-state {
		color: var(--accent-strong);
	}
	.toggle {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--font-small);
		color: var(--text);
	}
	.toggle input {
		width: auto;
		margin: 0;
	}
	fieldset small {
		font-size: var(--font-micro);
		line-height: 1.35;
		color: var(--text-dim);
	}
	.field input,
	.field select,
	.field textarea {
		font: inherit;
		font-size: var(--font-ui);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.35rem 0.5rem;
		/* No global border-box: without these the native input width (~20ch)
		   pushes grid/flex children past the drawer's right edge. */
		box-sizing: border-box;
		width: 100%;
		min-width: 0;
		resize: vertical;
	}
	.field input:disabled {
		opacity: 0.6;
	}
	.grid {
		display: grid;
		/* minmax(0, 1fr), not 1fr: three samplers have to fit a 28rem drawer. */
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 0.4rem;
	}
	.grid > .field {
		min-width: 0;
	}
	.draft-view {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	/* The env-var NAME is an advanced path, not the primary credential field. */
	.advanced {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.35rem 0.55rem 0.5rem;
	}
	.advanced summary {
		cursor: pointer;
		font-size: var(--font-ui);
		color: var(--text-dim);
	}
	.advanced[open] summary {
		margin-bottom: 0.4rem;
	}
	.key-state {
		color: var(--accent-strong);
	}
	fieldset {
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.4rem 0.55rem 0.55rem;
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	legend {
		font-size: var(--font-micro);
		color: var(--text-dim);
		padding: 0 0.25rem;
	}
	.discover-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.models {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
	}
	.chip {
		font: inherit;
		font-size: var(--font-micro);
		border: 1px solid var(--line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--text-muted);
		padding: 0.2rem 0.55rem;
		cursor: pointer;
	}
	.chip:hover {
		background: var(--bg-hover);
	}
	.chip.active {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.extra-row {
		display: grid;
		grid-template-columns: 1fr 1fr auto;
		gap: 0.35rem;
	}
	.extra-row input {
		font: inherit;
		font-size: var(--font-small);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.3rem 0.45rem;
		min-width: 0;
	}
	.row-remove {
		padding: 0.2rem 0.5rem;
	}
	.add-param {
		align-self: flex-start;
	}
	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.4rem;
		margin-top: 0.15rem;
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
