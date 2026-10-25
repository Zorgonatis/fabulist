<script lang="ts">
	// Cast pane (Wave 4b H5; W5C-2/W5C-3 completions; W9.22 cast bar tiers).
	//
	// Colour dialogue (B5, Wave 6) assigns each speaker a stable hue the first
	// time they're seen; the engine appends a `system`/`speaker_color` event
	// (D25: the log owns the mapping, forks replay it identically).
	//
	// W5C-3: GET /api/sessions/{sid}/cast merges the card-derived roster
	// (context/roster.py `extract_cards`) with the colour registry, so this pane
	// shows the real cast list instead of a pending note.
	// W3: that merged read also carries the BTS-discovered cast (the top-level
	// keys of `world_state.state`) and the engine assigns a palette hue to every
	// not-yet-coloured speaker on first sight, so a multi-persona / multi-world
	// chat lists every speaker with a colour. The read publishes the registry to
	// `speakerColorStore` for the same reason a PATCH does: quotes already
	// rendered for a speaker coloured just now repaint without a new turn.
	// W5C-2: PATCH /api/sessions/{sid}/speaker_colors {name, color} writes a
	// `user` override (latest-wins); the picker is now live and confirms on save.
	// The visible coloured dialogue/attribution itself is the B5 Wave-6
	// interaction — this pane owns only the registry read + write path.
	//
	// Bug fix: a confirmed PATCH is published to `speakerColorStore` so the
	// transcript repaints already-rendered quotes (it otherwise re-reads the
	// registry only when the transcript shape changes).
	//
	// W7: each row is a canonical member. The list is keyed by the immutable
	// `member_id` (the display `name` is a mutable label, so keying by it remounted
	// rows on a rename); `aliases` render as "previously …" and a `provisional`
	// badge marks a descriptor admitted before its name.
	//
	// W9.22 (plan §2 "Cast model"): the pane splits the ONE member list into the
	// two TIERS the plan defines, because they are not the same kind of thing:
	//
	//   * ACTORS (`provenance` card/world/actor/user) — the attached actor list.
	//     Lane-eligible, never cap-evicted. Rendered first, exactly as before.
	//   * IN SCENE (everything else: BTS-learned `bts` members and the read-only
	//     virtual rows) — presence tier. Never lanes, never pins; retires by
	//     silent LRU under its own budget. It sits in a COLLAPSED group with
	//     transient styling (dimmer row, provisional-style chip) so learned
	//     presence never reads as cast.
	//
	// The tier is derived from `provenance` in `#lib/api/hud` (no new wire field):
	// the engine's rule lives in `fabulist.state.presence.ACTOR_PROVENANCE`.
	//
	// W9.22 User-as-Actor toggle: the header checkbox writes the session setting
	// through `PATCH .../chat-config` (one latest-wins event, D9). Default OFF;
	// ON makes the player an actor — a first-class presence and a lane candidate
	// (impersonation by consent, per chat). It renders DISABLED with a tooltip
	// when the config read found no engine, because a setting the pane cannot
	// write must not look editable.
	//
	// W9.24 §3.5 NPC dispatch policy: the second row of that same read. Two
	// controls, because the policy is two fields — WHICH characters lane
	// (`strategy`) and HOW MANY (`max dispatch`, 0 = no bound). Both write the
	// SAME session setting event (per-key optional PATCH), never an asset. The
	// row is a cost knob for the owner: `decision` costs one d1-3b ask instead of
	// guessing, and a bound is the only way to say "fewer lane calls".
	//
	// Issue #56 (owner ruling): the ONE thing missing here was lanes themselves —
	// on/off and how many run at once. Those two are NOT session settings: they
	// are topology data on the turn flow's fan-out row (`data.enabled`,
	// `data.max_lanes`), because the running graph is compiled from that row. So
	// they sit beside the dispatch row as quick options and write through the
	// validated topology door (`#lib/api/npcLanes`, shared with the studio's NPC
	// lanes page), which either keeps the edit or refuses it and names why. The
	// same register as the row above: disabled with a reason when the read found
	// no engine (and when the flow has no lane step to configure at all).
	//
	// W9.30 §5.3 — the cast ladder's manual door. A member on the `actor`
	// provenance rung (admitted by an accepted cast proposal, the D79 door) can be
	// PROMOTED to a real studio actor persona card that outlives the chat. The
	// action reads its suggestion first (`GET .../cast/{member_id}/promotion`),
	// pre-fills the name, and writes through the persona registry
	// (`POST`, a studio-class write — no proposal, no decision row). The door never
	// refuses a NAME (duplicate persona names are legal; the owner typed it), so a
	// promoted member keeps the action, relabelled "promote again", next to a
	// `promoted` badge. Only eligibility is enforced, and the reason the engine
	// gives is shown verbatim.
	//
	// W10 — a row is a PERSONA NODE with three layers that must never be read as
	// each other (plan §2 "Cast model"):
	//
	//   * PARTICIPATION (the row's top line) — the roster's own facts: display
	//     name, aliases, provenance, tier and promotion badges, colour picker. The
	//     `/cast` read is its only source, so this layer is unchanged in meaning.
	//   * DEFINITION — a read-only `card` chip when the session tree found the card
	//     the member was seeded from, and NOTHING when they never had one: no
	//     filler and no door (this pane has no card editor, so the chip is an
	//     indicator only). A session node never looks like an alias of the persona
	//     card promotion mints — that link is the `promoted` badge, alone.
	//   * LIVE — one compact line: where they are, the top active condition, the
	//     body's visible surface, and whether they are in the scene. Empty parts
	//     are dropped, so a layer that knows nothing renders no line at all.
	//
	// The two extra layers come from `GET .../tree` (`#lib/api/hud`
	// `getCastTree`) joined onto the `/cast` rows by `participation.member_id` —
	// the tree's one identity field. The PLAYER is a node too: the tree's `Player`
	// row heads the same list, labelled `Player (<Name>)`, with the same live line.
	import { API_BASE, getChatConfig, getSpeakerColors } from '#lib/api';
	import {
		getCast,
		getCastTree,
		getPromotionOffer,
		promoteMember,
		type BeingLive,
		type CastRow,
		type CastTreeNode,
		type PlayerTreeNode
	} from '#lib/api/hud';
	import { setNpcDispatch, setUserAsActor, type NpcStrategy } from '#lib/api/personas';
	// Issue #56: the lane quick options write the turn flow's lane step, not a
	// session setting — the same door the studio's NPC lanes page uses.
	import {
		LANE_WIDTHS,
		laneConfigStore,
		laneRefusal,
		readLaneConfig,
		writeLaneConfig,
		type LaneConfig
	} from '#lib/api/npcLanes';
	import type { SpeakerColor } from '#lib/api';
	import { bumpSpeakerColors, type RegistryRow } from '#lib/speakerColorStore';

	let { sessionId = '' }: { sessionId?: string | null } = $props();

	//: Reserved near-white for the user character (cast_api.SPEAKER_USER_COLOR).
	const USER_COLOR = '#f2efe9';

	let cast = $state<CastRow[]>([]);
	//: W10 — the tree's cast nodes (definition + live), joined to the rows above by
	//: `member_id`, and the `Player` node that heads the list.
	let treeNodes = $state<CastTreeNode[]>([]);
	let playerNode = $state<PlayerTreeNode | null>(null);
	let loading = $state(false);
	let error = $state('');
	let savingName = $state<string | null>(null);
	let savedName = $state<string | null>(null);
	let saveError = $state('');

	//: W9.22 session setting (default OFF) + whether the config read reached the
	//: engine. The toggle is live only once the read landed: a flip the pane cannot
	//: persist must not look available, and "not read yet" is not "unreachable".
	let userAsActor = $state(false);
	let configState = $state<'loading' | 'ready' | 'unreachable'>('loading');
	let savingActor = $state(false);
	let actorError = $state('');

	//: W9.24 dispatch policy: the folded setting the engine will run the next turn
	//: with, plus whether a write is in flight (the controls disable together).
	let npcStrategy = $state<NpcStrategy>('decision');
	let npcMaxDispatch = $state(0);
	let savingDispatch = $state(false);
	let dispatchError = $state('');

	//: The number input's own draft: typing "1" before "2" must not send a PATCH
	//: per keystroke, and an empty field must not write 0 behind the owner's back.
	let maxDispatchDraft = $state('0');

	//: Issue #56 lane quick options. `laneState` is this pane's own read of the
	//: stored flow ('no-step' = the flow has no lane step, so there is nothing to
	//: configure); `laneConfig` is the stored value both controls show.
	let laneState = $state<'loading' | 'ready' | 'unreachable' | 'no-step'>('loading');
	let laneConfig = $state<LaneConfig | null>(null);
	let savingLane = $state(false);
	let laneError = $state('');
	let laneNote = $state('');
	/** The two controls' own state — see `applyLaneConfig` for why it is not derived. */
	let lanesOn = $state(true);
	let laneWidth = $state('4');
	/** A stored config's identity — the guard that keeps the shared store quiet. */
	let laneAppliedKey = '';

	//: W9.30 §5.3 — the manual promotion door. `promoteFor` is the member whose
	//: inline form is open (one at a time); the dialog's name draft is seeded from
	//: the GET's suggestion, which is why opening it is a read, not a guess.
	let promoteFor = $state<string | null>(null);
	let promoteName = $state('');
	let promoteReason = $state('');
	let promoteEligible = $state(true);
	let promoteLoading = $state(false);
	let promoteSaving = $state(false);
	let promoteError = $state('');
	/** The created card's name, kept visible after the form closes (role=status). */
	let promoteNotice = $state('');

	const configReachable = $derived(configState === 'ready');
	const toggleTitle = $derived(
		configState === 'loading'
			? 'Reading this chat’s config…'
			: configState === 'unreachable'
				? 'Engine unreachable — User as actor cannot be changed'
				: 'Make the player a cast actor in THIS chat: impersonation by consent, a first-class presence with a lane (default off)'
	);
	const strategyTitle = $derived(
		configState === 'loading'
			? 'Reading this chat’s config…'
			: configState === 'unreachable'
				? 'Engine unreachable — the dispatch policy cannot be changed'
				: 'Who gets a lane each turn: decision asks the decision model which cast members act this beat; random samples them from a log-seeded draw; lru keeps the presence ladder’s own order'
	);
	const maxDispatchTitle =
		'Most lanes one turn may run (0 = no bound). Orthogonal to the concurrency pool: this bounds the SET, that bounds how many run at once.';

	//: Issue #56: the lane quick options' reasons, in the same voice as the two
	//: titles above. A control this pane cannot write must not look editable.
	const lanesReachable = $derived(laneState === 'ready' && laneConfig !== null);
	const laneSwitchTitle = $derived(
		laneState === 'loading'
			? 'Reading the turn flow’s lane settings…'
			: laneState === 'unreachable'
				? 'Engine unreachable — the lane settings cannot be changed'
				: laneState === 'no-step'
					? 'This flow has no NPC lane step to switch on or off'
					: 'Switch lanes off: the narrator writes this scene alone'
	);
	const laneWidthTitle = $derived(
		laneState !== 'ready'
			? laneSwitchTitle
			: laneConfig && !laneConfig.enabled
				? 'Lanes are off — switch them on to choose how many run at once'
				: 'How many lanes may run at once (every lane still runs)'
	);

	const actorRows = $derived(cast.filter((row) => row.tier === 'actor'));
	const presenceRows = $derived(cast.filter((row) => row.tier !== 'actor'));
	//: `member_id` -> tree node: the ONE join all three layers share.
	const treeByMember = $derived(
		new Map<string, CastTreeNode>(treeNodes.map((node) => [node.member_id, node]))
	);
	//: With User-as-Actor ON the player arrives as an actor row (provenance
	//: `user`), so the standalone player chip would be the same identity twice.
	const playerInCast = $derived(cast.some((row) => row.provenance === 'user'));

	$effect(() => {
		const id = sessionId;
		cast = [];
		treeNodes = [];
		playerNode = null;
		error = '';
		savingName = null;
		savedName = null;
		saveError = '';
		userAsActor = false;
		configState = 'loading';
		savingActor = false;
		actorError = '';
		npcStrategy = 'decision';
		npcMaxDispatch = 0;
		maxDispatchDraft = '0';
		savingDispatch = false;
		dispatchError = '';
		laneState = 'loading';
		laneConfig = null;
		laneAppliedKey = '';
		lanesOn = true;
		laneWidth = '4';
		savingLane = false;
		laneError = '';
		laneNote = '';
		promoteFor = null;
		promoteName = '';
		promoteReason = '';
		promoteEligible = true;
		promoteLoading = false;
		promoteSaving = false;
		promoteError = '';
		promoteNotice = '';
		// Issue #56: the lane quick options are the turn flow's settings, not this
		// chat's, so they are read on mount and on every session switch alike —
		// an open session is not a precondition for showing or changing them.
		void loadLaneConfig();
		if (!id) return;
		void load(id);
	});

	async function load(id: string): Promise<void> {
		loading = true;
		try {
			const result = await getCast(id);
			if ('error' in result) {
				// Fallback to the colour registry alone (engine without /cast):
				// keep the registry rows that carry colours rather than go blank.
				// They carry no provenance, so the tier rule places them in the
				// collapsed presence group — never advertised as attached actors.
				const colors = await getSpeakerColors(id);
				if ('error' in colors) {
					error = colors.error;
				} else {
					cast = colors.speaker_colors.map((c: SpeakerColor) => ({
						member_id: null,
						name: c.name,
						color: c.color,
						assigned_by: c.assigned_by,
						from_card: false,
						provenance: 'unknown',
						status: 'active',
						provisional: false,
						aliases: [],
						promoted_persona_id: null,
						tier: 'presence'
					}));
				}
			} else {
				cast = result.cast;
				// The read is the engine's first-sighting capture site (it colours
				// every merged speaker), so republish what the transcript colours from.
				publishRegistry(id);
			}
		} finally {
			loading = false;
		}

		// W10: the tree adds the definition + live layers to the rows above, joined
		// by `member_id`, plus the `Player` node that heads the list. Best-effort —
		// a failed read (`error`) leaves the roster rendering exactly as before.
		const tree = await getCastTree(id);
		if (!('error' in tree)) {
			treeNodes = tree.cast;
			playerNode = tree.player;
		}

		// The User-as-Actor session setting off the ONE config read. Best-effort: an
		// unreachable engine leaves the toggle disabled with `configReachable=false`.
		try {
			const cfg = await getChatConfig(id);
			userAsActor = cfg.user_as_actor;
			npcStrategy = cfg.npc_strategy;
			npcMaxDispatch = cfg.npc_max_dispatch;
			maxDispatchDraft = String(cfg.npc_max_dispatch);
			configState = cfg.reachable ? 'ready' : 'unreachable';
		} catch {
			configState = 'unreachable';
		}
	}

	// --- Issue #56: the NPC lane quick options (on/off, how many at once) -----
	//
	// Both write the fan-out row of the stored topology through the validated
	// door (`#lib/api/npcLanes`), then show what the door says it kept: the
	// read-back is the save's own echo, so the control can never display an edit
	// the engine refused. The studio's NPC lanes page writes the same value, and both
	// render the shared `laneConfigStore`, so the two surfaces agree while both
	// are open.
	async function loadLaneConfig(): Promise<void> {
		laneState = 'loading';
		laneError = '';
		try {
			applyLaneConfig(await readLaneConfig());
		} catch {
			// Not a refusal, just no engine: the row says so in its own words and
			// the two controls render disabled — no error line to repeat it.
			laneState = 'unreachable';
		}
	}

	/** Set the two controls from a stored value (a read, or a save's echo). */
	function applyLaneConfig(next: LaneConfig | null): void {
		laneConfig = next;
		laneAppliedKey = next ? `${next.enabled}:${next.maxLanes}` : '';
		laneState = next ? 'ready' : 'no-step';
		// The controls keep their own state (below) so a REFUSED edit can be undone
		// on screen: a `checked={...}` bound straight to the stored value would be
		// "unchanged" from Svelte's point of view and leave the clicked box ticked.
		lanesOn = next?.enabled ?? true;
		laneWidth = String(next?.maxLanes ?? 4);
	}

	// Svelte 5: read the `$store` directly (`get(store)` inside an effect does not
	// track). The key guard drops the echo of this pane's own write.
	$effect(() => {
		const published = $laneConfigStore;
		if (!published || savingLane) return;
		const key = `${published.enabled}:${published.maxLanes}`;
		if (key === laneAppliedKey) return;
		applyLaneConfig(published);
		if (laneState !== 'unreachable') laneState = 'ready';
		laneNote = '';
	});

	// The lane settings belong to the turn flow, not to one chat: the session
	// effect above reads them on mount and on every session switch.

	/**
	 * Write one lane setting and read it back.
	 *
	 * The whole topology goes back (that is what the door takes), so a refused
	 * edit leaves the stored, compilable flow untouched — and the pane re-reads
	 * so the controls show the stored value rather than the edit that did not
	 * land. A violation is shown in the door's own words.
	 */
	async function saveLaneConfig(patch: { enabled?: boolean; maxLanes?: number }): Promise<void> {
		if (savingLane || laneState !== 'ready') return;
		savingLane = true;
		laneError = '';
		laneNote = '';
		try {
			applyLaneConfig(await writeLaneConfig(patch));
			laneNote = 'saved — applies to the next turn';
		} catch (err) {
			const refused = laneRefusal(err);
			// The refusal changed nothing: re-read so the controls show the stored
			// values, then keep the door's own words on screen.
			await loadLaneConfig();
			laneError = refused;
		} finally {
			savingLane = false;
		}
	}

	function toggleLanes(next: boolean): void {
		lanesOn = next;
		void saveLaneConfig({ enabled: next });
	}

	function chooseLaneWidth(raw: string): void {
		laneWidth = raw;
		const width = Number(raw);
		if (!Number.isFinite(width)) return;
		if (laneConfig && width === laneConfig.maxLanes) return;
		void saveLaneConfig({ maxLanes: width });
	}

	// The registry the transcript colours quotes from: the merged cast rows that
	// carry a hue. Published after a load (the engine's first-sighting assignment)
	// and after a confirmed PATCH, so a newly-coloured speaker repaints quotes that
	// are already rendered instead of waiting for the next turn or reload.
	function publishRegistry(id: string): void {
		const rows: RegistryRow[] = [];
		for (const row of cast) {
			if (row.color) {
				rows.push({
					member_id: row.member_id,
					name: row.name,
					color: row.color,
					assigned_by: row.assigned_by ?? 'engine'
				});
			}
		}
		bumpSpeakerColors(id, rows);
	}

	/** The row's provenance label: `card`/`world`/`actor`/`user`/`bts`/`registry`. */
	function roleLabel(row: CastRow): string {
		if (row.provenance && row.provenance !== 'unknown') return row.provenance;
		return row.from_card ? 'card' : 'registry';
	}

	/** The tree node for a roster row — the join is `member_id`, nothing else. */
	function nodeFor(row: CastRow): CastTreeNode | null {
		return row.member_id ? (treeByMember.get(row.member_id) ?? null) : null;
	}

	/** Presence as one word; `unknown` says nothing rather than guess. */
	function presenceLabel(presence: BeingLive['presence']): string {
		if (presence === 'in_scene') return 'in scene';
		if (presence === 'elsewhere') return 'elsewhere';
		if (presence === 'off_screen') return 'off screen';
		return '';
	}

	/**
	 * The live layer as line parts, in reading order: where, which condition, what
	 * is visibly wrong, and whether they are in the scene.
	 *
	 * Empty parts are dropped, so a layer the registry does not answer for renders
	 * NOTHING — no "nobody", no dash, and no dangling separator.
	 */
	function liveParts(live: BeingLive | null): string[] {
		if (!live) return [];
		return [
			live.location ?? '',
			live.condition ?? '',
			live.bodySurface ?? '',
			presenceLabel(live.presence)
		].filter((part) => part !== '');
	}

	async function toggleUserAsActor(next: boolean): Promise<void> {
		const id = sessionId;
		if (!id || savingActor) return;
		const previous = userAsActor;
		userAsActor = next; // optimistic: the checkbox reflects the click at once
		savingActor = true;
		actorError = '';
		try {
			// The engine echoes the folded value, so the checkbox shows what the log
			// now says rather than what we hoped it said.
			userAsActor = await setUserAsActor(id, next);
		} catch (err) {
			userAsActor = previous;
			actorError = err instanceof Error ? err.message : String(err);
		} finally {
			savingActor = false;
		}
	}

	async function chooseStrategy(next: NpcStrategy): Promise<void> {
		const id = sessionId;
		if (!id || savingDispatch) return;
		const previous = npcStrategy;
		npcStrategy = next; // optimistic, reverted if the engine refuses the write
		savingDispatch = true;
		dispatchError = '';
		try {
			// Per-key PATCH: the bound is not resent, so the engine keeps it.
			const folded = await setNpcDispatch(id, { strategy: next });
			npcStrategy = folded.npc_strategy;
			npcMaxDispatch = folded.npc_max_dispatch;
			maxDispatchDraft = String(folded.npc_max_dispatch);
		} catch (err) {
			npcStrategy = previous;
			dispatchError = err instanceof Error ? err.message : String(err);
		} finally {
			savingDispatch = false;
		}
	}

	/** Commit the number input on blur/Enter: whole numbers >= 0 only. */
	async function commitMaxDispatch(raw: string): Promise<void> {
		const id = sessionId;
		if (!id || savingDispatch) return;
		const parsed = Number.parseInt(raw.trim(), 10);
		const next = Number.isFinite(parsed) && parsed >= 0 ? parsed : npcMaxDispatch;
		maxDispatchDraft = String(next);
		if (next === npcMaxDispatch) return;
		savingDispatch = true;
		dispatchError = '';
		try {
			const folded = await setNpcDispatch(id, { maxDispatch: next });
			npcStrategy = folded.npc_strategy;
			npcMaxDispatch = folded.npc_max_dispatch;
			maxDispatchDraft = String(folded.npc_max_dispatch);
		} catch (err) {
			maxDispatchDraft = String(npcMaxDispatch);
			dispatchError = err instanceof Error ? err.message : String(err);
		} finally {
			savingDispatch = false;
		}
	}

	async function recolor(name: string, color: string): Promise<void> {
		const id = sessionId;
		if (!id || savingName) return;
		savingName = name;
		savedName = null;
		saveError = '';
		try {
			const res = await fetch(
				`${API_BASE}/api/sessions/${encodeURIComponent(id)}/speaker_colors`,
				{
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ name, color })
				}
			);
			if (!res.ok) throw new Error(`colour update failed (${res.status})`);
			const body = (await res.json()) as {
				speaker_colors?: Array<Partial<SpeakerColor>>;
			};
			const raw = Array.isArray(body.speaker_colors) ? body.speaker_colors : [];
			const registry: SpeakerColor[] = [];
			for (const row of raw) {
				if (row && typeof row.name === 'string' && typeof row.color === 'string') {
					registry.push({
						name: row.name,
						color: row.color,
						assigned_by: typeof row.assigned_by === 'string' ? row.assigned_by : 'user'
					});
				}
			}
			const byName = new Map(registry.map((row) => [row.name, row]));
			cast = cast.map((row) => {
				const updated = byName.get(row.name);
				return updated
					? { ...row, color: updated.color, assigned_by: updated.assigned_by }
					: row;
			});
			// The registry the engine just confirmed (latest-wins folded) — publish it
			// so the transcript repaints old quotes without waiting for a new turn.
			publishRegistry(id);
			savedName = name;
		} catch (err) {
			saveError = err instanceof Error ? err.message : String(err);
		} finally {
			savingName = null;
		}
	}

	// --- W9.30 §5.3: promote a session-cast member to an actor persona card ---
	//
	// The ladder's top rung, by the owner's own hand: a studio-class persona write
	// (no proposal, no decision row). Only members on the `actor` provenance rung
	// qualify — the D79 door's own admissions — and the action stays available
	// after a promotion, because the door never refuses a second card.

	/** Open the inline form for one member (a GET, so the name comes pre-filled). */
	async function openPromote(row: CastRow): Promise<void> {
		const id = sessionId;
		const memberId = row.member_id;
		if (!id || !memberId || promoteSaving) return;
		promoteFor = memberId;
		promoteName = '';
		promoteReason = '';
		promoteEligible = true;
		promoteError = '';
		promoteNotice = '';
		promoteLoading = true;
		try {
			const result = await getPromotionOffer(id, memberId);
			if (promoteFor !== memberId) return; // superseded by another open form
			if ('error' in result) {
				promoteError = result.error;
				return;
			}
			promoteName = result.offer.suggested_name;
			promoteEligible = result.offer.eligible;
			promoteReason = result.offer.reason;
		} finally {
			promoteLoading = false;
		}
	}

	function closePromote(): void {
		promoteFor = null;
		promoteName = '';
		promoteReason = '';
		promoteError = '';
	}

	/**
	 * Confirm the form: POST the (possibly edited) name.
	 *
	 * A blank field sends NO name, so the engine applies its own collision suffix;
	 * a typed name is written verbatim (a duplicate the owner typed is deliberate —
	 * never refused, never merged).
	 */
	async function confirmPromote(row: CastRow): Promise<void> {
		const id = sessionId;
		const memberId = row.member_id;
		if (!id || !memberId || promoteSaving) return;
		promoteSaving = true;
		promoteError = '';
		try {
			const result = await promoteMember(id, memberId, promoteName);
			if ('error' in result) {
				promoteError = result.error;
				return;
			}
			const created = result.persona;
			cast = cast.map((item) =>
				item.member_id === memberId
					? { ...item, promoted_persona_id: created.persona_id }
					: item
			);
			promoteNotice = `created persona “${created.name}”`;
			closePromote();
		} catch (err) {
			promoteError = err instanceof Error ? err.message : String(err);
		} finally {
			promoteSaving = false;
		}
	}
</script>

{#snippet liveLine(live: BeingLive | null)}
	{@const parts = liveParts(live)}
	{#if parts.length > 0}<span class="cast-live">{parts.join(' · ')}</span>{/if}
{/snippet}

{#snippet castRow(row: CastRow, node: CastTreeNode | null)}
	<li class="cast-row" class:cast-row-presence={row.tier === 'presence'}>
		<!-- Layer 1 — PARTICIPATION: the roster's own facts, unchanged in meaning. -->
		<div class="cast-participation">
			<span
				class="cast-swatch"
				class:cast-swatch-empty={!row.color}
				style="background:{row.color ?? 'transparent'}"
			></span>
			<span class="cast-name">
				{row.name}{#if row.aliases.length}<span class="cast-aliases">
						· previously {row.aliases.join(', ')}</span
					>{/if}
			</span>
			{#if row.tier === 'presence'}<span class="cast-badge">in scene</span>{/if}
			{#if row.provisional}<span class="cast-badge">provisional</span>{/if}
			{#if row.promoted_persona_id}
				<span
					class="cast-badge cast-badge-promoted"
					title="This member already has a studio persona card — the chat keeps the member; the card outlives it"
					>promoted</span
				>
			{/if}
			<span class="cast-role">
				{roleLabel(row)}{row.assigned_by ? ` · ${row.assigned_by}` : ''}
			</span>
			<input
				type="color"
				class="cast-picker"
				value={row.color ?? '#888888'}
				disabled={savingName === row.name}
				title="Reassign this speaker's colour"
				aria-label="Colour for {row.name}"
				onchange={(e) =>
					void recolor(row.name, (e.currentTarget as HTMLInputElement).value)}
			/>
			{#if row.provenance === 'actor' && row.member_id}
				<button
					type="button"
					class="cast-promote"
					disabled={promoteSaving}
					aria-label="Promote {row.name} to an actor persona"
					title="Mint a studio actor persona card from this cast member — the card outlives this chat"
					onclick={() => void openPromote(row)}
				>
					{row.promoted_persona_id ? 'promote again' : 'promote to actor persona'}
				</button>
			{/if}
			{#if savedName === row.name}<span class="cast-ok">saved</span>{/if}
		</div>
		<!-- Layers 2 + 3 — DEFINITION (read-only card chip; nothing when the person
		     never had a card) and LIVE (where/what/whether, empty parts dropped). -->
		{#if node?.definition || node?.live}
			<div class="cast-layers">
				{#if node?.definition}
					<span
						class="cast-definition"
						title="Seeded from the “{node.definition.name}” card — a definition, not this chat’s live state"
						>card</span
					>
				{/if}
				{@render liveLine(node?.live ?? null)}
			</div>
		{/if}
		{#if row.member_id && promoteFor === row.member_id}
			<!-- W9.30 §5.3: the manual door's dialog, inline on the row it acts on. -->
			<div
				class="cast-promote-form"
				role="dialog"
				aria-label="Promote {row.name} to an actor persona"
			>
				{#if promoteLoading}
					<span class="cast-role">reading the suggestion…</span>
				{:else}
					<label class="cast-promote-field">
						<span>name</span>
						<input
							type="text"
							value={promoteName}
							disabled={promoteSaving}
							aria-label="Persona name for {row.name}"
							oninput={(e) => (promoteName = e.currentTarget.value)}
						/>
					</label>
					<button
						type="button"
						class="cast-promote-confirm"
						disabled={promoteSaving || !promoteEligible}
						aria-label="Create persona for {row.name}"
						onclick={() => void confirmPromote(row)}
					>
						{promoteSaving ? 'creating…' : 'create persona'}
					</button>
					<button
						type="button"
						class="cast-promote-cancel"
						disabled={promoteSaving}
						aria-label="Cancel promoting {row.name}"
						onclick={closePromote}>cancel</button
					>
				{/if}
				{#if promoteReason}<span class="cast-role">{promoteReason}</span>{/if}
			</div>
			{#if promoteError}
				<p class="hud-note hud-note-error" role="alert">{promoteError}</p>
			{/if}
		{/if}
	</li>
{/snippet}

<div class="cast-pane">
	<div class="cast-head">
		<span class="cast-title">Cast</span>
		<label
			class="cast-toggle"
			class:cast-toggle-off={!configReachable}
			title={toggleTitle}
		>
			<input
				type="checkbox"
				checked={userAsActor}
				disabled={!configReachable || savingActor || !sessionId}
				onchange={(e) => void toggleUserAsActor(e.currentTarget.checked)}
			/>
			<span>User as actor</span>
		</label>
	</div>

	<!-- W9.24 §3.5: the NPC dispatch policy — a SESSION setting (one latest-wins
	     event), so it sits above the cast list rather than on any row. -->
	<div class="cast-policy">
		<label class="cast-policy-field" title={strategyTitle}>
			<span>lanes</span>
			<select
				value={npcStrategy}
				disabled={!configReachable || savingDispatch || !sessionId}
				aria-label="NPC lane dispatch strategy"
				onchange={(e) => void chooseStrategy(e.currentTarget.value as NpcStrategy)}
			>
				<option value="decision">decision</option>
				<option value="random">random</option>
				<option value="lru">lru</option>
			</select>
		</label>
		<label class="cast-policy-field" title={maxDispatchTitle}>
			<span>max</span>
			<input
				type="number"
				min="0"
				step="1"
				value={maxDispatchDraft}
				disabled={!configReachable || savingDispatch || !sessionId}
				aria-label="Maximum lanes per turn (0 = no bound)"
				oninput={(e) => (maxDispatchDraft = e.currentTarget.value)}
				onchange={(e) => void commitMaxDispatch(e.currentTarget.value)}
			/>
		</label>
		<span class="cast-policy-hint">0 = no bound</span>
	</div>

	<!-- Issue #56: lanes themselves, beside the dispatch row — on/off and how
	     many run at once. These are the turn flow's own settings (not this
	     chat's), so the copy says so; a control the pane cannot write (no engine,
	     or a flow with no lane step) renders disabled with the reason in its
	     tooltip. -->
	<div class="cast-policy cast-lane-policy" class:cast-policy-off={!lanesReachable}>
		<label class="cast-policy-field" title={laneSwitchTitle}>
			<input
				type="checkbox"
				checked={lanesOn}
				disabled={!lanesReachable || savingLane}
				aria-label="NPC lanes on or off"
				data-lane-enabled={lanesOn ? 'on' : 'off'}
				onchange={(e) => toggleLanes(e.currentTarget.checked)}
			/>
			<span>NPC lanes</span>
		</label>
		<label class="cast-policy-field" title={laneWidthTitle}>
			<select
				value={laneWidth}
				disabled={!lanesReachable || savingLane || !lanesOn}
				aria-label="Lanes running at once"
				onchange={(e) => chooseLaneWidth(e.currentTarget.value)}
			>
				{#each LANE_WIDTHS as width (width)}
					<option value={String(width)}>{width}</option>
				{/each}
			</select>
			<span>at once</span>
		</label>
		{#if laneState === 'loading'}
			<span class="cast-policy-hint">reading…</span>
		{:else if laneState === 'no-step'}
			<span class="cast-policy-hint">no lane step in this flow</span>
		{:else if laneState === 'unreachable'}
			<span class="cast-policy-hint">engine unreachable</span>
		{:else if !lanesOn}
			<span class="cast-policy-hint">the narrator writes this scene alone</span>
		{:else if laneNote}
			<span class="cast-policy-hint">{laneNote}</span>
		{/if}
	</div>

	{#if error}
		<p class="hud-note hud-note-error">{error}</p>
	{:else if loading}
		<p class="hud-note">loading…</p>
	{:else}
		{#if actorRows.length > 0 || (!!playerNode && !playerInCast)}
			<ul class="cast-list">
			<!-- W10: the player is a NODE, not a special case — the tree's `Player` row with
			     the same participation display and the same live line, heading the list.
			     This pane has no ✎ edit door, so there is none to route here. Hidden only
			     while User-as-Actor has the player arriving as a `user` cast row already. -->
			{#if playerNode && !playerInCast}
				<li
					class="cast-row cast-player"
					title="Your character — tinted by role, never attributed"
				>
					<div class="cast-participation">
						<span class="cast-swatch" style="background:{USER_COLOR}"></span>
						<span class="cast-name">{playerNode.display}</span>
						<span class="cast-role">player{userAsActor ? ' · actor' : ''}</span>
					</div>
					{#if playerNode.live}
						<div class="cast-layers">{@render liveLine(playerNode.live)}</div>
					{/if}
				</li>
			{/if}
			{#each actorRows as row (row.member_id ?? row.name)}
				{@render castRow(row, nodeFor(row))}
			{/each}
			</ul>
		{/if}

		{#if actorRows.length === 0}
			<p class="hud-note">
				No actors attached — import a character card or attach a world with a cast.
			</p>
		{/if}

		<!-- W9.30 §5.3: the manual door's confirmation, kept visible after the form
		     closes so the created card's name is not a flash the owner missed. -->
		{#if promoteNotice}
			<p class="cast-ok cast-notice" role="status">{promoteNotice}</p>
		{/if}

		{#if presenceRows.length > 0}
			<details class="cast-presence">
				<summary>
					in scene <span class="cast-count">{presenceRows.length}</span>
					<span class="cast-hint">tracked, not cast — no lanes</span>
				</summary>
				<ul class="cast-list">
					{#each presenceRows as row (row.member_id ?? row.name)}
						{@render castRow(row, nodeFor(row))}
					{/each}
				</ul>
			</details>
		{/if}

		{#if saveError}<p class="hud-note hud-note-error">{saveError}</p>{/if}
		{#if actorError}<p class="hud-note hud-note-error">{actorError}</p>{/if}
		{#if dispatchError}<p class="hud-note hud-note-error">{dispatchError}</p>{/if}
		{#if laneError}<p class="hud-note hud-note-error">{laneError}</p>{/if}
		<p class="hud-note">
			Actors get their own lanes and colours. “In scene” is worked out from where everyone is in
			the story — a tracked member is never given a lane. Colours you pick here win over the
			engine's. A cast member admitted from a proposal can be promoted to a studio persona card,
			which outlives this chat.
		</p>
	{/if}
</div>

<style>
	.cast-pane {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.cast-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding-bottom: 0.25rem;
		border-bottom: 1px solid var(--line);
	}
	.cast-title {
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	.cast-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.3rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
		cursor: pointer;
		white-space: nowrap;
	}
	.cast-toggle input {
		margin: 0;
		cursor: inherit;
	}
	.cast-toggle-off {
		color: var(--text-dim);
		cursor: not-allowed;
	}
	.cast-toggle-off input {
		cursor: not-allowed;
	}
	.cast-policy {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.cast-policy-field {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		white-space: nowrap;
	}
	.cast-policy-field select,
	.cast-policy-field input {
		font-size: var(--font-micro);
		color: var(--text);
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.1rem 0.25rem;
		min-width: 3.2rem;
	}
	.cast-policy-field select:disabled,
	.cast-policy-field input:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.cast-policy-hint {
		color: var(--text-dim);
		opacity: 0.8;
	}
	/* Issue #56: the lane quick options read quieter than the dispatch row while
	   they are not writable (no engine, no lane step in the flow). */
	.cast-policy-off {
		opacity: 0.6;
	}
	.cast-lane-policy {
		flex-wrap: wrap;
	}
	.cast-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
	}
	.cast-row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		padding: 0.3rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		font-size: var(--font-small);
	}
	/* Presence tier reads as PROVISIONAL, never as cast: dimmer, dashed edge. */
	.cast-row-presence {
		opacity: 0.72;
		border-style: dashed;
		background: transparent;
	}
	/* The three layers of one person: participation on its own line, then the
	   definition chip + live summary together under the name (past the swatch). */
	.cast-participation {
		flex: 1 0 100%;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	.cast-layers {
		flex: 1 0 100%;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-wrap: wrap;
		padding-left: calc(0.85rem + 0.5rem);
	}
	/* The definition layer: a READ-ONLY pointer to the card the member was seeded
	   from — square-cornered so it never reads as one of the participation badges. */
	.cast-definition {
		flex: none;
		font-size: var(--font-micro);
		color: var(--text-muted);
		border: 1px solid var(--line-strong);
		border-radius: 4px;
		padding: 0 0.3rem;
		letter-spacing: 0.02em;
	}
	/* The live layer: quieter than the name, and never coloured like the hue swatch. */
	.cast-live {
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.cast-player {
		background: transparent;
	}
	.cast-presence {
		border-top: 1px solid var(--line);
		padding-top: 0.4rem;
	}
	.cast-presence summary {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		font-size: var(--font-micro);
		color: var(--text-dim);
		cursor: pointer;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.cast-count {
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 0 0.35rem;
	}
	.cast-hint {
		text-transform: none;
		letter-spacing: normal;
		opacity: 0.8;
	}
	.cast-swatch {
		width: 0.85rem;
		height: 0.85rem;
		border-radius: 4px;
		border: 1px solid var(--line-strong);
		flex: none;
	}
	.cast-swatch-empty {
		background: repeating-linear-gradient(
			45deg,
			var(--bg-inset),
			var(--bg-inset) 3px,
			var(--line) 3px,
			var(--line) 6px
		);
	}
	.cast-name {
		flex: 1 1 auto;
		min-width: 0;
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.cast-aliases {
		color: var(--text-dim);
		font-size: var(--font-micro);
	}
	.cast-badge {
		flex: none;
		font-size: var(--font-micro);
		color: var(--text-dim);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 0 0.35rem;
		white-space: nowrap;
	}
	/* The ladder's top rung already reached: the door stays open (never refused). */
	.cast-badge-promoted {
		color: var(--text);
		border-color: var(--line-strong);
	}
	.cast-promote {
		flex: none;
		padding: 0.1rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
		white-space: nowrap;
	}
	.cast-promote:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	/* The inline dialog owns a full row under the member it acts on. */
	.cast-promote-form {
		flex: 1 0 100%;
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex-wrap: wrap;
		padding-top: 0.3rem;
		border-top: 1px dashed var(--line);
	}
	.cast-promote-field {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.cast-promote-field input {
		width: 9rem;
		font-size: var(--font-micro);
		color: var(--text);
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.1rem 0.25rem;
	}
	.cast-promote-form button {
		padding: 0.1rem 0.45rem;
		border-radius: 6px;
		border: 1px solid var(--line-strong);
		background: transparent;
		color: var(--text-muted);
		font-size: var(--font-micro);
		cursor: pointer;
	}
	.cast-promote-form button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.cast-notice {
		margin: 0;
	}
	.cast-role {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.cast-picker {
		width: 1.6rem;
		height: 1.4rem;
		padding: 0;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-inset);
		cursor: pointer;
	}
	.cast-picker:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.cast-ok {
		font-size: var(--font-micro);
		color: var(--ok);
	}
</style>
