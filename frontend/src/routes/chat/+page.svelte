<script lang="ts">
	// Chat screen (W1.5 + W2.13a): session drawer, transcript, streaming narrator
	// and deterministic state sidebar. Talks to the engine only through
	// `$lib/api`; view state lives in `$lib/stores`. The state sidebar reads the
	// projection, not the stream.
	//
	// Session ownership (W2.13a): the current session id lives in the URL
	// (`/chat?s=<id>`) so a refresh restores the same transcript + state. A
	// brand-new chat is a clean, session-less view; the first send lazily creates
	// the log session and pins it to the URL.
	import { onMount } from 'svelte';
	import { get } from 'svelte/store';
	import {
		createSession,
		deleteSession,
		forkTurn,
		getProjection,
		listBranches,
		listSessions,
		pollCompactionJob,
		renameSession,
		startCompaction,
		stopTurn,
		streamTurn,
		API_BASE,
		USE_MOCK
	} from '#lib/api';
	import type {
		BranchInfo,
		CompactionMarker,
		ContextLowRow,
		Projection,
		ProjectionItem,
		RevisionFrame,
		StageFrame,
		StreamOutcome,
		ValidationNotice
	} from '#lib/api';
	import {
		addNarratorEntry,
		addUserEntry,
		appendReasoning,
		bumpStateNonce,
		clearSessionView,
		connection,
		connectionError,
		facts,
		factsFromState,
		fillComposer,
		finishEntry,
		processingStage,
		removeEntry,
		sessionId,
		sessionTitle,
		sessions,
		setFacts,
		setProcessingStage,
		setSessions,
		setTranscript,
		setTurnSteps,
		coerceTurnSteps,
		transcript,
		turnSteps,
		updateEntry
	} from '#lib/stores';
	import type { NarratorSegment, TranscriptEntry, TurnKind, TurnStep } from '#lib/stores';
	import Composer from './Composer.svelte';
	import SessionDrawer from './SessionDrawer.svelte';
	// Owner ruling 2026-10-13 item 8: the global search + Studio map live in the
	// rail's bottom bar (SessionDrawer); the chat-scoped semantic search moved to
	// a top-bar control.
	import TopBarSearch from './TopBarSearch.svelte';
	import StateSidebar from './StateSidebar.svelte';
	import DraggablePane from '#lib/components/DraggablePane.svelte';
// Artifact components (D58): warms the name→artwork index for Phone/SMS/
// SocialPost avatars when the session's world loads (see avatars.ts).
import { hydrateAvatars } from '#lib/openui/avatars';
	import InspectorPane from './InspectorPane.svelte';
	import PromptDrawer from './PromptDrawer.svelte';
	import ConnectionsDrawer from './ConnectionsDrawer.svelte';
	import AppearanceDrawer from './AppearanceDrawer.svelte';
	import TranscriptList from './TranscriptList.svelte';
	import DslHeader from './DslHeader.svelte';
	import UserBadge from './UserBadge.svelte';
	import { splitProseAndDsl } from './segment';
	// D38: the assistant's own area, pinned on top of the composer.
	import ChatHud from './ChatHud.svelte';
	import AssistantPanel from './AssistantPanel.svelte';
	// D51: entity-definition CRUD lives in the Design Studio (one shell, five
	// panes). The panes are props-only components; the page hands them in.
	import DesignStudio from './studio/DesignStudio.svelte';
	// The ONE studio kind registry (D51 §2.3): the page's door request and the
	// `openStudio` signature name kinds through it, never a local mirror.
	import type { StudioKind } from '#lib/studio/kinds';
	import PersonaPane from './studio/PersonaPane.svelte';
	import WorldPane from './studio/WorldPane.svelte';
	import BookPane from './studio/BookPane.svelte';
	import AssistantPane from './studio/AssistantPane.svelte';
	import ProfilePane from './studio/ProfilePane.svelte';
	import DecisionQueriesPane from './studio/DecisionQueriesPane.svelte';
	import EntityCatalogPane from './studio/EntityCatalogPane.svelte';
	import GraphHomePane from './studio/GraphHomePane.svelte';
	import NodeTemplatesPane from './studio/NodeTemplatesPane.svelte';
	import PlannerPane from './studio/PlannerPane.svelte';
	// Issue #56: the NPC lane settings get a page where NPCs are managed.
	import LanesPane from './studio/LanesPane.svelte';
	// W9.22 §4: the review home's studio half — the cross-session proposal inbox.
	import ProposalsHome from './studio/ProposalsHome.svelte';
	// Wave 4b player HUD panes + transcript overlays.
	import RecapPane from './RecapPane.svelte';
	import MemoryPane from './MemoryPane.svelte';
	import CastPane from './CastPane.svelte';
	// W9.22 §4 / W9.23 §3.2: the review home's chat half — this session's proposal
	// queue, plus the SEPARATE archive drawer for the aged-out partition.
	import ProposalDrawer from './ProposalDrawer.svelte';
	import ProposalArchiveDrawer from './ProposalArchiveDrawer.svelte';
	import type {
		CompactionBreakView,
		TranscriptEntryView,
		TurnMeta,
		ValidatorFlag
	} from './TranscriptList.svelte';
	import { listAttachments, listRevisions, postRevision, uploadAttachment } from '#lib/api';
	// Issue #35: delete-tail lives beside the other session verbs; imported from
	// its module because the `#lib/api` barrel is not this lane's file.
	import { deleteTail } from '#lib/api/sessions';
	// Inspector live-refresh (2026-10-15 W1b): bump at every turn end so an open
	// Inspector re-reads without a manual reload. UI-invoked model calls (Prompt
	// Enhance, caption) bump it themselves from their own component.
	import { bumpInspectorRefresh } from '#lib/inspectorRefresh';
	// Issue #34: the ONE notice surface — swallowed-service degradations (read off
	// the inspector endpoint this page already calls) plus the pending proposal
	// queue (read through the existing proposals client). The header's Inspector
	// button wears the badge; the Inspector's notices tab renders the rows.
	import {
		loadPendingProposals,
		noticeBadge,
		setDegradationNotices,
		syncNoticeSession
	} from '#lib/notices';
	import { beginTurnWindow } from '#lib/stores';

	let streaming = $state(false);
	let controller: AbortController | null = null;

	// --- W9.26 context management: delivery state ------------------------------
	// The stream provably cannot carry hook results (`turn.done` is set before the
	// post-turn hooks run), so compaction is delivered by polling + 409s:
	//   * `compacting`  a compaction job is running (the [Compact] button's poll);
	//   * `compactError` the job's failed/noop reason, rendered inline (button re-arms);
	//   * `pendingStatus` the INFORMATIONAL status a turn shows while it waits out a
	//     forced compaction (`409 compacting`) — never an error.
	let compacting = $state(false);
	let compactError = $state('');
	let pendingStatus = $state('');
	/** Retry cadence while a turn is refused with `409 compacting` (~5 s). */
	const COMPACT_RETRY_MS = 5000;
	/** Give up on the forced path after this long (then informational, not error). */
	const COMPACT_WAIT_MS = 2 * 60 * 1000;
	/** Cap transitive successor following (a chain of superseded eras). */
	const SUPERSEDED_FOLLOW_LIMIT = 8;
	/** The one delayed projection refetch after a turn's `done` (~5 s, §2.2). */
	const CONTEXT_LOW_REFETCH_MS = 5000;
	let contextRefetchTimer: ReturnType<typeof setTimeout> | null = null;

	// Edge-detect the turn: a true -> false transition is the turn end.
	let wasStreaming = false;
	$effect(() => {
		const now = streaming;
		if (wasStreaming && !now) bumpInspectorRefresh();
		wasStreaming = now;
	});

	// Prompt manager view the ⚙ shortcut targets (W6.6/D2): 'rules' opens the
	// drawer straight on the Rules section; every other door opens 'steps'.
	let promptView = $state<string>('steps');

	// Branching (W2.2): the sibling set for the current session's latest reply,
	// the fork cut (last user message seq + kind), and an in-flight guard.
	let branchInfo = $state<BranchInfo | null>(null);
	let branchBusy = $state(false);

	// Image attachments (W2.8): a pending file rides the next send as an
	// `[image: caption]` prefix (captioner path; VLM content-part seam later).
	let pendingFile = $state<File | null>(null);
	// R13 panes: sidebars can undock into floating DraggablePanes.
	let stateUndocked = $state(false);
	let inspectorOpen = $state(false);
	let promptsOpen = $state(false);
	// D51: entity DEFINITIONS live in the Design Studio (personas · worlds ·
	// books · assistants · profiles); assembly stays in chat. The shell owns its
	// layout — dock/width/kind, persisted in `fabulist.studio.state` — while the
	// page owns `open` (the shell renders nothing while closed, so `open` must be
	// restored here) and the per-door request it hands down. The KINDS are the
	// shared registry's (#lib/studio/kinds), never a page-local mirror.
	/** The shell's persistence key (D51 §2.3) — read for `open`, JSON try/caught. */
	const STUDIO_STATE_KEY = 'fabulist.studio.state';
	let studioOpen = $state(false);
	let studioFocusId = $state<string | null>(null);
	let studioStartNew = $state(false);
	/**
	 * The door request, re-created on every click; `null` until the first door so
	 * a fresh page load can never overrule the kind the shell restored from its
	 * own persistence. `seq` exists because a plain `kind` prop cannot express
	 * "open this same tab again": the shell's effect keys off the request object,
	 * so a repeat request still re-fires.
	 */
	let studioRequest = $state<{ kind: StudioKind; seq: number } | null>(null);
	/**
	 * Assistant chrome (issue #22). The assistant's disabled strip collapsed per
	 * UI-LANGUAGE §6, so the door lives in the composer's action row now — which
	 * means the HOST owns the picker flag (bound both ways with the panel) and
	 * mirrors whether anything is pinned (the door hides while a panel shows).
	 */
	let assistantPickerOpen = $state(false);
	let assistantEnabled = $state(false);
	// Badge data (T5): the session's ACTIVE persona + world, for the user-message
	// badge and the top-bar identity chips. Definition images are keyed by
	// definition id, so the id must ride along with the asset ref.
	//
	// The persona resolves session persona -> global pinned default
	// (settings.default_persona_id; that read is GLOBAL, not session-scoped) ->
	// none. A stale pin names a deleted definition and resolves to no persona, so
	// no badge renders rather than an invented identity.
	let activePersona = $state<{ name: string; persona_id: string; image_asset: string | null } | null>(null);
	let activeWorld = $state<{ name: string; id: string; image_asset: string | null } | null>(null);
	// The session's cast roster (identity badges in the top bar). Loaded from the
	// session cast endpoint, which is populated even when no world/persona is set.
	// `persona_id`/`image_asset` are the badge join: the endpoint resolves each
	// cast name to the definition a card import captured, so a card character's
	// badge shows the card art (the image route is keyed by the definition id).
	type CastBadge = { name: string; persona_id: string | null; image_asset: string | null };
	let activeCast = $state<CastBadge[]>([]);
	/**
	 * Issue #22: the head's identity row is ONE line, full stop.
	 *
	 * The cluster used to wrap (`flex-wrap: wrap`) and a long cast added rows to
	 * the header, which is how the head reached 103px below 1136px. A badge row
	 * that grows the header is a layout bug dressed as content, so the row is
	 * capped at [IDENTITY_BADGE_CAP] badges (a world chip counts as one) and the
	 * remainder collapses into a `+N` count whose tooltip names them. Nothing is
	 * lost — the full roster is one click away in the rail's cast pane.
	 */
	const IDENTITY_BADGE_CAP = 4;

	/** The badges that actually render (a world chip takes one of the slots). */
	const identityCast = $derived(
		activeCast.slice(0, Math.max(0, IDENTITY_BADGE_CAP - (activeWorld ? 1 : 0)))
	);
	/** Cast members folded into the `+N` count. */
	const identityOverflow = $derived(Math.max(0, activeCast.length - identityCast.length));
	/** The names the `+N` tooltip carries (never invented — the roster's own). */
	const identityOverflowNames = $derived(
		activeCast
			.slice(identityCast.length)
			.map((cast) => cast.name)
			.join(', ')
	);
	/**
	 * The head's one-line title: the session id rides the tooltip (issue #22). It
	 * stays reachable — an id is a debug handle, not a thing to read beside the
	 * title on every chat.
	 */
	const headTitle = $derived(
		`${$sessionTitle || 'New chat'}${$sessionId ? ` — session ${$sessionId}` : ''}`
	);

	$effect(() => {
		const sid = $sessionId;
		if (!sid) {
			activePersona = null;
			activeWorld = null;
			activeCast = [];
			return;
		}
		let cancelled = false;
		void (async () => {
			// Persona ids whose artwork the artifact avatars must resolve (D58): the
			// session's own persona plus every cast member's joined definition.
			const artPersonaIds: string[] = [];
			let personaIdForArt: string | null = null;
			try {
				const cfg = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sid)}/chat-config`).then((r) => r.json());
				let pid: string | null = cfg?.persona_id ?? null;
				let label: string | null = null;
				if (!pid) {
					// No session persona: inherit the global pinned default. This
					// settings read is global, not session-scoped.
					const settings = await fetch(`${API_BASE}/api/settings`).then((r) => r.json());
					pid = settings?.default_persona_id ?? null;
					label = settings?.default_persona_label ?? null;
				}
				if (pid && !cancelled) {
					const body = await fetch(`${API_BASE}/api/personas/${encodeURIComponent(pid)}`).then((r) => r.json());
					const row = body?.persona;
					// A stale pin names a deleted definition (the engine skips it at
					// session creation): no record, no badge.
					activePersona = row
						? {
								name: row.name ?? label ?? '',
								persona_id: row.persona_id ?? pid,
								image_asset: row.image_asset ?? null
							}
						: null;
					if (activePersona) personaIdForArt = pid;
				} else if (!cancelled) {
					activePersona = null;
				}
				if (personaIdForArt) artPersonaIds.push(personaIdForArt);
			} catch {
				if (!cancelled) activePersona = null;
			}
			try {
				const body = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sid)}/world`).then((r) => r.json());
				const row = body?.world;
				if (!cancelled) {
					const wid = row ? (row.world_id ?? row.id ?? null) : null;
					activeWorld = row && wid
						? { name: row.name ?? '', id: wid, image_asset: row.image_asset ?? null }
						: null;
					// Artifact components (D58): resolve cast artwork once per world so
					// Phone/SMS/SocialPost avatars warm the name→image index. Quiet by
					// design — failures leave the initials-emblem fallback.
					void hydrateAvatars(Array.isArray(row?.cast) ? (row.cast as string[]) : []);
				}
			} catch {
				if (!cancelled) activeWorld = null;
			}
			try {
				const body = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(sid)}/cast`).then((r) => r.json());
				if (!cancelled) {
					const rows = Array.isArray(body?.cast) ? (body.cast as Array<Record<string, unknown>>) : [];
					activeCast = rows
						.filter((c) => typeof c?.name === 'string' && c.name.length > 0)
						.map((c) => ({
							name: c.name as string,
							persona_id: typeof c.persona_id === 'string' && c.persona_id ? c.persona_id : null,
							image_asset:
								typeof c.image_asset === 'string' && c.image_asset ? c.image_asset : null
						}));
					// Artifact avatars (D58) must know the CARD character too, not only a
					// world cast: the cast badge join names their definitions.
					for (const row of activeCast) {
						if (row.persona_id) artPersonaIds.push(row.persona_id);
					}
					void hydrateAvatars(artPersonaIds);
				}
			} catch {
				if (!cancelled) activeCast = [];
			}
		})();
		return () => {
			cancelled = true;
		};
	});
	/** Bumped on every studio definition change; the rail re-reads its catalogs. */
	let railRefreshToken = $state(0);
	// PLAN R5: the connections editor (endpoint/model/sampling/extra params).
	let connectionsOpen = $state(false);
	// Issue #12 / ADR-027: the appearance surface (theme preset + one CSS override).
	// Per-install user state (`data/appearance.json`), not session state, so it
	// opens with or without a chat — the same door the other Configure panels use.
	let appearanceOpen = $state(false);
	// Wave 4b player-HUD panes (Recap / Memories / Cast).
	let recapOpen = $state(false);
	let memoryOpen = $state(false);
	let castOpen = $state(false);
	// W9.22 §4: the proposal review drawer (the plan's "one shared component"
	// rendered session-scoped; the studio's Proposals tab is the inbox half).
	let proposalsOpen = $state(false);
	// W9.23 §3.2: the archive partition is a SEPARATE pane, linked from the queue
	// drawer — archived rows render no selection affordances, so keeping them out
	// of the queue's tree is what makes mass selection unable to reach them.
	let proposalsArchiveOpen = $state(false);

	// Wave 4b transcript overlays: dice chips / validator markers / edit markers.
	// `turnMeta` is keyed by transcript entry id; the two maps connect a live or
	// hydrated entry to the inspector turn by seq, and a recap seq to the DOM
	// anchor (`#history-{event_id}`) the search-jump pattern already uses.
	let turnMeta = $state<Record<string, TurnMeta>>({});
	const seqByEntry = new Map<string, number>();
	const seqToLogId = new Map<number, number>();
	let revisionMemo = new Map<number, string>();
	// Revision event id -> transcript entry id. A FAIL regeneration supersedes the
	// PREVIOUS revision rather than the narrative (nodes.py `_run_critic`), so a
	// live `revision` frame's `target_id` can name a revision; this map (filled at
	// hydrate and by each live frame) still lands the replacement on the turn.
	let revisionEntryByEvent = new Map<number, string>();

	type InspectorRoll = { dice?: unknown; result?: unknown; detail?: unknown; ask?: unknown };
	type InspectorTurn = {
		user_seq?: unknown;
		narrative_seq?: unknown;
		rolls?: InspectorRoll[];
		validation_details?: unknown;
		revision_refs?: Array<{ reason?: unknown }>;
	};
	// Undocked panes open clear of the session rail (13.5rem ≈ 216px) so they
	// never smother the chat list on first open (U5 pane discipline).
	const RAIL_WIDTH = 248;
	let statePanePos = $state({ x: RAIL_WIDTH, y: 96 });
	let attachBusy = $state(false);
	let attachError = $state('');
	let fileInput = $state<HTMLInputElement | null>(null);

	function pickFile(): void {
		fileInput?.click();
	}

	function onFileChosen(event: Event): void {
		const input = event.target as HTMLInputElement;
		pendingFile = input.files?.[0] ?? null;
		attachError = '';
		input.value = '';
	}
	let lastUserSeq = $state<number | null>(null);
	let lastUserContent = $state('');
	let lastUserKind: TurnKind = 'in_character';

	// --- the failure surface ---------------------------------------------------
	//
	// `connection` says a turn or a probe failed; `connectionError` carries WHY, so
	// the rail shows the cause beside the status instead of the transcript being the
	// only place it exists. The text is dropped the moment the status leaves `error`:
	// the state and its explanation can never disagree.
	$effect(() => {
		if ($connection !== 'error') connectionError.set('');
	});

	/** Put the connection into its error state WITH the cause the rail shows. */
	function failConnection(message: string): void {
		connectionError.set(message || 'the engine did not answer');
		connection.set('error');
	}

	/**
	 * The rail's recovery door: re-probe the session, then re-run the failed turn.
	 *
	 * A PRE-COMMIT abort (a provider failure in the planner, a lane or the narrator
	 * draft) leaves the turn's user row in the log and NO narrative for it — so
	 * nothing had refreshed `lastUserSeq` or the branch context, the ↻ affordance
	 * never appeared, and the status sat in `error` with no way out of it. The
	 * re-read IS the probe: a successful projection read clears the error state, and
	 * the ↻ it re-enables re-runs the unanswered turn through the normal retry path.
	 */
	async function recoverFromError(): Promise<void> {
		if (streaming || branchBusy) return;
		const id = get(sessionId);
		await refreshSessions();
		if (!id) {
			// No session to probe (a failed create, an expired id): the list read IS
			// the probe, and there is no turn to re-run.
			connection.set('idle');
			return;
		}
		await refreshTranscript(); // the failed turn's user row ⇒ lastUserSeq
		await refreshBranches(id);
		connection.set('idle'); // the probe answered: recoverable, never stuck
		if (lastUserSeq !== null) await retry();
	}

	// --- progressive state refresh (stage -> state views) ----------------------
	//
	// The state pane refetches on `facts`, which only changes once a KEPT turn's
	// projection lands — so the registry filled in as one catch-up dump at the
	// end. The engine already streams a `stage` frame per graph step, and three of
	// those steps WRITE the object stores the pane reads: `plan` (the W9.31 planner
	// bootstrap seeds entities before the narration runs), `world_state` (the
	// post-turn tracker) and `historian` (the asset-proposal lanes). Bumping the
	// shared `stateNonce` when one of them closes lets the pane swap its rows
	// mid-turn instead.
	const STATE_TOUCHING_STAGES = new Set(['plan', 'world_state', 'historian']);
	/** Trailing delay for the burst of stage frames a step can emit (ms). */
	const STATE_REFRESH_DEBOUNCE_MS = 250;
	let stateRefreshTimer: ReturnType<typeof setTimeout> | null = null;

	/**
	 * One `stage` frame from the turn stream: the pinned step bar, plus a state
	 * refresh for the steps that touched the object stores.
	 *
	 * The refresh is TRAILING-debounced (~250 ms) so a burst of frames costs one
	 * registry GET, never one per frame; `setProcessingStage` is never debounced
	 * (the bar must follow every boundary).
	 */
	function handleStage(frame: StageFrame): void {
		setProcessingStage(frame);
		if (frame.state !== 'done' || !STATE_TOUCHING_STAGES.has(frame.stage)) return;
		if (stateRefreshTimer !== null) clearTimeout(stateRefreshTimer);
		stateRefreshTimer = setTimeout(() => {
			stateRefreshTimer = null;
			bumpStateNonce();
		}, STATE_REFRESH_DEBOUNCE_MS);
	}

	onMount(() => {
		void initialize();
		window.addEventListener('popstate', onPopState);
		restoreStudioOpen();
		return () => {
			window.removeEventListener('popstate', onPopState);
			if (stateRefreshTimer !== null) clearTimeout(stateRefreshTimer);
			clearContextRefetch();
		};
	});

	/**
	 * D51: the shell persists `{open, dock, width, kind, …}` but never writes its
	 * `open` prop back — the host owns that. Child `onMount` runs first, so the
	 * shell has already hydrated its own fields by the time this runs.
	 */
	function restoreStudioOpen(): void {
		try {
			const raw = window.localStorage.getItem(STUDIO_STATE_KEY);
			if (!raw) return;
			const saved = JSON.parse(raw) as Record<string, unknown>;
			if (saved.open === true) studioOpen = true;
		} catch {
			// Corrupt state / private mode — the studio just starts closed.
		}
	}

	async function initialize(): Promise<void> {
		await refreshSessions();
		const requested = sessionFromUrl();
		if (requested) await openSession(requested);
		else newChat();
	}

	function onPopState(): void {
		const requested = sessionFromUrl();
		if (requested) void openSession(requested);
		else newChat();
	}

	function sessionFromUrl(): string | null {
		return new URLSearchParams(window.location.search).get('s');
	}

	/** Pin the current session in the URL without adding a history entry. */
	function persistUrl(id: string | null): void {
		const url = new URL(window.location.href);
		if (id) url.searchParams.set('s', id);
		else url.searchParams.delete('s');
		window.history.replaceState({}, '', url);
	}

	async function refreshSessions(): Promise<void> {
		try {
			setSessions(await listSessions());
		} catch {
			// Listing is best-effort; the transcript path still works.
		}
	}

	/** Load a session's transcript + facts and make it current. */
	async function applyTheme(id: string | null) {
		// R4 deterministic theming: tone tokens ride :root custom props.
		// Neutral (no tokens) clears the overrides — app.css defaults stand.
		const root = document.documentElement;
		for (const key of ['--accent-hue', '--accent-sat', '--bg-warmth']) {
			root.style.removeProperty(key);
		}
		if (!id) return;
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${id}/theme`);
			if (!res.ok) return;
			const body = (await res.json()) as { tokens: Record<string, string> };
			for (const [key, value] of Object.entries(body.tokens ?? {})) {
				root.style.setProperty(key, value);
			}
		} catch {
			/* theme is cosmetic — never surface */
		}
	}

	/**
	 * Load a session's transcript + facts and make it current.
	 *
	 * `committedNarrSeq` is the contract the fork/retry preview must settle: the
	 * streamed reply was committed as that narrative row, so the branch's
	 * projection MUST contain it. A miss on the first read is retried ONCE before
	 * hydrating — the same one-retry rule the in-place reconcile uses — instead of
	 * showing a branch view that silently lacks the turn just streamed into it.
	 */
	async function openSession(
		id: string,
		committedNarrSeq: number | null = null
	): Promise<void> {
		if (get(sessionId) === id && get(transcript).length > 0) {
			persistUrl(id);
			return;
		}
		abortActive();
		clearSessionView();
		seqByEntry.clear();
		seqToLogId.clear();
		branchInfo = null;
		// The previous chat's last turn must not travel into this one: `refreshBranches`
		// feeds `lastUserSeq` to `listBranches` as the client-side merge cut, so a
		// stale value silently hid the ↻ controls on the newly opened session.
		lastUserSeq = null;
		lastUserContent = '';
		// W9.26: a session switch ends any compaction delivery state; the delayed
		// context-low refetch belongs to the session it was scheduled for.
		clearContextRefetch();
		pendingStatus = '';
		compactError = '';
		compacting = false;
		sessionId.set(id);
		persistUrl(id);
		sessionTitle.set(get(sessions).find((item) => item.id === id)?.title ?? '');
		connection.set('connecting');
		try {
			let projection = await getProjection(id);
			if (
				committedNarrSeq !== null &&
				!projection.narratives.some((row) => row.seq === committedNarrSeq)
			) {
				projection = await getProjection(id); // one retry, then show what the log has
			}
			hydrate(projection, await hydrateAttachments(id));
			setFacts(factsFromState(projection.state));
			connection.set('idle');
			await applyTheme(id);
			await refreshBranches(id);
			await loadTurnMeta(id);
		} catch {
			// Unknown/expired session: fall back to a clean new-chat start, and say
			// so in the rail — silently resetting to a fresh chat hid the cause.
			newChat();
			failConnection('could not load that chat — the session is unknown or expired');
		}
	}

	/** Clean, session-less new-chat state (the URL carries no `s`). */
	function newChat(): void {
		abortActive();
		clearSessionView();
		sessionId.set(null);
		persistUrl(null);
		connection.set('idle');
		branchInfo = null;
		lastUserSeq = null;
		lastUserContent = '';
		turnMeta = {};
		// W9.26: the clean state clears the compaction delivery surfaces too.
		clearContextRefetch();
		pendingStatus = '';
		compactError = '';
		compacting = false;
		// Issue #34: a fresh chat carries no notices (the badge must not inherit
		// the last chat's degradations or its pending proposals).
		syncNoticeSession(null);
	}

	/**
	 * Build the per-entry transcript overlays (Wave 4b): dice chips from the
	 * inspector turn's `rolls`, validator markers from `validation_details`, and
	 * edit markers from the revisions read. Additive and best-effort — any missing
	 * route simply leaves the overlays empty (legacy engine / mock).
	 */
	async function loadTurnMeta(id: string): Promise<void> {
		// Issue #34: the notice surface is session-scoped and rides this read — the
		// same endpoint and the same turn-end/session-load trigger, so the badge
		// costs no extra poll. Switching sessions clears the previous rows here.
		syncNoticeSession(id);
		let turns: InspectorTurn[] = [];
		try {
			const res = await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(id)}/inspect/turns`);
			if (res.ok) {
				const body = (await res.json()) as { turns?: InspectorTurn[]; notices?: unknown };
				turns = body.turns ?? [];
				// The engine's swallowed-service rows (recall/lane/index/hook).
				setDegradationNotices(id, body.notices ?? []);
			}
		} catch {
			/* overlays are additive; absence is normal on a legacy engine */
		}
		// The proposal queue is read through the SAME client the review drawer uses,
		// so the Inspector badge and that drawer can never disagree (owner ruling:
		// "Re-use this notification for the proposals too when they fire").
		void loadPendingProposals(id);
		const revisions = await listRevisions(id);
		// A FAIL regeneration supersedes the PREVIOUS revision, not the narrative
		// (nodes.py `_run_critic`), so resolve every row to its chain root — the
		// event id a transcript entry carries as `logId`. The newest reason in the
		// chain wins for the edit badge, and every revision id maps back to that
		// entry so a live chained `revision` frame still lands on the right turn.
		const chainParent = new Map<number, number>();
		for (const row of revisions) {
			if (row.supersedes !== null) chainParent.set(row.id, row.supersedes);
		}
		const chainRoot = (eventId: number): number => {
			let current = eventId;
			for (let hops = 0; hops < 64 && chainParent.has(current); hops += 1) {
				current = chainParent.get(current) as number;
			}
			return current;
		};
		const entryIdByLogId = new Map<number, string>();
		for (const entry of get(transcript)) {
			if (entry.logId !== undefined) entryIdByLogId.set(entry.logId, entry.id);
		}
		revisionMemo = new Map();
		revisionEntryByEvent = new Map();
		for (const row of revisions) {
			const root = chainRoot(row.supersedes ?? row.id);
			revisionMemo.set(root, row.reason);
			const entryId = entryIdByLogId.get(root);
			if (entryId !== undefined) revisionEntryByEvent.set(row.id, entryId);
		}

		const byUser = new Map<number, InspectorTurn>();
		const byNarr = new Map<number, InspectorTurn>();
		for (const turn of turns) {
			if (typeof turn.user_seq === 'number') byUser.set(turn.user_seq, turn);
			if (typeof turn.narrative_seq === 'number') byNarr.set(turn.narrative_seq, turn);
		}

		const next: Record<string, TurnMeta> = {};
		for (const entry of get(transcript)) {
			const meta: TurnMeta = { rolls: [], flags: [], editReason: null };
			if (entry.logId !== undefined) {
				const reason = revisionMemo.get(entry.logId);
				if (reason !== undefined) meta.editReason = reason || 'user edit';
			}
			const seq = seqByEntry.get(entry.id);
			if (seq !== undefined && seq >= 0) {
				if (entry.role === 'user') {
					const turn = byUser.get(seq);
					if (turn?.rolls) meta.rolls = normalizeRolls(turn.rolls);
				} else {
					const turn = byNarr.get(seq);
					if (turn) {
						meta.flags = flattenFlags(turn.validation_details);
						// Only a fallback: the revisions list above resolves the whole
						// chain, so its newest reason wins. `revision_refs` lists direct
						// supersedes only and can be one regeneration attempt old.
						if (meta.editReason === null) {
							for (const rev of turn.revision_refs ?? []) {
								if (rev && typeof rev.reason === 'string' && rev.reason) {
									meta.editReason = rev.reason;
								}
							}
						}
					}
				}
			}
			if (meta.rolls.length || meta.flags.length || meta.editReason !== null) {
				next[entry.id] = meta;
			}
		}
		turnMeta = next;
	}

	function normalizeRolls(rolls: InspectorRoll[]): TurnMeta['rolls'] {
		const out: TurnMeta['rolls'] = [];
		for (const roll of rolls) {
			if (!roll || typeof roll.dice !== 'string') continue;
			out.push({
				dice: roll.dice,
				result: typeof roll.result === 'number' ? roll.result : 0,
				detail: typeof roll.detail === 'string' ? roll.detail : null,
				ask: typeof roll.ask === 'string' ? roll.ask : null
			});
		}
		return out;
	}

	/** Flatten `validation_details` payloads into one normalized flag list. */
	function flattenFlags(details: unknown): ValidatorFlag[] {
		const out: ValidatorFlag[] = [];
		if (!Array.isArray(details)) return out;
		for (const detail of details) {
			if (!detail || typeof detail !== 'object') continue;
			const flags = (detail as { flags?: unknown }).flags;
			if (!Array.isArray(flags)) continue;
			for (const raw of flags) {
				if (!raw || typeof raw !== 'object') continue;
				const obj = raw as Record<string, unknown>;
				out.push({
					rule_id: typeof obj.rule_id === 'string' ? obj.rule_id : undefined,
					check: typeof obj.check === 'string' ? obj.check : 'validator',
					severity: typeof obj.severity === 'string' ? obj.severity : 'warn',
					evidence: typeof obj.evidence === 'string' ? obj.evidence : '',
					source: typeof obj.source === 'string' ? obj.source : undefined,
					segment_index: typeof obj.segment_index === 'number' ? obj.segment_index : null,
					message: typeof obj.message === 'string' ? obj.message : ''
				});
			}
		}
		return out;
	}

	/** Jump the transcript to the first turn a recap covers (search-anchor pattern). */
	function onJumpSeq(seq: number): void {
		const logId = seqToLogId.get(seq);
		const targetId = logId !== undefined ? `history-${logId}` : `history-${seq}`;
		document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
	}

	// --- W9.26 §2.2/§2.5: system rows in the ordered transcript ---------------
	//
	// The projection carries two chain-walked lists: `context_lows` (the gauge's
	// notices) and `compactions` (one MARKER per era boundary). The transcript is
	// one ordered stream — old era rows … break … notice … new era rows — so each
	// row becomes a SYSTEM ENTRY in the same list, positioned by its own `seq`
	// (compaction forks at the tip, so chain seqs order the transcript).

	/** One `context_low` row as a transcript entry, plus its settled verdict.
	 *
	 * SETTLED iff a compaction marker exists LATER in the same chain (seq compare,
	 * §2.2): an inherited row renders above the break with its button spent. */
	function buildContextLowEntry(
		row: ContextLowRow,
		markers: CompactionMarker[]
	): TranscriptEntryView {
		const payload = row.payload ?? {};
		return {
			id: `context-low-${row.session_id}-${row.seq}`,
			role: 'narrator',
			prose: '',
			dsl: '',
			reasoning: '',
			streaming: false,
			system: {
				kind: 'context_low',
				contextLow: {
					ratio: typeof payload.ratio === 'number' ? payload.ratio : null,
					promptTokens: typeof payload.prompt_tokens === 'number' ? payload.prompt_tokens : null,
					budgetTokens: typeof payload.budget_tokens === 'number' ? payload.budget_tokens : null,
					basis: typeof payload.basis === 'string' ? payload.basis : '',
					overBudget: payload.over_budget === true,
					noop: payload.noop === true,
					settled: markers.some((marker) => marker.seq > row.seq)
				}
			}
		};
	}

	/** The compaction marker's view (shared by its break row and its notice row). */
	function compactionView(marker: CompactionMarker): CompactionBreakView {
		const payload = marker.payload ?? {};
		const covers = Array.isArray(payload.covers_seqs) ? payload.covers_seqs : [];
		const ref = Array.isArray(payload.summary_ref) ? payload.summary_ref : [];
		return {
			seq: marker.seq,
			sessionId: marker.session_id,
			sourceSession:
				typeof payload.source_session === 'string' && payload.source_session
					? payload.source_session
					: null,
			low: typeof covers[0] === 'number' ? covers[0] : null,
			high: typeof covers[1] === 'number' ? covers[1] : null,
			trigger: typeof payload.trigger === 'string' ? payload.trigger : '',
			summaryRef:
				typeof ref[0] === 'string' && typeof ref[1] === 'number'
					? ([ref[0], ref[1]] as [string, number])
					: null
		};
	}

	/** One break row per marker (the collapse chevron folds the span above it). */
	function buildCompactionBreakEntry(marker: CompactionMarker): TranscriptEntryView {
		return {
			id: `compaction-break-${marker.session_id}-${marker.seq}`,
			role: 'narrator',
			prose: '',
			dsl: '',
			reasoning: '',
			streaming: false,
			system: { kind: 'compaction_break', compaction: compactionView(marker) }
		};
	}

	/** The notice under the break: "Compaction done" + a fetch-on-expand <details>. */
	function buildCompactionNoticeEntry(marker: CompactionMarker): TranscriptEntryView {
		return {
			id: `compaction-notice-${marker.session_id}-${marker.seq}`,
			role: 'narrator',
			prose: '',
			dsl: '',
			reasoning: '',
			streaming: false,
			system: { kind: 'compaction_notice', compaction: compactionView(marker) }
		};
	}

	/**
	 * The [Compact] button's 202-then-poll flow (W9.26 §2.3).
	 *
	 * `refreshSessions()` BEFORE `openSession()`: `openSession` reads the era's
	 * title from the session store, and the lazily-registered era child serves
	 * `title:""` until the list has been refreshed.
	 */
	async function compactNow(): Promise<void> {
		const id = get(sessionId);
		if (!id || compacting) return;
		compacting = true;
		compactError = '';
		pendingStatus = 'Starting the compaction…';
		const started = await startCompaction(id);
		if ('error' in started) {
			compacting = false;
			pendingStatus = '';
			if (started.successorId) {
				// The idempotent successor return: the era already exists.
				await refreshSessions();
				await openSession(started.successorId);
				return;
			}
			compactError =
				started.reason === 'compacting'
					? 'a compaction is already running — try again in a moment'
					: started.error;
			return;
		}
		const job = await pollCompactionJob(id, started.job_id, {
			onStatus: (status) => {
				if (status.status === 'running') {
					pendingStatus = status.stage
						? `Compacting — ${status.stage}…`
						: 'Compacting the history…';
				}
			}
		});
		compacting = false;
		pendingStatus = '';
		if (job.status === 'done' && job.session_id) {
			await refreshSessions();
			await openSession(job.session_id);
			return;
		}
		// `failed` / `noop`: the reason renders inline on the context-low row and
		// the button re-arms (`compacting` is already false).
		compactError =
			job.error ||
			(job.status === 'noop'
				? 'nothing to compact — the era is already summarized'
				: 'the compaction failed');
	}

	/**
	 * ONE projection refetch ~5 s after a turn's `done` (W9.26 §2.2).
	 *
	 * The context monitor is a post-turn hook: the SSE stream closes at `done`, so
	 * a `context_low` row written by the hook can never ride a frame. A single
	 * delayed GET catches it without any tail machinery. Cancelled by the next
	 * turn and by any session switch; skipped while a turn is streaming so a
	 * wholesale rehydrate can never clobber live entries.
	 */
	function scheduleContextRefetch(turnSession: string): void {
		clearContextRefetch();
		contextRefetchTimer = setTimeout(() => {
			contextRefetchTimer = null;
			if (get(sessionId) !== turnSession) return;
			if (streaming || get(transcript).some((entry) => entry.streaming)) return;
			void refreshTranscript();
		}, CONTEXT_LOW_REFETCH_MS);
	}

	function clearContextRefetch(): void {
		if (contextRefetchTimer !== null) {
			clearTimeout(contextRefetchTimer);
			contextRefetchTimer = null;
		}
	}

	/**
	 * Refresh the sibling context for a session's latest reply. `null` (legacy
	 * backend / mock) renders exactly as before branching; a `count === 1`
	 * context still offers the retry affordance to create the first alternate.
	 */
	async function refreshBranches(id: string): Promise<void> {
		// Its OWN failure handling: every call site used to run this last inside a
		// shared `try`, so an upstream throw skipped it silently and the ↻ controls
		// (they render only when `branchInfo` is non-null) stayed gone until reload.
		for (let attempt = 0; attempt < 2; attempt += 1) {
			let info: BranchInfo | null = null;
			try {
				// `lastUserSeq` lets a root session merge children forked at its own
				// last turn even if the backend reports siblings=[self] only.
				info = await listBranches(id, lastUserSeq);
			} catch (error) {
				console.warn('branch refresh failed', error);
			}
			if (info !== null) {
				// Keep `count === 1` info: the retry affordance is still offered so the
				// first alternate can be created. `null` means the backend has no branch
				// support (legacy/no endpoint) and nothing extra renders.
				branchInfo = info;
				return;
			}
			// `listBranches` returns null on ANY non-OK/network failure, so one immediate
			// retry after a short settle recovers a transient blip instead of leaving the
			// session without branch controls for the rest of its life.
			if (attempt === 0) await new Promise((resolve) => setTimeout(resolve, 150));
		}
		branchInfo = null;
		console.warn(`branch refresh returned no context for ${id}`);
	}

	/** Step to an already-loaded sibling; no-op past either end.
	 *
	 * W9.26 §2.6: a `kind:"compaction"` sibling is a CONTINUATION era, never a
	 * swipe alternative — walking over it would silently move the reader into a
	 * different log. Step to the next USER sibling in the requested direction
	 * (the groups are already filtered in `normalizeBranches`; this is the guard
	 * for a backend that lists continuations among the siblings).
	 */
	async function swipe(delta: 1 | -1): Promise<void> {
		const id = get(sessionId);
		if (!id || !branchInfo || branchBusy || streaming) return;
		const from = branchInfo.siblings.findIndex((sibling) => sibling.sessionId === id);
		let target: (typeof branchInfo.siblings)[number] | undefined;
		for (let i = from + delta; i >= 0 && i < branchInfo.siblings.length; i += delta) {
			if (branchInfo.siblings[i].kind !== 'compaction') {
				target = branchInfo.siblings[i];
				break;
			}
		}
		if (!target) return;
		branchBusy = true;
		try {
			await openSession(target.sessionId);
		} finally {
			branchBusy = false;
		}
	}

	/**
	 * Fork at the last user message and stream a fresh reply into the new
	 * branch. When the current session is itself a branch, the fork source is
	 * its parent, so retry produces a *sibling* (not a child). The reply is
	 * previewed in place; on success we switch to the new branch and hydrate its
	 * projection. A cancelled/empty turn keeps the branch (no auto-prune) and
	 * just drops the preview from the current view.
	 */
	/** Message-edit door (SURFACE #6): append a revision, then rehydrate so
	 * the transcript shows latest-wins without a manual reload. */
	async function reviseEntry(logId: number, newText: string): Promise<boolean> {
		const id = get(sessionId);
		if (!id) return false;
		const result = await postRevision(id, logId, newText);
		if ('error' in result) {
			console.error(result.error);
			return false;
		}
		try {
			const projection = await getProjection(id);
			hydrate(projection, await hydrateAttachments(id));
			await applyTheme(id);
			await refreshBranches(id);
			await loadTurnMeta(id); // surface the edit marker immediately
		} catch {
			// projection refresh failed; the revision landed — reload will show it
		}
		return true;
	}

	/**
	 * Issue #35: delete a message and everything after it.
	 *
	 * Deleting is a fork, not a log rewrite: the engine appends a fork row just
	 * below the pivot and hands back the new branch's id, and we switch to it the
	 * same way a swipe or a retry does — same URL, same transcript rehydrate, same
	 * rail refresh. The old line is untouched and stays one ‹ away, so nothing is
	 * ever actually lost; the confirm the row asked for already named the count.
	 *
	 * No stream runs here and the source session may not be mid-turn, so this is
	 * the only concurrency guard it needs (`branchBusy` = the same serialization
	 * swipe/retry take).
	 */
	async function deleteFrom(logId: number): Promise<boolean> {
		const id = get(sessionId);
		if (!id || streaming || branchBusy) return false;
		branchBusy = true;
		try {
			const outcome = await deleteTail(id, logId);
			if (outcome.status !== 'done' || !outcome.sessionId) {
				failConnection(outcome.error?.message ?? 'the delete failed');
				return false;
			}
			connection.set('idle');
			await openSession(outcome.sessionId);
			await refreshSessions();
			return true;
		} finally {
			branchBusy = false;
		}
	}

	async function retry(): Promise<void> {
		const id = get(sessionId);
		if (!id || streaming || branchBusy) return;
		// Issue #74, #80: `lastUserSeq` is ALWAYS re-read from the projection here.
		// `send()` never updates it (only a settled/committed turn does) and ANY
		// failed turn — turn-level narration failures never put `connection` into
		// its error state — returns without a refresh, so the pointer still names
		// the PREVIOUS settled turn's user message. The refresh is unconditional so
		// the fork cuts at the message the reader actually acted on.
		await refreshTranscript();
		// The CURRENT session always owns the message acted on. `branchInfo`
		// describes a stored SIBLING pivot (the previous fork's own base_seq), so
		// forking the parent at that cut rewinds the story to an earlier turn.
		const source = id;
		const at = lastUserSeq;
		if (at === null) return;

		branchBusy = true;
		streaming = true;
		const entryId = addNarratorEntry();
		const local = new AbortController();
		controller = local;
		connection.set('streaming');

		let raw = '';
		// Once the critic has replaced the turn (D41), a late content delta must
		// not clobber the revision with the pre-critic raw text. In-graph order
		// already prevents that; the guard keeps it true.
		let replaced = false;
		// The row the barrier committed. The branch hydrate below uses it to
		// recognise the turn it just streamed (FIX: reconcile to ITS row).
		let committedNarrSeq: number | null = null;
		beginTurnWindow();
		const outcome = await forkTurn(source, at, lastUserContent, lastUserKind, {
			signal: local.signal,
			onReasoning: (delta) => appendReasoning(entryId, delta),
			onContent: (delta) => {
				if (replaced) return;
				raw += delta;
				const segmented = splitProseAndDsl(raw);
				updateEntry(entryId, {
					prose: segmented.prose,
					dsl: segmented.dsl,
					segments: segmented.segments
				});
			},
			onStage: handleStage,
			onRevision: (revision) => {
				// The preview lives in `id`'s view while the stream runs in the branch.
				if (applyRevisionFrame(revision, id) !== null) replaced = true;
			},
			// W9.19 (A) / owner ruling: the barrier means the turn is KEPT, not
			// that the turn is DONE. The preview stays the running draft until
			// `done`, so the pinned tracker keeps showing the in-graph tail.
			onCommitted: (meta) => {
				committedNarrSeq = barrierNarrSeq(meta);
			},
			onError: (error) => updateEntry(entryId, { error: error.message })
		});
		setProcessingStage(null);
		finishEntry(entryId);
		if (controller === local) controller = null;
		streaming = false;
		branchBusy = false;

		if (outcome.status === 'done' && outcome.branchId) {
			connection.set('idle');
			await openSession(outcome.branchId, committedNarrSeq);
			await refreshSessions();
			return;
		}
		if (outcome.status === 'error') {
			failConnection(outcome.error?.message ?? 'the fork failed');
			if (get(sessionId) === id) {
				updateEntry(entryId, { error: outcome.error?.message ?? 'fork failed' });
			}
			await refreshBranches(id);
			return;
		}
		// Stopped: keep the branch (retain-siblings policy), drop the preview.
		if (get(sessionId) === id) removeEntry(entryId);
		connection.set('idle');
		await refreshBranches(id);
		await refreshSessions();
	}

	function selectSession(id: string): void {
		if (id === get(sessionId)) return;
		void openSession(id);
	}

	/** A world chat was just materialized (the rail's world-row "+", D51): refresh
	 *  the list, then open it. */
	async function handleStartWorldChat(id: string): Promise<void> {
		await refreshSessions();
		selectSession(id);
	}

	/** A persona chat was just created from the rail's actor "+" (the actor door):
	 *  refresh the list FIRST so the persona picker resolves the new row (without
	 *  it the picker shows "No chat open" until the first turn), then open it. */
	async function handleStartPersonaChat(id: string): Promise<void> {
		await refreshSessions();
		selectSession(id);
	}

	/**
	 * Open the Design Studio on `kind`, optionally focusing one row (`focusId`) or
	 * jumping straight to the create form (`startNew`). Every KIND-TARGETING door
	 * calls this (the rail's ✎/New/tools doors, the assistant panel's ✎); the
	 * header Studio button just toggles `studioOpen` and lets the shell keep the
	 * tab the user left it on.
	 *
	 * Choice (D51 L3.a): the page ALWAYS sets the shell props and lets the shell /
	 * pane react — no page-side auto-switch heuristics. The dirty guard is the
	 * shell's: `openRequest` is routed through its `selectKind`, so a request that
	 * would discard staged edits raises the confirm, and a cancelled confirm leaves
	 * the open pane untouched. A focus id that names no loaded row is dropped by
	 * the pane (and retried once the catalog changes), so an over-eager
	 * `focusId` is harmless; a repeat request for the SAME row does not re-seed the
	 * pane's editor (L2 panes apply each request once).
	 */
	function openStudio(
		kind: StudioKind = 'persona',
		opts: { focusId?: string | null; startNew?: boolean } = {}
	): void {
		studioFocusId = opts.focusId ?? null;
		studioStartNew = opts.startNew ?? false;
		studioRequest = { kind, seq: (studioRequest?.seq ?? 0) + 1 };
		studioOpen = true;
	}

	/**
	 * A studio pane changed a definition. The page owns no entity catalog, but the
	 * rail does: bumping `railRefreshToken` makes SessionDrawer re-read personas /
	 * worlds / books, so a definition created in the studio shows up in the rail
	 * without a page reload. (The assistant panel re-reads its own catalog when its
	 * picker opens, and no definition change can move the page-owned session list.)
	 */
	function handleStudioChanged(): void {
		railRefreshToken += 1;
	}

	function abortActive(): void {
		controller?.abort();
		controller = null;
		streaming = false;
		setProcessingStage(null);
	}

	async function handleRename(id: string, title: string): Promise<void> {
		try {
			await renameSession(id, title);
			if (get(sessionId) === id) sessionTitle.set(title);
		} finally {
			await refreshSessions();
		}
	}

	async function handleDelete(id: string): Promise<void> {
		const known = get(sessions).find((item) => item.id === id);
		const label = known?.title || 'this chat';
		const ok = window.confirm(
			`Delete “${label}”? It leaves the list; its history is kept in the log.`
		);
		if (!ok) return;
		try {
			await deleteSession(id);
		} catch {
			return;
		}
		await refreshSessions();
		if (get(sessionId) === id) newChat();
	}

	/** Validate a stored `payload.segments` array; `undefined` when absent.
	 * Prose segments that still contain statement lines (backticked or bare —
	 * legacy payloads from before extractor tolerance) are re-split so older
	 * sessions render panels instead of their text. */
	function coerceSegments(value: unknown): NarratorSegment[] | undefined {
		if (!Array.isArray(value)) return undefined;
		const out: NarratorSegment[] = [];
		for (const raw of value) {
			if (!raw || typeof raw !== 'object') continue;
			const kind = (raw as { kind?: unknown }).kind;
			const text = (raw as { text?: unknown }).text;
			if ((kind === 'prose' || kind === 'dsl') && typeof text === 'string') {
				if (kind === 'prose') {
					const reSplit = splitProseAndDsl(text);
					if (reSplit.dslStarted) {
						out.push(...reSplit.segments);
						continue;
					}
				}
				out.push({ kind, text });
			}
		}
		return out;
	}

	/**
	 * The narrator render fields of ONE committed narrative payload.
	 *
	 * `segments` is the authoritative interleave; the flat `dsl` channel carries
	 * the JOINED statements (the same cumulative payload the one-parse renderer
	 * mounts) so flat-channel consumers — DslHeader's scene title (W3B) over
	 * sceneTitleOf (W3A) — see the whole reply, not an empty string. Merge-seam
	 * fix, found at the wave gate. A legacy payload with no `segments` is
	 * re-segmented with the SAME client segmenter the live stream uses: the flat
	 * pair renders prose then every panel stacked at the bottom, which is the
	 * layout bug segments exist to prevent.
	 *
	 * Shared by `hydrate` and by the live turn's post-`done` reconcile, so a
	 * streamed turn ends in exactly the state a reload builds: the engine's DSL
	 * gate may REWRITE the draft after its deltas (deterministic repair, or the
	 * last-resort model resynthesis — `nodes.narrator`), and no stream frame
	 * carries the repaired text (only the critic emits `revision`, D41). The
	 * streamed draft therefore stays on screen as raw DSL until it is rebuilt
	 * from the committed payload.
	 */
	function narratorFieldsFromPayload(payload: Record<string, unknown>): {
		prose: string;
		dsl: string;
		segments?: NarratorSegment[];
	} {
		const segments = coerceSegments(payload.segments);
		if (segments && segments.length > 0) {
			return {
				prose: '',
				dsl: segments
					.filter((segment) => segment.kind === 'dsl')
					.map((segment) => segment.text)
					.join('\n'),
				segments
			};
		}
		let prose = typeof payload.prose === 'string' ? payload.prose : '';
		let dsl = typeof payload.dsl === 'string' ? payload.dsl : '';
		if (!prose && !dsl && typeof payload.text === 'string') {
			const segmented = splitProseAndDsl(payload.text);
			prose = segmented.prose;
			dsl = segmented.dsl;
		}
		if (prose || dsl) {
			// Synthesize the interleave the payload omitted, preserving the flat
			// channels for their consumers. Splitting `prose + dsl` in document
			// order puts every statement back at its anchor.
			const synthesized = splitProseAndDsl(`${prose}\n\n${dsl}`);
			return { prose, dsl, segments: synthesized.segments };
		}
		return { prose, dsl };
	}

	/**
	 * D-F: the projection's validation notices keyed by the narrative seq they
	 * follow. ONE mapping shared by `hydrate` and by the live turn's post-`done`
	 * reconcile, so a live turn ends in exactly the state a reload builds.
	 *
	 * The notice is written by the validate stage AFTER the commit barrier, so no
	 * stream frame carries it: a live turn first learns of it from the post-`done`
	 * projection. Attaching it to the narrator ENTRY (not appending a separate
	 * transient row) is what makes it survive that rebuild — the same patch that
	 * stamps the committed text and log id stamps the notice.
	 */
	function noticeByNarrativeSeq(projection: Projection): Map<number, ValidationNotice> {
		const bySeq = new Map<number, ValidationNotice>();
		for (const notice of projection.notices ?? []) bySeq.set(notice.seq, notice);
		return bySeq;
	}

	/** The narrative row the commit barrier named, or `null` when the frame
	 *  carried none (a legacy server, or a resume that missed the frame). */
	function barrierNarrSeq(meta?: Record<string, unknown>): number | null {
		const raw = meta?.narrative_seq;
		return typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
	}

	/** The projection read, or `null` when it failed (the caller owns the retry). */
	async function readProjection(id: string): Promise<Projection | null> {
		try {
			return await getProjection(id);
		} catch {
			return null;
		}
	}

	/**
	 * The committed narrative this turn must reconcile against.
	 *
	 * `narrative_seq` from the barrier names the row exactly; `exact: false`
	 * means the read did not contain it (the caller retries once). With no seq
	 * the newest narrative is the only available answer and counts as exact —
	 * it is what the pre-fix code always used.
	 */
	function committedNarrative(
		projection: Projection,
		committedNarrSeq: number | null
	): { item: ProjectionItem | null; exact: boolean } {
		const ordered = [...projection.narratives].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
		const newest = ordered[ordered.length - 1] ?? null;
		if (committedNarrSeq === null) return { item: newest, exact: true };
		const match = ordered.find((item) => item.seq === committedNarrSeq) ?? null;
		return { item: match ?? newest, exact: match !== null };
	}

	/** Mark the live entry as an UNVERIFIED draft. `stale` is a view flag owned
	 *  by TranscriptList's `TranscriptEntryView`; the store's entry type predates
	 *  it, hence the cast. */
	function setEntryStale(entryId: string, stale: boolean): void {
		updateEntry(entryId, { stale } as Partial<TranscriptEntry>);
	}

	/**
	 * Settle the live narrator entry against the row the barrier committed.
	 *
	 * What streamed is the narrator's DRAFT: the engine's DSL gate may rewrite it
	 * after the last delta (`nodes.narrator`) and only the log holds the final
	 * text. The entry reconciles to `committedNarrSeq` — never blindly to "the
	 * newest row", which is a DIFFERENT turn if anything appended out of band.
	 *
	 * A projection error or a missing row marks the entry stale and retries the
	 * read ONCE; a second failure leaves the marker (and the draft) visible
	 * rather than silently presenting streamed text as committed.
	 */
	async function settleCommittedTurn(
		turnSession: string,
		entryId: string,
		userEntryId: string,
		committedNarrSeq: number | null
	): Promise<void> {
		let projection = await readProjection(turnSession);
		let selection = projection ? committedNarrative(projection, committedNarrSeq) : null;
		if (!selection || !selection.exact) {
			setEntryStale(entryId, true);
			projection = await readProjection(turnSession);
			selection = projection ? committedNarrative(projection, committedNarrSeq) : null;
		}
		if (!projection) return;
		setFacts(factsFromState(projection.state));
		await applyTheme(turnSession); // tone may have shifted this turn
		const lastUser = [...projection.user_messages].sort(
			(a, b) => (a.seq ?? 0) - (b.seq ?? 0)
		).pop();
		if (lastUser) {
			lastUserSeq = lastUser.seq ?? null;
			lastUserContent =
				typeof lastUser.payload?.text === 'string' ? lastUser.payload.text : '';
			lastUserKind = lastUser.payload?.kind === 'ooc' ? 'ooc' : 'in_character';
			seqByEntry.set(userEntryId, lastUser.seq ?? -1);
			// Issue #63: the optimistic bubble holds what was TYPED. Settle it to the
			// engine's display render (known `{{user}}` / `{{char}}` / `{{world}}`
			// macros resolved) once the turn commits. `lastUserContent` above stays
			// RAW: a regenerate/fork re-sends the stored bytes, never the render.
			updateEntry(userEntryId, {
				logId: lastUser.id,
				prose:
					typeof lastUser.payload?.text_rendered === 'string'
						? lastUser.payload.text_rendered
						: lastUserContent
			});
		}
		const narrative = selection?.item ?? null;
		if (narrative) {
			seqByEntry.set(entryId, narrative.seq ?? -1);
			// What streamed is the narrator's draft; the engine's DSL gate may have
			// rewritten it after the last delta and no frame carries that text. An
			// image row is a post-turn hook's canon entry, not this turn's text.
			const payload = narrative.payload ?? {};
			const isNarrativeText =
				typeof payload.image_asset_id !== 'string' && !payload.image_asset_id;
			// D-F: the validate stage runs in the tail AFTER the barrier, so the notice
			// is absent from every frame and present only here.
			const notice = noticeByNarrativeSeq(projection).get(narrative.seq ?? -1);
			updateEntry(entryId, {
				logId: narrative.id,
				...(isNarrativeText ? narratorFieldsFromPayload(payload) : {}),
				...(notice ? { notice } : {}),
				stale: !selection?.exact
			} as Partial<TranscriptEntry>);
		}
		await loadTurnMeta(turnSession);
		await refreshSessions();
	}

	/**
	 * D41: the critic amended a turn on the live stream. Replace that transcript
	 * turn in place with the frame's `segments` — the transcript renders
	 * `segments`, NOT the joined `prose` (a stale segment list would keep showing
	 * the pre-critic panels/DSL). The entry id is stable, so nothing remounts and
	 * the autoscroll pin keeps its node.
	 *
	 * `viewSessionId` is the session whose view holds the streamed turn: a fork
	 * preview lives in the parent session's view while the stream runs in the new
	 * branch, and `RevisionFrame` (lib/api) carries no `session_id` to check.
	 *
	 * Returns the replaced entry id, or `null` when the frame cannot be placed.
	 */
	function applyRevisionFrame(revision: RevisionFrame, viewSessionId: string): string | null {
		if (get(sessionId) !== viewSessionId) return null;
		const entries = get(transcript);
		let entryId: string | null = null;
		for (const entry of entries) {
			if (entry.logId !== undefined && entry.logId === revision.target_id) {
				entryId = entry.id;
				break;
			}
		}
		// A FAIL regeneration targets the previous revision; that id maps back to
		// the turn through the frames already seen (and the rows loaded at hydrate).
		if (!entryId) entryId = revisionEntryByEvent.get(revision.target_id) ?? null;
		if (!entryId) {
			// The live turn's narrator entry has no logId yet; the critic always
			// revises the turn that just streamed (D49, in-graph post-turn).
			for (let i = entries.length - 1; i >= 0; i -= 1) {
				if (entries[i].role === 'narrator' && entries[i].streaming) {
					entryId = entries[i].id;
					break;
				}
			}
		}
		if (!entryId) return null;
		const target: string = entryId;

		const entry = entries.find((item) => item.id === target);
		const patch: Partial<TranscriptEntry> = {
			prose: typeof revision.prose === 'string' ? revision.prose : '',
			dsl: typeof revision.dsl === 'string' ? revision.dsl : ''
		};
		const segments = coerceSegments(revision.segments);
		if (segments) patch.segments = segments;
		else console.warn('revision frame carried no usable segments; keeping the rendered turn');
		// The first commit's target IS the narrative event id (what a reload calls
		// this entry's `logId`); a chained regen's target is a revision id, which
		// must not become the entry's log id.
		if (entry && entry.logId === undefined && !revisionEntryByEvent.has(revision.target_id)) {
			patch.logId = revision.target_id;
		}
		updateEntry(target, patch);

		if (typeof revision.revision_id === 'number') {
			revisionEntryByEvent.set(revision.revision_id, target);
		}
		// The frame carries the reason but NOT the validator flags: keep the flags
		// already loaded for this turn and record the reason the edit badge reads.
		// `loadTurnMeta` (turn end) re-derives both from the log.
		const known = turnMeta[target] ?? { rolls: [], flags: [], editReason: null };
		turnMeta = {
			...turnMeta,
			[target]: { ...known, editReason: revision.reason || 'critic revision' }
		};
		return target;
	}

	/** Rebuild the transcript from a projection (best-effort history restore). */
	/** Map a session's assets to bubble payloads: image data URLs (by seq AND by
	 * asset id — an imagegen narrative references its asset by id, not seq) and
	 * audio/video raw-endpoint sources for <video>/<audio> elements. */
	async function hydrateAttachments(sessionId: string): Promise<{
		bySeq: Map<number, { image?: string; media?: { kind: string; mime: string; url: string } }>;
		byAssetId: Map<string, string>;
	}> {
		const bySeq = new Map<number, { image?: string; media?: { kind: string; mime: string; url: string } }>();
		const byAssetId = new Map<string, string>();
		try {
			const list = await listAttachments(sessionId);
			for (const asset of list.attachments) {
				if (asset.data_url) byAssetId.set(asset.asset_id, asset.data_url);
				if (typeof asset.seq !== 'number') continue;
				if (asset.data_url) {
					bySeq.set(asset.seq, { image: asset.data_url });
				} else if (asset.mime.startsWith('audio/') || asset.mime.startsWith('video/')) {
					bySeq.set(asset.seq, {
						media: {
							kind: asset.mime.startsWith('audio/') ? 'audio' : 'video',
							mime: asset.mime,
							url: `/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(asset.asset_id)}/raw`
						}
					});
				}
			}
		} catch {
			// attachments endpoint optional (legacy); rehydrate without media
		}
		return { bySeq, byAssetId };
	}

	/** One canon-image transcript entry — the SOLE construction for a generated
	 * image. Both shapes take it: the current canon `narrative` row (actor
	 * "imagegen") and the LEGACY `user` row that carried kind=="image". Each
	 * stores the asset id in `payload.image_asset_id`; the effective prompt is
	 * `payload.prompt` (legacy rows fall back to `payload.text`), mirroring the
	 * backend's `prompt or text` (wiring.history_from_log). A not-yet-resolvable
	 * asset data URL leaves `image` undefined — the entry is still canon, never a
	 * user bubble. */
	function buildCanonImageEntry(
		item: ProjectionItem,
		attachments: { byAssetId: Map<string, string> }
	): TranscriptEntryView {
		const payload = item.payload ?? {};
		const imageAssetId =
			typeof payload.image_asset_id === 'string' ? payload.image_asset_id : '';
		const image = imageAssetId ? attachments.byAssetId.get(imageAssetId) : undefined;
		const prompt =
			typeof payload.prompt === 'string' && payload.prompt
				? payload.prompt
				: typeof payload.text === 'string'
					? payload.text
					: '';
		return {
			id: `history-${item.id}`,
			logId: item.id,
			role: 'narrator',
			prose: '',
			dsl: '',
			reasoning: '',
			streaming: false,
			...(image ? { image } : {}),
			caption: `Image Prompt: ${prompt}`
		};
	}

	function hydrate(
		projection: Projection,
		attachments: {
			bySeq: Map<number, { image?: string; media?: { kind: string; mime: string; url: string } }>;
			byAssetId: Map<string, string>;
		} = { bySeq: new Map(), byAssetId: new Map() }
	): void {
		const rows = [...projection.user_messages, ...projection.narratives].sort(
			(a, b) => (a.seq ?? 0) - (b.seq ?? 0)
		);
		// A generated image is canon, but the asset's seq still sits just before
		// the row that carries it, so it would otherwise land inside a user
		// message's attachment window and render twice (once as a user bubble).
		// Exclude the images canon owns from the user-attachment path — they
		// render in the canon lane instead. TWO shapes carry a generated image:
		// the current canon `narrative` row (actor "imagegen") and the LEGACY
		// `user` row (type "user" + kind=="image"; the only writer that sets
		// kind=="image" is api/imagegen_api.py — never a real upload).
		const canonImageUrls = new Set<string>();
		for (const item of [...projection.narratives, ...projection.user_messages]) {
			const payload = item.payload ?? {};
			if (item.type === 'user' && payload.kind !== 'image') continue;
			const assetId = payload.image_asset_id;
			if (typeof assetId !== 'string') continue;
			const url = attachments.byAssetId.get(assetId);
			if (url) canonImageUrls.add(url);
		}
		// The fork cut is always the last user event in the projection.
		const lastUser = [...rows].reverse().find((item) => item.type === 'user');
		lastUserSeq = lastUser?.seq ?? null;
		lastUserContent =
			typeof lastUser?.payload?.text === 'string' ? lastUser.payload.text : '';
		lastUserKind = lastUser?.payload?.kind === 'ooc' ? 'ooc' : 'in_character';
		let prevUserSeq = -1; // assets between user messages belong to the later one
		// Wave-1 Bug 1: a world chat's opening scene is a `narrative` event marked
		// `opening: true` (fabulist.worlds.registry). It renders in the pinned
		// Opening block (TranscriptList), never as a transcript row — exactly how a
		// card chat's greeting appears only there. Display-only skip: the event
		// still rides history/recap/inspector on the server side.
		// D-F: a flagged turn's notice renders at the tail of ITS narrative entry,
		// so hydration joins it by the anchor seq the projection carries.
		const noticesBySeq = noticeByNarrativeSeq(projection);
		const visibleRows = rows.filter(
			(item) => !(item.type === 'narrative' && (item.payload ?? {}).opening === true)
		);
		const entries: TranscriptEntryView[] = visibleRows
			.map((item) => {
			const payload = item.payload ?? {};
			// Connect the hydrated entry to the inspector turn by seq (and the
			// recap jump anchors seq -> event id). See loadTurnMeta / onJumpSeq.
			seqByEntry.set(`history-${item.id}`, item.seq ?? -1);
			if (item.seq !== undefined && item.seq !== null) seqToLogId.set(item.seq, item.id);
			if (item.type === 'user') {
				const rawText = typeof payload.text === 'string' ? payload.text : '';
				// Issue #63: the engine renders the known macros for DISPLAY
				// (`text_rendered`); `text` stays the raw stored bytes, which the
				// revision door re-opens and a regenerate re-sends.
				const text =
					typeof payload.text_rendered === 'string' ? payload.text_rendered : rawText;
				const userSeq = item.seq ?? -1;
				// A LEGACY generated image is a `user` row carrying kind=="image" +
				// `image_asset_id` (the pre-narrative imagegen shape). Render it in the
				// canon lane exactly like the current narrative image row — never as a
				// user bubble — and advance the asset cursor past it so its asset does
				// not also ride this (or the next) user message's seq-window.
				if (
					payload.kind === 'image' &&
					typeof payload.image_asset_id === 'string' &&
					payload.image_asset_id
				) {
					prevUserSeq = userSeq;
					return buildCanonImageEntry(item, attachments);
				}
				// Assets uploaded in (prevUserSeq, userSeq) belong to this
				// user message (latest wins when several, v1).
				let image: string | undefined;
				let media: { kind: string; mime: string; url: string } | undefined;
				let latestAssetSeq = -1;
				for (const [assetSeq, asset] of attachments.bySeq) {
					if (asset.image && canonImageUrls.has(asset.image)) continue;
					if (assetSeq > prevUserSeq && assetSeq < userSeq && assetSeq > latestAssetSeq) {
						image = asset.image;
						media = asset.media;
						latestAssetSeq = assetSeq;
					}
				}
				prevUserSeq = userSeq;
				return {
					id: `history-${item.id}`,
					logId: item.id,
					role: 'user',
					prose: text,
					dsl: '',
					reasoning: '',
					streaming: false,
					...(image ? { image } : {}),
					...(media ? { media } : {}),
					kind: (payload.kind === 'ooc' ? 'ooc' : 'in_character') as TurnKind
				};
			}
			// A generated image is a canon row: a NARRATIVE row (actor "imagegen",
			// current shape) or a legacy `user` row (handled above). Payloads carry
			// `image_asset_id` (+ `prompt`). Render it in the canon lane as the image
			// with its prompt caption — never as an empty narrator bubble.
			if (typeof payload.image_asset_id === 'string' && payload.image_asset_id) {
				return buildCanonImageEntry(item, attachments);
			}
			const fields = narratorFieldsFromPayload(payload);
			const notice = noticesBySeq.get(item.seq ?? -1);
			return {
				id: `history-${item.id}`,
				// logId enables the revision door AND the #history-{event_id} jump
				// anchor (search hits + recap jumps); the segmented branch sets it,
				// the legacy branch previously dropped it.
				logId: item.id,
				role: 'narrator',
				...fields,
				reasoning: '',
				streaming: false,
				...(notice ? { notice } : {})
			};
		});
		// W9.26 §2.2/§2.5: fold the chain-walked system rows into the SAME ordered
		// stream, positioned by their own seqs (a compaction forks at the tip, so
		// chain seqs order the transcript): every context-low row, then per marker
		// the BREAK (at its seq) and BELOW it the NOTICE (half a step later, so the
		// sort can never put the notice above its break).
		const markers = projection.compactions ?? [];
		const systemRows: Array<{ seq: number; entry: TranscriptEntryView }> = [
			...(projection.context_lows ?? []).map((row) => ({
				seq: row.seq,
				entry: buildContextLowEntry(row, markers)
			})),
			...markers.map((marker) => ({
				seq: marker.seq,
				entry: buildCompactionBreakEntry(marker)
			})),
			...markers.map((marker) => ({
				seq: marker.seq + 0.5,
				entry: buildCompactionNoticeEntry(marker)
			}))
		];
		const ordered = [
			...entries.map((entry, i) => ({ seq: visibleRows[i].seq ?? 0, entry })),
			...systemRows
		].sort((a, b) => a.seq - b.seq);
		setTranscript(ordered.map((item) => item.entry));
		// Rehydrate the turn-progress trace from the projection: the header reads
		// the `turnSteps` store, which only the live stream otherwise fills — so a
		// reload, or a swipe/regenerate landing on a sibling branch (openSession ->
		// clearSessionView resets the store), showed "No steps recorded for this
		// turn yet" (the reported bug). Seed the newest narrative that carries a
		// persisted trace (an OOC turn records none, so the prior trace stands —
		// matching the live fold's persistence).
		let hydratedSteps: TurnStep[] = [];
		let newestStepSeq = -1;
		for (const item of projection.narratives) {
			const steps = coerceTurnSteps((item.payload ?? {}).steps);
			if (!steps.length) continue;
			const seq = typeof item.seq === 'number' ? item.seq : -1;
			if (seq >= newestStepSeq) {
				newestStepSeq = seq;
				hydratedSteps = steps;
			}
		}
		setTurnSteps(hydratedSteps);
	}

	/** Re-read the projection and rebuild the transcript. Used after an
	 *  out-of-band engine append the client didn't stream — e.g. a generated
	 *  image (a canon `imagegen` narrative). Best-effort: a failed refresh keeps
	 *  the current view; a reload still shows the row. */
	async function refreshTranscript(): Promise<void> {
		const id = get(sessionId);
		if (!id) return;
		try {
			hydrate(await getProjection(id), await hydrateAttachments(id));
		} catch {
			// projection refresh failed; the row landed — reload will show it
		}
	}

	async function send(
		content: string,
		kind: TurnKind,
		options: { replay?: boolean; followDepth?: number } = {}
	): Promise<void> {
		if (streaming) return;
		let id = get(sessionId);
		if (!id) {
			// Lazily create the session on the first turn of a fresh chat.
			connection.set('connecting');
			try {
				id = await createSession();
			} catch {
				failConnection('could not start a chat — the engine did not answer');
				return;
			}
			sessionId.set(id);
			persistUrl(id);
			connection.set('idle');
			void refreshSessions();
		}

		// Pending image: store it; the narrator sees it NATIVELY (VLM content
		// part via image_asset_id). The captioner event stays in the log as a
		// text record, but the model gets the image, not a description of it.
		let imageAssetId: string | undefined;
		let imagePreview: string | undefined;
		let liveMedia: { kind: string; mime: string; url: string } | undefined;
		if (pendingFile) {
			attachBusy = true;
			attachError = '';
			imagePreview = URL.createObjectURL(pendingFile); // live bubble preview
			if (pendingFile.type.startsWith('audio/') || pendingFile.type.startsWith('video/')) {
				liveMedia = {
					kind: pendingFile.type.startsWith('audio/') ? 'audio' : 'video',
					mime: pendingFile.type,
					url: imagePreview
				};
				imagePreview = undefined;
			}
			const result = await uploadAttachment(id, pendingFile);
			attachBusy = false;
			if ('error' in result) {
				attachError = result.error;
				pendingFile = null;
				if (imagePreview) URL.revokeObjectURL(imagePreview);
				if (liveMedia) URL.revokeObjectURL(liveMedia.url);
				imagePreview = undefined;
				liveMedia = undefined;
				return; // don't burn the turn on a failed upload
			}
			imageAssetId = result.asset_id;
			if (liveMedia?.kind === 'audio') imageAssetId = undefined; // audio is embedded-only
			pendingFile = null;
		}

		streaming = true;
		const userEntryId = addUserEntry(content, kind, imagePreview, liveMedia);
		const entryId = addNarratorEntry();
		let turnSession = id;
		controller = new AbortController();
		connection.set('streaming');

		let raw = '';
		// Once the critic has replaced the turn (D41), a late content delta must
		// not clobber the revision with the pre-critic raw text. In-graph order
		// already prevents that; the guard keeps it true.
		let replaced = false;
		// The narrative row the commit barrier named. The done-handler reconciles
		// THIS row (never "the newest") so the entry can never adopt another turn.
		let committedNarrSeq: number | null = null;
		beginTurnWindow();

		// W9.26 §2.2: the turn request may be REFUSED with a structured 409.
		//
		// A compaction job holds the claim ⇒ `{reason:"compacting"}` — wait and
		// retry every ~5 s for up to 2 min, then leave the state INFORMATIONAL (the
		// job finishes and the next interaction catches up) and hand the message
		// back to the composer (never a silent loss).
		//
		// The forced job completed first ⇒ the parent is `{reason:"superseded",
		// id}` — roll the optimistic entry back, follow the successor (transitively:
		// a successor can itself be superseded), then REPLAY the pending content
		// through this same send path.
		const replay = options.replay === true;
		let followDepth = options.followDepth ?? 0;
		const compactDeadline = Date.now() + COMPACT_WAIT_MS;

		/**
		 * One turn attempt against `turnSession`. Re-created per attempt so the
		 * handlers always close over the CURRENT target session and a fresh draft
		 * buffer (a refused attempt never streamed, but a retry must not append to
		 * the previous attempt's text).
		 */
		const runTurn = (): Promise<StreamOutcome> =>
			streamTurn(
				turnSession,
				content,
				kind,
				{
					signal: controller?.signal,
					onReasoning: (delta) => appendReasoning(entryId, delta),
					onContent: (delta, meta) => {
						if (replaced) return;
						// A `meta.replace` frame carries the FINAL text after an engine-side
						// rewrite (deterministic DSL gate / repair loop): the accumulated
						// draft is stale, so reset it and re-split the whole turn.
						if (meta?.replace) raw = '';
						raw += delta;
						const segmented = splitProseAndDsl(raw);
						updateEntry(entryId, {
							prose: segmented.prose,
							dsl: segmented.dsl,
							segments: segmented.segments
						});
					},
					onStage: handleStage,
					onRevision: (revision) => {
						if (applyRevisionFrame(revision, turnSession) !== null) replaced = true;
					},
					// W9.19 (A) / owner ruling: the barrier means the turn is KEPT, not
					// that the turn is DONE. The composer stays locked and the stage bar
					// stays live for the whole in-graph tail (critic, world_state, env_hud,
					// attribution); the server already queues a second send until done.
					// Capture the committed row for the done-handler reconcile.
					onCommitted: (meta) => {
						committedNarrSeq = barrierNarrSeq(meta);
					},
					onError: (error) => updateEntry(entryId, { error: error.message })
				},
				imageAssetId ? { imageAssetId } : {}
			);

		let outcome = await runTurn();

		// Refusals are retried/followed here, INSIDE the same send: the optimistic
		// rows stay put for a `compacting` wait (that is the honest view — the turn
		// is queued, not lost).
		while (outcome.status === 'error') {
			const reason =
				typeof outcome.error.meta?.reason === 'string' ? outcome.error.meta.reason : '';
			const successor =
				typeof outcome.error.meta?.id === 'string' ? outcome.error.meta.id : '';

			if (reason === 'compacting' && Date.now() < compactDeadline) {
				pendingStatus =
					'Compacting the history — your message will send when it finishes.';
				updateEntry(entryId, { error: undefined });
				connection.set('streaming');
				await new Promise((resolve) => setTimeout(resolve, COMPACT_RETRY_MS));
				if (controller?.signal.aborted) {
					// The reader stopped while waiting: nothing was sent.
					setProcessingStage(null);
					if (get(sessionId) === turnSession) removeEntry(entryId);
					streaming = false;
					controller = null;
					pendingStatus = '';
					connection.set('idle');
					return;
				}
				raw = '';
				replaced = false;
				outcome = await runTurn();
				continue;
			}

			if (reason === 'superseded' && successor && followDepth < SUPERSEDED_FOLLOW_LIMIT) {
				// The compaction finished while this turn was waiting: the row we sent
				// into is now read-only history. Roll the optimistic entry back (it
				// landed nowhere), refresh the list FIRST (openSession reads the era's
				// title from the store), follow the successor, then replay the pending
				// content through the normal send path. A successor that is itself
				// superseded hits this same branch again — transitive by construction.
				followDepth += 1;
				if (get(sessionId) === turnSession) {
					removeEntry(entryId);
					removeEntry(userEntryId);
				}
				setProcessingStage(null);
				streaming = false;
				controller = null;
				pendingStatus = '';
				connection.set('idle');
				await refreshSessions();
				await openSession(successor);
				await send(content, kind, { replay: true, followDepth });
				return;
			}

			if (reason === 'compacting') {
				// Past the 2-minute window: informational, not an error. The message is
				// handed back to the composer so nothing is silently lost.
				setProcessingStage(null);
				if (get(sessionId) === turnSession) {
					removeEntry(entryId);
					removeEntry(userEntryId);
				}
				fillComposer(content);
				pendingStatus =
					'Compaction is still running — your message was put back in the composer. Send it again once the history settles.';
				streaming = false;
				controller = null;
				connection.set('idle');
				return;
			}

			break;
		}
		setProcessingStage(null);
		pendingStatus = '';

		finishEntry(entryId);
		controller = null;
		streaming = false;

		// The user may have switched sessions mid-stream; only the still-open
		// session reflects its outcome.
		if (get(sessionId) !== turnSession) return;

		if (outcome.status === 'error') {
			failConnection(outcome.error.message);
			updateEntry(entryId, { error: outcome.error.message });
			// A REPLAYED message that fails again goes back to the composer: the
			// superseded-follow path already removed its optimistic rows, so losing
			// it here would be a silent message loss (§2.2).
			if (replay) fillComposer(content);
			return;
		}

		connection.set('idle');
		if (outcome.status === 'done') {
			// Settle the live entries against the COMMITTED log: state/facts, the
			// turn's inspector wiring, and the entry's own narrative row (selected
			// by the barrier's seq, with one stale-marked retry on a miss).
			await settleCommittedTurn(turnSession, entryId, userEntryId, committedNarrSeq);
			// The branch controls are a user-facing affordance and `refreshBranches`
			// carries its own failure handling + retry, so a failed state/meta read
			// above cannot swallow them.
			await refreshBranches(turnSession);
			// W9.26 §2.2: the context monitor is a post-turn HOOK — the stream closes
			// at `done`, so its `context_low` row can never ride a frame. ONE delayed
			// projection GET (~5 s) catches it without tail machinery.
			scheduleContextRefetch(turnSession);
		} else if (outcome.status === 'stopped' && committedNarrSeq !== null) {
			// Stop after the barrier: the turn is KEPT (the server reports a quiet
			// done), so the draft still has to settle against its committed row.
			// The abort means no done frame reaches this client.
			await settleCommittedTurn(turnSession, entryId, userEntryId, committedNarrSeq);
		}
	}

	function stop(): void {
		const id = get(sessionId);
		if (id) void stopTurn(id);
		controller?.abort();
	}

	/** W6.6/D2: the ⚙ button is a shortcut into the prompt manager's Rules view
	 * (the one directive control — the old settings drawer is retired). */
	function openRules(): void {
		promptView = 'rules';
		promptsOpen = true;
	}

	/** W9.2: a flow-graph node's prompt door opens the drawer straight on that
	 * step's block editor. */
	let promptStepId = $state<string | null>(null);
	function openPromptStep(stepId: string): void {
		promptStepId = stepId;
		promptView = 'steps';
		promptsOpen = true;
	}
</script>

<svelte:head>
	<title>Fabulist — chat</title>
</svelte:head>

<div class="chat" class:empty-chat={$transcript.length === 0}>
	<!-- D51: definition doors open the Design Studio on the matching tab; the rail
	     keeps scoping / assignment / the lore hint. Typed imports live in the
	     STUDIOS (issue #40) — the rail has no import surface. -->
	<SessionDrawer
		sessions={$sessions}
		currentId={$sessionId}
		onSelect={selectSession}
		onNew={newChat}
		onRename={(id, title) => void handleRename(id, title)}
		onDelete={(id) => void handleDelete(id)}
		onOpenStudio={(kind, opts) => openStudio(kind, opts)}
		onStartWorldChat={(id) => void handleStartWorldChat(id)}
		onStartPersonaChat={(id) => void handleStartPersonaChat(id)}
		refreshToken={railRefreshToken}
	/>

	{#if stateUndocked}
		<DraggablePane
			title="State"
			x={statePanePos.x}
			y={statePanePos.y}
			w={300}
			onclose={() => (stateUndocked = false)}
		>
			{#snippet children()}
				<div class="pane-body">
					<StateSidebar
						facts={$facts}
						connection={$connection}
						connectionError={$connectionError}
						onRetry={() => void recoverFromError()}
						sessionId={$sessionId}
					/>
				</div>
			{/snippet}
		</DraggablePane>
	{:else}
		<div class="side-dock">
			<button
				type="button"
				class="undock-btn"
				title="Undock into a floating pane (R13)"
				onclick={() => (stateUndocked = true)}
			>
				⧉
			</button>
			<StateSidebar
				facts={$facts}
				connection={$connection}
				connectionError={$connectionError}
				onRetry={() => void recoverFromError()}
				sessionId={$sessionId}
			/>
		</div>
	{/if}

	<main class="thread">
		<header class="thread-head">
			<div class="thread-title" class:has-identity={activeWorld || activeCast.length}>
				<!-- Issue #22: the session id is a TOOLTIP, not a row item. It is a
				     debug handle, and printing it beside the title cost a line of
				     header on every chat for information nobody reads at a glance. -->
				<h1 title={headTitle}>{$sessionTitle || 'New chat'}</h1>
				{#if USE_MOCK}<span class="mock-badge" title="VITE_MOCK=1 — scripted responses, no backend, no LLM">MOCK — no LLM</span>{/if}
				{#if activeWorld || activeCast.length}
					<!-- The identity art at the top of the chat is the reader's anchor to the
					     characters on stage, so it renders at 2.5rem (owner request: the
					     card image / initials were too small to read at 1.25rem). The
					     initials fallback inherits the same box via UserBadge's
					     `--badge-size`. -->
					<div class="identity-cluster" aria-label="Active identities">
						{#if activeWorld}
							<span class="identity-chip">
								<UserBadge
									name={activeWorld.name}
									id={activeWorld.id}
									imageAsset={activeWorld.image_asset}
									kind="world"
									size="2.5rem"
								/>
								<span class="identity-name">{activeWorld.name}</span>
							</span>
						{/if}
						{#each identityCast as cast (cast.name)}
							<UserBadge
								name={cast.name}
								id={cast.persona_id}
								imageAsset={cast.image_asset}
								kind="cast"
								size="2.5rem"
							/>
						{/each}
						{#if identityOverflow > 0}
							<!-- One line, always: the rest of the roster is a count, not a
							     second header row. -->
							<span
								class="identity-more"
								data-badge-overflow={identityOverflow}
								title={`also on stage: ${identityOverflowNames}`}
								aria-label={`${identityOverflow} more on stage: ${identityOverflowNames}`}
								>+{identityOverflow}</span
							>
						{/if}
					</div>
				{/if}
			</div>
			<nav class="head-actions" aria-label="Workspace panels">
				<div class="head-group" role="group" aria-labelledby="head-group-observe">
					<span class="head-group-label" id="head-group-observe">Observe</span>
					<button
						type="button"
						class="shell-btn"
						class:active={inspectorOpen}
						class:has-notice={$noticeBadge.count > 0}
						title={$noticeBadge.count > 0
							? `${$noticeBadge.count} notice${$noticeBadge.count === 1 ? '' : 's'} — degraded steps or proposals waiting (open the Inspector)`
							: 'Inspect the last turn — the context and model call behind it'}
						aria-label={$noticeBadge.count > 0
							? `Inspector — inspect the last turn (${$noticeBadge.count} notices)`
							: 'Inspector — inspect the last turn'}
						onclick={() => (inspectorOpen = !inspectorOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">ⓘ</span>
						{#if $noticeBadge.count > 0}
							<!-- Issue #34 / owner ruling: "a small prompting exclamation" — the
							     badge the eye lands on before it looks for a broken model. The
							     number is the count of unacknowledged degradations + pending
							     proposals; the Inspector's notices tab is where they're read. -->
							<span class="notice-badge" aria-hidden="true">{$noticeBadge.count > 9 ? '9+' : $noticeBadge.count}</span>
						{/if}
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={recapOpen}
						title="Compaction recaps — jump to the turns they cover"
						aria-label="Recap — compaction recaps"
						onclick={() => (recapOpen = !recapOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">☰</span>
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={memoryOpen}
						title="What was recalled this turn — the queries and the block the model used"
						aria-label="Memories — what was recalled this turn"
						onclick={() => (memoryOpen = !memoryOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">◈</span>
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={castOpen}
						title="Cast colours — who is who in this scene"
						aria-label="Cast — who is who in this scene"
						onclick={() => (castOpen = !castOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">☺</span>
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={proposalsOpen}
						title="Proposals — amendments waiting for your decision"
						aria-label="Proposals — amendments waiting for your decision"
						onclick={() => (proposalsOpen = !proposalsOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">⚖</span>
					</button>
				</div>
				<!-- D51: entity definitions live in this one surface; assembly stays in
				     the chat surfaces (rail scoping, cast pane, lore block). -->
				<div class="head-group" role="group" aria-labelledby="head-group-studio">
					<span class="head-group-label" id="head-group-studio">Studio</span>
					<button
						type="button"
						class="shell-btn"
						class:active={studioOpen}
						title="Design studio — define personas, worlds, books, assistants"
						aria-label="Design studio — define personas, worlds, books, assistants"
						onclick={() => (studioOpen = !studioOpen)}
					>
						<span class="shell-glyph" aria-hidden="true">◫</span>
					</button>
				</div>
				<div class="head-group" role="group" aria-labelledby="head-group-configure">
					<span class="head-group-label" id="head-group-configure">Configure</span>
					<button
						type="button"
						class="shell-btn"
						class:active={promptsOpen}
						title="The prompt — its steps, rules and variables"
						aria-label="Prompts — the prompt steps and rules"
						onclick={() => {
							promptView = 'steps';
							promptsOpen = true;
						}}
					>
						<span class="shell-glyph" aria-hidden="true">✎</span>
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={promptsOpen && promptView === 'rules'}
						title="Rules — turn rules on, off and reorder them"
						aria-label="Rules — the narrator rules"
						onclick={openRules}
					>
						<span class="shell-glyph" aria-hidden="true">⚙</span>
					</button>
					<button
						type="button"
						class="shell-btn"
						class:active={connectionsOpen}
						title="Define the models and endpoints each agent routes to"
						aria-label="Connections — models and endpoints per agent"
						onclick={() => (connectionsOpen = true)}
					>
						<span class="shell-glyph" aria-hidden="true">⇄</span>
					</button>
					<!-- Issue #12: appearance = the theme preset + ONE custom-CSS paste box.
					     A whole-app surface, not a chat panel (per-install user state). -->
					<button
						type="button"
						class="shell-btn"
						class:active={appearanceOpen}
						title="Appearance — theme preset and custom CSS"
						aria-label="Appearance — theme preset and custom CSS"
						onclick={() => (appearanceOpen = true)}
					>
						<span class="shell-glyph" aria-hidden="true">⊞</span>
					</button>
				</div>
				<!-- Owner ruling item 8: chat-scoped semantic search lives here, not
				     in the rail; it searches only the ACTIVE session. -->
				<div class="head-group" role="group" aria-labelledby="head-group-search">
					<span class="head-group-label" id="head-group-search">Search chat</span>
					<TopBarSearch sessionId={$sessionId} />
				</div>
			</nav>
			<!-- Issue #22: the env station MOVED INTO THE THREAD HEAD. It owns
			     time / date / location / weather / forecast outright, so it is the
			     head's own second band (CSS grid area `env`) instead of a separate
			     56px row between the head and the transcript. -->
			<ChatHud />
		</header>

		<!-- W3B: the pinned DSL header — scene title + engine step progress +
		     click summary. A normal layout row, so it never covers the assistants
		     bar (the slot-mounted StatusBar pill this replaced did). -->
		<!-- W9.26: DslHeader reads the LAST narrator entry's DSL for the pinned scene
		     title. A system row (context-low / break / notice) rides the transcript
		     list with role `narrator` but carries no DSL, so it is filtered here —
		     otherwise a notice landing after the newest reply would blank the title. -->
		<DslHeader
			entries={$transcript.filter((entry) => !(entry as TranscriptEntryView).system)}
			running={streaming}
			stage={$processingStage}
			steps={$turnSteps}
			onopenstep={openPromptStep}
		/>

		<TranscriptList
			entries={$transcript}
			persona={activePersona}
			branch={branchInfo}
			branchBusy={branchBusy}
			turnMeta={turnMeta}
			onSwipe={(delta) => void swipe(delta)}
			onRetry={() => void retry()}
			onRevise={reviseEntry}
			onDeleteFrom={deleteFrom}
			onCompact={() => void compactNow()}
			onNewChat={newChat}
			compactBusy={compacting}
			compactError={compactError}
			pendingStatus={pendingStatus}
		/>

		<AssistantPanel
			facts={$facts}
			{streaming}
			onOpenStudio={() => openStudio('assistant')}
			bind:pickerOpen={assistantPickerOpen}
			bind:enabled={assistantEnabled}
		/>

		<!-- Issue #22: the media attachment is an ACTION of the composer (chip
		     inline, row only while a file is pending), so the thread column no
		     longer carries a permanent 35px row for an empty control. The hidden
		     input stays here because the page owns the File: it prefixes the next
		     send. -->
		<input
			type="file"
			accept="image/*,audio/*,video/*"
			hidden
			bind:this={fileInput}
			onchange={(e) => onFileChosen(e)}
		/>

		<Composer
			{streaming}
			onSend={(content, kind) => void send(content, kind)}
			onImageGenerated={() => void refreshTranscript()}
			onStop={stop}
			pendingName={pendingFile?.name ?? null}
			{attachBusy}
			{attachError}
			onAttach={pickFile}
			onClearAttach={() => (pendingFile = null)}
			{assistantEnabled}
			{assistantPickerOpen}
			onToggleAssistant={() => (assistantPickerOpen = !assistantPickerOpen)}
		/>

	</main>

	<!-- D51: the Design Studio docks as a flex sibling of the transcript (its own
	     dock-left/right modes set the order + which edge the splitter rides);
	     floating mode is `position: fixed`, so its DOM slot is layout-free.
	     `onChanged` bumps the rail's catalog token: the rail is the page-owned
	     consumer of personas/worlds/books, so a definition saved in the studio has
	     to show up there without a reload. -->
	<DesignStudio
		open={studioOpen}
		onClose={() => (studioOpen = false)}
		onChanged={handleStudioChanged}
		panes={{
			flow: GraphHomePane,
			proposals: ProposalsHome,
			persona: PersonaPane,
			world: WorldPane,
			book: BookPane,
			assistant: AssistantPane,
			profile: ProfilePane,
			queries: DecisionQueriesPane,
			catalog: EntityCatalogPane,
			templates: NodeTemplatesPane,
			planner: PlannerPane,
			lanes: LanesPane
		}}
		focusId={studioFocusId}
		startNew={studioStartNew}
		openRequest={studioRequest}
		onopenstep={openPromptStep}
		sessionId={$sessionId}
	/>

</div>

{#if promptsOpen}
	<PromptDrawer
		sessionId={$sessionId}
		initialView={promptView}
		initialStepId={promptStepId}
		onClose={() => (promptsOpen = false)}
	/>
{/if}

{#if connectionsOpen}
	<ConnectionsDrawer onClose={() => (connectionsOpen = false)} />
{/if}

{#if appearanceOpen}
	<AppearanceDrawer onClose={() => (appearanceOpen = false)} />
{/if}

{#if inspectorOpen}
	<DraggablePane
		title="Inspector"
		x={typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 470) : 330}
		y={140}
		w={430}
		onclose={() => (inspectorOpen = false)}
	>
		{#snippet children()}
			<div class="pane-body">
				<InspectorPane
					sessionId={$sessionId}
					onclose={() => (inspectorOpen = false)}
					onopenproposals={() => {
						// Issue #34: the notices tab lists pending proposals as notices; the
						// action for one is the EXISTING review drawer (one mechanism).
						inspectorOpen = false;
						proposalsOpen = true;
					}}
				/>
			</div>
		{/snippet}
	</DraggablePane>
{/if}

{#if recapOpen}
	<DraggablePane
		title="Recap"
		x={typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 940) : 260}
		y={120}
		w={420}
		onclose={() => (recapOpen = false)}
	>
		{#snippet children()}
			<div class="pane-body">
				<RecapPane sessionId={$sessionId} onJump={onJumpSeq} />
			</div>
		{/snippet}
	</DraggablePane>
{/if}

{#if memoryOpen}
	<DraggablePane
		title="Memories (Used this turn)"
		x={typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 940) : 260}
		y={380}
		w={430}
		onclose={() => (memoryOpen = false)}
	>
		{#snippet children()}
			<div class="pane-body">
				<MemoryPane sessionId={$sessionId} />
			</div>
		{/snippet}
	</DraggablePane>
{/if}

{#if castOpen}
	<DraggablePane
		title="Cast"
		x={typeof window !== 'undefined' ? Math.max(60, window.innerWidth - 500) : 260}
		y={480}
		w={380}
		onclose={() => (castOpen = false)}
	>
		{#snippet children()}
			<div class="pane-body">
				<CastPane sessionId={$sessionId} />
			</div>
		{/snippet}
	</DraggablePane>
{/if}

<!-- W9.22 §4 / W9.23 §3.6.7: the session-scoped review surface. The drawer owns
     its own pane geometry (ProposalDrawer.svelte) so this mount is the whole page
     edit; its header carries the historian's run-now action and run/due line, and
     it links out to the archive pane below. -->
{#if proposalsOpen}
	<ProposalDrawer
		sessionId={$sessionId}
		onclose={() => (proposalsOpen = false)}
		onOpenArchive={() => (proposalsArchiveOpen = true)}
	/>
{/if}

<!-- W9.23 §3.2/§3.3: the aged-out partition, in its OWN pane — no checkboxes and
     no mass buttons render there, only single-row actions. -->
{#if proposalsArchiveOpen}
	<ProposalArchiveDrawer
		sessionId={$sessionId}
		onclose={() => (proposalsArchiveOpen = false)}
	/>
{/if}

<style>
	:global(body) {
		margin: 0;
		font-family: system-ui, -apple-system, 'Segoe UI', sans-serif;
	}
	.side-dock {
		position: relative;
		/* Issue #66: the docked state pane must scroll INSIDE the shell, never grow
		 * it. `.chat` is the 100vh row, so the dock is already the shell's height —
		 * but the pane inside it is a block whose auto height is its CONTENT, so
		 * `overflow-y: auto` had no box to scroll against: a long state tab pushed
		 * the pane past the shell and the DOCUMENT grew a second scrollbar beside
		 * the transcript's. A column here plus `min-height: 0` (the flex shrink
		 * floor) hands the pane a real height to scroll inside instead. */
		display: flex;
		flex-direction: column;
		min-height: 0;
	}
	.undock-btn {
		position: absolute;
		top: 0.4rem;
		right: 0.4rem;
		z-index: 5;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		color: var(--text-muted);
		border-radius: 6px;
		padding: 0.1rem 0.4rem;
		cursor: pointer;
		font-size: 0.85rem;
		opacity: 0;
		transition: opacity 120ms ease;
	}
	.side-dock:hover .undock-btn,
	.undock-btn:focus-visible {
		opacity: 1;
	}
	.pane-body {
		padding: 0.6rem;
		max-height: 70vh;
		overflow: auto;
	}
	.chat {
		display: flex;
		height: 100vh;
		min-height: 0;
		/* NOTE: no background here. `html, body` in app.css own the page paint;
		 * painting it again on `.chat` was the #13 double paint — a second opaque
		 * layer over the page, which made a wallpaper (painted behind both) unable
		 * to show through the prose column. tests/test_appearance_wallpaper.py
		 * pins this rule out so it cannot come back. */
		color: var(--text);
	}
	.thread {
		display: flex;
		flex-direction: column;
		min-width: 0;
		flex: 1;
		min-height: 0;
	}
	/* Issue #22: the head is a TWO-BAND GRID, not a wrapping flex row.
	 *
	 *   identity | actions      <- one line, always (the badge row is capped)
	 *   env ................   <- the HUD station, formerly its own 56px row
	 *
	 * A wrapping flex row made the header's height a function of the viewport and
	 * the cast length (103px below 1136px, one row above). The grid states the
	 * two bands once, so the header's height is its CONTENT and nothing else can
	 * push a third row into existence. */
	.thread-head {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		grid-template-areas:
			'identity actions'
			'env env';
		align-items: center;
		gap: 0.3rem 0.75rem;
		padding: 0.45rem 1rem 0.4rem;
		border-bottom: 1px solid var(--line);
	}
	/* The env band: one quiet line. The station keeps its own identity (clock,
	 * weather glyph, forecast chip, pin) and only its card geometry is compacted,
	 * so folding the HUD into the head costs ~28px instead of a second row. */
	.thread-head :global(.hud-bar) {
		grid-area: env;
	}
	.thread-head :global(.env-tracker) {
		min-height: 0;
		padding: 0.1rem 0.55rem;
		gap: 0.6rem;
	}
	.thread-head :global(.env-tracker .clock) {
		width: 1.5rem;
		height: 1.5rem;
	}
	.thread-title {
		grid-area: identity;
		display: flex;
		align-items: baseline;
		gap: 0.6rem;
		min-width: 0;
		/* No flex basis: the grid column owns the width now (this used to be a
		   wrapping row, where the title had to fight the nav for the line). */
	}
	/* Identity badges are part of the ONE title line; the cap (and the `+N`
	   count) is what keeps it from growing a second row. */
	.thread-title.has-identity {
		align-items: center;
	}
	h1 {
		margin: 0;
		min-width: 0;
		font-size: var(--font-h1);
		line-height: 1.2;
		letter-spacing: -0.01em;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	/* Identity cluster (world chip + cast badges): always-visible identity chrome
	   in the title row, independent of scene/turn state. ONE LINE, never wrapped
	   (issue #22): the badge cap plus the `+N` count keep it true at any width. */
	.identity-cluster {
		display: flex;
		align-items: center;
		flex-wrap: nowrap;
		gap: 0.3rem;
		min-width: 0;
		overflow: hidden;
	}
	/* The overflow count that replaced the wrapped second row. */
	.identity-more {
		flex: none;
		font-size: var(--font-micro);
		font-weight: 600;
		font-variant-numeric: tabular-nums;
		color: var(--text-muted);
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 0.05rem 0.4rem;
	}
	.identity-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}
	.identity-name {
		font-size: var(--font-micro);
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 16ch;
	}
	/* D40: the head-actions are grouped by plane — OBSERVE then CONFIGURE —
	   separated by whitespace, not divider elements: a wrapped line must never
	   end in an orphan divider. Every button is icon-only (its accessible name
	   rides `aria-label`). `.shell-btn` chrome lives in app.css so it stays
	   consistent across shell surfaces. */
	.head-actions {
		grid-area: actions;
		display: flex;
		align-items: center;
		gap: 0.75rem;
		flex: 0 1 auto;
		/* Right-anchored in the grid's `actions` column. */
		margin-left: auto;
		/* D51: with the studio docked the thread can be narrower than this row;
		   shrink + wrap the groups instead of letting them spill UNDER the studio
		   (the studio's own background swallows those clicks). */
		flex-wrap: wrap;
		justify-content: flex-end;
		row-gap: 0.35rem;
	}
	.head-group {
		display: flex;
		align-items: center;
		gap: 0.35rem;
	}
	/* Issue #34 (owner ruling): "a small prompting exclamation/highlight of the
	   Inspector button". A warning ring + count chip on the ONE existing door —
	   no new surface — because the detail lives one click away in the Inspector's
	   notices tab. The count is unacknowledged degradations + the pending proposal
	   queue, so the same prompt serves both producers. */
	.shell-btn.has-notice {
		position: relative;
		border-color: var(--warn);
		color: var(--warn);
	}
	.shell-btn.has-notice:hover {
		border-color: var(--warn);
		color: var(--warn);
	}
	.notice-badge {
		position: absolute;
		top: -0.45rem;
		right: -0.45rem;
		min-width: 1rem;
		height: 1rem;
		padding: 0 0.2rem;
		border-radius: 999px;
		background: var(--warn);
		color: #1c1917;
		font-size: 0.6rem;
		font-weight: 700;
		line-height: 1rem;
		text-align: center;
		box-shadow: 0 0 0 2px var(--bg);
	}
	.head-group-label {
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	:global(.shell-btn.active) {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.mock-badge {
		background: var(--danger);
		color: var(--on-accent);
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.04em;
		padding: 0.15rem 0.5rem;
		border-radius: 999px;
		margin-left: 0.5rem;
	}
</style>