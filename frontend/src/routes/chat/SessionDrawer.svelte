<script lang="ts">
	// Chat rail (W9.2, D40): collapsible, layered nav + the chat list.
	//
	// Presentational only — the parent owns the store, URL and API calls. The
	// list is derived from the event log by the backend; a rename appends a
	// revision and a delete is a soft hide, so this component just reflects the
	// refreshed list it is handed.
	//
	// Layering (D40): WORLDS -> PERSONAS -> USER PERSONA -> CHATS, driven by ONE
	// scope. Selecting a world re-queries the list with `?world_id=`; selecting
	// an actor re-queries with `?actor_id=` (the chats that actor is WITH, never
	// the chats of the worlds it appears in). D53 split the two records: the
	// actor (`actor_id` / `actor_ids`, folded from `chat_actor`) is the CAST the
	// row is grouped and badged by; `persona_id` is now the USER persona only,
	// read by the USER PERSONA slot + the per-row picker. Rows carry both, so no
	// per-session config fetch is needed.
	//
	// The unscoped "All chats" view keeps BOTH buckets: actor-attributed chats
	// first, then the trailing "Chats" group (the D53 rename of the old Sandbox
	// bucket). World-materialised chats carry no `chat_actor`, so a "known actor
	// only" filter would make them vanish.
	import { onMount } from 'svelte';
	import type {
		Book,
		Persona,
		SessionSummary,
		SessionWorld,
		SettingsCatalog,
		World
	} from '#lib/api';
	import {
		assignChatPersona,
		attachSessionBooks,
		createSessionWithPersona,
		getSessionWorld,
		getSettings,
		listBooks,
		listPersonas,
		listSessions,
		listWorlds,
		patchDefaultPersona,
		setChatMemory,
		startWorldChat
	} from '#lib/api';
	import DefinitionControls from '#lib/components/DefinitionControls.svelte';
	// ONE kind registry (#lib/studio/kinds) — the drawer's doors and the rail's
	// footer map name the same kinds as the studio shell.
	import type { StudioKind } from '#lib/studio/kinds';
	import BottomController from './BottomController.svelte';

	/** A door's optional targeting: focus one row, or jump to the create form. */
	interface StudioOpenOptions {
		focusId?: string | null;
		startNew?: boolean;
	}

	let {
		sessions,
		currentId,
		onSelect,
		onNew,
		onRename,
		onDelete,
		onOpenStudio,
		onStartWorldChat,
		onStartPersonaChat,
		refreshToken
	}: {
		sessions: SessionSummary[];
		currentId: string | null;
		onSelect: (id: string) => void;
		onNew: () => void;
		onRename: (id: string, title: string) => void;
		onDelete: (id: string) => void;
		/** The single definition door (D51): open the Design Studio on `kind`,
		 *  optionally focusing one row (`focusId`) or the create form (`startNew`).
		 *  The rail holds NO definition CRUD of its own — every ✎ / New / tools
		 *  entry below is a door into the studio, never a form. */
		onOpenStudio: (kind: StudioKind, opts?: StudioOpenOptions) => void;
		/** A world row's "+" materialized a chat: the parent refreshes its session
		 *  list, then opens it. Optional — an unwired mount falls back to a plain
		 *  `onSelect`. */
		onStartWorldChat?: (sessionId: string) => void;
		/** A persona row's "+" created a chat: the parent refreshes its session
		 *  list, then opens it — the same order the world "+" uses. Optional — an
		 *  unwired mount falls back to a plain `onSelect`. Without the refresh the
		 *  persona picker cannot resolve `currentSession` and shows "No chat open"
		 *  until the first turn refreshes the list. */
		onStartPersonaChat?: (sessionId: string) => void;
		/** Bumped by the parent after a studio edit: re-reads the rail's catalogs
		 *  (personas / worlds / books) so a definition created in the studio shows
		 *  up here without a page reload. */
		refreshToken?: number;
	} = $props();

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	// --- rail collapse (D40, issue #22 auto-collapse) ---
	// Collapse lives entirely here: the rail's width is this component's own
	// style, so `+page.svelte` never learns about it.
	//
	// Issue #22 adds the auto rule the density audit asked for: below 1440px the
	// rail starts collapsed (241px of permanent chrome at 1280 is 19% of the
	// viewport for a chat list). The reader's own choice still wins — a stored '1'
	// or '0' is an EXPLICIT state and is never overruled — so the key keeps its
	// meaning and only "never chosen" follows the viewport.
	const COLLAPSE_KEY = 'fabulist.rail.collapsed';
	/** Below this viewport width the rail starts collapsed (issue #22). */
	const AUTO_COLLAPSE_MAX_PX = 1440;
	let railCollapsed = $state(false);
	/** True once the reader has chosen a state; an explicit choice outranks auto. */
	let railChoice = false;

	function toggleRail(): void {
		railCollapsed = !railCollapsed;
		railChoice = true;
		// Persist best-effort; `window` is guarded because this can be reached
		// from an event that also runs during hydration (SSR has no window).
		if (typeof window !== 'undefined') {
			window.localStorage.setItem(COLLAPSE_KEY, railCollapsed ? '1' : '0');
		}
	}

	// --- scope (D40) ---
	/** One rail scope; `all` is the root. */
	type Scope = { kind: 'all' } | { kind: 'world'; id: string } | { kind: 'actor'; id: string };

	// A trail, not a single value: an actor reached from inside a world keeps
	// that world as its parent crumb, so the breadcrumb reads
	// `All chats / <world> / <actor>` and Escape pops exactly one level.
	let scopeTrail = $state<Scope[]>([{ kind: 'all' }]);
	const scope = $derived(scopeTrail[scopeTrail.length - 1] ?? { kind: 'all' });

	function scopeParams(active: Scope): { actor_id?: string; world_id?: string } | null {
		if (active.kind === 'all') return null;
		return active.kind === 'world' ? { world_id: active.id } : { actor_id: active.id };
	}

	function showWorld(worldId: string): void {
		scopeTrail = [{ kind: 'all' }, { kind: 'world', id: worldId }];
	}

	/** A persona picked from the PERSONAS layer: its own personal chats. */
	function showActor(personaId: string): void {
		scopeTrail = [{ kind: 'all' }, { kind: 'actor', id: personaId }];
	}

	function popScope(): void {
		if (scopeTrail.length > 1) scopeTrail = scopeTrail.slice(0, -1);
	}

	function popTo(index: number): void {
		scopeTrail = scopeTrail.slice(0, index + 1);
	}

	// --- catalogs ---
	let personas = $state<Persona[]>([]);
	let worlds = $state<World[]>([]);
	// Pinned global default persona (user request). New chats inherit it when no
	// persona is supplied; the picker shows it as the effective value with an
	// "inherited" marker rather than pretending it is this chat's own assignment.
	let defaultPersonaId = $state<string | null>(null);
	let defaultPersonaLabel = $state<string | null>(null);
	let pinBusy = $state(false);
	let status = $state('');
	let personaBusy = $state<string | null>(null);
	/** World id whose "+" (start world chat) is in flight — blocks double-starts. */
	let worldBusy = $state<string | null>(null);
	let rowBusy = $state<Record<string, boolean>>({});

	// The rail's catalogs re-load whenever the parent bumps `refreshToken` after a
	// studio edit; the first run is the mount load, so there is no separate one.
	$effect(() => {
		void refreshToken;
		void loadPersonas();
		void loadWorlds();
		void loadBooks();
		void loadSettings();
	});

	onMount(() => {
		if (typeof window === 'undefined') return;
		const saved = window.localStorage.getItem(COLLAPSE_KEY);
		if (saved === '1' || saved === '0') {
			// An explicit choice — honour it and stop following the viewport.
			railChoice = true;
			railCollapsed = saved === '1';
			return;
		}
		// Never chosen: follow the width. A resize keeps following it until the
		// reader picks a state, so a docked-then-undocked window stays honest.
		const narrow = window.matchMedia(`(max-width: ${AUTO_COLLAPSE_MAX_PX - 1}px)`);
		railCollapsed = narrow.matches;
		const sync = () => {
			if (!railChoice) railCollapsed = narrow.matches;
		};
		narrow.addEventListener('change', sync);
		return () => narrow.removeEventListener('change', sync);
	});

	async function loadPersonas(): Promise<void> {
		try {
			personas = await listPersonas();
		} catch (err) {
			status = message(err);
		}
	}

	async function loadWorlds(): Promise<void> {
		try {
			worlds = await listWorlds();
		} catch (err) {
			status = message(err);
		}
	}

	async function loadBooks(): Promise<void> {
		try {
			books = await listBooks();
		} catch (err) {
			status = message(err);
		}
	}

	/** Load the global settings surface (currently just the default-persona pin). */
	async function loadSettings(): Promise<void> {
		try {
			applySettings(await getSettings());
		} catch (err) {
			status = message(err);
		}
	}

	function applySettings(catalog: SettingsCatalog): void {
		defaultPersonaId = catalog.default_persona_id ?? null;
		defaultPersonaLabel = catalog.default_persona_label ?? null;
	}

	function worldName(worldId: string | null): string {
		return worlds.find((world) => world.world_id === worldId)?.name || worldId || 'World';
	}

	function actorName(personaId: string | null): string {
		return personas.find((persona) => persona.persona_id === personaId)?.name || 'Unnamed persona';
	}

	// --- scoped list (D40) ---
	// `scopedSessions` holds the server-filtered rows for `world` / `actor`;
	// the unscoped view reads the parent's `sessions` prop directly.
	let scopedSessions = $state<SessionSummary[] | null>(null);
	let scopeBusy = $state(false);
	// Bumped on every scope query so a slow response from a previous scope can
	// never overwrite the current list.
	let scopeToken = 0;

	/** Re-run the server-side scope query for the active scope. */
	async function reloadScoped(): Promise<void> {
		const params = scopeParams(scope);
		if (!params) {
			scopeToken += 1;
			scopedSessions = null;
			scopeBusy = false;
			return;
		}
		const token = (scopeToken += 1);
		scopeBusy = true;
		try {
			const rows = await listSessions(params);
			if (token === scopeToken) {
				scopedSessions = rows;
				status = '';
			}
		} catch (err) {
			if (token === scopeToken) {
				scopedSessions = [];
				status = message(err);
			}
		} finally {
			if (token === scopeToken) scopeBusy = false;
		}
	}

	// The active scope drives the query; `scope` is read directly (a `get(store)`
	// inside an effect would not track — AGENTS.md).
	$effect(() => {
		const active = scope;
		if (active.kind === 'all') {
			scopeToken += 1;
			scopedSessions = null;
			scopeBusy = false;
			return;
		}
		void reloadScoped();
	});

	/** The rows the CHATS layer renders: the parent's list, or the scoped query. */
	const chatRows = $derived(scope.kind === 'all' ? sessions : (scopedSessions ?? []));

	// --- optimistic row values ---
	// One small override map, not a config fetch: it bridges a PATCH and the
	// parent's next list refresh so the row controls do not snap back.
	let rowOverride = $state<
		Record<
			string,
			{ persona_id?: string | null; persona_cleared?: boolean; memory_enabled?: boolean }
		>
	>({});

	function setRowOverride(
		id: string,
		patch: { persona_id?: string | null; persona_cleared?: boolean; memory_enabled?: boolean }
	): void {
		rowOverride = { ...rowOverride, [id]: { ...rowOverride[id], ...patch } };
	}

	function rowPersonaId(session: SessionSummary): string | null {
		const override = rowOverride[session.id];
		if (override && 'persona_id' in override) return override.persona_id ?? null;
		return session.persona_id;
	}

	/**
	 * True when this row carries an EXPLICIT "no persona" clear (owner ruling:
	 * clear = no persona, not "use the default again"). A never-set chat also has
	 * `persona_id === null` but inherits the pinned default — only this flag
	 * tells the two apart.
	 *
	 * The server field (`GET /api/sessions` -> `persona_cleared`) lands on the
	 * `SessionSummary` type via an orchestrator-owned follow-up to the api.ts
	 * split lane; read it defensively so this component is correct both before
	 * and after the client type catches up (absent -> never explicitly cleared).
	 */
	function rowPersonaCleared(session: SessionSummary): boolean {
		const override = rowOverride[session.id];
		if (override && 'persona_cleared' in override) return override.persona_cleared ?? false;
		return (session as SessionSummary & { persona_cleared?: boolean }).persona_cleared === true;
	}

	function rowMemory(session: SessionSummary): boolean {
		const override = rowOverride[session.id];
		return override?.memory_enabled ?? session.memory_enabled;
	}

	/** The chat's recorded actor ids (D53 `chat_actor` fold). Read defensively so
	 *  the rail is correct before/after the client type carries the new fields. */
	function rowActorIds(session: SessionSummary): string[] {
		const ids = (session as SessionSummary & { actor_ids?: string[] }).actor_ids;
		if (!Array.isArray(ids)) return [];
		return ids.filter((id): id is string => typeof id === 'string' && id.length > 0);
	}

	/** The row's primary actor: the explicit `actor_id`, else the first folded id.
	 *  This is the CAST record — the user persona (`rowPersonaId`) is separate. */
	function rowActorId(session: SessionSummary): string | null {
		const primary = (session as SessionSummary & { actor_id?: string | null }).actor_id;
		if (typeof primary === 'string' && primary) return primary;
		return rowActorIds(session)[0] ?? null;
	}

	/** True when the row carries an actor that exists in the catalog. */
	function hasKnownActor(session: SessionSummary): boolean {
		const aid = rowActorId(session);
		return Boolean(aid && personas.some((persona) => persona.persona_id === aid));
	}

	/** Row badge text: the primary actor's name, or `Chat` for a chat with no
	 *  recorded actor (plain and world-materialised chats share the "Chats"
	 *  bucket, the D53 rename of the old Sandbox group). */
	function badgeLabel(session: SessionSummary): string {
		return hasKnownActor(session) ? actorName(rowActorId(session)) : 'Chat';
	}

	// --- layer counts (from the parent's unscoped list) ---
	// PERSONAS layer counts (D53): count each chat under every recorded actor, so
	// the actor rows reflect the chat's CAST, not the user persona.
	const actorCounts = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const session of sessions) {
			for (const aid of rowActorIds(session)) {
				if (personas.some((persona) => persona.persona_id === aid)) {
					counts.set(aid, (counts.get(aid) ?? 0) + 1);
				}
			}
		}
		return counts;
	});

	const worldCounts = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const session of sessions) {
			if (session.world_id) counts.set(session.world_id, (counts.get(session.world_id) ?? 0) + 1);
		}
		return counts;
	});

	// Unscoped CHATS buckets (D53): actor-attributed chats first, then the
	// "Chats" group for everything else (no recorded actor). Keeping the trailing
	// bucket is what keeps world-materialised chats (whose cast derives from the
	// world, not a `chat_actor` row) visible instead of filtered out of existence.
	const unscopedBuckets = $derived.by(() => {
		const actorChats: SessionSummary[] = [];
		const chats: SessionSummary[] = [];
		for (const session of sessions) {
			if (hasKnownActor(session)) actorChats.push(session);
			else chats.push(session);
		}
		return { actorChats, chats };
	});

	/** One breadcrumb step; the label is resolved late (catalogs load async). */
	interface Crumb {
		kind: Scope['kind'];
		id: string | null;
		label: string;
	}

	const crumbs = $derived.by((): Crumb[] =>
		scopeTrail.map((step): Crumb => {
			if (step.kind === 'all') return { kind: 'all', id: null, label: 'All chats' };
			if (step.kind === 'world') return { kind: 'world', id: step.id, label: worldName(step.id) };
			return { kind: 'actor', id: step.id, label: actorName(step.id) };
		})
	);

	const scopeEmptyText = $derived(
		scope.kind === 'world'
			? 'No chats in this world yet.'
			: scope.kind === 'actor'
				? 'No chats for this actor yet.'
				: 'No chats yet — start one above.'
	);

	// --- USER PERSONA slot ---
	// Shows who the open chat plays as (D43 `{{user}}`). Assignment still rides
	// each row's select; this slot's Edit/New are doors into the studio's Personas
	// tab (D51), never a form.
	const currentSession = $derived(
		(currentId ? sessions.find((session) => session.id === currentId) : undefined) ??
			(currentId && scopedSessions
				? scopedSessions.find((session) => session.id === currentId)
				: undefined) ??
			null
	);

	/** The persona this chat's own log assigns (never the inherited default). */
	const currentAssignedPersonaId = $derived(
		currentSession ? rowPersonaId(currentSession) : null
	);

	const currentPersona = $derived(
		personas.find((persona) => persona.persona_id === currentAssignedPersonaId) ?? null
	);

	const defaultPersona = $derived(
		personas.find((persona) => persona.persona_id === defaultPersonaId) ?? null
	);

	/** True when the chat carries an explicit "no persona" clear. */
	const currentPersonaCleared = $derived(
		currentSession ? rowPersonaCleared(currentSession) : false
	);

	/** True when the chat has no own persona and falls back to the pinned default.
	 *  An explicit clear suppresses inheritance (clear = no persona). */
	const personaInherited = $derived(
		!currentAssignedPersonaId && !currentPersonaCleared && defaultPersona !== null
	);

	/** The persona the context actually uses: own assignment, else the default. */
	const effectivePersona = $derived(currentPersona ?? (personaInherited ? defaultPersona : null));

	/** A select value representing the inherited-default state (no own
	 *  assignment, no explicit clear). Selecting it is a no-op — it exists so the
	 *  picker can show "inherited from default" distinctly from "No persona". */
	const INHERIT_VALUE = '__inherit__';

	/** Picker value: the own assignment, else inherited, else explicit none. */
	const personaPickerValue = $derived(
		currentAssignedPersonaId ?? (personaInherited ? INHERIT_VALUE : '')
	);

	const currentPersonaLabel = $derived(
		currentSession
			? effectivePersona
				? `${effectivePersona.name || 'Unnamed persona'}${personaInherited ? ' (default)' : ''}`
				: 'No persona assigned'
			: 'No chat open'
	);

	// --- row actions ---
	/** Start a new chat parented to a persona, then open it. The parent refreshes
	 *  its session list first (via `onStartPersonaChat`, the world "+" order) so
	 *  the persona picker resolves the new row immediately; an unwired mount
	 *  falls back to a plain `onSelect`. */
	async function startPersonaChat(persona: Persona): Promise<void> {
		if (personaBusy) return;
		personaBusy = persona.persona_id;
		try {
			const created = await createSessionWithPersona(persona.name, persona.persona_id);
			if (onStartPersonaChat) onStartPersonaChat(created.session_id);
			else onSelect(created.session_id);
			if (scope.kind !== 'all') void reloadScoped();
		} catch (err) {
			status = message(err);
		} finally {
			personaBusy = null;
		}
	}

	/** Materialize a world into a chat (D51: the world row's "+", the door that
	 *  replaced WorldDrawer's "Start chat"), then hand the id to the parent so it
	 *  refreshes the session list before opening. */
	async function materializeWorld(world: World): Promise<void> {
		if (worldBusy) return;
		worldBusy = world.world_id;
		try {
			const created = await startWorldChat(world.world_id);
			if (onStartWorldChat) onStartWorldChat(created.session_id);
			else onSelect(created.session_id);
			if (scope.kind !== 'all') void reloadScoped();
		} catch (err) {
			status = message(err);
		} finally {
			worldBusy = null;
		}
	}

	/** Optimistically flip a chat's memory toggle; revert when the PATCH fails. */
	async function toggleMemory(session: SessionSummary): Promise<void> {
		const current = rowMemory(session);
		const next = !current;
		setRowOverride(session.id, { memory_enabled: next });
		rowBusy = { ...rowBusy, [session.id]: true };
		try {
			await setChatMemory(session.id, next);
		} catch (err) {
			setRowOverride(session.id, { memory_enabled: current });
			status = message(err);
		} finally {
			rowBusy = { ...rowBusy, [session.id]: false };
		}
	}

	/** Pin a persona as the global default (user request: "pin a Default"). */
	async function setDefaultPersona(personaId: string | null): Promise<void> {
		if (pinBusy) return;
		pinBusy = true;
		try {
			applySettings(await patchDefaultPersona(personaId));
			status = '';
		} catch (err) {
			status = message(err);
		} finally {
			pinBusy = false;
		}
	}

	/** Reassign a chat to a persona; the empty value (the explicit "No persona"
	 *  option) clears it. The clear is recorded as an EXPLICIT none, so it sticks
	 *  even while a default is pinned (owner ruling). Selecting the inherited-
	 *  default option is a no-op; reverts the optimistic override on failure. */
	async function reassignPersona(session: SessionSummary, value: string): Promise<void> {
		if (value === INHERIT_VALUE) return;
		const prev = rowPersonaId(session);
		const prevCleared = rowPersonaCleared(session);
		const pid = value === '' ? null : value;
		// A clear is a real change even when persona_id was already null
		// (never-set -> explicit none); an inherited chat must be able to go
		// local. Re-selecting the current clear is a no-op.
		if (pid === prev && (pid !== null || prevCleared)) return;
		setRowOverride(session.id, { persona_id: pid, persona_cleared: pid === null });
		rowBusy = { ...rowBusy, [session.id]: true };
		try {
			await assignChatPersona(session.id, pid);
			// A move can push the row out of the actor scope it was listed in.
			if (scope.kind !== 'all') void reloadScoped();
		} catch (err) {
			setRowOverride(session.id, { persona_id: prev, persona_cleared: prevCleared });
			status = message(err);
		} finally {
			rowBusy = { ...rowBusy, [session.id]: false };
		}
	}

	/** Soft-delete through the parent, dropping the row locally first. */
	function deleteChat(id: string): void {
		if (scopedSessions) scopedSessions = scopedSessions.filter((row) => row.id !== id);
		onDelete(id);
	}

	let editingId = $state<string | null>(null);
	let draft = $state('');

	function beginRename(session: SessionSummary): void {
		editingId = session.id;
		draft = session.title;
	}

	function commitRename(): void {
		const id = editingId;
		if (!id) return;
		const title = draft.trim();
		editingId = null;
		const session =
			sessions.find((item) => item.id === id) ??
			scopedSessions?.find((item) => item.id === id);
		if (title && title !== session?.title) {
			if (scopedSessions) {
				scopedSessions = scopedSessions.map((row) => (row.id === id ? { ...row, title } : row));
			}
			onRename(id, title);
		}
	}

	function cancelRename(): void {
		editingId = null;
	}

	/** Focus + select the inline rename field as soon as it mounts. */
	function focusField(node: HTMLInputElement): void {
		node.focus();
		node.select();
	}

	function formatWhen(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		if (Number.isNaN(date.getTime())) return '';
		return date.toLocaleString(undefined, {
			month: 'short',
			day: 'numeric',
			hour: '2-digit',
			minute: '2-digit'
		});
	}

	// --- current chat's lore (D31/D32) ---
	// `GET /api/sessions/{id}/world` resolves the SAME book set the narrator's
	// world-info render reaches: the union of the assigned persona's books, the
	// chat's latest `session_books` registration, and every `world_ref` world's
	// books (the read delegates to the render helper, so the two cannot disagree).
	// A world-materialized chat also carries a `world_ref` snapshot here. The read
	// is additive and best-effort — it must never block the chat list.
	let sessionWorld = $state<SessionWorld | null>(null);
	let sessionWorldError = $state('');
	// The chat the loaded `sessionWorld` belongs to: guards against flashing chat
	// A's lore on chat B while the switch fetch is in flight.
	let sessionWorldId = $state<string | null>(null);
	// Bumped after an attach/clear lands: `attachSessionBooks` does not change the
	// `currentId` prop, so the effect below needs an explicit nudge to refetch.
	let sessionWorldNonce = $state(0);
	// The reusable lore catalog (for the add control); a plain list, best-effort.
	let books = $state<Book[]>([]);
	let bookBusy = $state(false);
	let loreError = $state('');

	// `currentId` is a prop: reading it directly here tracks it (AGENTS.md — a
	// `get(store)` call inside a `$effect` would not).
	$effect(() => {
		const id = currentId;
		// `sessionWorldNonce` re-runs the fetch after an in-place attach/clear.
		void sessionWorldNonce;
		if (!id) {
			sessionWorld = null;
			sessionWorldId = null;
			sessionWorldError = '';
			return;
		}
		let cancelled = false;
		getSessionWorld(id)
			.then((world) => {
				if (cancelled) return;
				sessionWorld = world;
				sessionWorldId = id;
				sessionWorldError = '';
			})
			.catch((err) => {
				if (cancelled) return;
				sessionWorld = null;
				sessionWorldId = id;
				sessionWorldError = message(err);
			});
		return () => {
			cancelled = true;
		};
	});

	/** The book ids the chat currently resolves (what the model receives). */
	const attachedBookIds = $derived(sessionWorld?.books.map((book) => book.book_id) ?? []);

	/** Catalog books not already in this chat's resolved set. */
	const addableBooks = $derived(
		books.filter((book) => !attachedBookIds.includes(book.book_id))
	);

	/**
	 * Attach one catalog book to the current chat, keeping the books it already
	 * resolves. `attachSessionBooks` replaces the chat's own registration
	 * latest-wins, so the full desired set is sent. Refetch on success; a failure
	 * surfaces the engine's 422 detail and leaves the previous state on screen.
	 */
	async function attachBook(bookId: string): Promise<void> {
		const id = currentId;
		if (!id || !bookId || bookBusy || attachedBookIds.includes(bookId)) return;
		bookBusy = true;
		loreError = '';
		try {
			await attachSessionBooks(id, [...attachedBookIds, bookId]);
			sessionWorldNonce += 1;
		} catch (err) {
			loreError = message(err);
		} finally {
			bookBusy = false;
		}
	}

	/** Clear the chat's own lore attachment (an explicit `[]`, D40 gap). */
	async function clearBooks(): Promise<void> {
		const id = currentId;
		if (!id || bookBusy) return;
		bookBusy = true;
		loreError = '';
		try {
			await attachSessionBooks(id, []);
			sessionWorldNonce += 1;
		} catch (err) {
			loreError = message(err);
		} finally {
			bookBusy = false;
		}
	}

	// --- Escape (D40) ---
	// Pops ONE scope level. Window-level because the rail itself is not
	// focusable; a modal drawer owns Escape while it is open, and the root scope
	// swallows nothing.
	function onWindowKeydown(event: KeyboardEvent): void {
		if (event.key !== 'Escape' || event.defaultPrevented) return;
		if (scopeTrail.length < 2) return;
		if (document.querySelector('[role="dialog"]')) return;
		event.preventDefault();
		popScope();
	}
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#snippet chatRow(session: SessionSummary, showBadge: boolean)}
	{@const memOn = rowMemory(session)}
	{@const pid = rowPersonaId(session)}
	{@const label = session.title || 'Untitled chat'}
	<li class:current={session.id === currentId} class:plain={showBadge && !hasKnownActor(session)}>
		{#if editingId === session.id}
			<input
				class="rename"
				bind:value={draft}
				use:focusField
				onblur={commitRename}
				onkeydown={(event) => {
					if (event.key === 'Enter') {
						event.preventDefault();
						commitRename();
					} else if (event.key === 'Escape') {
						event.preventDefault();
						cancelRename();
					}
				}}
			/>
		{:else}
			<button
				type="button"
				class="entry"
				title={`${label} — ${badgeLabel(session)}`}
				onclick={() => onSelect(session.id)}
			>
				<span class="title" title={label}>{label}</span>
				<span
					class="meta"
					title={`${badgeLabel(session)} · ${formatWhen(session.updated_at)} · ${session.turns} ${session.turns === 1 ? 'turn' : 'turns'}`}
				>
					<!-- Actor badge (D53): the chat's CAST name — redundant under a
					     single-actor scope (that actor already names every row); the
					     unscoped view needs it so a chat stays attributable to its actor,
					     or to the "Chats" group when it has none. -->
					{#if showBadge}
						<span class="badge" class:plain={!hasKnownActor(session)}>{badgeLabel(session)}</span>
					{/if}
					<!-- Compacted (W9.26 §2.6): the engine flags the SUPERSEDED chat —
					     its history was folded into a newer era and turns are refused
					     here (the client follows the successor). The live continuation
					     is an ordinary row, so the badge appears on exactly one line of
					     the story. -->
					{#if session.compacted === true}
						<span
							class="badge-era"
							title="Compacted — this chat continues in a newer era"
							>compacted</span
						>
					{/if}
					{formatWhen(session.updated_at)} · {session.turns}
					{session.turns === 1 ? 'turn' : 'turns'}
				</span>
			</button>
			<div class="row-actions">
				<select
					class="reassign"
					title="Assign user persona"
					aria-label="Assign user persona"
					value={pid ?? ''}
					disabled={rowBusy[session.id]}
					onchange={(event) =>
						void reassignPersona(session, (event.currentTarget as HTMLSelectElement).value)}
				>
					<option value="">No persona</option>
					{#each personas as persona (`actor-option:${persona.persona_id}`)}
						<option value={persona.persona_id}>{persona.name || 'Unnamed persona'}</option>
					{/each}
				</select>
				<button
					type="button"
					class="icon mem"
					class:on={memOn}
					title={`Long-term memory ${memOn ? 'on' : 'off'} — ${memOn ? 'this chat recalls relevant past moments' : 'this chat will not recall past moments'}. Click to turn ${memOn ? 'off' : 'on'}.`}
					aria-label={`Long-term memory for this chat: ${memOn ? 'on' : 'off'}`}
					aria-pressed={memOn}
					disabled={rowBusy[session.id]}
					onclick={() => void toggleMemory(session)}
				>
					<span aria-hidden="true">{memOn ? '◉' : '◌'}</span>
				</button>
				<button
					type="button"
					class="icon"
					title="Rename chat"
					aria-label="Rename chat"
					onclick={() => beginRename(session)}
				>
					✎
				</button>
				<button
					type="button"
					class="icon danger"
					title="Delete chat"
					aria-label="Delete chat"
					onclick={() => deleteChat(session.id)}
				>
					×
				</button>
				<!-- Card export (D39/D40): the per-row definition control. -->
				<DefinitionControls kind="chat" id={session.id} />
			</div>
		{/if}
	</li>
{/snippet}

<aside class="drawer" class:collapsed={railCollapsed}>
	<header class="rail-head">
		{#if !railCollapsed}
			<h2 class="section-label">Chats</h2>
			<button type="button" class="new" onclick={onNew} title="Start a new chat">+ New chat</button>
		{/if}
		<button
			type="button"
			class="icon rail-collapse"
			aria-expanded={!railCollapsed}
			aria-label={railCollapsed ? 'Expand chat rail' : 'Collapse chat rail'}
			title={railCollapsed ? 'Expand chat rail' : 'Collapse chat rail'}
			onclick={toggleRail}
		>
			{railCollapsed ? '»' : '«'}
		</button>
	</header>

	{#if !railCollapsed}
		<!-- Current chat's lore hint (D31/D32/D40, issue #40): the retired top-left
		     import slot now carries the brief lore/context hint. The book list is the
		     RESOLVED set the model receives — the union of the persona's books, the
		     chat's own `session_books` attachment and any world's books — so it is
		     headed "Lore", never "World"; the world name + clamped scenario stay as
		     provenance. Deliberately COMPACT (scenario two lines, one ellipsised line
		     per book, capped book list): it is a hint, not a browser.
		     It stays interactive — attach/clear is the affordance the dissolved rail
		     "Add lore" item left behind (D40 gap) and "open in editor" is the book
		     door into the studio. Neither imports anything: the STUDIOS own typed
		     imports (issue #40), so nothing here may regrow into a card drop target. -->
		{#if currentId}
			{#if sessionWorld && sessionWorldId === currentId}
				<div class="session-world">
					{#if sessionWorld.world}
						<div class="sw-head">
							<span class="sw-label">World</span>
							<span class="sw-name" title={sessionWorld.world.name}
								>{sessionWorld.world.name}</span
							>
						</div>
						{#if sessionWorld.world.scenario}
							<p class="sw-scenario" title={sessionWorld.world.scenario}
								>{sessionWorld.world.scenario}</p
							>
						{/if}
					{/if}
					{#if sessionWorld.books.length}
						<div class="sw-head"><span class="sw-label">Lore</span></div>
						<ul class="sw-books">
							{#each sessionWorld.books as book (`book:${book.book_id}`)}
								<li>
									<span class="sw-book-name" title={book.name}>{book.name || book.book_id}</span>
									<button
										type="button"
										class="sw-open"
										title={`Open ${book.name || book.book_id} in the studio`}
										onclick={() => onOpenStudio('book', { focusId: book.book_id })}>open in editor</button
									>
								</li>
							{/each}
						</ul>
					{:else}
						<!-- Quiet empty state: one dim line, no "Lore" head to announce a
						     section that has no rows. -->
						<p class="sw-none">No lore attached.</p>
					{/if}
					{#if sessionWorld.books.length || addableBooks.length}
						<div class="sw-attach">
							<label class="sw-add-label" for="session-book-add">Attach book</label>
							<select
								id="session-book-add"
								class="sw-add"
								value=""
								disabled={bookBusy || addableBooks.length === 0}
								onchange={(event) => {
									const select = event.currentTarget as HTMLSelectElement;
									const bookId = select.value;
									select.value = '';
									void attachBook(bookId);
								}}
							>
								<option value="" disabled>{addableBooks.length
										? 'Add lore book…'
										: 'No books to add'}</option>
								{#each addableBooks as book (`add:${book.book_id}`)}
									<option value={book.book_id}>{book.name || book.book_id}</option>
								{/each}
							</select>
							{#if sessionWorld.books.length}
								<button
									type="button"
									class="sw-clear"
									disabled={bookBusy}
									onclick={() => void clearBooks()}
									title="Clear this chat's own lore attachment; books carried by the persona or world stay"
									aria-label="Clear attached lore books">Clear</button
								>
							{/if}
						</div>
					{/if}
					{#if loreError}
						<p class="sw-error" role="alert" title={loreError}>{loreError}</p>
					{/if}
				</div>
			{:else if sessionWorldError && sessionWorldId === currentId}
				<p class="sw-error" title={sessionWorldError}>Lore info unavailable.</p>
			{/if}
		{/if}

		<div class="rail-scroll">
			<!-- WORLDS (D40 + issue #53): the name scopes CHATS to that world; "✎" opens
			     the studio's Worlds tab focused on that world (the same door persona rows
			     use); "▸" materializes the world into a new chat (D51 — the door
			     WorldDrawer's "Start chat" left behind) and the head "+" opens the World
			     editor's create form. The glyphs differ ON PURPOSE: "+" creates a
			     DEFINITION, "✎" edits one, "▸" starts a chat from one. There is NO inline
			     expander — a world's scenario/description/cast live in the studio's row
			     editor, so a row is never two start-chat-looking buttons. -->
			<section class="layer" aria-label="Worlds">
				<div class="layer-head-row">
					<h3 class="layer-head">Worlds</h3>
					<button
						type="button"
						class="icon row-new"
						title="New world"
						aria-label="New world"
						onclick={() => onOpenStudio('world', { startNew: true })}
					>
						+
					</button>
				</div>
				{#if worlds.length === 0}
					<p class="layer-empty">No worlds yet — use + to create one.</p>
				{:else}
					{#each worlds as world (`world:${world.world_id}`)}
						<div
							class="scope-row"
							class:active={scope.kind === 'world' && scope.id === world.world_id}
						>
							<button
								type="button"
								class="scope-name"
								aria-current={scope.kind === 'world' && scope.id === world.world_id
									? 'true'
									: undefined}
								onclick={() => showWorld(world.world_id)}
							>
								<span class="scope-label">{world.name || world.world_id}</span>
								<span class="count">{worldCounts.get(world.world_id) ?? 0}</span>
							</button>
							<button
								type="button"
								class="icon actor-edit"
								title={`Edit ${world.name || world.world_id}`}
								aria-label={`Edit ${world.name || world.world_id}`}
								onclick={() => onOpenStudio('world', { focusId: world.world_id })}
							>
								✎
							</button>
							<button
								type="button"
								class="icon row-new"
								title={`Start a chat in ${world.name || world.world_id}`}
								aria-label={`Start a chat in ${world.name || world.world_id}`}
								disabled={worldBusy !== null}
								onclick={() => void materializeWorld(world)}
							>
								{worldBusy === world.world_id ? '…' : '▸'}
							</button>
						</div>
					{/each}
				{/if}
			</section>

			<!-- PERSONAS (D40): the persona catalog — the ONE user-facing name for
			     the definition the studio's Personas tab edits. The name scopes CHATS
			     to that persona's OWN personal chats; "+" starts a chat parented to
			     it, and the head "+" is the rail's SINGLE persona create door (the
			     USER PERSONA layer below assigns an existing one; it never creates). -->
			<section class="layer" aria-label="Personas">
				<div class="layer-head-row">
					<h3 class="layer-head">Personas</h3>
					<button
						type="button"
						class="icon row-new"
						title="New persona"
						aria-label="New persona"
						onclick={() => onOpenStudio('persona', { startNew: true })}
					>
						+
					</button>
				</div>
				{#if personas.length === 0}
					<p class="layer-empty">No personas yet — use + to create one.</p>
				{:else}
					{#each personas as persona (`actor:${persona.persona_id}`)}
						<div
							class="scope-row"
							class:active={scope.kind === 'actor' && scope.id === persona.persona_id}
						>
							<button
								type="button"
								class="scope-name"
								aria-current={scope.kind === 'actor' && scope.id === persona.persona_id
									? 'true'
									: undefined}
								onclick={() => showActor(persona.persona_id)}
							>
								<span class="scope-label">{persona.name || 'Unnamed persona'}</span>
								<span class="count">{actorCounts.get(persona.persona_id) ?? 0}</span>
							</button>
							<button
								type="button"
								class="icon actor-edit"
								title={`Edit ${persona.name || 'Unnamed persona'}`}
								aria-label={`Edit ${persona.name || 'Unnamed persona'}`}
								onclick={() => onOpenStudio('persona', { focusId: persona.persona_id })}
							>
								✎
							</button>
							<button
								type="button"
								class="icon row-new"
								title={`New chat as ${persona.name || 'Unnamed persona'}`}
								aria-label={`New chat as ${persona.name || 'Unnamed persona'}`}
								disabled={personaBusy !== null}
								onclick={() => void startPersonaChat(persona)}
							>
								{personaBusy === persona.persona_id ? '…' : '▸'}
							</button>
						</div>
					{/each}
				{/if}
			</section>

			<!-- USER PERSONA (D40): WHO the open chat plays as — participation
			     (assignment) only. Creating a persona is the PERSONAS layer's single
			     "+": two doors opening the same create form from two layers was the
			     duplicate the owner flagged, and a definition needs no chat open
			     (AGENTS rule 7), so the catalog door above stays reachable without one. -->
			<section class="layer" aria-label="User persona">
				<div class="layer-head-row">
					<h3 class="layer-head">User persona</h3>
				</div>
				{#if currentSession}
					{@const assignedPersonaId = currentAssignedPersonaId}
					<div class="user-persona">
						<span class="up-name">{currentPersonaLabel}</span>
						{#if personaInherited}
							<p class="up-inherited">
								No persona assigned — inheriting the pinned default.
							</p>
						{/if}
						{#if effectivePersona?.scenario}
							<p class="up-text">{effectivePersona.scenario}</p>
						{/if}
						<!-- PICKER (Bug 7): reference an existing persona, no clone. The
						     write rides the SAME per-row seam as the cast select —
						     `assignChatPersona` (POST/DELETE .../persona). "No persona" is an
						     EXPLICIT clear (owner ruling: clear = no persona), kept distinct
						     from the inherited-default option so a pinned default is visible
						     but not forced. The catalog is the rail's `personas` state,
						     reloaded on every `refreshToken` bump, so a studio-created
						     persona appears here without a reload. -->
						{#if personas.length === 0}
							<p class="up-inherited">
								No personas defined yet — use + in the Personas layer above.
							</p>
						{/if}
						<select
							class="up-picker"
							title="Assign a persona to this chat"
							aria-label="Assign a persona to this chat"
							value={personaPickerValue}
							disabled={rowBusy[currentSession.id]}
							onchange={(event) =>
								void reassignPersona(currentSession, (event.currentTarget as HTMLSelectElement).value)}
						>
							<!-- An EXPLICIT "No persona": recorded as a hard clear, so the
							     chat stays local even while a default is pinned (owner
							     ruling) — distinct from the inherited-default option below. -->
							<option value="">No persona</option>
							{#if personaInherited && defaultPersona}
								<option value={INHERIT_VALUE}>
									Inherited from default — {defaultPersonaLabel}
								</option>
							{/if}
							{#each personas as persona (`user-persona-option:${persona.persona_id}`)}
								<option value={persona.persona_id}>{persona.name || 'Unnamed persona'}</option>
							{/each}
						</select>
						<div class="up-actions">
							<button
								type="button"
								class="up-btn"
								title="Edit the assigned persona"
								disabled={!assignedPersonaId}
								onclick={() => onOpenStudio('persona', { focusId: assignedPersonaId ?? null })}
							>
								Edit
							</button>
							<button
								type="button"
								class="up-btn"
								title="Clear this chat's persona (explicit 'no persona')"
								disabled={(!assignedPersonaId && !personaInherited) || rowBusy[currentSession.id]}
								onclick={() => void reassignPersona(currentSession, '')}
							>
								Clear
							</button>
							<!-- Pin control (user request: "pin a Default"). One button:
							     "Set as default" pins the selected persona; "Default ✓"
							     clears it. When no persona is selected but one is pinned,
							     the button still clears the pin (the picker stays
							     honest: this chat's OWN assignment is "No persona"). -->
							{#if assignedPersonaId && assignedPersonaId === defaultPersonaId}
								<button
									type="button"
									class="up-btn up-default"
									title="This persona is the pinned default — click to clear it"
									disabled={pinBusy}
									onclick={() => void setDefaultPersona(null)}
								>
									Default ✓
								</button>
							{:else if assignedPersonaId}
								<button
									type="button"
									class="up-btn"
									title="Pin this persona as the default for new chats"
									disabled={pinBusy}
									onclick={() => void setDefaultPersona(assignedPersonaId)}
								>
									Set as default
								</button>
							{:else if defaultPersonaId}
								<button
									type="button"
									class="up-btn up-default"
									title="Clear the pinned default persona"
									disabled={pinBusy}
									onclick={() => void setDefaultPersona(null)}
								>
									Default ✓
								</button>
							{/if}
						</div>
					</div>
				{:else}
					<p class="layer-empty">No chat open.</p>
				{/if}
			</section>

			<!-- CHATS (D40/D53): the breadcrumb scopes the list; the root view keeps
			     the "Chats" group so chats with no actor (plain or
			     world-materialised) stay visible. -->
			<section class="layer chats" aria-label="Chats">
				<h3 class="layer-head">Chats</h3>
				<nav class="crumbs" aria-label="Chat scope">
					{#each crumbs as crumb, index (`crumb:${crumb.kind}:${crumb.id ?? 'root'}`)}
						{#if index > 0}<span class="crumb-sep" aria-hidden="true">/</span>{/if}
						{#if index === crumbs.length - 1}
							<span class="crumb current" aria-current="true">{crumb.label}</span>
						{:else}
							<button type="button" class="crumb" onclick={() => popTo(index)}>
								{crumb.label}
							</button>
						{/if}
					{/each}
				</nav>

				{#if scopeBusy}
					<p class="layer-empty">Loading chats…</p>
				{/if}

				{#if scope.kind === 'all'}
					<ul class="chat-rows">
						{#each unscopedBuckets.actorChats as session (`chat:${session.id}`)}
							{@render chatRow(session, true)}
						{:else}
							{#if unscopedBuckets.chats.length === 0}
								<li class="empty">No chats yet — start one above.</li>
							{/if}
						{/each}
						{#if unscopedBuckets.chats.length > 0}
							<li class="bucket-label">Chats</li>
							{#each unscopedBuckets.chats as session (`chat:${session.id}`)}
								{@render chatRow(session, true)}
							{/each}
						{/if}
					</ul>
				{:else}
					<ul class="chat-rows">
						{#each chatRows as session (`chat:${session.id}`)}
							{@render chatRow(session, false)}
						{:else}
							<li class="empty">{scopeEmptyText}</li>
						{/each}
					</ul>
				{/if}
			</section>
		</div>

		<div class="rail-tools">
			<!-- The New-asset door + the Studio map + the GLOBAL search sit at the
			     rail's bottom edge. BottomController takes the studio door straight
			     through: its `(kind, opts)` signature IS this rail's. -->
			<BottomController {onOpenStudio} onOpenSession={onSelect} />
			{#if status}<p class="drawer-note" role="status">{status}</p>{/if}
		</div>
	{/if}
</aside>

<style>
	.drawer-note {
		color: var(--text-muted);
		font-size: var(--font-small);
		margin: 0;
		padding: 0 0.75rem 0.5rem;
	}
	.drawer {
		width: 15rem;
		flex: none;
		border-right: 1px solid var(--line);
		background: var(--bg-panel);
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	/* Collapsed rail (D40): a narrow strip holding only the toggle. */
	.drawer.collapsed {
		width: 2.4rem;
	}
	.rail-head {
		flex: none;
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.75rem 0.6rem 0.5rem;
	}
	.drawer.collapsed .rail-head {
		flex-direction: column;
		padding: 0.75rem 0.2rem 0.5rem;
	}
	/* Current chat's lore hint (D31/D32) in the slot the top-left card-import
	   control left (issue #40): pinned under the header like `+ New chat`, above
	   the scrolling layers. Compact by construction — scenario clamped to two
	   lines, names ellipsised, the book list capped and scrollable — because it is
	   a HINT, not a browser. */
	.session-world {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		margin: 0 0.6rem 0.4rem;
		padding: 0.35rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		min-width: 0;
	}
	.rail-collapse {
		flex: none;
		width: 1.5rem;
		height: 1.5rem;
	}
	/* Current chat's world snapshot + lore (D31/D32) with the attach/clear control
	   (D40 gap): the snapshot is frozen at creation, the book set is live. */
	.session-world {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		margin: 0 0.6rem 0.5rem;
		padding: 0.45rem 0.5rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
	}
	.sw-head {
		display: flex;
		align-items: baseline;
		gap: 0.35rem;
		min-width: 0;
	}
	.sw-label {
		flex: none;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--accent-strong);
	}
	.sw-name {
		flex: 1;
		min-width: 0;
		font-size: var(--font-small);
		font-weight: 600;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sw-scenario {
		margin: 0;
		font-size: var(--font-micro);
		line-height: 1.3;
		color: var(--text-muted);
		/* Two lines only: the hint carries the world's flavour; the full scenario is
		   one click away in the editor (the `title` holds the whole text). */
		display: -webkit-box;
		-webkit-box-orient: vertical;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		overflow: hidden;
		overflow-wrap: anywhere;
	}
	.sw-books {
		list-style: none;
		margin: 0.1rem 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		/* Cap the hint: a chat resolving many books scrolls inside the block
		   instead of pushing the chat list down. */
		max-height: 3.9rem;
		overflow-y: auto;
	}
	.sw-books li {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.35rem;
		min-width: 0;
	}
	.sw-book-name {
		flex: 1;
		min-width: 0;
		font-size: var(--font-micro);
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.sw-open {
		flex: none;
		font: inherit;
		font-size: var(--font-micro);
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-strong);
		cursor: pointer;
		text-decoration: underline;
	}
	.sw-open:hover {
		color: var(--accent);
	}
	.sw-error {
		margin: 0 0.6rem 0.4rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.session-world .sw-error {
		margin: 0;
		color: var(--danger-text);
	}
	.sw-none {
		margin: 0;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.sw-attach {
		display: flex;
		align-items: center;
		gap: 0.3rem;
		margin-top: 0.15rem;
	}
	.sw-add-label {
		flex: none;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.sw-add {
		flex: 1;
		min-width: 0;
		font: inherit;
		font-size: var(--font-micro);
		background: var(--bg-inset, var(--bg-raised));
		color: var(--text-muted);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.1rem 0.2rem;
	}
	.sw-clear {
		flex: none;
		font: inherit;
		font-size: var(--font-micro);
		background: none;
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.1rem 0.4rem;
		color: var(--text-muted);
		cursor: pointer;
	}
	.sw-clear:hover:not(:disabled) {
		border-color: var(--danger-text);
		color: var(--danger-text);
	}
	.sw-clear:disabled {
		opacity: 0.5;
		cursor: default;
	}
	/* The layers own the scroll; the tools stay pinned below them. */
	.rail-scroll {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0 0.6rem 0.4rem;
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	/* A bare pinned slot: BottomController brings its own top divider, padding
	   and background, so the wrapper adds none of its own (a border here would
	   draw a second hairline just above the bar). */
	.rail-tools {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.new {
		font: inherit;
		font-size: var(--font-small);
		font-weight: 600;
		padding: 0.25rem 0.6rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
		white-space: nowrap;
	}
	.new:hover {
		background: var(--accent-soft);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
	}
	/* --- layers (D40) --- */
	.layer {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.layer-head-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.15rem;
	}
	.layer-head {
		margin: 0;
		font-size: var(--font-micro);
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
		padding: 0 0.25rem;
	}
	.layer-empty {
		margin: 0.1rem 0.25rem;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.scope-row {
		display: flex;
		align-items: center;
		gap: 0.15rem;
		border-radius: 6px;
	}
	.scope-row.active {
		background: var(--bg-active);
	}
	.scope-name {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 0.3rem;
		background: none;
		border: none;
		border-radius: 6px;
		padding: 0.25rem 0.3rem;
		font: inherit;
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text-muted);
		cursor: pointer;
	}
	.scope-name:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.scope-label {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.count {
		margin-left: auto;
		font-size: var(--font-micro);
		font-weight: 400;
		color: var(--text-dim);
	}
	.row-new {
		color: var(--accent-strong);
	}
	.row-new:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.user-persona {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		margin: 0 0.25rem;
	}
	.up-name {
		font-size: var(--font-small);
		font-weight: 600;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.up-text {
		margin: 0;
		font-size: var(--font-micro);
		line-height: 1.35;
		color: var(--text-muted);
	}
	/* Inherited-default marker: this chat has no own persona and falls back to
	   the pinned default (read-time). Muted so it reads as contextual. */
	.up-inherited {
		margin: 0;
		font-size: var(--font-micro);
		line-height: 1.35;
		color: var(--text-muted);
		font-style: italic;
	}
	/* The pin control: an active pin reads as accented. */
	.up-default {
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	/* Persona picker (Bug 7): a reference to an existing persona, written through
	   the same `chat_persona` seam the per-row cast select uses. */
	.up-picker {
		font: inherit;
		font-size: var(--font-micro);
		margin: 0.15rem 0 0;
		padding: 0.1rem 0.3rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text);
		max-width: 100%;
	}
	/* Edit/New doors into the Design Studio's Personas tab (D51). */
	.up-actions {
		display: flex;
		gap: 0.3rem;
		margin: 0.15rem 0 0;
	}
	.up-btn {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.up-btn:hover:not(:disabled) {
		border-color: var(--accent-line);
		background: var(--bg-hover);
		color: var(--accent-strong);
	}
	.up-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	/* --- breadcrumb + chat rows --- */
	.crumbs {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.2rem;
		padding: 0 0.25rem 0.1rem;
		font-size: var(--font-small);
	}
	.crumb {
		font: inherit;
		font-size: var(--font-small);
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-strong);
		cursor: pointer;
	}
	.crumb:hover {
		text-decoration: underline;
	}
	.crumb.current {
		color: var(--text);
		font-weight: 600;
	}
	.crumb-sep {
		color: var(--text-dim);
	}
	.bucket-label {
		margin-top: 0.2rem;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--text-dim);
		padding: 0.1rem 0.4rem;
	}
	.badge {
		margin-right: 0.25rem;
		color: var(--accent-strong);
	}
	.badge.plain {
		color: var(--text-dim);
	}
	/* Compacted era badge (W9.26 §2.6): a pill, deliberately unlike the plain
	   actor-name badge beside it — it marks a lifecycle state, not a group. */
	.badge-era {
		margin-right: 0.25rem;
		padding: 0 0.3rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-size: var(--font-micro);
		font-style: italic;
		letter-spacing: 0.02em;
	}
	/* Reassign picker stays out of the way until the row is hovered/current.
	   Visually hidden, NOT `display: none` — a nav rail must keep every control
	   keyboard-reachable, and `display: none` drops it from the tab order. */
	.reassign {
		font: inherit;
		font-size: var(--font-micro);
		max-width: 5.6rem;
		background: var(--bg-inset);
		color: var(--text-muted);
		border: 1px solid var(--line);
		border-radius: 6px;
		position: absolute;
		width: 1px;
		height: 1px;
		overflow: hidden;
		clip-path: inset(50%);
	}
	li:hover .reassign,
	li.current .reassign,
	.reassign:focus-visible {
		position: static;
		width: auto;
		height: auto;
		overflow: visible;
		clip-path: none;
	}
	.chat-rows li {
		display: flex;
		align-items: center;
		gap: 0.15rem;
		border-radius: 8px;
		padding: 0.1rem 0.15rem;
	}
	.chat-rows li.current {
		background: var(--bg-active);
	}
	.chat-rows li.bucket-label,
	.chat-rows li.empty {
		display: block;
	}
	.entry {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		text-align: left;
		font: inherit;
		background: none;
		border: none;
		padding: 0.35rem 0.4rem;
		border-radius: 6px;
		cursor: pointer;
		color: var(--text);
	}
	.entry:hover {
		background: var(--bg-hover);
	}
	.title {
		font-size: var(--font-ui);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.meta {
		font-size: var(--font-small);
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.row-actions {
		display: flex;
		flex: none;
		align-items: center;
		gap: 0.05rem;
		position: relative;
	}
	.icon {
		font: inherit;
		font-size: var(--font-ui);
		line-height: 1;
		width: 1.35rem;
		height: 1.35rem;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 6px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.icon:hover {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.icon.danger:hover {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	/* Memory as an icon (D40): one control, state carried by the glyph + tint. */
	.mem {
		font-size: var(--font-small);
	}
	.mem.on {
		color: var(--accent-strong);
	}
	.mem:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.rename {
		font: inherit;
		font-size: var(--font-ui);
		flex: 1;
		min-width: 0;
		padding: 0.3rem 0.4rem;
		border: 1px solid var(--accent-line);
		border-radius: 6px;
		background: var(--bg-inset);
		color: var(--text);
	}
	.rename:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.empty {
		font-size: var(--font-small);
		color: var(--text-dim);
		padding: 0.3rem 0.4rem;
	}
</style>
