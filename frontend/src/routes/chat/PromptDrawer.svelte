<script lang="ts">
	// Prompt Manager drawer (D18 UI): preset selector, drag-drop ordered module
	// list with editors + a variable inserter, prompt overrides, agent bindings
	// and a live preview. The prompt pipeline is DATA (PLAN D18/D19), so this
	// component owns only draft state and API calls; the backend is the source
	// of truth and the drawer re-reads it after every mutation.
	//
	// Native HTML5 drag-and-drop is the primary reorder interaction. Event flow
	// per MDN ("HTML Drag and Drop API"): `dragstart` marks the source and puts
	// a payload on the dataTransfer; `dragover` MUST call preventDefault() to
	// declare the drop zone valid; `drop` reads the source and performs the move;
	// `dragend` always fires last and clears highlight state. Up/down buttons are
	// the keyboard/a11y fallback.
	import { onMount } from 'svelte';
	import {
		activatePreset,
		commitDefaultPreset,
		deactivatePreset,
		getActivePreset,
		getDefaultPromptStatus,
		getSessionModules,
		getSettings,
		listPresets,
		listPromptVariables,
		patchFeatures,
		putSessionModule,
		previewPrompt,
		PROMPT_FLOWS,
		resetDefaultPreset,
		seedFromDefault,
		updatePreset,
		type DefaultPromptStatus,
		type PromptFlow,
		type PromptHistorySurfaces,
		type PromptModule,
		type PromptModuleType,
		type PromptPreset,
		type PromptPreview,
		type PromptRecalledSurfaces,
		type PromptRegexRow,
		type PromptRole,
		type PromptSurfaces,
		type PromptVariableSpec,
		type SessionModule,
		type SettingsCatalog
	} from '#lib/api';
	// New-in-W3 prompt-client surface. The barrel (`src/lib/api.ts`) is not in
	// this lane's owned files, so these import the module directly — the same
	// pattern `Composer.svelte` uses for `#lib/api/imagegen`.
	import {
		exportPreset,
		getFactoryDirectives,
		importPreset,
		listPromptSteps,
		type PromptDirective,
		type PromptStep
	} from '#lib/api/prompts';
	import { register } from '#lib/uiLayers';
	import { reorder } from './promptSort';
	import StepList, { type StepRowView } from './prompt/StepList.svelte';
	import StepEditor, { sourceBadge } from './prompt/StepEditor.svelte';
	import PromptBlockEditor from './prompt/PromptBlockEditor.svelte';
	import RulesSection from './prompt/RulesSection.svelte';
	import AdvancedSection from './prompt/AdvancedSection.svelte';

	let {
		sessionId = null,
		initialView = 'steps',
		initialStepId = null,
		onClose
	}: {
		sessionId: string | null;
		initialView?: string;
		initialStepId?: string | null;
		onClose: () => void;
	} = $props();

	/** Preset select key of the persisted default pipeline row. The row IS a real
	 * preset (`DEFAULT_PRESET_ID='default'`), so the select shows exactly ONE
	 * default entry — the duplicate `__default__` sentinel is gone. */
	const DEFAULT_KEY = 'default';

	/** Step opened when none is selected. */
	const DEFAULT_STEP: PromptFlow = 'narrate';

	/** The three drawer views: the STEPS list, the RULES catalog (narrator
	 * directives) and ADVANCED token text (the old Surfaces tab). */
	type ViewKey = 'steps' | 'rules' | 'advanced';

	/** The 15 code directive ids, used only when the settings catalog is
	 * unreachable (the live list comes from `GET /api/settings`). */
	const FALLBACK_DIRECTIVE_IDS = [
		'director',
		'story_strings',
		'perspective',
		'no_user_control',
		'npc_cognition',
		'stop_and_pass',
		'length_control',
		'no_parroting',
		'anti_slop',
		'panel_placement',
		'visual_panels',
		'world_facts',
		'scene_images',
		'hard_prohibition',
		'ooc'
	];

	/** D18 interaction flows. The steps list edits one flow at a time; the single
	 * Save persists the whole preset (every flow). `narrate` is the canonical
	 * flow — its module list also mirrors the legacy `modules` field. */
	const FLOWS: readonly PromptFlow[] = PROMPT_FLOWS;
	/** Plain-language step descriptions — the DEGRADE titles used when
	 * `GET /api/prompts/steps` is unreachable (the live titles/blurbs/triggers
	 * come from `fabulist.prompts.catalog` through that endpoint). */
	const FLOW_TITLES: Record<PromptFlow, string> = {
		narrate: 'narrator — the prose turn',
		ooc: 'out-of-character directive',
		extract: 'state/fact extraction',
		summarize: 'history compaction',
		caption: 'image captioning',
		assistant: 'assistant consult',
		bts: 'world-state tracker',
		critic: 'critic — the post-turn quality gate',
		npc: 'NPC intent lane',
		planner: 'story planner',
		proposals: 'asset proposal lanes'
	};
	/** Structural inserter types valid for each flow (mirrors the backend
	 * `modules.FLOW_STRUCTURAL_TYPES`; the live value rides the steps endpoint). */
	const FLOW_STRUCTURAL_TYPES: Record<PromptFlow, PromptModuleType[]> = {
		narrate: ['narrator_system', 'library', 'history', 'facts', 'recalled', 'user'],
		ooc: ['system', 'history', 'facts', 'user'],
		extract: ['system', 'turn_payload'],
		summarize: ['system', 'payload'],
		caption: ['system', 'payload'],
		assistant: ['system', 'history', 'user'],
		bts: ['system', 'payload'],
		critic: ['system', 'payload'],
		npc: ['system', 'payload'],
		planner: ['system'],
		proposals: ['system', 'payload']
	};

	/** Every module type the wire accepts (used to filter the steps endpoint's
	 * `structural_types` down to known values). */
	const MODULE_TYPES: readonly PromptModuleType[] = [
		'text',
		'narrator_system',
		'library',
		'history',
		'facts',
		'user',
		'recalled',
		'system',
		'turn_payload',
		'payload'
	];

	/** Interaction types bound to agents (PLAN D19). Mirrors the engine's
	 * `fabulist.prompts.agent_sets.INTERACTIONS` (Wave 6 added bts;
	 * attribution left with its model flow — deterministic engine code now).
	 * The live list rides `GET /api/prompts/steps`; this is the DEGRADE order. */
	const INTERACTIONS = [
		'narrate',
		'ooc',
		'extract',
		'summarize',
		'caption',
		'assistant',
		'bts',
		'critic'
	] as const;
	/** Known default agent per interaction, used as the seed when no binding is set. */
	const AGENT_DEFAULTS: Record<string, string> = {
		narrate: 'narrator',
		ooc: 'narrator',
		extract: 'extractor',
		summarize: 'summarizer',
		caption: 'captioner',
		assistant: 'narrator',
		bts: 'extractor',
		critic: 'extractor'
	};
	/** Fixed agent list offered by the inline rebind select (a custom binding is
	 * still listed as an extra option when a preset carries one). */
	const KNOWN_AGENTS = ['narrator', 'extractor', 'summarizer', 'captioner', 'assistant', 'oracle'];

	const VAR_RE = /\{\{\s*([\w.]+)\s*\}\}/g;

	let presets = $state<PromptPreset[]>([]);
	let variables = $state<PromptVariableSpec[]>([]);
	let activeId = $state<string | null>(null);
	let selectedKey = $state<string>(DEFAULT_KEY);
	let draft = $state<PromptPreset | null>(null);
	let baseline = $state<PromptPreset | null>(null);
	/** The persisted default pipeline row (D18 inheritance source + the DEFAULT
	 * view). It is directly editable and saved in place (D26). */
	let defaultPreset = $state<PromptPreset | null>(null);
	/** D26 factory status for the default row (drives the "factory changed"
	 * notice + the commit/reset toolbar). */
	let defaultStatus = $state<DefaultPromptStatus | null>(null);

	/** The step catalog (`GET /api/prompts/steps`): display meta + defaults. */
	let steps = $state<PromptStep[]>([]);
	/** The step whose StepEditor is open (null = the steps list). */
	let openStepId = $state<string | null>(null);
	/** The block open in the full-screen PromptBlockEditor. */
	let editingBlock = $state<PromptModule | null>(null);

	/** Active VIEW: the steps list, the RULES catalog, or ADVANCED token text. */
	// W6.6/D2: the ⚙ header shortcut opens straight on Rules; coerce once.
	// Intentional initial-value capture — the drawer mounts per open ({#if}).
	// svelte-ignore state_referenced_locally
	const _initialView: ViewKey = (['steps', 'rules', 'advanced'] as ViewKey[]).includes(
		initialView as ViewKey
	)
		? (initialView as ViewKey)
		: 'steps';
	let activeView = $state<ViewKey>(_initialView);
	// W9.2: a flow-graph door may name the step to open directly.
	// svelte-ignore state_referenced_locally
	const _initialStepId: string | null =
		typeof initialStepId === 'string' && initialStepId ? initialStepId : null;
	/** Directive id -> label catalog (from `GET /api/settings`). Only the
	 * degrade display when the factory row fetch is unavailable. */
	let directiveCatalog = $state<{ id: string; label: string }[]>([]);
	/** Factory directive rows (the W3 materialization source), fetched lazily
	 * once and cached for the drawer's lifetime. */
	let factoryDirectives = $state<PromptDirective[]>([]);
	/** Set when the factory-row fetch failed (degrade + retry hint). */
	let factoryDirectivesError = $state('');
	/** In-flight factory-row fetch (concurrent mutations share one request). */
	let factoryDirectivesPending: Promise<PromptDirective[]> | null = null;
	/** Issue #39: the persisted Anti Injection product setting (engine FEATURE
	 * flag, default OFF). Folded from `GET /api/settings`; written through
	 * `PATCH {features}` — the engine reads it per prompt build, so a flip
	 * applies to the next turn with no restart. */
	let antiInjection = $state(false);
	let antiInjectionBusy = $state(false);
	let antiInjectionError = $state('');
	/** Directive row whose editor is open. */
	let editingDirectiveId = $state<string | null>(null);
	/** Commit/reset action state + result toast. */
	let actionBusy = $state(false);
	let surfacesStatus = $state('');

	let loading = $state(true);
	let saving = $state(false);
	let busy = $state(false);
	let previewBusy = $state(false);
	let error = $state('');
	let status = $state('');
	let previewError = $state('');

	let dragFrom = $state<number | null>(null);
	let dragOverIndex = $state<number | null>(null);

	/** Interaction previewed in the render pane (defaults to the first step). */
	let previewInteraction = $state<string>(DEFAULT_STEP);

	let previewSession = $state('');
	let previewText = $state('');
	let preview = $state<PromptPreview | null>(null);

	// Wave B (D29): session-scoped module overrides seeded from the imported card.
	// These ride the CHAT (session events), never the shared preset, so a card
	// import cannot mutate preset state. Edit/enable state is append-only.
	let sessionModules = $state<SessionModule[]>([]);
	let sessionModulesError = $state('');
	let moduleBusyId = $state<string | null>(null);
	let editingModuleId = $state<string | null>(null);
	let moduleDraft = $state('');
	let sessionModulesSig = '';

	$effect(() => {
		const id = sessionId;
		if (!id || id === sessionModulesSig) return;
		sessionModulesSig = id;
		void loadSessionModules(id);
	});

	// The factory directive rows back the RULES view's inherited rows and every
	// first edit (materialization); fetch lazily the first time the view opens,
	// cache for the drawer's lifetime.
	$effect(() => {
		if (activeView === 'rules') void ensureFactoryDirectives();
	});

	async function loadSessionModules(id: string): Promise<void> {
		try {
			sessionModules = await getSessionModules(id);
			sessionModulesError = '';
		} catch (err) {
			sessionModules = [];
			sessionModulesError = message(err);
		}
	}

	function upsertSessionModule(saved: SessionModule): void {
		const index = sessionModules.findIndex((m) => m.module_id === saved.module_id);
		if (index >= 0) sessionModules[index] = saved;
		else sessionModules = [...sessionModules, saved];
	}

	async function toggleSessionModule(module: SessionModule): Promise<void> {
		if (!sessionId || moduleBusyId) return;
		moduleBusyId = module.module_id;
		try {
			const saved = await putSessionModule(sessionId, { ...module, enabled: !module.enabled });
			upsertSessionModule(saved);
			sessionModulesError = '';
		} catch (err) {
			sessionModulesError = message(err);
		} finally {
			moduleBusyId = null;
		}
	}

	function startModuleEdit(module: SessionModule): void {
		editingModuleId = module.module_id;
		moduleDraft = module.content;
	}

	async function saveModuleEdit(module: SessionModule): Promise<void> {
		if (!sessionId || moduleBusyId) return;
		moduleBusyId = module.module_id;
		try {
			const saved = await putSessionModule(sessionId, { ...module, content: moduleDraft });
			upsertSessionModule(saved);
			editingModuleId = null;
			sessionModulesError = '';
		} catch (err) {
			sessionModulesError = message(err);
		} finally {
			moduleBusyId = null;
		}
	}

	/** The last focused editor; variable inserts splice at its cursor. */
	let activeField: {
		el: HTMLInputElement | HTMLTextAreaElement;
		apply: (value: string) => void;
	} | null = null;

	const dirty = $derived(
		draft !== null && baseline !== null && serialize(draft) !== serialize(baseline)
	);

	const knownNames = $derived(variables.map((v) => v.name).join(', ') || 'none');

	/** Unknown `{{name}}` tokens stay literal — surface them so they are visible. */
	const unknownVars = $derived.by((): string[] => {
		if (!draft) return [];
		const known = new Set(variables.map((v) => v.name));
		const found = new Set<string>();
		const scan = (text: string): void => {
			for (const match of text.matchAll(VAR_RE)) {
				if (!known.has(match[1])) found.add(match[1]);
			}
		};
		for (const modules of Object.values(draft.flows ?? {})) {
			for (const module of modules) scan(module.content);
		}
		for (const module of draft.modules) scan(module.content);
		return [...found];
	});

	/** What the shell renders: the editable draft when one is selected, else the
	 * persisted default pipeline row. */
	const displayPreset = $derived(draft ?? defaultPreset);

	/** Presets offered by the select: every named preset EXCEPT the persisted
	 * default row, which the single "Default pipeline" option owns. */
	const selectablePresets = $derived(presets.filter((p) => p.preset_id !== DEFAULT_KEY));

	/** True when a string is one of the engine's interaction flows. */
	function isPromptFlow(id: string): id is PromptFlow {
		return (PROMPT_FLOWS as readonly string[]).includes(id);
	}

	/** The step catalog: the live endpoint when it answered, else the local
	 * degrade rows (same ids, generic descriptions) so the drawer still works. */
	const stepCatalog = $derived.by((): PromptStep[] => {
		const live = steps.filter((step) => isPromptFlow(step.id));
		if (live.length > 0) return live;
		return INTERACTIONS.map((id) => ({
			id,
			title: FLOW_TITLES[id] ?? id,
			blurb: FLOW_TITLES[id] ?? '',
			trigger: '',
			default_agent: AGENT_DEFAULTS[id] ?? null,
			bound_agent: null,
			structural_types: FLOW_STRUCTURAL_TYPES[id] ?? [],
			block_count: 0,
			enabled: true,
			system_source: 'engine' as const
		}));
	});

	/** One resolved step row: catalog display meta + the DRAFT's own state, so an
	 * edit (rebind, enable, add block) reads back here immediately. */
	const stepRows = $derived.by((): StepRowView[] => {
		const preset = displayPreset;
		return stepCatalog.map((step) => {
			const flow = step.id as PromptFlow;
			const rows = preset ? flowModules(preset, flow) : [];
			return {
				id: step.id,
				title: step.title,
				blurb: step.blurb,
				trigger: step.trigger,
				defaultAgent: step.default_agent ?? AGENT_DEFAULTS[step.id] ?? '',
				boundAgent: preset?.agent_bindings[step.id] ?? '',
				blockCount: rows.length,
				enabled: rows.some((m) => m.enabled),
				selected: step.id === openStepId
			};
		});
	});

	/** The open step's catalog row (null while the list is showing). */
	const openStep = $derived(stepCatalog.find((step) => step.id === openStepId) ?? null);

	/** The open step's blocks (inherited default flow when the preset has none). */
	const openStepModules = $derived(
		openStepId && displayPreset ? flowModules(displayPreset, openStepId as PromptFlow) : []
	);

	/** The open step's flow is still the default pipeline's list. */
	const openStepInherited = $derived(
		displayPreset !== null && openStepId !== null && !flowIsCustomized(displayPreset, openStepId as PromptFlow)
	);

	/** Whether the open step's `system` block carries authored text (the engine
	 * compiles its own default prompt when the body is empty). Computed from the
	 * draft so a typed override shows up immediately. */
	const openStepSystemSource = $derived(
		openStepModules.some((m) => m.type === 'system' && m.content.trim() !== '')
			? ('authored' as const)
			: ('engine' as const)
	);

	/** Structural block types the open step accepts (live contract, local fallback). */
	const openStepStructural = $derived.by((): PromptModuleType[] => {
		const known = new Set<string>(MODULE_TYPES);
		const live = (openStep?.structural_types ?? []).filter((t) => known.has(t));
		if (live.length > 0) return live as PromptModuleType[];
		return openStepId ? (FLOW_STRUCTURAL_TYPES[openStepId as PromptFlow] ?? []) : [];
	});

	/** Source badge of the block open in the overlay (resolved by `sourceBadge`). */
	const editingBlockSource = $derived(editingBlock ? sourceBadge(editingBlock) : null);

	/** The selected draft is the persisted default row (D26): edits save in place,
	 * and the commit/reset toolbar applies to it. */
	const editingDefault = $derived(draft !== null && draft.preset_id === DEFAULT_KEY);
	/** D27 surfaces of the selected preset (always an object for safe reads). */
	const surfaces = $derived<PromptSurfaces>(draft?.surfaces ?? {});
	const regexRows = $derived<PromptRegexRow[]>(surfaces.regexes ?? []);
	const historyKnobs = $derived<PromptHistorySurfaces>(surfaces.history ?? {});
	const directiveOverrides = $derived<Record<string, string>>(surfaces.directives ?? {});
	const libraryDocChars = $derived((surfaces.library_doc ?? '').length);

	/** Directive rows to display (W3): the preset's own list when it carries
	 * one, else the INHERITED factory catalog (read-only until an edit
	 * materializes it — the same inheritance pattern as the flows). */
	const directiveRows = $derived.by((): PromptDirective[] => {
		const own = draft?.directives ?? [];
		if (own.length > 0) return own;
		return factoryDirectives;
	});

	/** The preset stores NO directive rows — the list shown is inherited. */
	const directivesInherited = $derived((draft?.directives ?? []).length === 0);

	/** Legacy `surfaces.directives` text overrides still awaiting migration. */
	const legacyDirectiveOverrideCount = $derived(Object.keys(directiveOverrides).length);

	/** Unknown `{{name}}` tokens in the preset's directive rows (W3 rows render
	 * through the same variable pipeline as the legacy map they replace). */
	const unknownDirectiveVars = $derived.by((): string[] => {
		const known = new Set(variables.map((v) => v.name));
		const found = new Set<string>();
		for (const row of draft?.directives ?? []) {
			for (const match of row.content.matchAll(VAR_RE)) {
				if (!known.has(match[1])) found.add(match[1]);
			}
		}
		return [...found];
	});

	/** Unknown `{{name}}` tokens in the surfaces block (union of all text fields). */
	const unknownSurfaceVars = $derived.by((): string[] => {
		const known = new Set(variables.map((v) => v.name));
		const found = new Set<string>();
		const scan = (text: string | undefined): void => {
			if (!text) return;
			for (const match of text.matchAll(VAR_RE)) if (!known.has(match[1])) found.add(match[1]);
		};
		scan(surfaces.library_doc);
		scan(surfaces.recalled?.framing);
		scan(surfaces.recalled?.open);
		scan(surfaces.recalled?.close);
		for (const value of Object.values(surfaces.recalled?.headers ?? {})) scan(value);
		for (const value of Object.values(directiveOverrides)) scan(value);
		return [...found];
	});

	/** Activate/Deactivate label follows the active id. */
	const isActive = $derived(draft !== null && activeId === draft.preset_id);

	onMount(() => {
		// Seed the preview field from the current chat once mounted (a prop read
		// inside a `$state` initializer only captures the initial value).
		previewSession = sessionId ?? '';
		void load().then(() => {
			// W9.2: a flow-graph door names its step — open it once the boot
			// selection has landed (selectKey keeps the view per keepView).
			if (_initialStepId) selectStep(_initialStepId);
		});
		// Escape is owned by the uiLayers surface stack (D51 §2.4): this drawer
		// registers while mounted, so only the TOP surface closes — no pile-up with
		// the studio open underneath. The dirty guard in `requestClose` still applies.
		return register('prompts', () => requestClose());
	});

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	/** Stable serialization for dirty comparison (maps key-sorted, modules ordered).
	 * The D27 surfaces block is canonicalized separately: directive/header keys are
	 * order-insensitive, so an untouched round-trip never reads as dirty. The W3
	 * directive ROWS are the opposite: order-sensitive (a reorder is a real edit),
	 * so they map verbatim with NO sorting. */
	function serialize(preset: PromptPreset | null): string {
		if (!preset) return '';
		const sortStringMap = (map: Record<string, string>): Record<string, string> =>
			Object.fromEntries(Object.entries(map).sort(([a], [b]) => a.localeCompare(b)));
		const sortBoolMap = (map: Record<string, boolean>): Record<string, boolean> =>
			Object.fromEntries(Object.entries(map).sort(([a], [b]) => a.localeCompare(b)));
		const sortFlowMap = (
			map: Record<string, PromptModule[]>
		): Record<string, PromptModule[]> =>
			Object.fromEntries(Object.entries(map).sort(([a], [b]) => a.localeCompare(b)));
		const s = preset.surfaces ?? {};
		const canonSurfaces = JSON.stringify({
			library_doc: s.library_doc ?? '',
			recalled: {
				framing: s.recalled?.framing ?? '',
				open: s.recalled?.open ?? '',
				close: s.recalled?.close ?? '',
				headers: sortStringMap(s.recalled?.headers ?? {})
			},
			regexes: (s.regexes ?? []).map((row) => ({ ...row })),
			history: {
				depth: s.history?.depth ?? null,
				strip_dsl: s.history?.strip_dsl ?? null
			},
			directives: sortStringMap(s.directives ?? {})
		});
		return JSON.stringify({
			...preset,
			toggles: sortBoolMap(preset.toggles),
			prompt_overrides: sortStringMap(preset.prompt_overrides),
			agent_bindings: sortStringMap(preset.agent_bindings),
			flows: sortFlowMap(preset.flows ?? {}),
			// ORDER-SENSITIVE (see the doc comment): no sorting, unlike the maps.
			directives: (preset.directives ?? []).map((row) => ({ ...row })),
			surfaces: canonSurfaces
		});
	}

	async function load(): Promise<void> {
		loading = true;
		error = '';
		try {
			const [list, active, vars, defaults, catalog, stepList] = await Promise.all([
				listPresets(),
				getActivePreset(),
				listPromptVariables(),
				getDefaultPromptStatus(),
				// The directive catalog is non-critical chrome; a failure must not
				// block the drawer (fall back to the known 15 ids).
				getSettings().catch((): SettingsCatalog | null => null),
				// The step catalog is display chrome too: a failure degrades to the
				// local titles instead of blocking the drawer.
				listPromptSteps().catch((): PromptStep[] => [])
			]);
			presets = list;
			activeId = active?.preset_id ?? null;
			variables = vars;
			defaultStatus = defaults;
			defaultPreset = defaults.preset;
			directiveCatalog = buildDirectiveCatalog(catalog);
			antiInjection = catalog?.features?.anti_injection?.enabled === true;
			steps = stepList;
			// D18: open on the ACTIVE preset, else the RUNNING DEFAULT — never
			// list[0]. A stale saved preset (flows predating flow completion)
			// must not hijack the drawer and show "No modules" for flows the
			// engine actually runs from the default pipeline.
			const initial = active?.preset_id ?? DEFAULT_KEY;
			selectKey(initial, true);
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	/** Deep-clone a preset. Svelte $state proxies are NOT structuredClone-able
	 * (crashed preset selection live): presets are JSON-shaped by contract, so a
	 * JSON round-trip is the correct deep copy here. */
	function clonePreset(preset: PromptPreset): PromptPreset {
		return JSON.parse(JSON.stringify(preset)) as PromptPreset;
	}

	/** Directive id + label catalog: the live settings order when available, else
	 * the fixed 15-id list. */
	function buildDirectiveCatalog(catalog: SettingsCatalog | null): { id: string; label: string }[] {
		if (catalog) {
			const order = catalog.meta?.order;
			if (Array.isArray(order) && order.length > 0) {
				return order.map((id) => ({ id, label: catalog.toggles[id]?.label ?? id }));
			}
			const ids = Object.keys(catalog.toggles);
			if (ids.length > 0) return ids.map((id) => ({ id, label: catalog.toggles[id]?.label ?? id }));
		}
		return FALLBACK_DIRECTIVE_IDS.map((id) => ({ id, label: id }));
	}

	/** Select a preset key; clones it into the editable draft + dirty baseline.
	 * The default entry edits the persisted default row IN PLACE (D26).
	 * `keepView` serves the W6.6 ⚙ shortcut: boot must not clobber the
	 * requested initial view (Rules) when it selects the active preset. */
	function selectKey(key: string, keepView = false): void {
		selectedKey = key;
		openStepId = null;
		editingDirectiveId = null;
		if (!keepView) activeView = 'steps';
		if (key === DEFAULT_KEY) {
			// The persisted default row (or null pre-boot). Editable; Save PUTs to
			// `/api/prompts/presets/default` directly (no materialize ritual).
			const found = defaultPreset ?? presets.find((p) => p.preset_id === DEFAULT_KEY) ?? null;
			if (found) {
				draft = clonePreset(found);
				baseline = clonePreset(found);
			} else {
				draft = null;
				baseline = null;
			}
			return;
		}
		const found = presets.find((p) => p.preset_id === key) ?? null;
		if (!found) {
			draft = null;
			baseline = null;
			selectedKey = DEFAULT_KEY;
			return;
		}
		draft = clonePreset(found);
		baseline = clonePreset(found);
	}

	function onSelectChange(event: Event): void {
		const value = (event.currentTarget as HTMLSelectElement).value;
		if (value === selectedKey) return;
		if (dirty && !window.confirm('Discard unsaved preset changes?')) {
			(event.currentTarget as HTMLSelectElement).value = selectedKey;
			return;
		}
		selectKey(value);
	}

	async function newFromDefault(): Promise<void> {
		const name = window.prompt('New preset name', 'My preset');
		if (name === null) return;
		busy = true;
		error = '';
		try {
			const created = await seedFromDefault(name.trim() || 'My preset');
			presets = [...presets, created];
			selectKey(created.preset_id);
			status = 'Seeded from the default pipeline.';
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** Only non-empty override/binding values are sent (contract). */
	function presetPayload(preset: PromptPreset): PromptPreset {
		const prune = (map: Record<string, string>): Record<string, string> =>
			Object.fromEntries(Object.entries(map).filter(([, value]) => value.trim() !== ''));
		return {
			...preset,
			prompt_overrides: prune(preset.prompt_overrides),
			agent_bindings: prune(preset.agent_bindings)
		};
	}

	async function save(): Promise<PromptPreset | null> {
		if (!draft || saving) return null;
		saving = true;
		error = '';
		try {
			// D26: the persisted default row is edited IN PLACE via the same PUT
			// (the "materialize a Custom preset" ritual is dead). Named presets
			// save identically. `surfaces` rides the payload so it is never wiped.
			const target = presetPayload(draft);
			const saved = await updatePreset(target.preset_id, target);
			presets = presets.some((p) => p.preset_id === saved.preset_id)
				? presets.map((p) => (p.preset_id === saved.preset_id ? saved : p))
				: [...presets, saved];
			if (saved.preset_id === 'default') defaultPreset = clonePreset(saved);
			draft = clonePreset(saved);
			baseline = clonePreset(saved);
			status = saved.preset_id === 'default' ? 'Saved to the default preset.' : 'Saved.';
			return saved;
		} catch (err) {
			error = message(err);
			return null;
		} finally {
			saving = false;
		}
	}

	async function toggleActivate(): Promise<void> {
		if (!draft) return;
		busy = true;
		error = '';
		try {
			if (isActive) {
				await deactivatePreset();
				activeId = null;
				status = 'Deactivated — the engine uses the default pipeline.';
			} else {
				await activatePreset(draft.preset_id);
				activeId = draft.preset_id;
				status = 'Activated.';
			}
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	function requestClose(): void {
		if (dirty && !window.confirm('Discard unsaved preset changes?')) return;
		onClose();
	}

	// --- module operations ----------------------------------------------------

	/** The preset's OWN rows for one flow, or null when it does not define them
	 * (key absent, or present-but-empty — the engine treats an empty flow as
	 * unset via `flows.get(flow) or DEFAULT_FLOWS`). `narrate` mirrors the
	 * legacy `modules` field. */
	function localFlow(preset: PromptPreset | null, flow: PromptFlow): PromptModule[] | null {
		if (!preset) return null;
		const flows = preset.flows;
		if (flows && Object.prototype.hasOwnProperty.call(flows, flow)) {
			const rows = flows[flow];
			if (rows && rows.length > 0) return rows;
			return null;
		}
		if (flow === 'narrate' && preset.modules.length > 0) return preset.modules;
		return null;
	}

	/** Structural identity of a flow, mirroring the engine's `flow_is_default`
	 * (type/role/content/enabled; ids and cosmetic names ignored). */
	function flowSignature(modules: PromptModule[]): string {
		return modules
			.map((m) => [m.type, m.role, m.content, m.enabled ? '1' : '0'].join('\u0001'))
			.join('\u0002');
	}

	/** Whether a flow is CUSTOMIZED: locally defined and structurally different
	 * from the default. An absent (or all-default) flow is INHERITED — the
	 * engine runs the default for it (D18: unset flow = default). */
	function flowIsCustomized(preset: PromptPreset | null, flow: PromptFlow): boolean {
		const local = localFlow(preset, flow);
		if (local === null) return false;
		const def = defaultPreset ? localFlow(defaultPreset, flow) : null;
		if (def === null) return local.length > 0;
		return flowSignature(local) !== flowSignature(def);
	}

	/** Modules to display for a flow: the preset's own rows when it defines them,
	 * else the INHERITED default flow for that interaction. */
	function flowModules(preset: PromptPreset | null, flow: PromptFlow): PromptModule[] {
		const local = localFlow(preset, flow);
		if (local !== null) return local;
		return defaultPreset ? (localFlow(defaultPreset, flow) ?? []) : [];
	}

	/** Materialize the read-only default pipeline as an editable synthetic draft
	 * (no stored preset yet; the first Save creates one). */
	function ensureDraft(): PromptPreset | null {
		if (draft) return draft;
		if (!defaultPreset) return null;
		draft = clonePreset(defaultPreset);
		baseline = clonePreset(defaultPreset);
		selectedKey = DEFAULT_KEY;
		return draft;
	}

	/** The flow the block operations act on: the open step, else the fallback. */
	function activeStepFlow(): PromptFlow {
		return openStepId !== null && isPromptFlow(openStepId) ? openStepId : DEFAULT_STEP;
	}

	/** The open step's blocks for the preset being edited. */
	function stepModules(): PromptModule[] {
		return displayPreset ? flowModules(displayPreset, activeStepFlow()) : [];
	}

	/** Write one flow's list; keep `modules` in step for narrate. */
	function commitModulesFor(flow: PromptFlow, next: PromptModule[]): void {
		if (!ensureDraft() || !draft) return;
		const flows = { ...(draft.flows ?? {}) };
		flows[flow] = next;
		draft.flows = flows;
		if (flow === 'narrate') draft.modules = next;
	}

	function updateModule(id: string, patch: Partial<PromptModule>): void {
		if (!draft) return;
		commitModulesFor(
			activeStepFlow(),
			stepModules().map((m) => (m.id === id ? { ...m, ...patch } : m))
		);
	}

	function toggleEnabled(id: string): void {
		if (!draft) return;
		commitModulesFor(
			activeStepFlow(),
			stepModules().map((m) => (m.id === id ? { ...m, enabled: !m.enabled } : m))
		);
	}

	function deleteModule(id: string): void {
		if (!draft) return;
		commitModulesFor(
			activeStepFlow(),
			stepModules().filter((m) => m.id !== id)
		);
	}

	function setModuleRole(id: string, role: PromptRole): void {
		updateModule(id, { role });
	}

	function move(from: number, to: number): void {
		if (!draft) return;
		commitModulesFor(activeStepFlow(), reorder(stepModules(), from, to));
	}

	function addBlock(type: PromptModuleType): void {
		if (!draft) return;
		const id = `mod_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
		const module: PromptModule = {
			id,
			name: type,
			type,
			role: 'system',
			content: '',
			enabled: true
		};
		commitModulesFor(activeStepFlow(), [...stepModules(), module]);
	}

	// --- step operations (W6.4 shell) -----------------------------------------

	/** Open one step's block list. */
	function selectStep(id: string): void {
		openStepId = id;
		activeView = 'steps';
	}

	/** Show the RULES catalog / ADVANCED token text; `null` step = the steps list. */
	function showView(view: ViewKey, stepId: string | null = null): void {
		activeView = view;
		openStepId = stepId;
	}

	/** Turn a whole step on/off: every block in its flow flips together. The step
	 * dot is the flow's `enabled`, which the engine reads as "any row enabled". */
	function toggleStep(id: string): void {
		if (!draft || !isPromptFlow(id)) return;
		const rows = flowModules(displayPreset, id);
		const enable = !rows.some((m) => m.enabled);
		commitModulesFor(
			id,
			rows.map((m) => ({ ...m, enabled: enable }))
		);
	}

	// --- full-screen block editor (W6.5) --------------------------------------

	/** Open one block in the full-screen editor (in-app overlay). */
	function openBlockEditor(module: PromptModule): void {
		editingBlock = module;
	}

	/** Write the editor's content back into the draft and close the overlay. */
	function applyBlockEdit(content: string): void {
		if (!editingBlock) return;
		updateModule(editingBlock.id, { content });
	}

	function closeBlockEditor(): void {
		editingBlock = null;
	}

	// --- native HTML5 drag-and-drop (MDN event flow) --------------------------

	function onDragStart(index: number, event: DragEvent): void {
		dragFrom = index;
		if (event.dataTransfer) {
			event.dataTransfer.effectAllowed = 'move';
			try {
				event.dataTransfer.setData('text/plain', String(index));
			} catch {
				// Some browsers block setData for non-text drags; index is in-memory anyway.
			}
		}
	}

	function onDragOver(index: number, event: DragEvent): void {
		event.preventDefault(); // declares this row a valid drop target
		dragOverIndex = index;
	}

	function onDragEnd(): void {
		dragFrom = null;
		dragOverIndex = null;
	}

	// --- variable inserter ----------------------------------------------------

	/** Register any surfaces / directive field as the variable-insertion target
	 * (the section-level inserter of the RULES and ADVANCED views). Step blocks
	 * carry their own inserter inside `PromptBlockEditor`. */
	function focusSurface(
		apply: (value: string) => void,
		el: HTMLInputElement | HTMLTextAreaElement
	): void {
		activeField = { el, apply };
	}

	function insertVariable(name: string): void {
		if (!activeField) return;
		const { el, apply } = activeField;
		const start = el.selectionStart ?? el.value.length;
		const end = el.selectionEnd ?? start;
		const token = `{{${name}}}`;
		const next = el.value.slice(0, start) + token + el.value.slice(end);
		apply(next);
		const caret = start + token.length;
		queueMicrotask(() => {
			try {
				el.focus();
				el.selectionStart = el.selectionEnd = caret;
			} catch {
				// Field detached (module deleted mid-edit); nothing to restore.
			}
		});
	}

	// --- agent bindings -------------------------------------------------------

	function setBinding(key: string, value: string): void {
		if (!draft) return;
		draft.agent_bindings = { ...draft.agent_bindings, [key]: value };
	}

	// --- D27 token surfaces ---------------------------------------------------

	/** Replace the whole surfaces block (reactive reassignment). */
	function setSurfaces(next: PromptSurfaces): void {
		if (!draft) return;
		draft.surfaces = next;
	}

	function setLibraryDoc(value: string): void {
		setSurfaces({ ...surfaces, library_doc: value });
	}

	function setRecalled(key: 'framing' | 'open' | 'close', value: string): void {
		setSurfaces({ ...surfaces, recalled: { ...(surfaces.recalled ?? {}), [key]: value } });
	}

	function setRecalledHeader(
		key: 'recall' | 'world_info' | 'recap' | 'bts',
		value: string
	): void {
		const recalled: PromptRecalledSurfaces = surfaces.recalled ?? {};
		setSurfaces({
			...surfaces,
			recalled: { ...recalled, headers: { ...(recalled.headers ?? {}), [key]: value } }
		});
	}

	function setRegexRow(index: number, patch: Partial<PromptRegexRow>): void {
		const next = regexRows.map((row, i) => (i === index ? { ...row, ...patch } : row));
		setSurfaces({ ...surfaces, regexes: next });
	}

	function addRegexRow(): void {
		const row: PromptRegexRow = {
			id: `regex_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
			name: 'New regex',
			find: '',
			replace: '',
			flags: 'gi',
			prompt_only: true,
			min_depth: 0,
			enabled: true
		};
		setSurfaces({ ...surfaces, regexes: [...regexRows, row] });
	}

	function removeRegexRow(index: number): void {
		setSurfaces({ ...surfaces, regexes: regexRows.filter((_, i) => i !== index) });
	}

	function setHistory(patch: PromptHistorySurfaces): void {
		setSurfaces({ ...surfaces, history: { ...(surfaces.history ?? {}), ...patch } });
	}

	// --- W3 directive rows (ordered, full CRUD) --------------------------------

	/** Fetch the factory directive rows once and cache them (the materialization
	 * source for an inherited list). Concurrent callers share one request. */
	function ensureFactoryDirectives(): Promise<PromptDirective[]> {
		if (factoryDirectives.length > 0) return Promise.resolve(factoryDirectives);
		if (!factoryDirectivesPending) {
			factoryDirectivesPending = getFactoryDirectives()
				.then((rows) => {
					factoryDirectives = rows;
					factoryDirectivesError = '';
					return rows;
				})
				.catch((err) => {
					factoryDirectivesError = message(err);
					return [] as PromptDirective[];
				})
				.finally(() => {
						factoryDirectivesPending = null;
				});
		}
		return factoryDirectivesPending;
	}

	/** MATERIALIZE ON FIRST EDIT (mirrors `ensureDraft()` for flows): the
	 * directive list is inherit-by-default, so the first mutation copies the
	 * factory rows into the draft verbatim, then every edit operates on the
	 * preset's own list. Re-reads the draft AFTER the await so two rapid
	 * mutations cannot each materialize a fresh copy (last-write loses rows). */
	async function materializeDirectives(): Promise<PromptDirective[]> {
		if (!draft) return [];
		if ((draft.directives ?? []).length > 0) return draft.directives ?? [];
		const rows = await ensureFactoryDirectives();
		if (rows.length === 0) return []; // fetch failed; error already surfaced
		if (!draft || (draft.directives ?? []).length > 0) return draft?.directives ?? [];
		draft.directives = rows.map((row) => ({ ...row }));
		return draft.directives;
	}

	async function addDirective(): Promise<void> {
		if (!draft) return;
		const rows = await materializeDirectives();
		const row: PromptDirective = {
			id: `dir_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`,
			name: 'New directive',
			content: '',
			tier: 2,
			priority: 50,
			core: false,
			protected: false,
			enabled: true,
			description: ''
		};
		draft.directives = [...rows, row];
	}

	async function moveDirective(from: number, to: number): Promise<void> {
		if (!draft) return;
		const rows = await materializeDirectives();
		draft.directives = reorder(rows, from, to);
	}

	async function toggleDirective(id: string): Promise<void> {
		if (!draft) return;
		const rows = await materializeDirectives();
		draft.directives = rows.map((row) =>
			row.id === id ? { ...row, enabled: !row.enabled } : row
		);
	}

	/** Flip the Anti Injection feature flag (issue #39). Not part of the preset
	 * draft: it is a persisted PRODUCT setting, written immediately, and the
	 * engine applies it on the next turn's prompt build. */
	async function toggleAntiInjection(): Promise<void> {
		if (antiInjectionBusy) return;
		const next = !antiInjection;
		antiInjectionBusy = true;
		antiInjectionError = '';
		antiInjection = next; // optimistic; the response re-syncs below
		try {
			const catalog = await patchFeatures({ anti_injection: next });
			antiInjection = catalog.features?.anti_injection?.enabled === true;
		} catch (err) {
			antiInjection = !next;
			antiInjectionError = message(err);
		} finally {
			antiInjectionBusy = false;
		}
	}

	async function deleteDirective(id: string): Promise<void> {
		if (!draft) return;
		const rows = await materializeDirectives();
		draft.directives = rows.filter((row) => row.id !== id);
		if (editingDirectiveId === id) editingDirectiveId = null;
	}

	async function patchDirective(id: string, patch: Partial<PromptDirective>): Promise<void> {
		if (!draft) return;
		const rows = await materializeDirectives();
		draft.directives = rows.map((row) => (row.id === id ? { ...row, ...patch } : row));
	}

	function toggleDirectiveEdit(id: string): void {
		editingDirectiveId = editingDirectiveId === id ? null : id;
	}

	function onDirectiveDrop(index: number, event: DragEvent): void {
		event.preventDefault();
		const raw = event.dataTransfer?.getData('text/plain');
		const from = dragFrom ?? (raw ? Number(raw) : null);
		if (from !== null && !Number.isNaN(from)) void moveDirective(from, index);
		onDragEnd();
	}

	/** Fold the legacy `surfaces.directives` text overrides into the matching
	 * directive rows' content (materializing the list first if needed), then
	 * clear the map. Unknown ids had no live effect either — they are dropped. */
	async function migrateDirectiveOverrides(): Promise<void> {
		if (!draft) return;
		const overrides = { ...directiveOverrides };
		const count = Object.keys(overrides).length;
		if (count === 0) return;
		const rows = await materializeDirectives();
		if (rows.length === 0) return; // materialization failed; error surfaced
		let folded = 0;
		draft.directives = rows.map((row) => {
			const text = overrides[row.id];
			if (text === undefined) return row;
			folded += 1;
			return { ...row, content: text };
		});
		setSurfaces({ ...surfaces, directives: {} });
		surfacesStatus =
			folded > 0
				? `Migrated ${folded} legacy text override${folded === 1 ? '' : 's'} into the directive rows.`
				: `No matching directive rows for ${count} legacy override${count === 1 ? '' : 's'} — the map was cleared.`;
	}

	// --- preset export / import (W3 Part B) ------------------------------------

	/** Hidden file input behind the Import button (the CardImport pattern). */
	let importFileInput = $state<HTMLInputElement | null>(null);

	/** Download the SELECTED preset's stored row as JSON (unsaved edits are not
	 * included — save first if that is wanted). */
	async function exportSelected(): Promise<void> {
		if (!draft) return;
		busy = true;
		error = '';
		try {
			const result = await exportPreset(draft.preset_id);
			status = `Exported ${result.filename}.`;
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	/** Read the chosen file, import it as a NEW preset, refresh the list and
	 * select it. A dirty draft is confirmed before the selection switches. */
	async function onImportFile(event: Event): Promise<void> {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		const keepDraft =
			dirty && !window.confirm('Importing selects the new preset — discard unsaved changes?');
		busy = true;
		error = '';
		try {
			const parsed = JSON.parse(await file.text()) as Record<string, unknown>;
			const created = await importPreset(parsed);
			presets = [...presets, created];
			if (!keepDraft) selectKey(created.preset_id);
			status = `Imported ${created.name}.`;
		} catch (err) {
			error = message(err);
		} finally {
			busy = false;
		}
	}

	// --- default-row actions (D26) -------------------------------------------

	/** Save any pending edits to the default row, then export it to the repo
	 * factory artifact (`POST /api/prompts/default/commit`). */
	async function commitToDefault(): Promise<void> {
		if (actionBusy) return;
		actionBusy = true;
		error = '';
		surfacesStatus = '';
		try {
			if (dirty) {
				const saved = await save();
				if (!saved) return; // save surfaced its own error
			}
			const result = await commitDefaultPreset();
			defaultStatus = await getDefaultPromptStatus().catch(() => defaultStatus);
			surfacesStatus = `Committed to ${result.artifact_path || 'prompts/factory-defaults.json'}`;
		} catch (err) {
			error = message(err);
		} finally {
			actionBusy = false;
		}
	}

	/** Re-seed the default row from the factory image; discards edits (confirm). */
	async function resetToFactory(): Promise<void> {
		if (actionBusy) return;
		if (
			!window.confirm(
				'Reset the default preset to the factory image? This discards every edit made to the default.'
			)
		)
			return;
		actionBusy = true;
		error = '';
		surfacesStatus = '';
		try {
			const status = await resetDefaultPreset();
			defaultStatus = status;
			defaultPreset = status.preset;
			// The default row may be selected either as the `__default__` sentinel
			// or by its real id (`default` is a real preset in the list too).
			if (status.preset && (selectedKey === DEFAULT_KEY || selectedKey === 'default')) {
				presets = presets.map((p) => (p.preset_id === 'default' ? status.preset! : p));
				draft = clonePreset(status.preset);
				baseline = clonePreset(status.preset);
			}
			surfacesStatus = 'Default reset to factory.';
		} catch (err) {
			error = message(err);
		} finally {
			actionBusy = false;
		}
	}

	// --- preview --------------------------------------------------------------

	/**
	 * Render through the engine's own builder. The preview must reflect the
	 * EDITED modules, so a dirty draft is saved first ("Save & preview"); the
	 * button label and tooltip say so.
	 */
	async function renderPreview(): Promise<void> {
		previewError = '';
		let target = draft;
		if (dirty) {
			const saved = await save();
			if (!saved) return; // save failed; error already surfaced
			target = saved;
		}
		previewBusy = true;
		try {
			preview = await previewPrompt(
				previewSession || sessionId || '',
				previewText,
				// The persisted default row previews by its real id (`default`);
				// null falls back to the active preset / engine default.
				target?.preset_id ?? null,
				previewInteraction
			);
		} catch (err) {
			previewError = message(err);
		} finally {
			previewBusy = false;
		}
	}
</script>

<div class="scrim" role="presentation" onclick={requestClose}></div>

<div class="prompt-drawer" role="dialog" aria-modal="true" aria-label="Prompt manager">
	<header>
		<div class="head-left">
			<h2>Prompt manager</h2>
			{#if dirty}<span class="dirty">● unsaved</span>{/if}
		</div>
		<div class="head-actions">
			<select
				class="preset-select"
				aria-label="Preset"
				value={selectedKey}
				onchange={onSelectChange}
			>
				<option value={DEFAULT_KEY}>
					Default pipeline{activeId === DEFAULT_KEY ? ' ● active' : ''}
				</option>
				{#each selectablePresets as preset (preset.preset_id)}
					<option value={preset.preset_id}>
						{preset.name}{preset.preset_id === activeId ? ' ● active' : ''}
					</option>
				{/each}
			</select>
			<button type="button" class="btn" disabled={busy} onclick={() => void newFromDefault()}>
				New from default
			</button>
			<button
				type="button"
				class="btn"
				disabled={busy || !draft}
				title={draft ? 'Download this preset as JSON' : 'Select a preset to export'}
				onclick={() => void exportSelected()}
			>
				Export
			</button>
			<input
				hidden
				bind:this={importFileInput}
				type="file"
				accept="application/json"
				aria-label="Import preset file"
				onchange={(event) => void onImportFile(event)}
			/>
			<button
				type="button"
				class="btn"
				disabled={busy}
				title="Import a preset export as a new preset"
				onclick={() => importFileInput?.click()}
			>
				Import
			</button>
			<button
				type="button"
				class="btn primary"
				disabled={!draft || !dirty || saving}
				onclick={() => void save()}
			>
				{saving ? 'Saving…' : 'Save'}
			</button>
			<button
				type="button"
				class="btn"
				disabled={!draft || busy || editingDefault}
				onclick={() => void toggleActivate()}
				title={editingDefault ? 'The default pipeline is always in effect' : undefined}
			>
				{isActive ? 'Deactivate' : 'Activate'}
			</button>
			<button type="button" class="close" aria-label="Close prompt manager" onclick={requestClose}>
				×
			</button>
		</div>
	</header>

	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if status}<p class="status">{status}</p>{/if}
	{#if surfacesStatus}<p class="status" role="status">{surfacesStatus}</p>{/if}
	{#if defaultStatus?.factory_changed}
		<p class="factory-notice" role="status">
			The factory defaults changed in an engine update (v{defaultStatus.seeded_version ?? '?'} →
			v{defaultStatus.factory_version}).
			<button type="button" class="link" disabled={actionBusy} onclick={() => void resetToFactory()}
				>Review &amp; reset to factory</button
			> or keep your edits.
		</p>
	{/if}

	<div class="body">
		<section class="flow">
			{#if loading}
				<p class="loading">Loading presets…</p>
			{:else if !draft}
				<div class="empty">
					<p>No preset selected — the engine is using the byte-faithful default pipeline.</p>
					<p>Use <strong>New from default</strong> to seed an editable preset, then Activate it.</p>
				</div>
			{:else}
				{#if editingDefault}
					<p class="default-note">
						There is no named preset active — you are editing the <strong
							>persisted default pipeline</strong
						> directly (D26). Save applies in place; use <strong>Commit to default</strong> to
						export it to the repo factory image.
					</p>
				{/if}
				{#if variables.length && activeView !== 'steps'}
					<!-- Section-level inserter for the RULES / ADVANCED fields (step blocks
					     carry their own inside PromptBlockEditor). -->
					<div class="vars">
						<span class="vars-label">Insert variable:</span>
						{#each variables as variable (variable.name)}
							<button
								type="button"
								class="var-btn"
								title={variable.description || `{{${variable.name}}}`}
								onclick={() => insertVariable(variable.name)}
							>
								{variable.name}
							</button>
						{/each}
					</div>
				{/if}

				{#if activeView === 'advanced'}
					<AdvancedSection
						{surfaces}
						editingDefault={editingDefault}
						presetName={draft.name}
						{actionBusy}
						{saving}
						legacyOverrideCount={legacyDirectiveOverrideCount}
						unknownVars={unknownSurfaceVars}
						{libraryDocChars}
						{regexRows}
						{historyKnobs}
						onBack={() => showView('steps')}
						onCommitToDefault={() => void commitToDefault()}
						onResetToFactory={() => void resetToFactory()}
						onMigrateDirectives={() => void migrateDirectiveOverrides()}
						{setLibraryDoc}
						{setRecalled}
						{setRecalledHeader}
						{setRegexRow}
						{addRegexRow}
						{removeRegexRow}
						{setHistory}
						onFocusField={focusSurface}
					/>
				{:else if activeView === 'rules'}
					<RulesSection
						rows={directiveRows}
						inherited={directivesInherited}
						factoryError={factoryDirectivesError}
						factoryCount={factoryDirectives.length}
						catalog={directiveCatalog}
						editingId={editingDirectiveId}
						{dragOverIndex}
						knownNames={knownNames}
						unknownVars={unknownDirectiveVars}
						onBack={() => showView('steps')}
						onAdd={() => void addDirective()}
						onMove={(from, to) => void moveDirective(from, to)}
						onDelete={(id) => void deleteDirective(id)}
						onToggle={(id) => void toggleDirective(id)}
						onPatch={(id, patch) => void patchDirective(id, patch)}
						onToggleEdit={toggleDirectiveEdit}
						{antiInjection}
						antiInjectionBusy={antiInjectionBusy}
						antiInjectionError={antiInjectionError}
						onToggleAntiInjection={() => void toggleAntiInjection()}
						onDragStart={onDragStart}
						onDragOver={onDragOver}
						onDragEnd={onDragEnd}
						onDrop={onDirectiveDrop}
						onFocusField={focusSurface}
					/>
				{:else}
					{#if openStep}
						<StepEditor
							step={{
								id: openStep.id,
								title: openStep.title,
								blurb: openStep.blurb,
								trigger: openStep.trigger,
								defaultAgent: openStep.default_agent ?? AGENT_DEFAULTS[openStep.id] ?? '',
								boundAgent: draft.agent_bindings[openStep.id] ?? '',
								systemSource: openStepSystemSource
							}}
							modules={openStepModules}
							structuralTypes={openStepStructural}
							inherited={openStepInherited}
							agentOptions={KNOWN_AGENTS}
							onBack={() => showView('steps')}
							onToggle={toggleEnabled}
							onDelete={deleteModule}
							onMove={move}
							onAddBlock={addBlock}
							onSetRole={setModuleRole}
							onRebind={(agent) => setBinding(openStep.id, agent)}
							onEditBlock={openBlockEditor}
						/>
					{:else}
						<div class="section-head">
							<h3>Steps</h3>
							<span class="hint">What runs, in order, on one turn.</span>
						</div>
						<p class="sub">
							Each step is one prompt sent to one agent. Open a step to edit the ordered
							blocks it sends.
						</p>
						<StepList
							rows={stepRows}
							agentOptions={KNOWN_AGENTS}
							onSelect={selectStep}
							onRebind={setBinding}
							onToggle={toggleStep}
						/>
						<div class="entries">
							<button type="button" class="entry-btn" onclick={() => showView('rules')}>
								<span class="entry-title">Rules</span>
								<span class="entry-sub">
									The narrator's directives — {directiveRows.length} rule{directiveRows.length ===
									1
										? ''
										: 's'}
								</span>
							</button>
							<button type="button" class="entry-btn" onclick={() => showView('advanced')}>
								<span class="entry-title">Advanced</span>
								<span class="entry-sub">
									Token text — component library, recalled framing, regex, history
								</span>
							</button>
						</div>
						<p class="hint">
							Known variables: {knownNames}. Unknown tokens render literally.
							{#if unknownVars.length}<span class="warn">Unknown: {unknownVars.join(', ')}</span
								>{/if}
						</p>
					{/if}
				{/if}
			{/if}

			<div class="session-overrides">
				<h3>Session overrides</h3>
				<p class="sub">
					Modules seeded from the imported card for THIS chat (D29). Toggling or editing
					appends a session event — shared presets are never touched.
				</p>
				{#if !sessionId}
					<p class="hint">Open a chat to see its session modules.</p>
				{:else if sessionModules.length === 0}
					<p class="hint">No session modules (the card carried no system/example text).</p>
				{:else}
					{#each sessionModules as module (module.module_id)}
						<div class="session-module" class:disabled={!module.enabled}>
							<div class="session-module-head">
								<span class="session-module-name">{module.name}</span>
								<span class="session-origin">{module.origin || 'session'}</span>
								<span class="session-anchor">{module.anchor}</span>
								<label class="session-toggle">
									<input
										type="checkbox"
										checked={module.enabled}
										disabled={moduleBusyId === module.module_id}
										onchange={() => void toggleSessionModule(module)}
									/>
									enabled
								</label>
								<button
									type="button"
									class="session-edit"
									disabled={moduleBusyId === module.module_id}
									onclick={() => startModuleEdit(module)}>edit</button
								>
							</div>
							{#if editingModuleId === module.module_id}
								<div class="editor">
									<textarea
										rows="4"
										aria-label={`${module.name} content`}
										bind:value={moduleDraft}
									></textarea>
									<div class="editor-actions">
										<button
											type="button"
											class="btn primary"
											disabled={moduleBusyId === module.module_id}
											onclick={() => void saveModuleEdit(module)}>Save</button
										>
										<button
											type="button"
											class="btn"
											onclick={() => (editingModuleId = null)}>Cancel</button
										>
									</div>
								</div>
							{:else}
								<p class="session-content">{module.content}</p>
							{/if}
						</div>
					{/each}
				{/if}
				{#if sessionModulesError}<p class="error" role="alert">{sessionModulesError}</p>{/if}
			</div>
		</section>

		<section class="preview-pane">
			<h3>Preview</h3>
			<label class="preview-field">
				<span>Session id</span>
				<input
					type="text"
					placeholder="(current chat)"
					bind:value={previewSession}
					aria-label="Preview session id"
				/>
			</label>
			<label class="preview-field">
				<span>User text</span>
				<input
					type="text"
					placeholder="What do you do?"
					bind:value={previewText}
					aria-label="Preview user text"
				/>
			</label>
			<label class="preview-field">
				<span>Interaction</span>
				<select
					class="preview-interaction"
					aria-label="Preview interaction"
					value={previewInteraction}
					onchange={(event) => (previewInteraction = (event.currentTarget as HTMLSelectElement).value)}
				>
					{#each FLOWS as flow (flow)}
						<option value={flow}>{flow}</option>
					{/each}
				</select>
			</label>
			<button
				type="button"
				class="btn primary render"
				disabled={previewBusy || saving}
				title={draft
					? 'Saves any unsaved modules first, then renders through the same builder the engine uses.'
					: 'Renders the default pipeline (no preset active).'}
				onclick={() => void renderPreview()}
			>
				{previewBusy ? 'Rendering…' : draft ? 'Save & preview' : 'Render default'}
			</button>

			{#if previewError}<p class="error" role="alert">{previewError}</p>{/if}

			{#if preview}
				<div class="preview-meta">
					{preview.messages.length} message{preview.messages.length === 1 ? '' : 's'} ·
					{preview.chars} chars
					{#if preview.preview_mode}<span class="mode">mode: {preview.preview_mode}</span>{/if}
				</div>
				<div class="messages">
					{#each preview.messages as msg, i (i)}
						<div class="msg">
							<div class="msg-head">
								<span class="role-badge role-{msg.role}">{msg.role}</span>
								<span class="msg-count">{msg.content.length} chars</span>
							</div>
							<pre>{msg.content}</pre>
						</div>
					{/each}
				</div>
			{:else}
				<p class="hint">Rendered messages appear here.</p>
			{/if}
		</section>
	</div>
</div>

{#if editingBlock && editingBlockSource}
	<PromptBlockEditor
		scope={openStep?.title ?? activeStepFlow()}
		title={editingBlock.name}
		source={editingBlockSource}
		value={editingBlock.content}
		{variables}
		onSave={applyBlockEdit}
		onClose={closeBlockEditor}
	/>
{/if}

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 22;
	}
	.prompt-drawer {
		position: fixed;
		top: 0;
		right: 0;
		height: 100vh;
		width: 46rem;
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
		align-items: center;
		justify-content: space-between;
		gap: 0.75rem;
		padding: 0.75rem 1rem;
		border-bottom: 1px solid var(--line);
		flex-wrap: wrap;
	}
	.head-left {
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	h2 {
		margin: 0;
		font-size: 0.95rem;
	}
	.dirty {
		font-size: 0.7rem;
		color: var(--warn);
	}
	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex-wrap: wrap;
	}
	.preset-select {
		font: inherit;
		font-size: 0.75rem;
		max-width: 12rem;
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.3rem 0.4rem;
	}
	.btn {
		font: inherit;
		font-size: 0.72rem;
		font-weight: 600;
		padding: 0.3rem 0.6rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
		white-space: nowrap;
	}
	.btn:hover:not(:disabled) {
		background: var(--accent-soft);
	}
	.btn.primary {
		border-color: var(--accent-hover);
		background: var(--accent-hover);
		color: var(--on-accent);
	}
	.btn.primary:hover:not(:disabled) {
		background: var(--accent);
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
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
	.error {
		margin: 0.5rem 1rem 0;
		padding: 0.45rem 0.6rem;
		font-size: 0.75rem;
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.status {
		margin: 0.5rem 1rem 0;
		font-size: 0.72rem;
		color: var(--text-muted);
	}
	.body {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
	}
	.flow,
	.preview-pane {
		min-height: 0;
		overflow-y: auto;
		padding: 0.75rem 1rem 1.5rem;
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.preview-pane {
		border-left: 1px solid var(--line);
		background: var(--bg);
	}
	h3 {
		margin: 0.4rem 0 0;
		font-size: 0.66rem;
		letter-spacing: 0.12em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.section-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.back {
		font: inherit;
		font-size: 0.7rem;
		background: none;
		border: none;
		padding: 0;
		color: var(--accent-strong);
		cursor: pointer;
	}
	.entries {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		margin-top: 0.35rem;
	}
	.entry-btn {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 0.1rem;
		width: 100%;
		padding: 0.4rem 0.55rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-raised);
		color: var(--text);
		text-align: left;
		cursor: pointer;
	}
	.entry-btn:hover {
		border-color: var(--accent-line);
	}
	.entry-title {
		font-size: 0.82rem;
		font-weight: 600;
	}
	.entry-sub {
		font-size: 0.68rem;
		color: var(--text-dim);
	}
	.default-note {
		margin: 0 0 0.5rem;
		font-size: 0.72rem;
		color: var(--text-dim);
	}
	.loading,
	.empty {
		font-size: 0.8rem;
		color: var(--text-dim);
	}
	.empty p {
		margin: 0 0 0.4rem;
	}
	.vars {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.3rem;
	}
	.vars-label {
		font-size: 0.68rem;
		color: var(--text-muted);
	}
	.var-btn {
		font: inherit;
		font-size: 0.68rem;
		padding: 0.15rem 0.45rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--bg-raised);
		color: var(--accent-strong);
		cursor: pointer;
	}
	.var-btn:hover {
		background: var(--accent-soft);
	}
	.hint {
		margin: 0.1rem 0 0;
		font-size: 0.66rem;
		color: var(--text-dim);
	}
	.hint .warn {
		margin-left: 0.35rem;
		color: var(--warn);
	}
	.sub {
		margin: 0.1rem 0 0;
		font-size: 0.68rem;
		color: var(--text-dim);
	}
	.preview-field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: 0.7rem;
		color: var(--text-muted);
	}
	.render {
		align-self: flex-start;
	}
	.preview-meta {
		font-size: 0.7rem;
		color: var(--text-muted);
	}
	.preview-meta .mode {
		margin-left: 0.4rem;
		color: var(--warn);
	}
	.messages {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
	}
	.msg {
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-panel);
		overflow: hidden;
	}
	.msg-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.25rem 0.5rem;
		border-bottom: 1px solid var(--line);
	}
	.role-badge {
		font-size: 0.62rem;
		font-weight: 600;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: var(--accent-strong);
	}
	.role-badge.role-user {
		color: var(--ok);
	}
	.role-badge.role-assistant {
		color: var(--text-muted);
	}
	.msg-count {
		font-size: 0.62rem;
		color: var(--text-dim);
	}
	.msg pre {
		margin: 0;
		padding: 0.5rem;
		font-size: 0.72rem;
		line-height: 1.45;
		white-space: pre-wrap;
		word-break: break-word;
		color: var(--text);
	}
	@media (max-width: 60rem) {
		.body {
			grid-template-columns: minmax(0, 1fr);
		}
		.preview-pane {
			border-left: none;
			border-top: 1px solid var(--line);
		}
	}

	/* Wave B (D29): session module overrides. */
	.session-overrides {
		margin-top: 1.1rem;
		border-top: 1px solid var(--line);
		padding-top: 0.6rem;
	}
	.session-overrides h3 {
		margin: 0 0 0.15rem;
	}
	.session-module {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		padding: 0.4rem 0.5rem;
		margin-top: 0.4rem;
		background: var(--bg-raised);
	}
	.session-module.disabled {
		opacity: 0.55;
	}
	.session-module-head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.session-module-name {
		font-size: 0.74rem;
		font-weight: 600;
		color: var(--text);
	}
	.session-origin,
	.session-anchor {
		font-size: var(--font-micro);
		color: var(--text-dim);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 0 0.4rem;
	}
	.session-toggle {
		display: inline-flex;
		align-items: center;
		gap: 0.2rem;
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-muted);
	}
	.session-edit {
		font: inherit;
		font-size: var(--font-micro);
		padding: 0.05rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 5px;
		background: var(--bg-panel);
		color: var(--text-muted);
		cursor: pointer;
	}
	.session-content {
		margin: 0;
		font-size: 0.68rem;
		color: var(--text-muted);
		white-space: pre-wrap;
		max-height: 6rem;
		overflow-y: auto;
	}
</style>
