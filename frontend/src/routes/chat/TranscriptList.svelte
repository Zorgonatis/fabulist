<script module lang="ts">
	import type { TranscriptEntry } from '#lib/stores';

	// Shared shapes for the Wave-4b player-HUD overlays. The parent computes these
	// from the inspector payload (rolls + validator flags) plus the revisions
	// read (edit reasons) and hands them down keyed by transcript entry id.

	/** A transcript entry plus the optional caption an image narrative carries
	 *  (`"Image Prompt: …"`). Hydration (`hydrate` in +page.svelte) sets it for a
	 *  generated image; the narrator lane renders it under the thumbnail. Plain
	 *  entries leave it undefined. */
	export interface TranscriptEntryView extends TranscriptEntry {
		caption?: string;
		/**
		 * The live entry could not be verified against the committed narrative row
		 * (a projection miss or read failure, even after the one retry). It still
		 * shows the narrator's draft, but says so — a streamed draft must never
		 * masquerade as the committed turn. Cleared when a reconcile lands.
		 */
		stale?: boolean;
		/**
		 * W9.26: a SYSTEM row — a context-low notice, a compaction break, or a
		 * compaction-done notice. It rides the SAME ordered entry list as turns
		 * because the transcript's order (old era rows … break … notice … new era
		 * rows) is the whole point; `role` stays `narrator` (a system row is not a
		 * speaker) and every lane checks `system` first.
		 */
		system?: TranscriptSystemRow;
	}

	/** W9.26 §2.2/§2.5: the three system row kinds. */
	export type SystemRowKind = 'context_low' | 'compaction_break' | 'compaction_notice';

	/** One `context_low` projection row, resolved for rendering. */
	export interface ContextLowView {
		/** Measured ratio of the usable prompt budget (`1.0` = over-budget sentinel). */
		ratio: number | null;
		promptTokens: number | null;
		budgetTokens: number | null;
		/** `measured` is the only basis the gauge acts on (⚑R1a). */
		basis: string;
		/** The declared window cannot pay for a reply — a declaration problem. */
		overBudget: boolean;
		/** The forced compaction ran and cannot free more window this era. */
		noop: boolean;
		/**
		 * W9.26 §2.2: SETTLED iff a compaction marker exists LATER in the same chain
		 * (seq compare) — the row is inherited history, its button is spent.
		 */
		settled: boolean;
	}

	/** One chain-walked compaction marker, resolved for the break/notice rows. */
	export interface CompactionBreakView {
		/** Seq of the marker row itself (orders this break in the transcript). */
		seq: number;
		/** Session that OWNS the marker (the successor era). */
		sessionId: string;
		/** Session the compaction ran in (the parent era), when recorded. */
		sourceSession: string | null;
		/** Inclusive covered seq span (NEVER turn indices). */
		low: number | null;
		high: number | null;
		trigger: string;
		/** `[session_id, seq]` of the `summary` v2 row the era forked from. */
		summaryRef: [string, number] | null;
	}

	/** The discriminator payload a system row carries. */
	export interface TranscriptSystemRow {
		kind: SystemRowKind;
		contextLow?: ContextLowView;
		compaction?: CompactionBreakView;
	}

	/** One persisted roll (graph/rolls.py `as_payload`): dice + result + split. */
	export interface RollChip {
		dice: string;
		result: number;
		/** Human face breakdown, e.g. `2d6[3,4]+1 = 8` (S4e). */
		detail: string | null;
		/** The roll's short purpose from trailing prose, or null. */
		ask: string | null;
	}

	/** One Dress-Rehearsal validator flag (graph/validator.py `ValidatorFlag`). */
	export interface ValidatorFlag {
		rule_id?: string;
		check: string;
		severity: string;
		evidence: string;
		source?: string;
		/** Narrator segment the flag anchors to (IDE marker), or null. */
		segment_index: number | null;
		message: string;
	}

	/** Player-HUD overlays attached to one transcript entry. */
	export interface TurnMeta {
		rolls: RollChip[];
		flags: ValidatorFlag[];
		/** Plain-language reason when this entry was edited (revision). */
		editReason: string | null;
	}
</script>

<script lang="ts">
	// Transcript list: user turns right/plain, narrator prose left. Reasoning is
	// a collapsed <details> that fills in live while the narrator streams.
	//
	// W2.2: when the current session has sibling replies for its latest turn, the
	// last narrator message carries a small branch chip and swipe controls. The
	// parent owns the sibling data and the API calls; this component is
	// presentational. Legacy sessions (no branches) render exactly as before.
	//
	// Wave 4b: the parent also supplies `turnMeta` (rolls / validator flags / edit
	// reasons). Rolls render as inline dice chips (replacing the `[roll …]` token
	// when present); validator flags render as a turn badge plus an IDE-style
	// gutter marker on the anchored prose segment.
	import { get } from 'svelte/store';
	import type { NarratorSegment } from '#lib/stores';
	import { sessionId as currentSessionId, transcript } from '#lib/stores';
	import { speakerColors, type RegistryRow } from '#lib/speakerColorStore';
	import {
		getAttributions,
		getOpening,
		getProjection,
		getRecap,
		getSpeakerColors,
		selectGreeting
	} from '#lib/api';
	import type {
		AttributedQuote,
		BranchInfo,
		OpeningInfo,
		Projection,
		RecapEntry,
		ValidationNoticeFlag
	} from '#lib/api';
	import { renderMarkdown } from '#lib/markdown';
	import BranchControls from './BranchControls.svelte';
	import CardDetails from './CardDetails.svelte';
	import DslFallback from './DslFallback.svelte';
	import ImageViewer from './ImageViewer.svelte';
	import UserBadge from './UserBadge.svelte';
	import { isSceneStatement } from '#lib/openui/library';

	let {
		entries,
		branch = null,
		branchBusy = false,
		onSwipe,
		onRetry,
		onRevise,
		onDeleteFrom,
		turnMeta = {},
		persona = null,
		onCompact,
		onNewChat,
		compactBusy = false,
		compactError = '',
		pendingStatus = ''
	}: {
		entries: TranscriptEntryView[];
		branch?: BranchInfo | null;
		branchBusy?: boolean;
		onSwipe?: (delta: 1 | -1) => void;
		onRetry?: () => void;
		onRevise?: (logId: number, newText: string) => Promise<boolean> | boolean;
		/** Issue #35: delete this message and every message after it. The page owns
		 *  the call (the engine forks below the pivot and the page switches to the
		 *  new branch); resolves `true` once the switch landed. */
		onDeleteFrom?: (logId: number) => Promise<boolean>;
		turnMeta?: Record<string, TurnMeta>;
		/** The ACTIVE chat persona; its badge renders left of each user row. The
		 *  page supplies `persona_id` (image route key) + `image_asset`; absent it,
		 *  user rows render exactly as before. */
		persona?: { name: string; persona_id?: string | null; image_asset?: string | null } | null;
		/** W9.26: the context-low row's [Compact] door (the page's 202-poll flow). */
		onCompact?: () => void;
		/** W9.26: the clean, session-less new-chat state. */
		onNewChat?: () => void;
		/** A compaction job is running — the button reads "Compacting…" and blocks. */
		compactBusy?: boolean;
		/** The last job's failure/noop reason, rendered inline under the row. */
		compactError?: string;
		/** Page-owned informational status ("Compacting…" while a turn waits on the
		 *  forced path). NEVER an error — the plan calls for informational copy. */
		pendingStatus?: string;
	} = $props();

	//: The lightbox target (`src`) plus its alt text; null = closed.
	let lightboxSrc = $state<string | null>(null);
	let lightboxAlt = $state('');

	type RollToken =
		| { kind: 'text'; text: string }
		| { kind: 'roll'; roll: RollChip };

	//: A bracketed span that leads with a dice expression (`[2d6+1]`, `[roll 2d6]`).
	const ROLL_TOKEN_RE = /\[[^\]]*\]/g;
	const ROLL_EXPR_RE = /^\[\s*(?:\/?roll\s+)?\d+\s*[dD]\s*\d+/i;

	function isRollToken(token: string): boolean {
		return ROLL_EXPR_RE.test(token);
	}

	/**
	 * Markdown images in narrator prose are plain `<img>` after sanitized
	 * `{@html renderMarkdown(…)}`, so ONE delegated handler per prose div opens
	 * the lightbox for any image inside it. Only prose divs carry this handler:
	 * avatars (UserBadge) are not rendered inside them.
	 */
	function openProseImage(event: MouseEvent): void {
		const image = (event.target as HTMLElement | null)?.closest('img');
		const src = image?.getAttribute('src');
		if (!src) return;
		lightboxAlt = image?.getAttribute('alt') ?? '';
		lightboxSrc = src;
	}

	/**
	 * The per-flag line of a validation notice (D-F): what tripped the check.
	 * `message` is the validator's prose; DSL-gate rows always carry it too, and
	 * `check`/`rule_id` are the fallback ids so a sparse row still says something.
	 */
	function noticeText(flag: ValidationNoticeFlag): string {
		return flag.message || flag.check || flag.rule_id || 'finding';
	}

	/**
	 * Split a user message into text runs and dice chips, replacing each
	 * `[roll …]` token with the matching persisted roll (in appearance order).
	 * Rolls with no inline token (an OOC `/roll` command) are appended after the
	 * prose so they are never lost.
	 */
	function rollParts(prose: string, meta: TurnMeta | undefined): RollToken[] {
		const rolls = meta?.rolls ?? [];
		if (rolls.length === 0) return [{ kind: 'text', text: prose }];
		const parts: RollToken[] = [];
		let used = 0;
		let last = 0;
		let match: RegExpExecArray | null;
		ROLL_TOKEN_RE.lastIndex = 0;
		while ((match = ROLL_TOKEN_RE.exec(prose))) {
			if (!isRollToken(match[0]) || used >= rolls.length) continue;
			if (match.index > last) parts.push({ kind: 'text', text: prose.slice(last, match.index) });
			parts.push({ kind: 'roll', roll: rolls[used] });
			last = match.index + match[0].length;
			used += 1;
		}
		if (last < prose.length) parts.push({ kind: 'text', text: prose.slice(last) });
		for (let i = used; i < rolls.length; i += 1) parts.push({ kind: 'roll', roll: rolls[i] });
		return parts;
	}

	function flagCount(meta: TurnMeta | undefined): number {
		return meta?.flags.length ?? 0;
	}

	/** Plain-language hover for the validator badge ("what changed and why"). */
	function flagExplain(flag: ValidatorFlag): string {
		const severity = flag.severity === 'warn' ? 'Warning' : flag.severity || 'Note';
		const evidence = flag.evidence ? ` — matched ${flag.evidence}` : '';
		const message = flag.message || flag.check;
		return `${severity}: ${message}${evidence}`;
	}

	function flagSummary(meta: TurnMeta | undefined): string {
		if (!meta || meta.flags.length === 0) return '';
		return meta.flags.map(flagExplain).join('\n');
	}

	/** Flags anchored to one narrator segment index (for the inline marker). */
	function flagsForSegment(meta: TurnMeta | undefined, index: number): ValidatorFlag[] {
		if (!meta) return [];
		return meta.flags.filter((flag) => flag.segment_index === index);
	}

	function segmentTitle(flags: ValidatorFlag[]): string | undefined {
		if (flags.length === 0) return undefined;
		return flags.map(flagExplain).join('\n');
	}

	function editExplain(reason: string | null): string {
		return `Edited (${reason || 'user edit'}) — the original row is kept in the log; this text is the latest revision.`;
	}

	/** True when this narrator turn has any flag (badge-only when unanchored). */
	function hasFlags(meta: TurnMeta | undefined): boolean {
		return flagCount(meta) > 0;
	}

	/** Flags with no segment anchor (badge-only list). */
	function unanchoredFlags(meta: TurnMeta | undefined): ValidatorFlag[] {
		if (!meta) return [];
		return meta.flags.filter((flag) => flag.segment_index === null || flag.segment_index === undefined);
	}

	// Message-edit door (SURFACE #6): revisions are appends (D10) — the page
	// POSTs the revision and refreshes the projection (latest-wins).
	let editingId: string | null = $state(null);
	let draft = $state('');
	let reviseBusy = $state(false);

	// The stale-reply offer (Wave-1 Bug 2b): editing your own latest message
	// makes the reply that already answered it stale, so offer a one-click
	// regeneration (the page's existing retry path) right on that row. Shown only
	// while the edited row is still the latest user message and no new reply has
	// landed (the narrator count has not moved past the baseline); clicking or
	// dismissing clears it, and the next send retargets the latest user row.
	let regenOfferId = $state<string | null>(null);
	let regenBaseline = $state(0);
	// W9.26: a SYSTEM row (context-low / break / notice) rides the same list with
	// role `narrator` but is not a spoken reply — it must never take the branch
	// chip/controls or count as a turn.
	const narratorCount = $derived(
		entries.filter((entry) => entry.role === 'narrator' && !entry.system).length
	);
	const latestUserId = $derived(
		entries.reduce<string | null>((acc, entry) => (entry.role === 'user' ? entry.id : acc), null)
	);
	const regenOnEntryId = $derived(
		regenOfferId !== null &&
			regenOfferId === latestUserId &&
			narratorCount === regenBaseline &&
			!entries.some((entry) => entry.streaming)
			? regenOfferId
			: null
	);

	/**
	 * The text the ✎ editor seeds from — what the row actually RENDERS.
	 *
	 * A segmented narrator row renders `segments` and its flat `prose` is ''
	 * (`narratorFieldsFromPayload`: the segments are the prose channel), so seeding
	 * the editor from `prose` opened an EMPTY box on every modern narrator row.
	 * Join the prose segments — one block, which is also how the revision rewrites
	 * them (`api/revisions.py:_revise_segments`) — and fall back to `prose` for a
	 * legacy row with no segments.
	 */
	function editableText(entry: TranscriptEntry): string {
		const prose = (entry.segments ?? [])
			.filter((segment) => segment.kind === 'prose')
			.map((segment) => segment.text)
			.join('\n\n');
		return prose || entry.prose;
	}

	function startEdit(id: string, text: string): void {
		editingId = id;
		draft = text;
	}

	async function saveEdit(entry: TranscriptEntry): Promise<void> {
		if (!onRevise || reviseBusy || entry.logId === undefined) return;
		reviseBusy = true;
		const ok = await onRevise(entry.logId, draft);
		reviseBusy = false;
		if (!ok) return;
		editingId = null;
		if (entry.role === 'user') {
			// Wave-1 Bug 2b: editing your own latest message makes the reply that
			// already answered it stale, so offer a one-click regeneration (the
			// page's existing retry path) right on the row. The revision rehydrates
			// the transcript (fresh `history-<logId>` entries), so re-resolve the
			// row by its log id instead of trusting the pre-edit entry id.
			const revised = entries.find((candidate) => candidate.logId === entry.logId);
			if (revised) {
				regenOfferId = revised.id;
				regenBaseline = narratorCount;
			}
		}
	}

	function dismissRegen(): void {
		regenOfferId = null;
	}

	function regenerate(): void {
		regenOfferId = null;
		onRetry?.();
	}

	// The reply the swipe controls apply to is always the latest narrative
	// (v1 scope: last reply only).
	const lastNarratorIndex = $derived(
		entries.reduce((acc, entry, i) => (entry.role === 'narrator' && !entry.system ? i : acc), -1)
	);
	const showBranch = $derived(branch !== null);
	const hasAlternates = $derived(branch !== null && branch.count > 1);

	// --- W9.26 §2.5: the era boundary ------------------------------------------
	//
	// The transcript is `old era rows … break … notice … new era rows`, one break
	// per chain-walked compaction marker (ordered by seq). A break's collapse
	// chevron folds ONLY the span ABOVE it — the rows between the previous break
	// (exclusive) and this one (exclusive) — so with several eras each boundary
	// folds its own stretch and nothing else. Old eras start EXPANDED.
	let collapsedBreaks = $state<Record<string, boolean>>({});

	const foldedIndexes = $derived.by(() => {
		const folded = new Set<number>();
		const list = entries;
		let spanStart = 0;
		for (let i = 0; i < list.length; i += 1) {
			if (list[i].system?.kind !== 'compaction_break') continue;
			if (collapsedBreaks[list[i].id]) {
				for (let j = spanStart; j < i; j += 1) folded.add(j);
			}
			spanStart = i + 1;
		}
		return folded;
	});

	function isFolded(index: number): boolean {
		return foldedIndexes.has(index);
	}

	function toggleBreak(entryId: string, collapsed: boolean): void {
		collapsedBreaks = { ...collapsedBreaks, [entryId]: !collapsed };
	}

	/** The covered SEQ span of one break — never turn indices (§2.5). */
	function breakSpan(view: CompactionBreakView | undefined): string {
		if (!view || (view.low === null && view.high === null)) return 'an unknown stretch of turns';
		if (view.low === null) return `turns ?–${view.high}`;
		if (view.high === null || view.high === view.low) return `turn ${view.low}`;
		return `turns ${view.low}–${view.high}`;
	}

	/** Quiet gauge line on a context-low row (absent numbers render nothing). */
	function ratioLabel(view: ContextLowView): string {
		const parts: string[] = [];
		if (view.ratio !== null) parts.push(`${Math.round(view.ratio * 100)}% of the budget`);
		if (view.promptTokens !== null && view.budgetTokens !== null) {
			parts.push(`${view.promptTokens.toLocaleString()} / ${view.budgetTokens.toLocaleString()} tokens`);
		}
		if (view.basis) parts.push(view.basis);
		return parts.join(' · ');
	}

	// --- W9.26 §2.5: the compaction-done notice's raw data ----------------------
	//
	// The break row is deliberately text-free beyond its label: the summary text is
	// NOT duplicated into the marker (§2.3 step 9). The notice therefore FETCHES
	// `/recap` the first time it is expanded and renders the raw summary v2 row:
	// summary text, covered span, trigger, model, calls, measured tokens.
	type RecapLookup =
		| { state: 'loading' }
		| { state: 'error'; message: string }
		| { state: 'ready'; entry: RecapEntry | null };

	let recapLookup = $state<Record<string, RecapLookup>>({});

	function recapKey(view: CompactionBreakView | undefined): string {
		if (!view) return '';
		return view.summaryRef ? `${view.summaryRef[0]}:${view.summaryRef[1]}` : `break:${view.sessionId}:${view.seq}`;
	}

	async function onNoticeToggle(view: CompactionBreakView | undefined, open: boolean): Promise<void> {
		if (!open || !view) return;
		const key = recapKey(view);
		if (recapLookup[key]) return; // already loading / loaded / failed
		const id = get(currentSessionId);
		if (!id) return;
		recapLookup = { ...recapLookup, [key]: { state: 'loading' } };
		const result = await getRecap(id);
		if ('error' in result) {
			recapLookup = { ...recapLookup, [key]: { state: 'error', message: result.error } };
			return;
		}
		// The marker names the summary it forked from; fall back to the newest row
		// (a legacy engine that predates `summary_ref`).
		const targetSeq = view.summaryRef?.[1];
		const match =
			(targetSeq !== undefined ? result.recap.find((row) => row.seq === targetSeq) : undefined) ??
			result.recap[result.recap.length - 1] ??
			null;
		recapLookup = { ...recapLookup, [key]: { state: 'ready', entry: match } };
	}

	/** The failed read's message ('' when there is none) — a helper because a
	 *  second index expression would lose the union narrowing. */
	function recapError(view: CompactionBreakView | undefined): string {
		const found = recapLookup[recapKey(view)];
		return found?.state === 'error' ? found.message : '';
	}

	function recapEntryFor(view: CompactionBreakView | undefined): RecapEntry | null {
		const found = recapLookup[recapKey(view)];
		return found?.state === 'ready' ? found.entry : null;
	}

	// --- Wave 6 W6C-2: per-turn speaker-colour dialogue -------------------------
	// Attribution lives on the `attribution` agent_call (raw JSON) and the colour
	// registry on GET .../speaker_colors. We map an attribution row to the
	// narrative seq it follows, then tint the matched quote spans at render — the
	// prose never changes. Best-effort: any missing read leaves the prose plain.
	let quoteColors = $state<Record<string, string>>({});
	let attrByNarrSeq = $state<Map<number, AttributedQuote[]>>(new Map());
	let narrSeqByLogId = $state<Map<number, number>>(new Map());
	// Identity of the session + transcript shape we last loaded for. The effect
	// re-runs on every streaming token (the entries array is replaced), so the
	// signature early-returns until the shape changes (hydration / turn end).
	let loadedSig = '';
	// Colour-registry counter we last loaded for: CastPane bumps it on a
	// confirmed PATCH, so a pure recolour re-fetches without a shape change.
	let loadedColorVersion = 0;

	$effect(() => {
		const id = get(currentSessionId);
		// Read the store value directly (NOT `get(speakerColors)`) — `get()` inside
		// an `$effect` does not track.
		const storeState = $speakerColors;
		const colorVersion = storeState.version;
		const shape = entries.map((entry) => `${entry.id}:${entry.streaming ? 1 : 0}`).join(',');
		const key = `${id ?? ''}|${shape}`;
		if (!id) {
			if (loadedSig !== '') {
				loadedSig = '';
				quoteColors = {};
				attrByNarrSeq = new Map();
				narrSeqByLogId = new Map();
			}
			return;
		}
		if (key === loadedSig && colorVersion === loadedColorVersion) return;
		loadedSig = key;
		loadedColorVersion = colorVersion;
		let cancelled = false;
		// Only the rows published for this session (the store outlives a switch).
		void loadQuoteColorData(
			id,
			storeState.sessionId === id ? storeState.colors : [],
			() => cancelled
		);
		return () => {
			cancelled = true;
		};
	});

	// --- Wave B (D30): the pinned Opening block ---------------------------------
	// The greeting (first_mes + alternate_greetings) used to be invisible in the
	// transcript. It now renders read-only at the top with swipe arrows; each
	// swipe appends a latest-wins `greeting_selected` event so the selection
	// persists across reloads (and drives the persona block the narrator sees).
	//
	// Wave-1 Bug 1: a world chat's opening is NOT a card greeting — it is the
	// seeded scenario narrative (payload `opening: true`, fabulist.worlds.
	// registry). That variant carries `openingLogId`, the `narrative` event the ✎
	// edit door revises; a card greeting stays read-only (card events are not
	// editable through the revision path, fabulist.api.revisions).
	type OpeningView = OpeningInfo & { openingLogId?: number };
	let opening = $state<OpeningView | null>(null);
	let openingIndex = $state(0);
	let openingBusy = $state(false);
	let openingEditing = $state(false);
	let openingDraft = $state('');
	let openingRevising = $state(false);
	let detailsOpen = $state(false);
	let openingSig = '';

	// NOTE: read the store through `$currentSessionId` (not `get(...)`) so the
	// effect re-runs when the session changes. `get()` snapshots the value and
	// does NOT register a dependency in a Svelte-5 `$effect`.
	$effect(() => {
		const id = $currentSessionId;
		if (!id) {
			opening = null;
			openingIndex = 0;
			detailsOpen = false;
			openingSig = '';
			return;
		}
		if (id === openingSig) return;
		openingSig = id;
		let cancelled = false;
		void loadOpening(id, () => cancelled);
		return () => {
			cancelled = true;
		};
	});

	async function loadOpening(id: string, isCancelled: () => boolean): Promise<void> {
		try {
			// The world-scenario opening lives on the projection (the same read the
			// transcript uses); the card-opening read stays the D30 source for
			// persona/card chats.
			const [info, projection] = await Promise.all([
				getOpening(id),
				getProjection(id).catch(() => null)
			]);
			if (isCancelled()) return;
			const scene = projection ? openingNarrative(projection) : null;
			if (scene) {
				opening = {
					greetings: [scene.prose],
					index: 0,
					card: null,
					cardAsset: null,
					openingLogId: scene.id
				};
				openingIndex = 0;
				return;
			}
			opening = info;
			openingIndex = info.index;
		} catch {
			if (!isCancelled()) opening = null;
		}
	}

	/** The seeded world-chat opening narrative (payload `opening: true`), if any.
	 *
	 * The projection resolves revisions latest-wins, so `prose` is always the
	 * currently displayed opening; `id` is the event the ✎ door revises. A
	 * marker row with empty prose is ignored (nothing to pin).
	 */
	function openingNarrative(projection: Projection): { id: number; prose: string } | null {
		for (const item of projection.narratives) {
			const payload = item.payload ?? {};
			if (payload.opening !== true) continue;
			const prose = typeof payload.prose === 'string' ? payload.prose : '';
			if (!prose.trim()) continue;
			return { id: item.id, prose };
		}
		return null;
	}

	function startOpeningEdit(): void {
		if (!opening || opening.openingLogId === undefined) return;
		openingEditing = true;
		openingDraft = opening.greetings[0] ?? '';
	}

	/** Append a revision to the opening narrative, then re-read it (latest-wins).
	 * The original row stays in the log (D10); `onRevise` is the page's normal
	 * message-edit path, so nothing about the opening is special. */
	async function saveOpeningEdit(): Promise<void> {
		const logId = opening?.openingLogId;
		if (!onRevise || openingRevising || logId === undefined) return;
		openingRevising = true;
		const ok = await onRevise(logId, openingDraft);
		openingRevising = false;
		if (!ok) return;
		openingEditing = false;
		const id = get(currentSessionId);
		if (id) await loadOpening(id, () => false);
	}

	async function swipeOpening(delta: 1 | -1): Promise<void> {
		const id = get(currentSessionId);
		if (!id || !opening || openingBusy || opening.greetings.length <= 1) return;
		const total = opening.greetings.length;
		const next = (openingIndex + delta + total) % total;
		openingBusy = true;
		try {
			const confirmed = await selectGreeting(id, next);
			openingIndex = confirmed;
			opening = { ...opening, index: confirmed };
		} catch {
			// Keep the previous selection if the append failed.
		} finally {
			openingBusy = false;
		}
	}

	// --- Issue #35: delete this message and everything after it -----------------
	//
	// Deleting is a FORK, not a log rewrite: the page asks the engine for a branch
	// cut just below the pivot and switches to it, so the old line stays in the
	// append-only log, one ‹ away in the same branches switcher. This component
	// only says WHICH row was picked and how many messages leave with it.

	/** The pinned opening block renders (same condition as the markup below). */
	const showOpening = $derived(
		opening !== null && opening.greetings.some((greeting) => greeting.trim())
	);

	/**
	 * The chain's FIRST message row — the one row that can never be a pivot (the
	 * engine answers `409 first_message`). `null` once the opening block renders:
	 * the opening IS message #1, so the first transcript entry is deletable after
	 * all (it is the reply to the opening, not the opening).
	 */
	const firstMessageId = $derived.by(() => {
		if (showOpening) return null;
		return entries.find((entry) => !entry.system && entry.logId !== undefined)?.id ?? null;
	});

	/** A delete request is in flight (it forks + rehydrates): second click is a no-op. */
	let deleting = $state(false);

	/** Message rows from `index` to the end — the confirm's N (pivot inclusive;
	 *  system rows are notes ABOUT the chat, never messages that can leave). */
	function messagesFrom(index: number): number {
		return entries.slice(index).filter((entry) => !entry.system).length;
	}

	/** Ask, then hand the pivot's log id to the page. Cancel is a no-op. */
	async function requestDeleteFrom(entry: TranscriptEntryView, index: number): Promise<void> {
		if (!onDeleteFrom || deleting || entry.logId === undefined || entry.streaming) return;
		const count = messagesFrom(index);
		if (count < 1) return;
		if (!window.confirm(`Delete ${count} message${count === 1 ? '' : 's'}?`)) return;
		deleting = true;
		try {
			await onDeleteFrom(entry.logId);
		} finally {
			deleting = false;
		}
	}

	async function loadQuoteColorData(
		id: string,
		storeColors: RegistryRow[],
		isCancelled: () => boolean
	): Promise<void> {
		const [colors, attrs, projection] = await Promise.all([
			getSpeakerColors(id),
			getAttributions(id),
			getProjection(id).catch(() => null)
		]);
		if (isCancelled()) return;

		// Colours keyed by the canonical `member_id` where one is known (so a
		// rename keeps the hue the quote already has), with a name-keyed fallback
		// for rows written before W7 or by a legacy engine with no member ids. The
		// registry read is the name-keyed source (it works before the cast pane is
		// ever opened); the cast pane's publish carries the member ids.
		const colorMap: Record<string, string> = {};
		const failed = 'error' in colors;
		if (!failed) {
			for (const row of colors.speaker_colors) colorMap[row.name] = row.color;
		}
		for (const row of storeColors) {
			colorMap[row.name] = row.color;
			if (row.member_id) colorMap[row.member_id] = row.color;
		}
		if (!failed || storeColors.length > 0) quoteColors = colorMap;

		const seqByLogId = new Map<number, number>();
		const narrSeqs: number[] = [];
		if (projection) {
			for (const item of projection.narratives) {
				if (typeof item.id === 'number' && typeof item.seq === 'number') {
					seqByLogId.set(item.id, item.seq);
					narrSeqs.push(item.seq);
				}
			}
		}
		narrSeqByLogId = seqByLogId;
		narrSeqs.sort((a, b) => a - b);

		const map = new Map<number, AttributedQuote[]>();
		for (const row of attrs) {
			if (row.attributions.length === 0) continue;
			// The attribution call runs post-turn: it belongs to the largest
			// narrative seq smaller than the call's seq.
			let best: number | null = null;
			for (const seq of narrSeqs) {
				if (seq < row.seq) best = seq;
				else break;
			}
			if (best === null) continue;
			map.set(best, [...(map.get(best) ?? []), ...row.attributions]);
		}
		attrByNarrSeq = map;
	}

	function attrsForEntry(entry: TranscriptEntry): AttributedQuote[] {
		if (entry.role !== 'narrator' || entry.logId === undefined) return [];
		const seq = narrSeqByLogId.get(entry.logId);
		if (seq === undefined) return [];
		return attrByNarrSeq.get(seq) ?? [];
	}

	function normalizeQuote(text: string): string {
		return text
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, ' ')
			.trim();
	}

	// --- Colour markup (W9.28 W5) ---------------------------------------------
	//
	// The narrator writes each spoken line inside the speaking character's palette
	// colour (`<span style="color:#e06c75">"You're late."</span>`) and the ENGINE
	// parses that markup to attribute speakers. The frontend's quote scan must
	// mask the colour tags exactly as the engine does (`state/attribution.py::
	// _mask_tags`): `style="color:#e06c75"` carries two quote marks, and scanned
	// raw they are a phantom quote span per wrapped line — the phantom is what
	// made `colorMarkdownSource` splice a `<span class="quote">` INTO the
	// attribute, corrupting the render (R2 P0-1).

	/** One `<span>`/`<font>` tag: offsets, opening vs closing, and its attribute
	 * text. The colour contract's whole markup vocabulary — the engine's `_TAG_RE`
	 * matches the same set, so the two scans stay in lockstep. */
	interface ColorTag {
		start: number;
		end: number;
		close: boolean;
		tag: string;
		attrs: string;
	}

	function colorTags(text: string): ColorTag[] {
		const out: ColorTag[] = [];
		const re = /<\s*(\/?)\s*(span|font)\b([^<>]*)>/gi;
		let match: RegExpExecArray | null;
		while ((match = re.exec(text))) {
			out.push({
				start: match.index,
				end: match.index + match[0].length,
				close: match[1] === '/',
				tag: match[2].toLowerCase(),
				attrs: match[3]
			});
		}
		return out;
	}

	/** Blank every colour tag to a SAME-LENGTH run of blanks. Offsets survive (one
	 * blank per character) so a span found in the mask still indexes the prose, and
	 * newlines are kept so the paragraph boundaries the engine's colour context is
	 * bounded by survive too. */
	function maskColorTags(text: string): string {
		if (!text.includes('<')) return text;
		const chars = text.split('');
		for (const tag of colorTags(text)) {
			for (let i = tag.start; i < tag.end; i += 1) {
				if (chars[i] !== '\n') chars[i] = ' ';
			}
		}
		return chars.join('');
	}

	/** Whether a tag's attributes name a colour — a `style` carrying a `color`
	 * declaration, or a bare `color` attribute. A tag without one (`class`, `id`)
	 * is not colour markup, so a quote inside it still gets the engine's wrapper. */
	function declaresColor(attrs: string): boolean {
		return (
			/\bstyle\s*=\s*(?:"[^"]*\bcolor\s*:[^"]*"|'[^']*\bcolor\s*:[^']*')/i.test(attrs) ||
			/\bcolor\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>]+)/i.test(attrs)
		);
	}

	/** The offset where a paragraph starting at `from` ends (a blank line). */
	function endOfParagraph(text: string, from: number): number {
		const match = /\n[ \t\r]*\n/.exec(text.slice(from));
		return match ? from + match.index : text.length;
	}

	/** Ranges of the prose that already sit inside a colour tag the MODEL wrote. A
	 * quote there is coloured by the narrator already, so the attribution wrapper
	 * would double-wrap the same hue. An unclosed tag's context stops at the
	 * paragraph break — the engine bounds colour that way (D5). */
	function modelColorRanges(text: string): Array<{ start: number; end: number }> {
		const ranges: Array<{ start: number; end: number }> = [];
		const open: Array<{ from: number; stop: number; tag: string; colored: boolean }> = [];
		for (const tag of colorTags(text)) {
			if (!tag.close) {
				open.push({
					from: tag.end,
					stop: endOfParagraph(text, tag.end),
					tag: tag.tag,
					colored: declaresColor(tag.attrs)
				});
				continue;
			}
			for (let i = open.length - 1; i >= 0; i -= 1) {
				if (open[i].tag !== tag.tag) continue;
				const context = open.splice(i, 1)[0];
				if (context.colored) ranges.push({ start: context.from, end: Math.min(tag.start, context.stop) });
				break;
			}
		}
		for (const context of open) {
			if (context.colored) ranges.push({ start: context.from, end: context.stop });
		}
		return ranges;
	}

	/** Quoted ranges of `text`, scanned over the tag-MASKED prose so a colour
	 * attribute's own quote marks can never be read as speech. Both quote forms
	 * (straight and curly) count; the offsets index `text` unchanged. */
	function quotedSpans(text: string): Array<{ start: number; end: number; inner: string }> {
		const source = maskColorTags(text);
		const out: Array<{ start: number; end: number; inner: string }> = [];
		let match: RegExpExecArray | null;
		const straight = /"([^"]*)"/g;
		while ((match = straight.exec(source))) {
			out.push({ start: match.index + 1, end: match.index + 1 + match[1].length, inner: match[1] });
		}
		const curly = /“([^”]*)”/g;
		while ((match = curly.exec(source))) {
			out.push({ start: match.index + 1, end: match.index + 1 + match[1].length, inner: match[1] });
		}
		return out;
	}

	/**
	 * Coloured ranges for one attributed quote in `text`. Handles a quote split
	 * across a dialogue tag (`"A," he said, "B."`) by consuming consecutive
	 * quoted spans whose normalised text continues the quote.
	 */
	function matchQuoteSpans(text: string, quote: string): Array<{ start: number; end: number }> {
		const target = normalizeQuote(quote);
		if (!target) return [];
		const spans = quotedSpans(text);
		if (spans.length === 0) {
			// Masked like the scan: a bare quote must not match attribute syntax.
			const idx = maskColorTags(text).indexOf(quote);
			return idx >= 0 ? [{ start: idx, end: idx + quote.length }] : [];
		}
		const matched: Array<{ start: number; end: number }> = [];
		let pos = 0;
		for (const span of spans) {
			const inner = normalizeQuote(span.inner);
			if (!inner) continue;
			if (target.startsWith(inner, pos)) {
				matched.push({ start: span.start, end: span.end });
				pos += inner.length;
				while (target[pos] === ' ') pos += 1;
			}
		}
		return matched;
	}

	/** Escape a value for use inside a double-quoted HTML attribute. */
	function escapeAttr(value: string): string {
		return value.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
	}

	/** A blank line — the end of a colour span's block. `marked` closes its `<p>`
	 * there, so an inline `<span>` may not cross it. */
	const PARAGRAPH_BREAK = /(\n[ \t\r]*\n)/;

	/** Render a paragraph as plain text when its colour spans do not balance.
	 *
	 * The contract asks the narrator for ONE span per line/paragraph. A span that
	 * straddles a blank line opens in one `{@html}` sink and closes in the next,
	 * leaking `<span>`/`</span>` markup across two blocks (the browser's tag repair
	 * then silently reparents the prose). Defence in depth: a block that starts
	 * inside a span, ends inside one, or closes a span it did not open loses its
	 * colour tags, so it renders as plain text rather than unbalanced markup. */
	function plainWhenUnbalanced(source: string): string {
		if (!source.includes('<')) return source;
		const blocks = source.split(PARAGRAPH_BREAK);
		const plain: boolean[] = [];
		let depth = 0;
		for (const block of blocks) {
			if (/^\n[ \t\r]*\n$/.test(block)) {
				plain.push(false);
				continue;
			}
			let local = depth;
			let lowest = depth;
			for (const tag of colorTags(block)) {
				local += tag.close ? -1 : 1;
				lowest = Math.min(lowest, local);
			}
			plain.push(local !== depth || lowest < 0);
			depth = local;
		}
		if (!plain.some(Boolean)) return source;
		return blocks
			.map((block, i) =>
				plain[i] ? block.replace(/<\s*\/?\s*(?:span|font)\b[^<>]*>/gi, '') : block
			)
			.join('');
	}

	/**
	 * Decorate a prose segment with speaker-colour spans IN THE MARKDOWN SOURCE
	 * (the prose itself is untouched), so the segment is still parsed by
	 * `renderMarkdown` exactly ONCE. Splitting the segment per quote and parsing
	 * each part separately forced `marked` to wrap every part in its own `<p>` —
	 * an orphan `"` paragraph at each quote boundary, a paragraph break where the
	 * prose had none, and a block-level `<p>` nested inside the inline `.quote`
	 * span. Injecting the spans instead keeps the segment's real block structure,
	 * identical to the uncoloured path (`marked` treats the injected `<span>` as
	 * inline HTML, so it stays inside its paragraph and markdown inside the quote
	 * still parses). Returns `text` unchanged when the entry has no attributed
	 * quotes with a known colour (the streaming/first-paint case).
	 *
	 * W9.28 W5: a quote the narrator already wrapped in its own palette colour is
	 * left alone — the model's span renders as-is and gets no second wrapper (the
	 * engine attributed it from that markup in the first place), and a paragraph
	 * whose colour tags do not balance is downgraded to plain text.
	 */
	function colorMarkdownSource(entry: TranscriptEntry, text: string): string {
		if (!text) return text;
		const attrs = attrsForEntry(entry);
		if (attrs.length === 0) return plainWhenUnbalanced(text);
		const matches: Array<{ start: number; end: number; color: string }> = [];
		const wrapped = modelColorRanges(text);
		for (const attr of attrs) {
			const color = quoteColors[attr.speaker_id ?? attr.speaker];
			if (!color) continue;
			for (const span of matchQuoteSpans(text, attr.quote)) {
				const alreadyColored = wrapped.some(
					(range) => span.start >= range.start && span.end <= range.end
				);
				if (alreadyColored) continue;
				matches.push({ ...span, color });
			}
		}
		if (matches.length === 0) return plainWhenUnbalanced(text);
		matches.sort((a, b) => a.start - b.start);
		let out = '';
		let cursor = 0;
		for (const match of matches) {
			if (match.start < cursor) continue; // skip overlaps
			out += text.slice(cursor, match.start);
			const open = `<span class="quote" style="color:${escapeAttr(match.color)}">`;
			// A quote can straddle a blank line. Inline HTML must stay balanced
			// within each block, so each paragraph piece gets its own span.
			for (const piece of text.slice(match.start, match.end).split(/(\n{2,})/)) {
				if (!piece) continue;
				out += /^\n{2,}$/.test(piece) ? piece : `${open}${piece}</span>`;
			}
			cursor = match.end;
		}
		return plainWhenUnbalanced(out + text.slice(cursor));
	}

	// --- Streaming autoscroll (Lane 1d) ---------------------------------------
	// When a streaming reply's *content* starts (NOT the thinking phase), pin the
	// message start a small fixed gap below the top of the transcript so the reader
	// follows from the first line. The pin stays engaged while the reply streams,
	// but any explicit user scroll hands control back for the rest of the turn; the
	// next turn's content start re-engages.
	const AUTOSCROLL_GAP_PX = 12; // ~0.75rem below the collapsed thinking banner
	let scroller = $state<HTMLDivElement | null>(null);
	// Streaming entry we pinned for this turn, or null between turns.
	let followEntryId: string | null = null;
	// Whether the follow is still driving the scroll (false once the user scrolls).
	let followEngaged = false;

	/** An entry shows content once prose, DSL text or segments exist — during the
	 * pure-thinking phase all three are empty (reasoning streams in its own
	 * channel), so this is exactly the thinking→content transition. */
	function hasStreamedContent(entry: TranscriptEntry): boolean {
		return entry.prose.length > 0 || entry.dsl.length > 0 || (entry.segments?.length ?? 0) > 0;
	}

	/**
	 * A dsl segment mounts its own statement at its prose anchor — the
	 * teaching contract places each panel where it narratively belongs. The
	 * STRUCTURAL scene line (`root = ScenePane(…)`) is suppressed here: its
	 * title persists at the top of the reply via DslHeader, and its children
	 * are already drawn inline at their anchors, so drawing it would both
	 * duplicate every child and trail the message with a scene card. Every
	 * dsl segment is a single statement by the segmentation contract (one per
	 * statement, both fence and unfenced paths), so each mount is
	 * self-contained — no cross-segment refs to resolve and no orphans.
	 */
	function isMountedDsl(segment: NarratorSegment): boolean {
		return segment.kind === 'dsl' && !isSceneStatement(segment.text);
	}

	/** The first prose/DSL element of a narrator turn — the "message start". */
	function firstContentEl(article: Element): HTMLElement | null {
		return article.querySelector<HTMLElement>(':scope > .prose, :scope > .dsl-wrap');
	}

	$effect(() => {
		// Read `entries` directly (a prop) so this re-runs on every streamed token.
		const list = entries;
		const el = scroller;
		if (typeof window === 'undefined' || !el) return;

		// The live turn is the last streaming entry (only narrators stream).
		let live: TranscriptEntry | null = null;
		for (let i = list.length - 1; i >= 0; i -= 1) {
			if (list[i].streaming) {
				live = list[i];
				break;
			}
		}
		if (!live) {
			// Turn finished (or none running): arm the next turn's content start.
			followEntryId = null;
			followEngaged = false;
			return;
		}
		if (!hasStreamedContent(live)) return; // still thinking — leave the scroll alone

		const article = el.querySelector(`article[data-entry-id="${CSS.escape(live.id)}"]`);
		const anchor = article ? firstContentEl(article) : null;
		if (!anchor) return;

		if (followEntryId !== live.id) {
			// Content just started for this turn — take the scroll and engage the follow.
			followEntryId = live.id;
			followEngaged = true;
		}
		if (!followEngaged) return;

		// Pin the anchor's top to a fixed gap below the container's top edge. Re-run
		// each token corrects layout drift; a no-op when already pinned.
		const containerTop = el.getBoundingClientRect().top;
		const drift = anchor.getBoundingClientRect().top - containerTop - AUTOSCROLL_GAP_PX;
		if (Math.abs(drift) >= 1) el.scrollTop += drift;
	});

	// Explicit user scroll input disengages the follow for this turn: wheel covers
	// mouse/touchpad in either direction, touchmove the touch gesture, keyup the
	// PageUp/ArrowUp keyboard scroll. Programmatic `scrollTop` writes do not emit
	// these events, so they only ever mean "the user took over".
	$effect(() => {
		const el = scroller;
		if (!el) return;
		const disengage = () => {
			followEngaged = false;
		};
		const onKeyup = (event: KeyboardEvent) => {
			if (event.key === 'PageUp' || event.key === 'ArrowUp' || event.key === 'Home') disengage();
		};
		el.addEventListener('wheel', disengage, { passive: true });
		el.addEventListener('touchmove', disengage, { passive: true });
		el.addEventListener('keyup', onKeyup);
		return () => {
			el.removeEventListener('wheel', disengage);
			el.removeEventListener('touchmove', disengage);
			el.removeEventListener('keyup', onKeyup);
		};
	});

	// --- Open-at-newest ---------------------------------------------------------
	// A hydrated session replaces the transcript wholesale, and the container sits
	// at scrollTop 0 — a long chat then opened ~1000px above its newest reply.
	// Same scroller as the turn pin above, INSTANT (a smooth animation would fight
	// a reader who starts scrolling) and once per session open, so it can never
	// pull anyone back down during normal reading or a live turn.
	let openedSessionId: string | null = null;

	/**
	 * Instant, bounded landing on the newest message.
	 *
	 * The container grows AFTER the first paint — openui panels and the DSL
	 * segments mount asynchronously, so a one-shot jump stops short of the real
	 * bottom. Correct every frame for at most ~1.5s (never smooth), and stop as
	 * soon as the container reports a position ABOVE the bottom: our own writes
	 * always land at the bottom, so that can only be the reader taking over (wheel,
	 * scrollbar drag, keyboard) and we must not fight them.
	 */
	const OPEN_SCROLL_MAX_FRAMES = 90;
	function pinToBottomOnOpen(el: HTMLDivElement): void {
		let frames = 0;
		let cancelled = false;
		const onScroll = () => {
			if (el.scrollTop + el.clientHeight < el.scrollHeight - 4) cancelled = true;
		};
		el.addEventListener('scroll', onScroll, { passive: true });
		const tick = () => {
			frames += 1;
			// A live turn belongs to the streaming pin, not to this landing.
			const streaming = get(transcript).some((entry) => entry.streaming);
			if (cancelled || streaming || frames > OPEN_SCROLL_MAX_FRAMES) {
				el.removeEventListener('scroll', onScroll);
				return;
			}
			const gap = el.scrollTop + el.clientHeight - el.scrollHeight;
			if (Math.abs(gap) >= 1) el.scrollTop = el.scrollHeight;
			requestAnimationFrame(tick);
		};
		tick();
	}

	$effect(() => {
		const id = get(currentSessionId);
		const list = entries;
		const el = scroller;
		if (typeof window === 'undefined' || !el) return;
		if (!id) {
			openedSessionId = null; // a new chat arms the next open
			return;
		}
		if (id === openedSessionId || list.length === 0) return;
		// A session that arrives WITH a live turn (a fresh chat's first send, or an
		// open that raced a stream) belongs to the turn pin, never to this landing:
		// arming here also keeps us from yanking the reader to the bottom when that
		// turn ends and `streaming` flips off (the pin releases where it was).
		if (list.some((entry) => entry.streaming)) {
			openedSessionId = id;
			return;
		}
		openedSessionId = id;
		pinToBottomOnOpen(el);
	});
</script>

<div class="transcript" aria-live="polite" bind:this={scroller}>
	{#if opening && opening.greetings.some((greeting) => greeting.trim())}
		<section class="opening" aria-label="Opening scene">
			<div class="opening-head">
				<span class="opening-label">Opening</span>
				{#if opening.greetings.length > 1}
					<div class="opening-swipe">
						<button
							type="button"
							class="opening-arrow"
							aria-label="Previous opening variant"
							disabled={openingBusy}
							onclick={() => void swipeOpening(-1)}>‹</button
						>
						<span class="opening-count">{openingIndex + 1} / {opening.greetings.length}</span>
						<button
							type="button"
							class="opening-arrow"
							aria-label="Next opening variant"
							disabled={openingBusy}
							onclick={() => void swipeOpening(1)}>›</button
						>
					</div>
				{/if}
				{#if opening.card}
					<button
						type="button"
						class="opening-details"
						onclick={() => (detailsOpen = true)}>Card details</button
					>
				{/if}
				{#if opening.openingLogId !== undefined && onRevise}
					<!-- Wave-1 Bug 2a: world-scenario openings get the same ✎ revise door as
					     rows. Card-greeting openings stay read-only: card events are not on
					     the revision path (fabulist.api.revisions EDITABLE_TEXT_KEYS). -->
					<button
						type="button"
						class="opening-edit"
						title="Edit the opening (appends a revision)"
						aria-label="edit opening"
						onclick={startOpeningEdit}>✎</button
					>
				{/if}
			</div>
			{#if openingEditing && opening.openingLogId !== undefined}
				<div class="edit-box">
					<textarea bind:value={openingDraft} rows="4" aria-label="edit opening"></textarea>
					<div class="edit-actions">
						<button
							type="button"
							class="edit-save"
							disabled={openingRevising}
							onclick={() => void saveOpeningEdit()}
						>
							{openingRevising ? 'saving…' : 'Save revision'}
						</button>
						<button
							type="button"
							class="edit-cancel"
							disabled={openingRevising}
							onclick={() => (openingEditing = false)}
						>
							Cancel
						</button>
					</div>
				</div>
			{:else}
				<div class="opening-text">{@html renderMarkdown(opening.greetings[openingIndex] ?? '')}</div>
			{/if}
		</section>
	{/if}

	{#if entries.length === 0}
		<div class="empty-state">
			<h2 class="empty-title">Start a story</h2>
			<p class="empty-lead">
				Fabulist is a roleplay narrator. You play your character; the narrator runs the
				world, remembers what happened, and keeps the scene moving turn by turn.
			</p>
			<p class="empty-hint">
				…or import a character card from the left panel, then type below to begin.
			</p>
		</div>
	{/if}

	{#each entries as entry, index (entry.id)}
		{#if !isFolded(index)}
			{#if entry.system}
				{#if entry.system.kind === 'context_low' && entry.system.contextLow}
					{@const low = entry.system.contextLow}
					<!-- W9.26 §2.2: the context gauge's message. Owner copy verbatim; it is UI-only
					     and never enters the model prompt. D-F styling class, deliberately quiet:
					     it is a note ABOUT the conversation, not the narrator speaking. -->
					<div
						class="validation-notice context-low"
						role="note"
						aria-label="context window notice"
					>
						<span class="notice-glyph" aria-hidden="true">⚑</span>
						<div class="context-low-body">
							<p class="context-low-line">
								Your context window is getting low.
								{#if low.overBudget}
									<!-- A declaration problem compaction cannot fix (§2.1/§2.3): the
									     declared window cannot pay for a reply, so the button is meaningless. -->
									<span class="context-low-problem">
										This window is already smaller than the reply needs — compaction cannot
										free more. Check the declared Context Size / Response Length / prefix size.
									</span>
								{:else}
									<button
										type="button"
										class="compact-btn"
										disabled={low.settled || compactBusy}
										title={low.settled
											? 'This era was already compacted'
											: 'Summarize the history into a new era'}
										onclick={() => onCompact?.()}
									>
										{low.settled ? 'compacted' : compactBusy ? 'Compacting…' : 'Compact'}
									</button>
								{/if}
								the history now, or
								<button
									type="button"
									class="context-newchat"
									onclick={() => onNewChat?.()}>start a new chat</button
								>.
							</p>
							<p class="context-note">
								At 90%, the history is compacted automatically.
								{#if low.noop}
									The forced compaction already ran this era and cannot free more window —
									check the declared Context Size / Response Length / prefix size.
								{/if}
							</p>
							{#if low.settled}
								<p class="context-note">Compacted into a later era; the original stays readable above.</p>
							{/if}
							{#if ratioLabel(low)}
								<p class="context-metrics">{ratioLabel(low)}</p>
							{/if}
							{#if compactError && !low.settled && !low.overBudget}
								<!-- The job's failed/noop reason, inline: the button above re-arms. -->
								<p class="context-error" role="status">{compactError}</p>
							{/if}
						</div>
					</div>
				{:else if entry.system.kind === 'compaction_break'}
					{@const cut = entry.system.compaction}
					<!-- W9.26 §2.5: one break per marker. The collapse chevron folds ONLY the span
					     above it; the label names the covered SEQ span, never turn indices. -->
					<div class="compaction-break" role="separator" aria-label="compaction break">
						<span class="break-rule" aria-hidden="true"></span>
						<span class="break-label">
							Older turns condensed — {breakSpan(cut)} rolled up into a summary
						</span>
						<button
							type="button"
							class="break-toggle"
							aria-expanded={!collapsedBreaks[entry.id]}
							title={collapsedBreaks[entry.id]
								? 'Show the turns hidden under this break'
								: 'Hide the turns above this break'}
							onclick={() => toggleBreak(entry.id, !!collapsedBreaks[entry.id])}
						>
							{collapsedBreaks[entry.id] ? '▸ collapse' : '▾ collapse'}
						</button>
						<span class="break-rule" aria-hidden="true"></span>
					</div>
				{:else if entry.system.kind === 'compaction_notice'}
					{@const done = entry.system.compaction}
					<!-- W9.26 §2.5: BELOW the break. Text is never duplicated onto the marker, so
					     the raw summary is FETCHED from /recap on first expand. -->
					<div class="compaction-notice" role="note" aria-label="compaction notice">
						<span class="notice-glyph" aria-hidden="true">⌁</span>
						<details
							class="compaction-details"
							ontoggle={(event) => void onNoticeToggle(done, event.currentTarget.open)}
						>
							<summary>Compaction done</summary>
							{#if recapLookup[recapKey(done)]?.state === 'loading'}
								<p class="notice-line">loading the summary…</p>
							{:else if recapLookup[recapKey(done)]?.state === 'error'}
								<p class="notice-line notice-error">{recapError(done)}</p>
							{:else if recapEntryFor(done)}
								{@const recap = recapEntryFor(done)}
								<dl class="compaction-raw">
									<dt>summary</dt>
									<dd class="raw-summary">{recap?.text}</dd>
									<dt>covered</dt>
									<dd>
										{#if recap?.covers_seqs[0] !== null && recap?.covers_seqs[1] !== null}
											turns {recap?.covers_seqs[0]}–{recap?.covers_seqs[1]}
										{:else}
											{breakSpan(done)}
										{/if}
									</dd>
									<dt>trigger</dt>
									<dd>{recap?.trigger ?? done?.trigger ?? 'unknown'}</dd>
									<dt>model</dt>
									<dd>{recap?.stats.model ?? 'unknown'}</dd>
									<dt>calls</dt>
									<dd>{recap?.stats.calls ?? '—'}</dd>
									<dt>measured</dt>
									<dd>
										{#if recap?.stats.prompt_tokens !== null && recap?.stats.prompt_tokens !== undefined}
											{recap.stats.prompt_tokens.toLocaleString()} prompt tokens
										{:else}
											—
										{/if}
									</dd>
									{#if recap?.absorbs}
										<dt>absorbs</dt>
										<dd>{recap.absorbs[0]} · seq {recap.absorbs[1]}</dd>
									{/if}
								</dl>
							{:else}
								<p class="notice-line">No summary row recorded for this era.</p>
							{/if}
						</details>
					</div>
				{/if}
			{:else}
				{@const meta = turnMeta[entry.id]}
				<!-- #28 search-jump anchor: SessionDrawer hits scroll to #history-{event_id} -->
				<article
					class="turn"
					class:user={entry.role === 'user'}
					id={entry.logId !== undefined ? `history-${entry.logId}` : undefined}
					data-entry-id={entry.id}
				>
			{#if hasFlags(meta) || meta?.editReason}
				<div class="turn-meta">
					{#if hasFlags(meta)}
						<span class="validator-badge" title={flagSummary(meta)}>
							<span class="badge-glyph" aria-hidden="true">⚠</span>
							{flagCount(meta)} validator {flagCount(meta) === 1 ? 'note' : 'notes'}
						</span>
					{/if}
					{#if meta?.editReason}
						<span class="edit-badge" title={editExplain(meta.editReason)}>
							<span class="badge-glyph" aria-hidden="true">✎</span>edited
						</span>
					{/if}
				</div>
			{/if}
			{#if editingId === entry.id}
				<div class="edit-box">
					<textarea
						bind:value={draft}
						rows="4"
						aria-label="edit message"
					></textarea>
					<div class="edit-actions">
						<button
							type="button"
							class="edit-save"
							disabled={reviseBusy}
							onclick={() => void saveEdit(entry)}
						>
							{reviseBusy ? 'saving…' : 'Save revision'}
						</button>
						<button
							type="button"
							class="edit-cancel"
							disabled={reviseBusy}
							onclick={() => (editingId = null)}
						>
							Cancel
						</button>
					</div>
				</div>
			{:else}
			{#if onRevise && entry.logId !== undefined && !entry.streaming}
				<button
					type="button"
					class="revise-btn"
					title="Edit (appends a revision)"
					aria-label="edit message"
					onclick={() => startEdit(entry.id, editableText(entry))}
				>
					✎
				</button>
			{/if}
			{#if onDeleteFrom && entry.logId !== undefined && !entry.streaming && entry.id !== firstMessageId}
				<!-- Issue #35: delete this message and every message after it. Absent on
				     the chain's FIRST message (the engine refuses it as `first_message`),
				     so the opening — and everything above it — can never be swept away. -->
				<button
					type="button"
					class="revise-btn delete-btn"
					title="Delete this message and everything after it (forks the chat; the old line stays one ‹ away)"
					aria-label="delete messages from here"
					disabled={deleting}
					onclick={() => void requestDeleteFrom(entry, index)}
				>
					✂
				</button>
			{/if}
			{#if entry.role === 'narrator'}
				{#if hasAlternates && branch && index === lastNarratorIndex}
					<span class="branch-chip" title="This reply has alternate branches">
						<span class="chip-glyph" aria-hidden="true">⑂</span>
						{branch.index} / {branch.count}
					</span>
				{/if}
				{#if entry.reasoning}
					<details class="reasoning">
						<summary>{entry.streaming ? 'thinking…' : 'thinking'}</summary>
						<pre>{entry.reasoning}</pre>
					</details>
				{/if}
				{#if entry.image}
					<!-- A generated image is a canon row (an `imagegen` narrative): it
					     renders in the narrator lane as a clickable thumbnail with its
					     prompt caption, never as the empty prose/segments path. -->
					<figure class="canon-image">
						<button
							type="button"
							class="image-zoom"
							title="View larger"
							onclick={() => {
								lightboxAlt = entry.caption ?? 'Generated image';
								lightboxSrc = entry.image ?? null;
							}}
						>
							<img
								class="canon-image-img"
								src={entry.image}
								alt={entry.caption ?? 'Generated image'}
							/>
						</button>
						{#if entry.caption}
							<figcaption class="canon-caption">{entry.caption}</figcaption>
						{/if}
					</figure>
				{:else if entry.segments}
					{#each entry.segments as segment, segmentIndex (segmentIndex)}
						{@const segFlags = flagsForSegment(meta, segmentIndex)}
						{#if isMountedDsl(segment)}
							<!-- One renderer per panel statement, at its prose anchor. The structural
						     scene line renders nothing here (its title persists in the DslHeader);
						     re-parsed per delta while streaming. -->
							<div class="dsl-wrap" class:flagged={segFlags.length > 0} title={segmentTitle(segFlags)}>
								<DslFallback dsl={segment.text} streaming={entry.streaming} />
							</div>
						{:else if segment.kind === 'prose'}
							<!-- Click handling is delegated to the prose div so images inside
							     sanitized `{@html}` markdown are zoomable. The ignored a11y
							     diagnostics are the known cost of that: an `<img>` rendered
							     from markdown carries no focusable markup of its own. -->
							<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
							<div
								class="prose narrator-prose"
								class:flagged={segFlags.length > 0}
								title={segmentTitle(segFlags)}
								onclick={openProseImage}
							>{@html renderMarkdown(colorMarkdownSource(entry, segment.text))}</div>
						{/if}
					{/each}
					{#if entry.streaming && entry.segments.length === 0}
						<div class="prose narrator-prose"><span class="dots">…</span></div>
					{/if}
				{:else}
					{@const legacyFlags = flagsForSegment(meta, 0)}
					<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
					<div
						class="prose narrator-prose"
						class:flagged={legacyFlags.length > 0}
						title={segmentTitle(legacyFlags)}
						onclick={openProseImage}
					>
						{@html renderMarkdown(colorMarkdownSource(entry, entry.prose))}{#if entry.streaming && !entry.prose}<span class="dots">…</span>{/if}
					</div>
					<DslFallback dsl={entry.dsl} streaming={entry.streaming} />
				{/if}
				{#if entry.notice && entry.notice.flags.length > 0}
					<!-- D-F: the committed turn's teaching notice, at the tail of the turn it
					     refers to — the system turn's natural position in the transcript
					     (after its narrative), never a toast or a trailing digest. -->
					<div class="validation-notice" role="note" aria-label="validation notice">
						<span class="notice-glyph" aria-hidden="true">⚑</span>
						<ul>
							{#each entry.notice.flags as flag, flagIndex (flagIndex)}
								<li>
									<span class="notice-text">{noticeText(flag)}</span>
									{#if flag.fix}<span class="notice-fix">fix: {flag.fix}</span>{/if}
								</li>
							{/each}
						</ul>
					</div>
				{/if}
				{#if hasFlags(meta)}
					<details class="flag-list">
						<summary>
							{flagCount(meta)} validator {flagCount(meta) === 1 ? 'note' : 'notes'}
						</summary>
						<ul>
							{#each meta?.flags ?? [] as flag, flagIndex (flagIndex)}
								<li>
									{flagExplain(flag)}{#if flag.source}<span class="flag-source"> · {flag.source}</span>{/if}
								</li>
							{/each}
						</ul>
					</details>
				{/if}
			{:else}
				<div class="user-row">
					{#if persona}
						<UserBadge
							name={persona.name}
							id={persona.persona_id ?? null}
							imageAsset={persona.image_asset ?? null}
							kind="persona"
						/>
					{/if}
					<div class="user-body">
				{#if entry.image}
					<!-- User attachments and imagegen results share this thumbnail; the
					     button opens the full-size lightbox. -->
					<button
						type="button"
						class="image-zoom"
						title="View larger"
						onclick={() => {
							lightboxAlt = '';
							lightboxSrc = entry.image ?? null;
						}}
					>
						<img class="user-image" src={entry.image} alt="" />
					</button>
				{/if}
<!-- svelte-ignore a11y_media_has_caption -->
				{#if entry.media?.kind === 'video'}
					<video class="user-media" src={entry.media.url} controls playsinline></video>
				{:else if entry.media?.kind === 'audio'}
					<audio class="user-media" src={entry.media.url} controls></audio>
				{/if}
				<div class="prose user-prose">
					{#if entry.kind === 'ooc'}<span class="ooc-tag">OOC</span>{/if}
					{#each rollParts(entry.prose, meta) as part, partIndex (partIndex)}
						{#if part.kind === 'roll'}
							<span
								class="dice-chip"
								title={part.roll.detail ?? `${part.roll.dice} = ${part.roll.result}`}
							>
								<span class="dice-glyph" aria-hidden="true">⚄</span>
								<span class="dice-expr">{part.roll.dice}</span>
								<span class="dice-eq">=</span>
								<span class="dice-result">{part.roll.result}</span>
								{#if part.roll.ask}<span class="dice-ask">{part.roll.ask}</span>{/if}
							</span>
						{:else}{@html renderMarkdown(part.text)}{/if}
					{/each}
				</div>
				{#if regenOnEntryId === entry.id}
					<!-- Wave-1 Bug 2b: this row was just revised and is still the latest
					     user message, so the reply that answered it is stale. One click
					     re-runs the last narrator turn (the page's retry path). -->
					<div class="regen-offer">
						<button type="button" class="regen-btn" onclick={regenerate}>
							Regenerate reply
						</button>
						<button
							type="button"
							class="regen-dismiss"
							title="Dismiss"
							aria-label="dismiss regenerate offer"
							onclick={dismissRegen}>×</button
						>
					</div>
				{/if}
					</div>
				</div>
			{/if}
			{/if}
			{#if entry.stale}
				<p class="stale" role="status">
					This reply has not been verified against the saved turn. Reload to re-sync it.
				</p>
			{/if}
			{#if entry.error}
				<p class="error">{entry.error}</p>
			{/if}
			{#if showBranch && branch && index === lastNarratorIndex}
				<BranchControls
					{branch}
					busy={branchBusy || entry.streaming}
					onPrev={() => onSwipe?.(-1)}
					onNext={() => onSwipe?.(1)}
					onRetry={() => onRetry?.()}
				/>
			{/if}
		</article>
			{/if}
		{/if}
	{/each}

	{#if pendingStatus}
		<!-- W9.26 §2.2: the page-owned INFORMATIONAL status (a turn waiting out a
		     `409 compacting`, or a job still running). Never styled as an error: the
		     job is doing exactly what it should. -->
		<div class="validation-notice pending-status" role="status" aria-live="polite">
			<span class="notice-glyph" aria-hidden="true">⌁</span>
			<span class="notice-text">{pendingStatus}</span>
		</div>
	{/if}
</div>

{#if detailsOpen && opening?.card}
	<CardDetails
		card={opening.card}
		sessionId={$currentSessionId}
		cardAsset={opening.cardAsset}
		onClose={() => (detailsOpen = false)}
	/>
{/if}

{#if lightboxSrc}
	<ImageViewer src={lightboxSrc} alt={lightboxAlt} onClose={() => (lightboxSrc = null)} />
{/if}

<style>
	.transcript {
		flex: 1;
		overflow-y: auto;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.9rem;
	}
	/* Issue #22: THE READING COLUMN.
	 *
	 * The scroll AREA stays full-bleed (its scrollbar and the page paint belong to
	 * the thread), and every block inside it is capped at ONE measure and CENTRED.
	 * Before this, the 46rem `max-width` on `.turn` was a left-anchored block in a
	 * wide column: the cap existed in the stylesheet but the eye read a ragged
	 * right gutter, and the width the density pass reclaimed landed entirely on
	 * one side. Centring is what makes the cap BIND as a reading measure — the
	 * reclaimed width becomes symmetric gutter on both sides.
	 *
	 * 46rem is the owner's measure (issue #22). One owner for the width: the turn
	 * rows below no longer carry their own `max-width`. */
	.transcript > * {
		width: 100%;
		max-width: 46rem;
		margin-inline: auto;
	}

	/* Issue #41: alignment. The measure above keeps its width; only its inline
	 * margins move — `left` flushes the column to the start, `right` to the end,
	 * and `center` (the shipped default) has no rule of its own because the base
	 * `margin-inline: auto` IS centred. The switch is `<html data-align>` from
	 * `#lib/theme/apply` (applied with the preset, so a reload restores it). */
	:global(:root[data-align='left']) .transcript > * {
		margin-inline: 0 auto;
	}
	:global(:root[data-align='right']) .transcript > * {
		margin-inline: auto 0;
	}

	/* Wave B (D30): pinned read-only Opening block atop the transcript. */
	.opening {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding: 0.55rem 0.7rem;
		border: 1px solid var(--accent-line);
		border-left: 3px solid var(--accent-strong);
		border-radius: 8px;
		background: var(--accent-soft);
	}
	.opening-head {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.opening-label {
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.07em;
		text-transform: uppercase;
		color: var(--accent-strong);
	}
	.opening-swipe {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		margin-left: auto;
	}
	.opening-count {
		font-size: var(--font-micro);
		color: var(--text-muted);
		font-variant-numeric: tabular-nums;
	}
	.opening-arrow {
		font: inherit;
		font-size: var(--font-ui);
		line-height: 1;
		padding: 0 0.35rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.opening-arrow:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.opening-details {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
	/* Wave-1 Bug 2a: the world-scenario opening's ✎ revise door (rows use the
	   same affordance shape at `.revise-btn`). Always visible: the opening head
	   has no hover-only discoverability requirement like a transcript row. */
	.opening-edit {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.1rem 0.45rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
	/* Wave-1 Bug 2b: the stale-reply offer under a just-edited user message. */
	.regen-offer {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
	}
	.regen-btn {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.15rem 0.6rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
	.regen-dismiss {
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1;
		padding: 0.1rem 0.35rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.opening-text {
		margin: 0;
		line-height: 1.5;
		font-size: 0.86rem;
		color: var(--text);
	}
	.empty-state {
		margin: auto;
		max-width: 32rem;
		text-align: center;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.7rem;
		padding: 1.5rem;
	}
	.empty-title {
		margin: 0;
		font-size: 1.25rem;
		font-weight: 600;
		color: var(--text);
	}
	.empty-lead {
		margin: 0;
		font-size: var(--font-ui);
		line-height: 1.55;
		color: var(--text-muted);
	}
	.empty-hint {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.turn {
		/* Anchors the absolutely-positioned ✎ revise door to ITS row: without
		   this every row's door stacks on the transcript (they overlap and the
		   topmost one swallows the click). */
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	/* D48: the user turn is indented, not flushed to the right edge; the boxed
	   `.user-prose` card still marks it apart from the narrator prose.
	   The indent is PADDING, not a margin: the reading column centres every block
	   by its box (issue #22), so an inline-start margin would push this row off
	   centre and reintroduce the ragged gutter the pass removed. */
	.turn.user {
		padding-left: 3rem;
		align-items: flex-start;
	}
	/* Circular persona badge sits to the LEFT of the user message body. The body
	   keeps its own column so the boxed `.user-prose` card shrink-wraps exactly
	   as it did when it was a direct child of `.turn.user`. */
	.user-row {
		display: flex;
		align-items: flex-start;
		gap: 0.5rem;
	}
	/* Optical alignment: `align-items: flex-start` puts the badge's box top flush
	   with the boxed card's OUTER edge, but the card's first text line starts
	   0.45rem (its padding) lower. Nudge the badge down by that padding so its
	   centre (~12px for the 1.5rem badge) lands on the card's first line centre
	   (~7.2px padding + 11.2px half of the 0.9rem/1.55 line box). */
	.user-row :global(.user-badge) {
		margin-top: 0.4rem;
	}
	.user-body {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.4rem;
		min-width: 0;
	}
	.prose {
		line-height: 1.55;
		font-size: 0.9rem;
	}
	/* The two markdown sinks (`.prose` = narrator + user prose, `.opening-text` =
	   seeded opening). Neither uses `white-space: pre-wrap` any more: marked emits
	   literal `\n` text nodes between block tags, and pre-wrap turned those into
	   blank lines on top of the default `p` margins (double spacing). Single
	   newlines still break because `renderMarkdown` runs with `breaks: true`, and
	   compact margins below own the block spacing. */
	.prose :global(p),
	.prose :global(ul),
	.prose :global(ol),
	.prose :global(blockquote),
	.opening-text :global(p),
	.opening-text :global(ul),
	.opening-text :global(ol),
	.opening-text :global(blockquote) {
		margin: 0.4em 0;
	}
	.prose :global(> :first-child),
	.opening-text :global(> :first-child) {
		margin-top: 0;
	}
	.prose :global(> :last-child),
	.opening-text :global(> :last-child) {
		margin-bottom: 0;
	}
	.narrator-prose {
		color: var(--text);
	}
	.revise-btn {
		position: absolute;
		top: 0.3rem;
		right: 0.3rem;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		color: var(--text-muted);
		border-radius: 6px;
		padding: 0.1rem 0.4rem;
		cursor: pointer;
		font-size: 0.8rem;
		opacity: 0;
		transition: opacity 120ms ease;
	}
	.turn:hover .revise-btn,
	.revise-btn:focus-visible {
		opacity: 1;
	}
	/* Issue #35: the ✂ delete door shares the ✎ door's styling and its
	   reveal-on-hover behaviour; only the offset differs so the two sit side by
	   side at the row's top right instead of stacking on one pixel. */
	.delete-btn {
		right: 2.1rem;
	}
	.delete-btn:disabled {
		cursor: default;
	}
	.turn:hover .delete-btn:disabled {
		opacity: 0.35;
	}
	.edit-box textarea {
		width: 100%;
		box-sizing: border-box;
		background: var(--bg-inset, var(--bg-raised));
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.5rem;
		font: inherit;
		resize: vertical;
	}
	.edit-actions {
		display: flex;
		gap: 0.4rem;
		margin-top: 0.35rem;
		justify-content: flex-end;
	}
	.edit-save {
		background: var(--accent);
		color: #fff;
		border: none;
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		cursor: pointer;
	}
	.edit-cancel {
		background: var(--bg-raised);
		color: var(--text-muted);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.25rem 0.7rem;
		cursor: pointer;
	}
	.user-media {
		display: block;
		max-width: 320px;
		width: 100%;
		margin: 0.25rem 0 0.35rem auto;
		border-radius: 8px;
	}
	.narrator-prose :global(img) {
		cursor: zoom-in;
	}
	.image-zoom {
		display: block;
		width: fit-content;
		margin: 0.25rem 0 0.35rem auto;
		padding: 0;
		border: 0;
		background: none;
		cursor: zoom-in;
	}
	.user-image {
		display: block;
		max-width: 260px;
		max-height: 200px;
		border-radius: 8px;
		border: 1px solid var(--line);
		margin: 0;
	}
	/* Canon lane: a generated image (imagegen narrative) as a left-aligned
	   thumbnail + its prompt caption — never the empty prose path. */
	.canon-image {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.3rem;
		margin: 0.1rem 0 0.3rem;
	}
	.canon-image .image-zoom {
		margin: 0;
	}
	.canon-image-img {
		display: block;
		max-width: 320px;
		max-height: 260px;
		width: auto;
		border-radius: 8px;
		border: 1px solid var(--line);
	}
	.canon-caption {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.user-prose {
		background: var(--bg-raised);
		border: 1px solid var(--accent-line);
		border-radius: 10px;
		padding: 0.45rem 0.7rem;
		color: var(--text);
	}
	.ooc-tag {
		display: inline-block;
		margin-right: 0.45rem;
		padding: 0 0.35rem;
		border-radius: 4px;
		background: var(--bg-hover);
		color: var(--text-muted);
		font-size: 0.6rem;
		letter-spacing: 0.08em;
		vertical-align: middle;
	}
	.dots {
		color: var(--text-dim);
	}
	.reasoning {
		font-size: 0.75rem;
		color: var(--text-muted);
	}
	.reasoning summary {
		cursor: pointer;
		color: var(--accent-strong);
		width: fit-content;
	}
	.reasoning pre {
		margin: 0.35rem 0 0;
		padding: 0.4rem 0.55rem;
		background: var(--bg-inset);
		border-radius: 6px;
		white-space: pre-wrap;
		font-family: inherit;
		font-size: 0.72rem;
		line-height: 1.45;
	}
	.error {
		margin: 0;
		color: var(--danger-text);
		font-size: 0.78rem;
	}
	.stale {
		margin: 0;
		color: var(--text-muted);
		font-size: 0.78rem;
		font-style: italic;
	}
	.branch-chip {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		width: fit-content;
		padding: 0.05rem 0.4rem;
		border-radius: 999px;
		background: var(--accent-soft);
		border: 1px solid var(--accent-line);
		color: var(--accent-strong);
		font-size: 0.65rem;
		font-variant-numeric: tabular-nums;
	}
	.chip-glyph {
		color: var(--accent-strong);
	}

	/* Wave 4b: player-HUD overlays on the transcript (dice chips + validator
	   markers). All additive — a turn with no meta renders exactly as before. */
	.turn-meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.35rem;
	}
	.validator-badge,
	.edit-badge {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		padding: 0.05rem 0.45rem;
		border-radius: 999px;
		font-size: var(--font-micro);
		font-variant-numeric: tabular-nums;
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: help;
	}
	.validator-badge {
		border-color: var(--warn);
		color: var(--warn);
	}
	.edit-badge {
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.badge-glyph {
		font-size: 0.7rem;
	}

	/* Inline dice chip: replaces the `[roll …]` token in the user message. */
	.dice-chip {
		display: inline-flex;
		align-items: baseline;
		gap: 0.2rem;
		margin: 0 0.1rem;
		padding: 0.02rem 0.4rem;
		border-radius: 6px;
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-size: var(--font-small);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
		vertical-align: baseline;
		cursor: help;
	}
	.dice-glyph {
		font-size: 0.7rem;
	}
	.dice-expr {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		color: var(--accent-strong);
	}
	.dice-eq {
		color: var(--text-dim);
	}
	.dice-result {
		font-weight: 700;
	}
	.dice-ask {
		color: var(--text-muted);
		font-size: var(--font-micro);
		border-left: 1px solid var(--accent-line);
		padding-left: 0.3rem;
		margin-left: 0.1rem;
	}

	/* IDE-style gutter marker on a validator-flagged section.
	   The marker lives in the MARGIN and never styles the text it flags: a
	   bar + tinted background turned a flagged prose run into what read as a
	   component container, and the validator anchors most flags to the whole
	   first prose run, so the whole message looked boxed (owner report item
	   14; live session 05f0b5a9 had ten turns at 98-100% of the message inside
	   the box). The note stays visible twice — the message-level badge above
	   the turn, and this glyph at the anchor (its `title` carries the notes). */
	.flagged {
		position: relative;
	}
	.flagged::before {
		content: '⚠';
		position: absolute;
		left: -0.95rem;
		top: 0.1rem;
		font-size: var(--font-micro);
		line-height: 1;
		color: var(--warn);
		pointer-events: none;
	}
	.flag-list {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.flag-list summary {
		cursor: pointer;
		color: var(--warn);
		width: fit-content;
	}
	.flag-list ul {
		margin: 0.3rem 0 0;
		padding-left: 1.1rem;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.flag-source {
		color: var(--text-dim);
	}

	/* D-F: the validation notice — a quiet SYSTEM line at the tail of the turn it
	   refers to. Deliberately not a bubble and not an accent card: it is a note
	   ABOUT the reply above it, so it must not read as the narrator speaking nor
	   as an error on the reader's screen. The left rule is the only emphasis. */
	.validation-notice {
		display: flex;
		align-items: flex-start;
		gap: 0.4rem;
		max-width: 42rem;
		padding: 0.3rem 0.55rem;
		border-left: 2px solid var(--warn);
		border-radius: 0 4px 4px 0;
		background: color-mix(in srgb, var(--warn) 6%, var(--bg-raised));
		color: var(--text-muted);
		font-size: var(--font-micro);
		line-height: 1.5;
	}
	.notice-glyph {
		color: var(--warn);
		font-size: 0.7rem;
		line-height: 1.5;
	}
	.validation-notice ul {
		margin: 0;
		padding: 0;
		list-style: none;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	.notice-text {
		color: var(--text-muted);
	}
	.notice-fix {
		display: block;
		color: var(--text-dim);
	}

	/* --- W9.26 §2.2/§2.5: context-low rows, era breaks, compaction notices ----
	   All three borrow the D-F notice's quiet system voice (left rule, micro
	   type, no bubble): they are notes ABOUT the conversation, never the narrator
	   speaking and never an error on the reader's screen. */
	.context-low .context-low-body {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		min-width: 0;
	}
	.context-low-line {
		margin: 0;
		color: var(--text-muted);
	}
	.context-low-problem {
		color: var(--warn);
	}
	.compact-btn,
	.context-newchat {
		font: inherit;
		font-size: var(--font-micro);
		line-height: 1.3;
		padding: 0.05rem 0.45rem;
		margin: 0 0.1rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
	.compact-btn:disabled {
		opacity: 0.65;
		cursor: default;
		color: var(--text-muted);
		border-color: var(--line);
	}
	.context-newchat {
		border: none;
		background: none;
		padding: 0;
		margin: 0;
		color: var(--accent-strong);
		text-decoration: underline;
	}
	.context-note,
	.context-metrics {
		margin: 0;
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	.context-metrics {
		font-variant-numeric: tabular-nums;
	}
	.context-error {
		margin: 0;
		color: var(--danger-text);
		font-size: var(--font-micro);
	}

	/* One break per compaction marker; the rule lines either side make it read as
	   a separator between eras, not as another message. */
	.compaction-break {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.15rem 0;
	}
	.break-rule {
		flex: 1;
		height: 1px;
		background: var(--line);
	}
	.break-label {
		font-size: var(--font-micro);
		letter-spacing: 0.02em;
		color: var(--text-dim);
		white-space: nowrap;
	}
	.break-toggle {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.05rem 0.4rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.break-toggle:hover {
		color: var(--accent-strong);
		border-color: var(--accent-line);
	}

	/* The compaction-done notice sits BELOW its break. Collapsed by default: the
	   summary text is fetched only when the reader asks for the raw data. */
	.compaction-notice {
		display: flex;
		align-items: flex-start;
		gap: 0.4rem;
		max-width: 42rem;
		padding: 0.3rem 0.55rem;
		border-left: 2px solid var(--accent-line);
		border-radius: 0 4px 4px 0;
		background: color-mix(in srgb, var(--accent-soft) 55%, var(--bg-raised));
		color: var(--text-muted);
		font-size: var(--font-micro);
		line-height: 1.5;
	}
	.compaction-details summary {
		cursor: pointer;
		color: var(--accent-strong);
		width: fit-content;
	}
	.compaction-raw {
		display: grid;
		grid-template-columns: max-content 1fr;
		gap: 0.15rem 0.6rem;
		margin: 0.35rem 0 0;
	}
	.compaction-raw dt {
		color: var(--text-dim);
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}
	.compaction-raw dd {
		margin: 0;
		font-variant-numeric: tabular-nums;
	}
	.raw-summary {
		color: var(--text-muted);
		font-size: var(--font-small);
		line-height: 1.5;
	}
	.notice-line {
		margin: 0.25rem 0 0;
		color: var(--text-dim);
	}
	.notice-error {
		color: var(--danger-text);
	}
	.pending-status {
		max-width: 42rem;
	}
</style>
