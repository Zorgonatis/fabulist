<script lang="ts">
	// Behind-the-Scenes state dashboard. It renders the session TREE — the
	// engine's ONE render-ready projection of what a session is (W9.32 §2,
	// `GET .../tree`) — branch by branch, in the order the wire serves it:
	// clock, scene, places, items, threads, relations, meters, conditions,
	// economy, cast, Player. The pane reads as PEOPLE and VALUES: occupants,
	// holders and hosts arrive as display names. Beside those names each
	// addressable node carries its registry id (`loc_…`/`itm_…`/`th_…`/`rel_…`,
	// `cd_…`/`eco_…`, and `participation.member_id` for a being), and a row shows
	// that id only while its own ✎ editor is open — the id is the address the
	// write door takes, never a rest label.
	//
	// The turn-level style rows (time / date / location / weather / tone / genre)
	// are the palette derivation's inputs (theme/tokens.py `style_tokens`). Each
	// reads the TREE first and the flat `facts` fold BENEATH it — the same
	// precedence the tree itself applies (registry object -> flat fold) — so an
	// edit shows in the row at once instead of at the next turn, while a session
	// the registry has not seen yet still renders its flat values. Forecast is the
	// one row with a pin: it reads the HUD statement its own door writes (D45).
	//
	// Every ✎ goes through ONE validation path — the registry write door
	// (`POST .../entities` via `writeEntityOp`), which runs the tracker's own tool
	// code. An out-of-range meter, an unknown catalog key, a bad severity and a
	// balance that would go negative are refused with the engine's readable text,
	// nothing is written, and the refusal is shown (never swallowed). Two rows are
	// deliberately NOT entity actions: time/date stay on the flat `POST /state`
	// door (the clock is not an object) and Forecast keeps its nested
	// `["forecast"]` pin door.
	import { onMount } from 'svelte';
	import type { ConnectionStatus } from '#lib/stores';
	import { stateNonce } from '#lib/stores';
	import { API_BASE, checkReconcileNow, writeEntityOp, writeNestedState } from '#lib/api';
	import { getEntityTree } from '#lib/api/bts';
	import type {
		SessionTree,
		TreeBody,
		TreeCastNode,
		TreeCondition,
		TreeEconomy,
		TreeItem,
		TreeLive,
		TreeMeterGroup,
		TreeMeterRow,
		TreePlace,
		TreePlayer,
		TreeRelation,
		TreeThread
	} from '#lib/api/bts';
	import { getHud } from '#lib/hudApi';
	import { hud, setHud } from '#lib/hudStore';
	import { parseHudStatement } from './ChatHud.svelte';

	let {
		facts,
		connection = 'idle',
		connectionError = '',
		onRetry,
		sessionId = ''
	}: {
		facts: Record<string, string>;
		connection?: ConnectionStatus;
		/** WHY the connection is in `error` ('' otherwise) — the rail's cause line. */
		connectionError?: string;
		/** The recovery door: re-probe the session and re-run the failed turn. */
		onRetry?: () => void;
		sessionId?: string | null;
	} = $props();

	/**
	 * The rail's retry door is NEVER disabled by the error state — that state is
	 * exactly when it is needed. It stays pressable while `error` (the dead end
	 * this exists to remove); only a live turn holds it, because a second re-run
	 * cannot start before the first settles.
	 */
	const retryDisabled = $derived(connection === 'connecting' || connection === 'streaming');

	// --- collapse (T1) ---
	// The D40 rail pattern (SessionDrawer): collapse lives entirely here. The
	// bar's width is this component's own style, so `+page.svelte` never learns
	// about it.
	//
	// The DEFAULT is COLLAPSED (issue #22): docked at 265px this pane's turn rows
	// (time / date / location / weather) reprinted what the HUD station in the
	// thread head already owns, and permanent left chrome is exactly what kept
	// prose off its reading measure. Both states persist under the SAME key — an
	// explicit expand writes '0' and is honoured, an absent key means "never
	// chosen" and starts collapsed, so a fresh install pays ~38px, not 265px.
	const COLLAPSE_KEY = 'fabulist.statebar.collapsed';
	let collapsed = $state(true);

	function toggleCollapsed(): void {
		collapsed = !collapsed;
		// Persist best-effort; `window` is guarded because this can be reached from
		// an event that also runs during hydration (SSR has no window).
		if (typeof window !== 'undefined') {
			window.localStorage.setItem(COLLAPSE_KEY, collapsed ? '1' : '0');
		}
	}

	onMount(() => {
		if (typeof window === 'undefined') return;
		// Only an explicit expand re-opens the dock; '1' and an absent key both
		// leave it collapsed (the default above).
		if (window.localStorage.getItem(COLLAPSE_KEY) === '0') collapsed = false;
	});

	// --- the session tree ------------------------------------------------------
	//
	// ONE read for the whole pane. The tree is a PURE engine projection (it mints
	// no member, materialises no roster, appends no row), so it is safe to refetch
	// mid-turn and after every write.
	let tree = $state<SessionTree | null>(null);
	// Bumped after a write lands: an entity append does not change `facts`, so the
	// effect below needs an explicit nudge to refetch.
	let treeNonce = $state(0);
	// W9.29 §12 — the manual "check state now" door: the reconciliation pass the
	// cadence runs every tenth turn, asked for on demand. Pending / result / error.
	let reconcileBusy = $state(false);
	let reconcileNote = $state('');
	let reconcileError = $state('');

	$effect(() => {
		const id = sessionId;
		// Reading `facts` re-runs this after each kept turn (the parent replaces
		// the object with the refreshed projection); `treeNonce` re-runs it after an
		// in-place edit; `$stateNonce` re-runs it MID-turn, when a stage that writes
		// the world closes (planner bootstrap, the post-turn tracker — debounced by
		// the page), so the pane fills in instead of catching up at `done`.
		void facts;
		void treeNonce;
		void $stateNonce;
		if (!id) {
			tree = null;
			return;
		}
		let cancelled = false;
		void getEntityTree(id).then((next) => {
			if (!cancelled) tree = next;
		});
		return () => {
			cancelled = true;
		};
	});

	/** A trimmed value, or the em dash the pane uses for "nothing yet". */
	function text(value: string | null | undefined): string {
		return (value ?? '').trim() || '—';
	}

	const playerNode = $derived<TreePlayer | null>(tree?.Player ?? null);
	const playerDisplay = $derived(playerNode?.identity.display ?? 'Player');
	const playerKey = $derived(playerNode?.identity.key ?? 'player');

	/**
	 * The being key a write door resolves for a display label (W9.32 R3: a name
	 * resolves roster-first and `player` is the reserved player key). The pane
	 * shows names; the doors take names too — except the player, whose label is
	 * the `Player (Name)` display and whose key is the reserved literal.
	 */
	function beingArg(label: string | null | undefined): string {
		const name = (label ?? '').trim();
		if (!name) return playerKey;
		return name === playerDisplay ? playerKey : name;
	}

	/** The scene's reserved host renders as a word, not as a lowercase literal. */
	function hostLabel(label: string): string {
		return label === 'scene' ? 'Scene' : label;
	}

	/**
	 * The ONE turn-level style row this pane still owns: TONE.
	 *
	 * Issue #22 (duplicated facts): time, date, location, weather and forecast now
	 * have exactly ONE owner — the HUD station in the thread head (engine-emitted
	 * `EnvTracker`, `ChatHud.svelte`). Printing them here as well meant the reader
	 * saw the same clock and the same place three times on one screen (head HUD,
	 * these rows, the DSL strip). What stays is the one reading that is NOT a
	 * scene fact but a THEME input: `tone` feeds `theme/tokens.py style_tokens`, so
	 * it belongs beside the ✎ that changes the palette. Tree-first with the flat
	 * fold beneath, exactly as before.
	 */
	const topRows = $derived([
		{ key: 'tone', label: 'Tone', value: text(tree?.scene.tone || facts['tone']) }
	]);
	const genre = $derived(text(tree?.scene.genre || facts['genre']));

	// Forecast is the one HUD slot the flat fold does not natively carry (it rides
	// the BTS result), so its write path is the NESTED door at `["forecast"]` (D45):
	// the backend pins a `state-delta` override AND re-emits the HUD statement, so
	// the chip updates without waiting for a turn. An empty value CLEARS the pin and
	// restores the automatic ladder (override -> BTS -> prior -> weather). The row
	// refetches the HUD after a successful write (see `refreshHud`).
	/**
	 * Is the outlook MANUALLY PINNED? The pin is the flat fold's own `forecast`
	 * row (D45: the write door stores a `state-delta` override), so this row shows
	 * the PIN's state — `pinned` / `automatic` — never the outlook text, which the
	 * HUD station owns (issue #22: one printing per fact).
	 */
	const pinForecast = $derived.by(() => {
		const raw = (facts['forecast'] ?? '').trim();
		return raw.length > 0 && raw !== '—';
	});

	const forecast = $derived.by(() => {
		const current = $hud;
		if (!current.statement || current.sessionId !== sessionId) return '';
		return parseHudStatement(current.statement)?.forecast ?? '';
	});

	// --- the branch rows ------------------------------------------------------

	/**
	 * Locations in render order, indented by depth. The wire is a flat list whose
	 * entries carry their parent's NAME and their `parent > child` PATH, so the
	 * depth is the path's own segment count — no second wire shape, and an orphan
	 * (a dangling parent) simply renders at its own depth.
	 */
	const placeRows = $derived(
		(tree?.places.tree ?? []).map((place: TreePlace) => ({
			place,
			depth: Math.max(0, place.path.split(' > ').length - 1)
		}))
	);
	const offScreen = $derived(tree?.places.off_screen ?? null);

	/**
	 * Items GROUPED BY THEIR HOLDER (owner 2026-10-21: "group items by the person
	 * holding them for a cleaner layout"). One group per holder, the player's
	 * first, then alphabetical by display name; an unheld item sits last under
	 * "unassigned" (never dropped).
	 */
	const itemGroups = $derived.by(() => {
		const byHolder = new Map<string, { label: string; items: TreeItem[] }>();
		for (const item of tree?.items ?? []) {
			const label = (item.holder ?? '').trim() || 'unassigned';
			const group = byHolder.get(label) ?? { label, items: [] };
			group.items.push(item);
			byHolder.set(label, group);
		}
		return [...byHolder.values()].sort((left, right) => {
			if (left.label === playerDisplay && right.label !== playerDisplay) return -1;
			if (right.label === playerDisplay && left.label !== playerDisplay) return 1;
			if (left.label === 'unassigned') return 1;
			if (right.label === 'unassigned') return -1;
			return left.label.localeCompare(right.label);
		});
	});

	const threadRows = $derived<TreeThread[]>(tree?.threads ?? []);
	const relationRows = $derived<TreeRelation[]>(tree?.relations ?? []);
	const meterGroups = $derived<TreeMeterGroup[]>(tree?.meters ?? []);
	const conditionRows = $derived<TreeCondition[]>(tree?.conditions ?? []);
	const castNodes = $derived<TreeCastNode[]>(tree?.cast ?? []);
	const economy = $derived<TreeEconomy | null>(tree?.economy ?? null);

	/**
	 * The scene's own readings the top rows do NOT already show.
	 *
	 * The wire serves the scene's attributes in the `scene` branch only (`meters`
	 * carries every NON-scene attribute), so without this group `tension` and
	 * `irony` would have no row at all. weather/tone/genre/forecast stay in the top
	 * rows, where they were — one reading never renders twice.
	 */
	const sceneRows = $derived.by<TreeMeterRow[]>(() => {
		const scene = tree?.scene;
		if (!scene) return [];
		const rows: TreeMeterRow[] = [];
		if (typeof scene.tension === 'number') rows.push({ key: 'tension', value: scene.tension });
		if (scene.irony) rows.push({ key: 'irony', value: scene.irony });
		return rows;
	});

	/**
	 * One row per being with a body slot (every cast member, then the player), so
	 * a being with no body yet still has an ✎ to give them one.
	 */
	const bodyRows = $derived.by(() => {
		const current = tree;
		if (!current) return [];
		const rows: Array<{ display: string; key: string; body: TreeBody | null }> = current.cast.map(
			(node) => ({
				display: node.participation.display,
				key: node.participation.member_id,
				body: node.live.body
			})
		);
		rows.push({
			display: current.Player.identity.display,
			key: current.Player.identity.key,
			body: current.Player.live.body
		});
		return rows;
	});

	/** A being's whereabouts as one place or word, or '' when nothing is known. */
	function liveLine(live: TreeLive): string {
		if (live.location) return `@${live.location}`;
		if (live.presence === 'elsewhere') return 'elsewhere';
		if (live.presence === 'off_screen') return 'off screen';
		return '';
	}

	/** A relation's prose label, only when the writer wrote one (not the default). */
	function relationLabel(row: TreeRelation): string {
		const pair = `${row.from ?? ''}→${row.to ?? ''}`;
		return row.label && row.label !== pair ? row.label : '';
	}

	const THREAD_ICON: Record<string, string> = {
		new: '+',
		active: '~',
		paused: '=',
		resolved: '-'
	};

	// --- legacy 5-key fallback -------------------------------------------------
	//
	// Shown only when the tree read answers nothing (an engine without the route,
	// a session with no log yet). The rows are the flat fold the pane always read,
	// and every one of them writes through the flat door it came from — MINUS the
	// facts the HUD station now owns (issue #22): reprinting the clock, the date,
	// the place or the weather here would restore exactly the duplication the
	// density pass removed. Tone stays (it is the theming input) and so does every
	// other flat key the pane never had a named owner for.

	/** Facts the thread head's HUD station (`ChatHud.svelte`) is the ONE owner of. */
	const HUD_OWNED = new Set(['time', 'date', 'loc', 'location', 'weather', 'forecast']);

	const KNOWN: Array<{ key: string; label: string }> = [{ key: 'tone', label: 'Tone' }];

	const rows = $derived.by(() => {
		const ordered: Array<{ key: string; label: string; value: string }> = [];
		const seen = new Set<string>();
		for (const known of KNOWN) {
			seen.add(known.key);
			ordered.push({ key: known.key, label: known.label, value: text(facts[known.key]) });
		}
		for (const [key, value] of Object.entries(facts)) {
			if (seen.has(key) || HUD_OWNED.has(key)) continue;
			ordered.push({ key, label: key, value: text(value) });
		}
		return ordered;
	});

	const statusLabel: Record<ConnectionStatus, string> = {
		idle: 'ready',
		connecting: 'connecting…',
		streaming: 'streaming…',
		error: 'error',
		closed: 'closed'
	};

	// --- the ✎ edit model -----------------------------------------------------
	//
	// ONE open editor. Which door the ✓ takes is the target's `kind`, and every
	// registry edit carries the tool action plus its own arguments, with `field`
	// naming the single argument the draft overwrites — so the engine validates
	// the owner's edit with the exact code path the BTS tools run.
	type EditTarget =
		/** The flat `POST /state` door: time / date (D45). */
		| { kind: 'flat'; id: string; label: string }
		/** The nested `["forecast"]` pin door: an empty value clears it (D45). */
		| { kind: 'forecast'; id: string; label: string }
		/** A registry action, run through `writeEntityOp`. */
		| {
				kind: 'entity';
				id: string;
				label: string;
				op: string;
				args: Record<string, unknown>;
				field: string;
				/** Value mapping applied to the draft before it is sent (e.g. holder). */
				map?: (value: string) => string;
		  }
		/** The purse: the draft is the NEW balance, the door takes the delta. */
		| { kind: 'economy'; id: string; label: string; balance: number };

	let edit: EditTarget | null = $state(null);
	let draft = $state('');
	let busy = $state(false);
	// Last write failure, shown inline — a rejected write must never look saved.
	let editError = $state('');

	// The scalar nested path addressing the turn-level forecast HUD slot (D45).
	const FORECAST_PATH = ['forecast'];

	function focusNow(el: HTMLInputElement) {
		// a11y_autofocus: focus programmatically instead of the attribute
		queueMicrotask(() => el.focus());
	}

	/**
	 * Is an editor open on THIS object? Then — and only then — the row shows its
	 * registry id, which is the addressing detail the write door takes and never a
	 * rest label.
	 */
	function editingObject(kind: string, id: string): boolean {
		return (edit?.id ?? '').startsWith(`${kind}::${id}::`);
	}

	/**
	 * The holder a patch stores. The store keeps a held item's `holder` as a being
	 * KEY (`establish_item` resolves the name before writing, and a being's live
	 * `items` list compares canonically against that key), while `update_object`
	 * stores whatever it is handed. So a name this pane can resolve becomes its
	 * key, and anything else — a place, a prop — passes through as typed.
	 */
	function holderArg(name: string): string {
		const wanted = (name ?? '').trim();
		if (!wanted) return wanted;
		if (wanted.toLowerCase() === playerDisplay.toLowerCase()) return playerKey;
		const member = castNodes.find(
			(node) => node.participation.display.toLowerCase() === wanted.toLowerCase()
		);
		return member ? member.participation.member_id : wanted;
	}

	/**
	 * Open the editor on a leaf. `display` is what the row shows; the draft is
	 * seeded from it, except that a place keeps the leaf NAME (the `move_being`
	 * door resolves a name or an id, never a `parent > child` path).
	 */
	function beginEdit(target: EditTarget, display: string) {
		edit = target;
		editError = '';
		draft = seedDraft(target, display);
	}

	function seedDraft(target: EditTarget, display: string): string {
		if (display === '—') return '';
		if (target.kind === 'entity' && target.op === 'move_being')
			return display.split(' > ').pop()?.trim() ?? display;
		return display;
	}

	/** A flat fold leaf: the `POST /state {key, value}` door the no-tree fallback uses. */
	function beginFlat(id: string, label: string, display: string) {
		beginEdit({ kind: 'flat', id, label }, display);
	}

	/** A being/object leaf: `op` + `args`, with `field` overwritten by the draft. */
	function beginEntity(
		id: string,
		label: string,
		op: string,
		args: Record<string, unknown>,
		field: string,
		display: string,
		map?: (value: string) => string
	) {
		beginEdit({ kind: 'entity', id, label, op, args, field, ...(map ? { map } : {}) }, display);
	}

	/** A turn-level style row (today: Tone only) — `set_attr` on the scene host. */
	function beginTop(row: { key: string; label: string; value: string }) {
		beginEntity(row.key, row.label, 'set_attr', { host: 'scene', key: row.key }, 'value', row.value);
	}

	function beginGenre() {
		beginEntity('genre', 'Genre', 'set_attr', { host: 'scene', key: 'genre' }, 'value', genre);
	}

	function beginForecast() {
		beginEdit({ kind: 'forecast', id: 'forecast', label: 'Forecast' }, forecast);
	}

	/** The purse: the draft is the new balance; the door takes the difference. */
	function beginEconomy(purse: TreeEconomy) {
		beginEdit(
			{ kind: 'economy', id: `eco::${purse.id}::balance`, label: 'Balance', balance: purse.balance },
			String(purse.balance)
		);
	}

	/**
	 * Ask the tracker to reconcile NOW (W9.29 §12).
	 *
	 * The same pass the BTS cadence runs: the model states what it believes, the
	 * engine files one `reconciliation_drift` notice per disagreement, and the
	 * registry is never written — so the counts are the whole result and the view
	 * does not need a refetch. A soft failure (`error` in a 200) is shown as its
	 * own line rather than as "0 flags".
	 */
	async function checkStateNow() {
		if (!sessionId || reconcileBusy) return;
		reconcileBusy = true;
		reconcileNote = '';
		reconcileError = '';
		try {
			const result = await checkReconcileNow(sessionId);
			if (result.error) {
				reconcileError = result.error;
			} else {
				reconcileNote = `checked ${result.checked} beliefs · ${result.flags.length} flags`;
			}
		} finally {
			reconcileBusy = false;
		}
	}

	function cancel() {
		edit = null;
		busy = false;
	}

	/** The engine's readable refusal text from a non-2xx answer (never invented). */
	async function refusalText(res: Response): Promise<string> {
		try {
			const body = (await res.json()) as Record<string, unknown>;
			const detail = body.detail;
			if (typeof detail === 'string') return detail;
			if (detail && typeof detail === 'object') {
				const reason = (detail as Record<string, unknown>).reason;
				if (typeof reason === 'string') return reason;
			}
		} catch {
			// a non-JSON body carries no reason — fall through to the status line
		}
		return `the edit was refused (${res.status})`;
	}

	async function save() {
		const current = edit;
		if (!current || !sessionId || busy) return;
		const value = draft.trim();
		// A `["forecast"]` edit is the one row where an empty value is meaningful:
		// it CLEARS the pin (latest-wins, D45). Every other row needs a value.
		if (!value && current.kind !== 'forecast') return;
		busy = true;
		editError = '';
		try {
			if (current.kind === 'flat') {
				const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/state`, {
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ key: current.id, value })
				});
				if (res.ok) {
					facts[current.id] = value;
					edit = null;
					// The tree reads the same fold, so refetch it: the clock row must not
					// disagree with the door that just wrote.
					treeNonce += 1;
				} else {
					editError = await refusalText(res);
				}
			} else if (current.kind === 'forecast') {
				// Forecast pin through the nested door: the backend writes the override
				// onto the flat fold AND re-emits the HUD statement, so one refetch of
				// the HUD shows the edit without waiting for the next turn.
				const ok = await writeNestedState(sessionId, FORECAST_PATH, value);
				if (ok) {
					edit = null;
					treeNonce += 1;
					await refreshHud();
				} else {
					// Keep the editor open and surface the rejection — never leave a value
					// the engine did not accept looking saved.
					editError = 'Forecast override was not saved.';
				}
			} else if (current.kind === 'economy') {
				if (!/^-?\d+$/.test(value)) {
					editError = `a balance is a whole number of ${economy?.denomination ?? 'the purse'}`;
					return;
				}
				const delta = Number.parseInt(value, 10) - current.balance;
				// `adjust_economy` moves the balance by an amount and refuses 0 — an
				// unchanged row is simply closed, never a write the engine would refuse.
				if (delta === 0) {
					edit = null;
					return;
				}
				const result = await writeEntityOp(sessionId, 'adjust_economy', { amount: delta });
				if (result.ok) {
					edit = null;
					treeNonce += 1;
				} else {
					editError = result.error ?? 'The edit was not saved.';
				}
			} else {
				const result = await writeEntityOp(sessionId, current.op, {
					...current.args,
					[current.field]: current.map ? current.map(value) : value
				});
				if (result.ok) {
					edit = null;
					treeNonce += 1;
				} else {
					editError = result.error ?? 'The edit was not saved.';
				}
			}
		} finally {
			busy = false;
		}
	}

	/**
	 * Clear the forecast pin through the nested door: an empty value is the
	 * latest-wins clear (D45), restoring the automatic ladder, then refetch the
	 * HUD so the chip drops the pinned forecast at once.
	 */
	async function clearForecast() {
		if (!sessionId || busy) return;
		busy = true;
		try {
			const ok = await writeNestedState(sessionId, FORECAST_PATH, '');
			if (ok) {
				editError = '';
				edit = null;
				treeNonce += 1;
				await refreshHud();
			} else {
				editError = 'Forecast override was not cleared.';
			}
		} finally {
			busy = false;
		}
	}

	/**
	 * Re-read the session HUD statement after a write and publish it to the shared
	 * store. A `null` read leaves the store untouched — the write already re-emitted
	 * the statement server-side, so a transient read miss is not a lie.
	 */
	async function refreshHud() {
		if (!sessionId) return;
		const statement = await getHud(sessionId);
		if (statement) setHud(sessionId, statement);
	}
</script>

{#snippet editBox(id: string, label: string, clearable = false)}
	<input
		bind:value={draft}
		onkeydown={(e) => {
			if (e.key === 'Enter') save();
			if (e.key === 'Escape') cancel();
		}}
		aria-label={`edit ${label}`}
		use:focusNow
	/>
	<button class="ok" onclick={save} disabled={busy}>✓</button>
	{#if clearable}
		<button
			class="no clear"
			onclick={clearForecast}
			disabled={busy}
			title="Remove the pinned forecast and use the automatic outlook"
			aria-label="clear Forecast override">clear</button
		>
	{/if}
	<button class="no" onclick={cancel} disabled={busy}>✕</button>
{/snippet}

<!-- A registry leaf: the ✎ writes back through the entity door (`op` + `args`,
     with `field` overwritten by the draft), so the engine validates it with the
     tracker's own action code. -->
{#snippet entityLeaf(
	id: string,
	label: string,
	op: string,
	args: Record<string, unknown>,
	field: string,
	display: string,
	cls = ''
)}
	{#if edit?.id === id}
		<span class="edit">{@render editBox(id, label)}</span>
	{:else}
		<span class="editable">
			<span class={cls}>{display}</span>
			<button
				class="edit-btn"
				onclick={() => beginEntity(id, label, op, args, field, display)}
				aria-label={`edit ${label}`}>✎</button
			>
		</span>
	{/if}
{/snippet}

<!-- One body field of one being: its own `set_body` edit, on the being's line. -->
{#snippet bodyLeaf(key: string, field: 'pose' | 'clothing' | 'surface', display: string)}
	<span class="body-field" class:muted={display === '—'}>
		{field}
		{@render entityLeaf(
			`body::${key}::${field}`,
			`body ${field}`,
			'set_body',
			{ host: key },
			field,
			display
		)}
	</span>
{/snippet}

<aside class="sidebar" class:collapsed>
	<header>
		{#if !collapsed}<h2>State</h2>{/if}
		<div class="head-right">
			{#if !collapsed}
				<span class="status" data-status={connection}>{statusLabel[connection]}</span>
				<button
					type="button"
					class="retry-btn"
					title={connection === 'error'
						? 'Reconnect and re-run the failed turn'
						: 'Re-read the session state'}
					aria-label="Retry the failed turn"
					data-retry
					disabled={retryDisabled}
					onclick={() => onRetry?.()}
				>
					↻
				</button>
			{/if}
			<button
				type="button"
				class="collapse-btn"
				aria-expanded={!collapsed}
				aria-label={collapsed ? 'Expand state bar' : 'Collapse state bar'}
				title={collapsed ? 'Expand state bar' : 'Collapse state bar'}
				onclick={toggleCollapsed}
			>
				{collapsed ? '»' : '«'}
			</button>
		</div>
	</header>

	{#if connectionError}
		<!-- The cause, not just the state: an error that only lives in the
		     transcript leaves the rail saying "error" with nothing to act on. -->
		<p class="note connection-error" role="alert" title={connectionError}>{connectionError}</p>
	{/if}

	{#if editError}
		<p class="note" role="alert">{editError}</p>
	{/if}

	{#if tree}
	<!-- Turn-level style rows (issue #22: TONE only — the HUD station in the thread
	     head owns time / date / location / weather / forecast). The VTK palette
	     derivation reads tone (theme/tokens.py `style_tokens`), so this is the one
	     row that is a theming input rather than a scene fact. -->
		<dl class="top">
			{#each topRows as row (row.key)}
				<div>
					<dt>{row.label}</dt>
					{#if edit?.id === row.key}
						<dd class="edit">{@render editBox(row.key, row.label)}</dd>
					{:else}
						<dd class="view">
							<span>{row.value}</span>
							<button class="edit-btn" onclick={() => beginTop(row)} aria-label={`edit ${row.label}`}
								>✎</button
							>
						</dd>
					{/if}
				</div>
			{/each}
			<div>
				<dt>Genre</dt>
				{#if edit?.id === 'genre'}
					<dd class="edit">{@render editBox('genre', 'Genre')}</dd>
				{:else}
					<dd class="view">
						{#if genre === '—'}
							<span class="muted">not captured yet</span>
						{:else}
							<span>{genre}</span>
						{/if}
						<button class="edit-btn" onclick={beginGenre} aria-label="edit Genre">✎</button>
					</dd>
				{/if}
			</div>
			<div>
				<dt>Forecast</dt>
				{#if edit?.id === 'forecast'}
					<dd class="edit">{@render editBox('forecast', 'Forecast', true)}</dd>
				{:else}
					<dd class="view">
						<!-- Issue #22: the PIN's state, not the outlook. The HUD station
						     prints the value; this row is the override's own door (D45). -->
						{#if pinForecast}
							<span title="pinned forecast — the HUD station shows it">pinned</span>
						{:else}
							<span class="muted" title="no pin — the station derives the outlook"
								>automatic</span
							>
						{/if}
						<button
							class="edit-btn"
							onclick={beginForecast}
							title="Pin a forecast; clear it to return to the automatic outlook"
							aria-label="edit Forecast">✎</button
						>
					</dd>
				{/if}
			</div>
		</dl>

		<!-- The session tree, branch by branch, in the wire's own order. Rendered
		     PLAINLY (one row per thing, no new visual system): a name appears once,
		     a note and a detail line get their own line, and a node's registry id is
		     shown only while that node's editor is open. -->
		<div class="tree" data-session-tree="1">
			<details open>
				<summary>Locations <span class="count">{placeRows.length}</span></summary>
				{#if placeRows.length === 0 && !(offScreen && offScreen.occupants.length)}
					<p class="none">no locations yet</p>
				{:else}
					<!-- Indentation IS the hierarchy (an entry carries its parent's NAME
					     and its own path), so a room sits under its parent by depth alone.
					     Occupants are names, comma-joined, and never a "nobody" filler. -->
					<ul class="reg tree">
						{#each placeRows as row (`place-${row.place.id}`)}
							{@const loc = row.place}
							<li style={`--loc-depth:${row.depth}`}>
								<div class="reg-head">
									{@render entityLeaf(
										`loc::${loc.id}::name`,
										`location ${loc.name} name`,
										'update_object',
										{ id: loc.id },
										'name',
										loc.name,
										'name'
									)}
									{#if editingObject('loc', loc.id)}<span class="reg-id">{loc.id}</span>{/if}
								</div>
								<div class="reg-note">
									{@render entityLeaf(
										`loc::${loc.id}::note`,
										`location ${loc.name} note`,
										'update_object',
										{ id: loc.id },
										'note',
										loc.note || '—'
									)}
								</div>
								{#if loc.occupants.length}
									<div class="reg-people">{loc.occupants.join(', ')}</div>
								{/if}
							</li>
						{/each}
						{#if offScreen && offScreen.occupants.length}
							<li style="--loc-depth:0">
								<div class="reg-head">
									<span class="name">{offScreen.name || 'off screen'}</span>
								</div>
								{#if offScreen.at}<div class="reg-meta">at: {offScreen.at}</div>{/if}
								<div class="reg-people">{offScreen.occupants.join(', ')}</div>
							</li>
						{/if}
					</ul>
				{/if}
			</details>

			<details open>
				<summary>Items <span class="count">{tree.items.length}</span></summary>
				{#if tree.items.length === 0}
					<p class="none">no items yet</p>
				{:else}
					{#each itemGroups as group (`items-${group.label}`)}
						<div class="item-group">
							<div class="item-group-head">
								<span class="item-group-name">{group.label}</span>
								<span class="item-group-count">{group.items.length}</span>
							</div>
							<ul class="reg">
								{#each group.items as item (item.id)}
									<li>
										<div class="reg-head">
											{@render entityLeaf(
												`item::${item.id}::name`,
												'item name',
												'update_object',
												{ id: item.id },
												'name',
												item.name
											)}
											{#if editingObject('item', item.id)}
												<span class="reg-id">{item.id}</span>
											{/if}
											<span class="item-move">
												{#if edit?.id === `item::${item.id}::holder`}
													{@render editBox(`item::${item.id}::holder`, 'item holder (move)')}
												{:else}
													<button
														class="move-btn"
														aria-label={`move ${item.name} to another holder`}
														title="move to another holder"
														onclick={() =>
															beginEntity(
																`item::${item.id}::holder`,
																'item holder',
																'update_object',
																{ id: item.id },
																'holder',
																item.holder ?? '',
																holderArg
															)}
														>⇄</button
													>
												{/if}
											</span>
										</div>
										{#if item.note}<div class="reg-note">{item.note}</div>{/if}
									</li>
								{/each}
							</ul>
						</div>
					{/each}
				{/if}
			</details>

			<details open>
				<summary>Threads <span class="count">{threadRows.length}</span></summary>
				{#if threadRows.length === 0}
					<p class="none">no threads yet</p>
				{:else}
					<ul class="reg">
						{#each threadRows as thread (thread.id)}
							<li data-status={thread.status}>
								<div class="reg-head">
									<span class="thread-icon" aria-hidden="true"
										>{THREAD_ICON[thread.status] ?? '~'}</span
									>
									<span class="name">{thread.name.replace(/_/g, ' ')}</span>
									{@render entityLeaf(
										`thread::${thread.id}::status`,
										'thread status',
										'update_object',
										{ id: thread.id },
										'status',
										thread.status,
										'thread-status'
									)}
									{#if editingObject('thread', thread.id)}
										<span class="reg-id">{thread.id}</span>
									{/if}
								</div>
								<div class="reg-meta">
									{@render entityLeaf(
										`thread::${thread.id}::detail`,
										'thread detail',
										'update_object',
										{ id: thread.id },
										'detail',
										thread.detail || '—'
									)}
								</div>
								{#if thread.note}<div class="reg-note">{thread.note}</div>{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</details>

			<details open>
				<summary>Relationships <span class="count">{relationRows.length}</span></summary>
				{#if relationRows.length === 0}
					<p class="none">no relationships yet</p>
				{:else}
					<ul class="reg">
						{#each relationRows as row (row.id)}
							<li>
								<div class="rel-head">
									<span class="rel-char">{row.from ?? '—'}</span>
									<span class="rel-arrow" aria-hidden="true">→</span>
									<span class="rel-target">{row.to ?? '—'}</span>
									{#if editingObject('rel', row.id)}
										<span class="reg-id">{row.id}</span>
									{/if}
								</div>
								{#if relationLabel(row)}<div class="reg-meta">{relationLabel(row)}</div>{/if}
								<div class="reg-note">
									{@render entityLeaf(
										`rel::${row.id}::note`,
										'relationship note',
										'update_object',
										{ id: row.id },
										'note',
										row.note || '—'
									)}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</details>

			<details open>
				<summary
					>Meters <span class="count"
						>{meterGroups.length + (sceneRows.length ? 1 : 0)}</span
					></summary
				>
				{#if meterGroups.length === 0 && sceneRows.length === 0}
					<p class="none">no meters yet</p>
				{:else}
					<!-- The scene's own rows first: the wire serves them in the `scene`
					     branch only, so only the keys the top rows do not already show
					     (tension, irony) land here — never a second copy of weather. -->
					{#if sceneRows.length}
						<div class="meter-group">
							<div class="reg-head"><span class="name">Scene</span></div>
							<ul class="reg">
								{#each sceneRows as row (`scene-${row.key}`)}
									<li>
										<div class="reg-head">
											<span class="reg-key">{row.key}</span>
											{@render entityLeaf(
												`meter::scene::${row.key}::`,
												`${row.key} value`,
												'set_attr',
												{ host: 'scene', key: row.key },
												'value',
												String(row.value),
												'reg-value'
											)}
										</div>
									</li>
								{/each}
							</ul>
						</div>
					{/if}
					{#each meterGroups as group (group.host)}
						<div class="meter-group">
							<div class="reg-head"><span class="name">{hostLabel(group.host)}</span></div>
							<ul class="reg">
								{#each [...group.numbers, ...group.traits] as row, rowIndex (`row-${rowIndex}-${row.key}-${row.subject ?? ''}`)}
									<li>
										<div class="reg-head">
											<span class="reg-key">{row.key}</span>
											{@render entityLeaf(
												`meter::${group.host}::${row.key}::${row.subject ?? ''}`,
												`${row.key} value`,
												'set_attr',
												row.subject
													? { host: beingArg(group.host), key: row.key, subject: row.subject }
													: { host: beingArg(group.host), key: row.key },
												'value',
												String(row.value),
												'reg-value'
											)}
											{#if row.subject}<span class="reg-meta">→ {row.subject}</span>{/if}
										</div>
									</li>
								{/each}
							</ul>
						</div>
					{/each}
				{/if}
			</details>

			<details open>
				<summary>Conditions <span class="count">{conditionRows.length}</span></summary>
				{#if conditionRows.length === 0}
					<p class="none">no conditions yet</p>
				{:else}
					<ul class="reg">
						{#each conditionRows as condition (condition.id)}
							<li>
								<div class="reg-head">
									<span class="name">{hostLabel(condition.host ?? 'scene')}</span>
									<span class="reg-key">{condition.key}</span>
									{@render entityLeaf(
										`cond::${condition.id}::severity`,
										`${condition.key} severity`,
										'set_condition',
										{ host: beingArg(condition.host), key: condition.key },
										'severity',
										condition.severity,
										'reg-value'
									)}
									{#if condition.expired}<span class="reg-meta">expired</span>{/if}
									{#if editingObject('cond', condition.id)}
										<span class="reg-id">{condition.id}</span>
									{/if}
								</div>
								{#if condition.source}
									<div class="reg-meta">source: {condition.source}</div>
								{/if}
								{#if condition.expires_at}
									<div class="reg-meta">expires {condition.expires_at}</div>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</details>

			<details open>
				<summary>Bodies <span class="count">{bodyRows.length}</span></summary>
				{#if bodyRows.length === 0}
					<p class="none">no beings yet</p>
				{:else}
					<ul class="reg">
						{#each bodyRows as row (`body-${row.key}`)}
							<li>
								<div class="reg-head">
									<span class="name">{row.display}</span>
									<!-- the raw identity key, only while this being's editor is open -->
									{#if editingObject('body', row.key)}
										<span class="reg-id">{row.key}</span>
									{/if}
								</div>
								<!-- One line per being: pose / clothes / surface, each its own
								     `set_body` edit and no separators at rest. -->
								<div class="body-line">
									{@render bodyLeaf(row.key, 'pose', row.body?.pose || '—')}
									{@render bodyLeaf(
										row.key,
										'clothing',
										(row.body?.clothing ?? []).join(', ') || '—'
									)}
									{@render bodyLeaf(row.key, 'surface', row.body?.surface || '—')}
								</div>
							</li>
						{/each}
					</ul>
				{/if}
			</details>

			<details open>
				<summary>Economy</summary>
				{#if economy}
					{@const purse = economy}
					<div class="reg-head">
						{#if edit?.id === `eco::${purse.id}::balance`}
							<span class="edit"
								>{@render editBox(`eco::${purse.id}::balance`, 'balance')}</span
							>
						{:else}
							<span class="editable">
								<span class="reg-value">{purse.balance}</span>
								<button class="edit-btn" onclick={() => beginEconomy(purse)} aria-label="edit balance"
									>✎</button
								>
							</span>
						{/if}
						<span class="reg-key">{purse.denomination}</span>
						{#if editingObject('eco', purse.id)}<span class="reg-id">{purse.id}</span>{/if}
					</div>
					{#if purse.note}<div class="reg-note">{purse.note}</div>{/if}
				{:else}
					<p class="none">no purse yet</p>
				{/if}
			</details>

			<!-- Cast: the roster's own facts plus where each member is right now.
			     Meters / conditions / bodies / items live in their own branches. -->
			<details open>
				<summary>Cast <span class="count">{castNodes.length}</span></summary>
				{#if castNodes.length === 0}
					<p class="none">no cast yet</p>
				{:else}
					<ul class="reg">
						{#each castNodes as node, index (`cast-${index}-${node.participation.member_id}`)}
							<li>
								<div class="reg-head">
									<span class="name">{node.participation.display}</span>
									{#if node.participation.status !== 'active'}
										<span class="reg-meta">{node.participation.status}</span>
									{/if}
								</div>
								{#if liveLine(node.live)}
									<div class="reg-meta">{liveLine(node.live)}</div>
								{/if}
							</li>
						{/each}
					</ul>
				{/if}
			</details>

			<!-- Player: the same row a member gets, on the reserved identity key. The
			     `Player (<Name>)` display IS the identity; `persona_ref` is addressing,
			     so it stays on the wire and never prints here.
			     Issue #22: the MOVE door lives here, on the line that already shows
			     where the player is. The retired Location top row was that door's only
			     home, and the fact now has one owner (the HUD) — but the WRITE path is
			     not a duplicate fact, so it moves to the row that still names it
			     (`move_being`, the tracker's own action; the engine validates it). -->
			<details open>
				<summary>Player</summary>
				<div class="reg-head">
					<span class="name">{playerDisplay}</span>
				</div>
				{#if playerNode}
					<div class="reg-meta">
						{#if edit?.id === 'loc'}
							<span class="edit">{@render editBox('loc', 'Location')}</span>
						{:else}
							<span class="editable">
								<!-- Always rendered, even unplaced: an EMPTY place is exactly
								     when the move door is needed. -->
								<span>{liveLine(playerNode.live) || '—'}</span>
								<button
									class="edit-btn"
									onclick={() =>
										beginEntity(
											'loc',
											'Location',
											'move_being',
											{ who: playerKey },
											'to',
											playerNode?.live.location ?? ''
										)}
									aria-label="edit Location"
									title="Move the player (a registry move is a deterministic presence change)"
									>✎</button
								>
							</span>
						{/if}
					</div>
				{/if}
			</details>

			<!-- W9.29 §12: the manual door onto the reconciliation pass. It asks the
			     tracker what it believes and files one flag per disagreement; it never
			     writes the registry (a check that "fixed" what it found would be the
			     silent overwrite the pass exists to prevent). -->
			<div class="reconcile-door">
				<button
					class="check-now"
					type="button"
					onclick={checkStateNow}
					disabled={reconcileBusy || !sessionId}
					title="Ask the tracker what it believes and flag every disagreement with the registry"
				>
					{reconcileBusy ? 'Checking…' : 'Check state now'}
				</button>
				{#if reconcileNote}
					<span class="check-note" role="status">{reconcileNote}</span>
				{/if}
			</div>
			{#if reconcileError}
				<p class="note" role="alert">{reconcileError}</p>
			{/if}
		</div>
	{:else}
		<!-- No tree (an engine without the route, or a session with no log yet): the
		     flat fold rows the pane always read, each on the flat door it came from. -->
		<dl class="fallback">
			{#each rows as row (row.key)}
				<div>
					<dt>{row.label}</dt>
					{#if edit?.id === row.key}
						<dd class="edit">{@render editBox(row.key, row.label)}</dd>
					{:else}
						<dd class="view">
							<span>{row.value}</span>
							<button
								class="edit-btn"
								onclick={() => beginFlat(row.key, row.label, row.value)}
								aria-label={`edit ${row.label}`}>✎</button
							>
						</dd>
					{/if}
				</div>
			{/each}
		</dl>
	{/if}
</aside>

<style>
	.sidebar {
		width: 15rem;
		flex: none;
		border-right: 1px solid var(--line);
		padding: 0.85rem 0.75rem;
		overflow-y: auto;
		background: var(--bg-panel);
		transition:
			width 160ms ease,
			padding 160ms ease;
	}
	/* Collapsed state bar (T1): a narrow strip holding only the toggle, mirroring
	   the D40 rail. The bar is a left-docked column, so width is the animated
	   axis; everything but the header is dropped from view for the strip. */
	.sidebar.collapsed {
		width: 2.4rem;
		/* Vertical padding only: the collapsed strip is a 2.4rem column holding one
		   toggle, so horizontal padding is chrome the reader pays for twice (the
		   rail beside it is the same 2.4rem). Measured 39.4px, matching the rail —
		   the pair is the 77-79px floor the density audit priced. */
		padding: 0.85rem 0;
		overflow: hidden;
	}
	.sidebar.collapsed > :not(header) {
		display: none;
	}
	.sidebar.collapsed header {
		flex-direction: column;
		align-items: center;
		/* The pane's undock button is absolutely positioned at the dock's
		   top-right (z-index 5); keep the toggle below it so it stays
		   clickable in the narrow collapsed strip. */
		padding: 1.8rem 0 0 0;
	}
	header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.5rem;
		margin-bottom: 0.6rem;
		/* Reserve the undock-button zone (top-right, z-index 5) so the collapse
		   toggle is not covered: Playwright gate caught the overlap. */
		padding-right: 1.9rem;
	}
	.head-right {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		margin-left: auto;
	}
	.sidebar.collapsed .head-right {
		margin-left: 0;
	}
	.collapse-btn {
		flex: none;
		width: 1.5rem;
		height: 1.5rem;
		border: 0;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		font-size: 0.85rem;
		line-height: 1;
		padding: 0;
	}
	.collapse-btn:hover,
	.collapse-btn:focus-visible {
		color: var(--text);
	}
	h2 {
		margin: 0;
		font-size: var(--font-micro);
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.status {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.status[data-status='streaming'],
	.status[data-status='connecting'] {
		color: var(--accent-strong);
	}
	.status[data-status='error'] {
		color: var(--danger-text);
	}
	/* The retry door: pressable in EVERY state, the error one included — that is
	   the whole point. It only greys out while a turn it would re-run is live. */
	.retry-btn {
		flex: none;
		width: 1.35rem;
		height: 1.35rem;
		border: 1px solid var(--accent-line);
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		font-size: 0.8rem;
		line-height: 1;
		cursor: pointer;
	}
	.retry-btn:hover:not(:disabled),
	.retry-btn:focus-visible {
		color: var(--text);
	}
	.retry-btn:disabled {
		opacity: 0.45;
		cursor: default;
	}
	/* The cause line wraps rather than clipping: a rate limit's own words are the
	   part the reader needs. */
	.connection-error {
		white-space: pre-wrap;
		overflow-wrap: anywhere;
	}
	dl {
		margin: 0;
		display: grid;
		gap: 0.5rem;
	}
	dl > div {
		display: grid;
		gap: 0.1rem;
	}
	dt {
		font-size: var(--font-micro);
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	dd {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text);
		overflow-wrap: anywhere;
	}
	.muted {
		color: var(--text-dim);
	}
	.note {
		margin: 0.4rem 0 0;
		font-size: var(--font-micro);
		color: var(--danger-text);
	}

	/* edit overlay (W2.12) */
	.edit-btn {
		border: 0;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		font-size: 0.8rem;
		padding: 0 0.15rem;
		opacity: 0;
	}
	dl > div:hover .edit-btn,
	.editable:hover .edit-btn,
	.body-field:hover .edit-btn {
		opacity: 1;
	}
	/* an inline leaf with its ✎: same editor, lane styling kept */
	.editable {
		display: inline-flex;
		align-items: baseline;
		gap: 0.25rem;
	}
	dd.view {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.25rem;
	}
	.edit {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		flex: 1 1 8rem;
	}
	.edit input {
		flex: 1;
		min-width: 0;
		background: var(--bg);
		border: 1px solid var(--line);
		color: var(--text);
		font-size: var(--font-small);
		padding: 0.15rem 0.35rem;
		border-radius: 4px;
	}
	.edit button {
		border: 0;
		border-radius: 4px;
		cursor: pointer;
		font-size: 0.8rem;
		padding: 0.15rem 0.4rem;
	}
	.ok {
		background: var(--accent-strong);
		color: var(--bg-panel);
	}
	.no {
		background: transparent;
		color: var(--text-muted);
		border: 1px solid var(--line);
	}
	/* the forecast editor's explicit clear door (D45) */
	.edit .clear {
		font-size: var(--font-micro);
		white-space: nowrap;
	}

	/* --- the session tree ---------------------------------------------------- */
	.tree {
		margin-top: 0.85rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	/* Deliberately plain (no new visual system), one row per object. The raw
	   identity key is a RECORDING detail revealed only while that row's editor is
	   open — the resting view reads as people. */
	.tree ul.reg {
		list-style: none;
		margin: 0 0 0.5rem;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.tree .reg-head {
		display: flex;
		align-items: baseline;
		gap: 0.4rem;
	}
	/* The location tree — indentation IS the hierarchy (a place carries its
	   parent's name and its own path, and the pane does not repeat either). */
	.tree ul.tree > li {
		padding-left: calc(var(--loc-depth, 0) * 0.8rem);
	}
	.tree .reg-id {
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-dim);
		letter-spacing: 0.02em;
	}
	.tree .reg-key {
		font-size: var(--font-small);
		color: var(--text);
	}
	.tree .reg-value {
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text);
		font-variant-numeric: tabular-nums;
	}
	.tree .item-group + .item-group,
	.tree .meter-group + .meter-group {
		margin-top: 0.5rem;
	}
	.tree .item-group-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.4rem;
		padding: 0.15rem 0 0.05rem;
		border-bottom: 1px solid var(--line);
		margin-bottom: 0.2rem;
	}
	.tree .item-group-name {
		font-size: var(--font-micro);
		font-weight: 600;
		color: var(--text);
		text-transform: none;
	}
	.tree .item-group-count {
		font-size: var(--font-micro);
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}
	.tree .reg-meta,
	.tree .reg-note,
	.tree .reg-people {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.tree .reg-people {
		color: var(--text-dim);
	}
	/* one line per being: pose / clothes / surface, each its own edit */
	.tree .body-line {
		display: flex;
		flex-wrap: wrap;
		gap: 0.15rem 0.6rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.tree .body-field {
		display: inline-flex;
		align-items: baseline;
		gap: 0.25rem;
	}
	/* W9.29 §12: the manual reconciliation door — plain, like the view it sits on. */
	.tree .reconcile-door {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		margin: 0.5rem 0 0.25rem;
	}
	.tree .check-now {
		border: 1px solid var(--line);
		background: none;
		color: var(--text-muted);
		cursor: pointer;
		font-size: var(--font-micro);
		padding: 0.15rem 0.45rem;
		border-radius: 4px;
	}
	.tree .check-now:hover:not(:disabled) {
		color: var(--text);
	}
	.tree .check-now:disabled {
		cursor: default;
		opacity: 0.6;
	}
	.tree .check-note {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	details {
		border-top: 1px solid var(--line);
	}
	details > summary {
		cursor: pointer;
		list-style: none;
		display: flex;
		align-items: center;
		gap: 0.4rem;
		padding: 0.4rem 0;
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text-muted);
	}
	details > summary::-webkit-details-marker {
		display: none;
	}
	details > summary::before {
		content: '▸';
		font-size: 0.7rem;
		color: var(--text-dim);
		transition: transform 120ms ease;
	}
	details[open] > summary::before {
		transform: rotate(90deg);
	}
	.count {
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-dim);
		font-variant-numeric: tabular-nums;
	}
	.none {
		margin: 0 0 0.4rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}

	.name {
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text);
	}
	.rel-head {
		display: flex;
		align-items: baseline;
		gap: 0.3rem;
		font-size: var(--font-small);
	}
	.rel-char {
		color: var(--text);
		font-weight: 600;
	}
	.rel-arrow {
		color: var(--text-dim);
	}
	.rel-target {
		color: var(--accent-strong);
	}
	.thread-icon {
		font-weight: 700;
		color: var(--accent-strong);
	}
	li[data-status='resolved'] .thread-icon {
		color: var(--text-dim);
	}
	li[data-status='paused'] .thread-icon {
		color: var(--warn);
	}
	.thread-status {
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	/* the item row's holder door: the group head already names the holder, so the
	   affordance is a plain button and the editor opens in its place */
	.tree .item-move {
		margin-left: auto;
	}
	.tree .move-btn {
		background: none;
		border: 1px solid var(--line);
		border-radius: 4px;
		color: var(--text-dim);
		cursor: pointer;
		font-size: var(--font-micro);
		line-height: 1;
		padding: 0.05rem 0.3rem;
	}
	.tree .move-btn:hover {
		color: var(--text);
		background: var(--bg-hover);
	}
</style>
