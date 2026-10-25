<script lang="ts">
	// Design Studio shell (D51 §2.3).
	//
	// A NON-modal, anchorable surface that hosts per-kind editor panes (Flow ·
	// Proposals · Personas · Worlds · Books · Assistants · Profiles · Queries ·
	// Templates · Planner). The shell owns layout only: kind
	// tabs, dock/float placement, collapse, persistence, the Escape registration
	// and the dirty guard. It reads NO page stores and makes NO fetches — panes
	// receive plain props (`focusId`, `startNew`, `onChanged`, `onDirty`,
	// `onopenstep`), which keeps a future `window.open` popout an additive re-host.
	//
	// Docked mode is a full-height flex column (the host wires it into `.chat`);
	// floating mode is a fixed, draggable, resizable card. Layout state persists in
	// `fabulist.studio.state`; while `open` is false the shell renders nothing so
	// SSR is unaffected.
	//
	// Host open requests (D51 L3.a): a door (`+page.svelte`) hands down an
	// `openRequest` object it re-creates on every click. `seq` exists because the
	// kind alone cannot express a repeat — a door reopened on the tab that is
	// already active would be a no-op under a plain `kind` prop — so the host bumps
	// `seq` and this shell keys its effect off the request object. A request routes
	// through `selectKind`, which keeps the dirty confirm the single gate for a tab
	// switch: a cancelled confirm leaves the current pane (and its draft) open.
	import { onMount } from 'svelte';
	import type { Component } from 'svelte';
	import { register } from '#lib/uiLayers';
	// ONE kind registry (#lib/studio/kinds): the union, the tab/pane labels and
	// the grouping are shared with the rail footer map and every door, so a kind
	// can never be reachable from one surface and invisible from another.
	import {
		STUDIO_GROUPS,
		isStudioKind,
		kindsInGroup,
		studioKindSpec,
		type StudioKind
	} from '#lib/studio/kinds';

	/** Props every studio pane receives from the shell. */
	interface StudioPaneProps {
		focusId?: string | null;
		startNew?: boolean;
		/** Monotonic request token; a NEW value re-applies `focusId`/`startNew`. */
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
		/** Prompt-door callback (graph pane): carries a W6 step id to the host. */
		onopenstep?: (stepId: string) => void;
		/** The open chat — the flow pane draws the ACTUAL lane arms from it. */
		sessionId?: string | null;
	}

	type Dock = 'left' | 'right' | 'float';

	type Props = {
		open: boolean;
		onClose: () => void;
		panes?: Partial<Record<StudioKind, Component<StudioPaneProps>>>;
		focusId?: string | null;
		startNew?: boolean;
		onChanged?: () => void;
		/** A host door's request to show a kind; re-created per click (see header). */
		openRequest?: { kind: StudioKind; seq: number } | null;
		/** Forwarded to the panes: a graph node's prompt-door click (the page opens it). */
		onopenstep?: (stepId: string) => void;
		/** The open chat — the flow pane draws the ACTUAL lane arms from it. */
		sessionId?: string | null;
	};

	let {
		open,
		onClose,
		panes,
		focusId = null,
		startNew = false,
		onChanged,
		openRequest = null,
		onopenstep,
		sessionId = null
	}: Props = $props();

	// The landing kind (R11) — a persisted kind still wins on restore, so the
	// shell reopens where the owner left it.
	const LANDING_KIND: StudioKind = 'flow';

	const STORAGE_KEY = 'fabulist.studio.state';
	const MIN_DOCK_REM = 26;
	const MAX_DOCK_REM = 46;
	const MIN_VISIBLE = 80;
	const MIN_W = 320;
	const MIN_H = 240;

	// --- the fresh-layout decision (issue #65) -------------------------------
	// A new (or reset) studio POPS OUT rather than docking, and it is generous:
	// the panes are editors, and a persona's Personality or a world's
	// description is a long text input that a 440px rail column squeezed into a
	// cramped scroll box. A floated panel also opens clear of the window edge
	// instead of flush against it — the edge is where the page scrollbar lives,
	// which is how a drag aimed at the resize edge landed on a scrollbar instead.
	// Every one of these values lives HERE and nowhere else; saved layouts are
	// never migrated (the restore pass applies what the owner left, field by
	// field).
	const DEFAULT_DOCK: Dock = 'float';
	const DEFAULT_WIDTH = 640;
	const DEFAULT_HEIGHT = 720;
	/** Clear of the page's 248px session rail (`RAIL_WIDTH`), plus a 2rem gap. */
	const DEFAULT_X = 280;
	const DEFAULT_Y = 88;

	// 16px is the browser default root size; read the live value so a host that
	// resizes the root font still gets the right clamps.
	function rootRem(): number {
		if (typeof window === 'undefined') return 16;
		const value = parseFloat(getComputedStyle(document.documentElement).fontSize);
		return Number.isFinite(value) && value > 0 ? value : 16;
	}

	// --- owned layout state ---
	let dock = $state<Dock>(DEFAULT_DOCK);
	let width = $state(DEFAULT_WIDTH);
	let height = $state(DEFAULT_HEIGHT);
	let x = $state(DEFAULT_X);
	let y = $state(DEFAULT_Y);
	let collapsed = $state(false);
	let kind = $state<StudioKind>(LANDING_KIND);
	let dirty = $state(false);
	// Guards the persistence effect: nothing is written back until the restore
	// pass in `onMount` has run, so stale defaults can never clobber a saved state.
	let hydrated = $state(false);

	let headerEl = $state<HTMLElement | undefined>(undefined);

	const ActivePane = $derived(panes?.[kind]);

	// --- persistence ---
	function persist(): void {
		if (typeof window === 'undefined') return;
		try {
			window.localStorage.setItem(
				STORAGE_KEY,
				JSON.stringify({ open, dock, width, height, x, y, collapsed, kind, focusId })
			);
		} catch {
			/* private mode / quota — persistence is best-effort */
		}
	}

	// Reads every persisted field so a change to any of them re-runs the write.
	$effect(() => {
		if (!hydrated) return;
		void open;
		void dock;
		void width;
		void height;
		void x;
		void y;
		void collapsed;
		void kind;
		void focusId;
		persist();
	});

	/**
	 * Fit the DEFAULT box into the current window. Only the defaults go through
	 * this: a saved layout is restored exactly as it was saved, never re-clamped
	 * under the owner's hands.
	 */
	function fitDefaultFloat(): void {
		if (typeof window === 'undefined') return;
		const edge = 16;
		width = Math.min(DEFAULT_WIDTH, Math.max(MIN_W, window.innerWidth - edge * 2));
		height = Math.min(DEFAULT_HEIGHT, Math.max(MIN_H, window.innerHeight - edge * 2));
		// Keep the rail clear whenever there is room; on a window too small for that,
		// the shared clamps below still leave the header reachable.
		x = clampX(Math.min(DEFAULT_X, Math.max(0, window.innerWidth - width - edge)));
		y = clampY(Math.min(DEFAULT_Y, Math.max(0, window.innerHeight - height - edge)));
	}

	/** Put the layout back to the fresh-open defaults (the shell's own reset). */
	function resetLayout(): void {
		dock = DEFAULT_DOCK;
		collapsed = false;
		fitDefaultFloat();
	}

	onMount(() => {
		if (typeof window === 'undefined') {
			hydrated = true;
			return;
		}
		// A fresh layout starts from the defaults, fitted to THIS window; the
		// saved fields below then win one by one, so an owner who docked stays
		// docked and no saved value is silently rewritten.
		fitDefaultFloat();
		try {
			const raw = window.localStorage.getItem(STORAGE_KEY);
			if (raw) {
				const saved = JSON.parse(raw) as Record<string, unknown>;
				if (saved.dock === 'left' || saved.dock === 'right' || saved.dock === 'float') {
					dock = saved.dock;
				}
				if (typeof saved.width === 'number') width = saved.width;
				if (typeof saved.height === 'number') height = saved.height;
				if (typeof saved.x === 'number') x = saved.x;
				if (typeof saved.y === 'number') y = saved.y;
				if (typeof saved.collapsed === 'boolean') collapsed = saved.collapsed;
				if (isStudioKind(saved.kind)) kind = saved.kind;
			}
		} catch {
			/* corrupt state — fall back to defaults */
		}
		// A partial saved layout (an older write, a hand-edited value) could leave a
		// floated default width on a DOCKED panel; the dock band clamp the splitter
		// already uses puts that right without touching a saved float size.
		if (dock !== 'float') width = Math.min(Math.max(width, dockMinWidth), dockMaxWidth);
		hydrated = true;
	});

	// --- Escape via the surface stack (D51 §2.4) ---
	// `onClose` is called by uiLayers when the studio is the TOP surface; the
	// dirty guard inside still applies. Registered only while open.
	$effect(() => {
		if (!open) return;
		return register('studio', () => requestClose());
	});

	// --- float focus management (D51 §2.4) ---
	// Floating mode is a non-modal dialog: focus moves to the header so keyboard
	// users land inside it. The element focused when the studio OPENED is saved and
	// handed focus back on close (guarded: the original node may have unmounted).
	// Docked mode never steals focus, so it never restores it either.
	let savedFocus: HTMLElement | null = null;
	let stoleFocus = false;
	let wasOpen = false;

	$effect(() => {
		const isOpen = open;
		if (typeof window === 'undefined') return;
		if (isOpen && !wasOpen) {
			wasOpen = true;
			const active = document.activeElement;
			savedFocus = active instanceof HTMLElement ? active : null;
		} else if (!isOpen && wasOpen) {
			wasOpen = false;
			const target = savedFocus;
			savedFocus = null;
			if (stoleFocus && target && target.isConnected) target.focus();
			stoleFocus = false;
		}
	});

	$effect(() => {
		if (!open || dock !== 'float' || typeof window === 'undefined') return;
		stoleFocus = true;
		headerEl?.focus();
	});

	// --- guards ---
	function confirmDiscard(message: string): boolean {
		if (!dirty) return true;
		if (typeof window === 'undefined') return true;
		return window.confirm(message);
	}

	function selectKind(next: StudioKind): void {
		if (next === kind) return;
		if (!confirmDiscard('Discard unsaved changes in this pane?')) return;
		// A new pane mounts under a fresh key, so its dirty flag starts clean.
		dirty = false;
		kind = next;
	}

	function requestClose(): void {
		if (!confirmDiscard('Close the Design Studio and discard unsaved changes?')) return;
		dirty = false;
		onClose();
	}

	function setDirty(value: boolean): void {
		dirty = value;
	}

	// --- host open requests (D51 L3.a) ---
	// Consumed by `seq`: the run that consumes a request also reads `kind`, later
	// runs return before touching it, so the effect's deps shrink back to the
	// request object and a manual tab click can never be undone by a stale request.
	let appliedRequestSeq = -1;

	$effect(() => {
		const request = openRequest;
		if (!request || request.seq === appliedRequestSeq) return;
		appliedRequestSeq = request.seq;
		if (request.kind !== kind) selectKind(request.kind);
	});

	/**
	 * Switch placement, clamping the shared width into the dock band when leaving
	 * float — a wide floated panel must not dock past `min(46rem, 45vw)`.
	 */
	function setDock(next: Dock): void {
		if (next !== 'float') {
			width = Math.min(Math.max(width, dockMinWidth), dockMaxWidth);
		}
		dock = next;
	}

	// --- dock resize ---
	const dockMinWidth = $derived(rootRem() * MIN_DOCK_REM);
	const dockMaxWidth = $derived(
		typeof window === 'undefined'
			? rootRem() * MAX_DOCK_REM
			: Math.min(rootRem() * MAX_DOCK_REM, window.innerWidth * 0.45)
	);

	type SplitState = { pointerId: number; startX: number; originW: number };
	let split = $state<SplitState | null>(null);

	function onSplitterPointerDown(event: PointerEvent): void {
		if ((event.target as HTMLElement).closest('button')) return;
		if (event.button !== 0) return;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
		split = { pointerId: event.pointerId, startX: event.clientX, originW: width };
	}

	function onSplitterPointerMove(event: PointerEvent): void {
		if (!split || split.pointerId !== event.pointerId) return;
		// Dragging the right-dock's LEFT edge leftward grows the panel; the
		// left-dock's RIGHT edge grows it rightward.
		const delta = event.clientX - split.startX;
		const next = dock === 'left' ? split.originW + delta : split.originW - delta;
		width = Math.min(Math.max(next, dockMinWidth), dockMaxWidth);
	}

	function onSplitterPointerEnd(event: PointerEvent): void {
		if (!split || split.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		split = null;
	}

	// --- float drag ---
	type DragState = { pointerId: number; startX: number; startY: number; originX: number; originY: number };
	let drag = $state<DragState | null>(null);

	function clampX(value: number): number {
		if (typeof window === 'undefined') return value;
		const lo = -(width - MIN_VISIBLE);
		const hi = Math.max(0, window.innerWidth - MIN_VISIBLE);
		return Math.min(Math.max(value, lo), hi);
	}

	function clampY(value: number): number {
		if (typeof window === 'undefined') return value;
		const headerH = headerEl?.offsetHeight ?? 44;
		return Math.min(Math.max(value, 0), Math.max(0, window.innerHeight - headerH));
	}

	function onHeaderPointerDown(event: PointerEvent): void {
		// Buttons INSIDE the header (tabs / dock / collapse / close) must receive
		// their own pointer events: capturing on the header swallows their pointerup
		// so the click never fires (project gotcha).
		if ((event.target as HTMLElement).closest('button')) return;
		if (dock !== 'float' || event.button !== 0) return;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
		drag = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			originX: x,
			originY: y
		};
	}

	function onHeaderPointerMove(event: PointerEvent): void {
		if (!drag || drag.pointerId !== event.pointerId) return;
		x = clampX(drag.originX + (event.clientX - drag.startX));
		y = clampY(drag.originY + (event.clientY - drag.startY));
	}

	function onHeaderPointerEnd(event: PointerEvent): void {
		if (!drag || drag.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		drag = null;
	}

	// --- float resize ---
	type ResizeState = { pointerId: number; startX: number; startY: number; originW: number; originH: number };
	let resize = $state<ResizeState | null>(null);

	function onGripPointerDown(event: PointerEvent): void {
		if ((event.target as HTMLElement).closest('button')) return;
		if (event.button !== 0) return;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
		resize = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			originW: width,
			originH: height
		};
	}

	function onGripPointerMove(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		width = Math.max(MIN_W, resize.originW + (event.clientX - resize.startX));
		height = Math.max(MIN_H, resize.originH + (event.clientY - resize.startY));
		x = clampX(x);
	}

	function onGripPointerEnd(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		resize = null;
	}
</script>

{#snippet kindTabs()}
	<nav class="tabs" aria-label="Entity kinds">
		{#each STUDIO_GROUPS as group (group.group)}
			<!-- The caption names the band; the pills sit in a labelled group so the
			     band is announced too (the caption itself is decoration). -->
			<span class="tab-group-label" aria-hidden="true">{group.label}</span>
			<div class="tab-group" role="group" aria-label={group.label}>
				{#each kindsInGroup(group.group) as spec (spec.kind)}
					<button
						type="button"
						class="tab"
						class:active={spec.kind === kind}
						aria-pressed={spec.kind === kind}
						onclick={() => selectKind(spec.kind)}>{spec.tab}</button
					>
				{/each}
			</div>
		{/each}
	</nav>
{/snippet}

{#snippet paneBody()}
	<div class="studio-body">
		{#key `pane:${kind}`}
			{#if ActivePane}
				<ActivePane
					{focusId}
					{startNew}
					requestSeq={openRequest?.seq ?? null}
					onChanged={onChanged}
					onDirty={setDirty}
					{onopenstep}
					{sessionId}
				/>
			{:else}
				<p class="placeholder">{studioKindSpec(kind).pane} pane not wired yet</p>
			{/if}
		{/key}
	</div>
{/snippet}

{#if open}
	{#if dock === 'float'}
		<div
			class="studio floating"
			class:collapsed
			role="dialog"
			aria-modal="false"
			aria-label="Design studio"
			tabindex="-1"
			style="left: {x}px; top: {y}px; width: {width}px;{collapsed
				? ''
				: ` height: ${height}px;`} z-index: var(--z-studio, 18);"
		>
			<header
				class="studio-head"
				role="toolbar"
				aria-label="Design studio header"
				bind:this={headerEl}
				tabindex="-1"
				onpointerdown={onHeaderPointerDown}
				onpointermove={onHeaderPointerMove}
				onpointerup={onHeaderPointerEnd}
				onpointercancel={onHeaderPointerEnd}
			>
				<span class="drag-handle" title="Drag to move" aria-hidden="true">⠿</span>
				{@render kindTabs()}
				<div class="head-actions">
					<button
						type="button"
						class="icon"
						aria-label="Reset studio layout"
						title="Reset layout"
						onclick={resetLayout}>⟲</button
					>
					<button
						type="button"
						class="icon"
						aria-label="Dock studio left"
						title="Dock left"
						onclick={() => setDock('left')}>◧</button
					>
					<button
						type="button"
						class="icon"
						aria-label="Dock studio right"
						title="Dock right"
						onclick={() => setDock('right')}>◨</button
					>
					<button
						type="button"
						class="icon"
						aria-label="Collapse design studio"
						title="Collapse"
						onclick={() => (collapsed = true)}>—</button
					>
					<button
						type="button"
						class="icon"
						aria-label="Close design studio"
						title="Close"
						onclick={requestClose}>×</button
					>
				</div>
			</header>
			{#if collapsed}
				<button
					type="button"
					class="expand float-expand"
					aria-label="Expand design studio"
					title="Expand"
					onclick={() => (collapsed = false)}>▢</button
				>
			{:else}
				{@render paneBody()}
				<div
					class="grip"
					role="separator"
					aria-label="Resize design studio"
					onpointerdown={onGripPointerDown}
					onpointermove={onGripPointerMove}
					onpointerup={onGripPointerEnd}
					onpointercancel={onGripPointerEnd}
				></div>
			{/if}
		</div>
	{:else}
		<section
			class="studio docked"
			class:dock-left={dock === 'left'}
			class:collapsed
			role="complementary"
			aria-label="Design studio"
			style="width: {collapsed ? '2.5rem' : `${width}px`};"
		>
			{#if collapsed}
				<div class="collapsed-tab">
					<span class="tab-glyph" aria-hidden="true">◫</span>
					<button
						type="button"
						class="expand"
						aria-label="Expand design studio"
						title="Expand"
						onclick={() => (collapsed = false)}>{dock === 'left' ? '›' : '‹'}</button
					>
				</div>
			{:else}
				{#if dock === 'left'}
					<div
						class="splitter"
						role="separator"
						aria-label="Resize design studio"
						onpointerdown={onSplitterPointerDown}
						onpointermove={onSplitterPointerMove}
						onpointerup={onSplitterPointerEnd}
						onpointercancel={onSplitterPointerEnd}
					></div>
				{/if}
				<div class="studio-main">
					<header class="studio-head">
						{@render kindTabs()}
						<div class="head-actions">
							<button
								type="button"
								class="icon"
								aria-label="Reset studio layout"
								title="Reset layout"
								onclick={resetLayout}>⟲</button
							>
							<button
								type="button"
								class="icon"
								aria-label="Dock studio right"
								title="Dock right"
								onclick={() => setDock('right')}>◨</button
							>
							<button
								type="button"
								class="icon"
								aria-label="Float studio"
								title="Float"
								onclick={() => (dock = 'float')}>⧉</button
							>
							<button
								type="button"
								class="icon"
								aria-label="Collapse design studio"
								title="Collapse"
								onclick={() => (collapsed = true)}>»</button
							>
							<button
								type="button"
								class="icon"
								aria-label="Close design studio"
								title="Close"
								onclick={requestClose}>×</button
							>
						</div>
					</header>
					{@render paneBody()}
				</div>
				{#if dock === 'right'}
					<div
						class="splitter"
						role="separator"
						aria-label="Resize design studio"
						onpointerdown={onSplitterPointerDown}
						onpointermove={onSplitterPointerMove}
						onpointerup={onSplitterPointerEnd}
						onpointercancel={onSplitterPointerEnd}
					></div>
				{/if}
			{/if}
		</section>
	{/if}
{/if}

<style>
	/* Docked mode: a full-height flex column the host drops into `.chat`. */
	.studio.docked {
		flex: none;
		display: flex;
		height: 100%;
		min-height: 0;
		background: var(--bg-panel);
		color: var(--text);
		border-left: 1px solid var(--line);
	}

	.studio.docked.dock-left {
		order: -1;
		border-left: none;
		border-right: 1px solid var(--line);
	}

	.studio-main {
		flex: 1;
		min-width: 0;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}

	/* Floating mode: fixed, draggable, resizable card. */
	.studio.floating {
		position: fixed;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
		box-shadow: 0 12px 32px var(--shadow);
		color: var(--text);
	}

	.studio-head {
		flex: none;
		display: flex;
		align-items: center;
		gap: 0.35rem;
		padding: 0.35rem 0.4rem;
		background: var(--bg-raised);
		border-bottom: 1px solid var(--line);
	}

	/* Only the floating header drags; docked stays put. */
	.floating .studio-head {
		cursor: grab;
		touch-action: none;
		user-select: none;
	}

	.floating .studio-head:active {
		cursor: grabbing;
	}

	.drag-handle {
		flex: none;
		font-size: var(--font-small);
		color: var(--text-dim);
		letter-spacing: -0.1em;
		padding: 0 0.1rem;
	}

	.studio-head:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: -2px;
	}

	.tabs {
		display: flex;
		flex: 1;
		min-width: 0;
		gap: 0.2rem 0.45rem;
		flex-wrap: wrap;
		align-items: center;
	}

	/* Band caption between pill groups — the same mono-uppercase micro register
	   the rail's `.layer-head` uses, one step quieter than the pills. */
	.tab-group-label {
		font-size: var(--font-micro);
		font-weight: 600;
		line-height: 1;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}

	.tab-group {
		display: flex;
		flex-wrap: wrap;
		gap: 0.2rem;
	}

	.tab {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.2rem 0.5rem;
		border: 1px solid transparent;
		border-radius: 999px;
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}

	.tab:hover {
		background: var(--bg-hover);
		color: var(--text);
	}

	.tab.active {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-weight: 600;
	}

	.head-actions {
		display: flex;
		align-items: center;
		gap: 0.1rem;
		flex: none;
	}

	.icon {
		font: inherit;
		font-size: var(--font-ui);
		line-height: 1;
		width: 1.5rem;
		height: 1.5rem;
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

	.studio-body {
		flex: 1;
		min-height: 0;
		overflow: auto;
		padding: 0.75rem 0.85rem;
	}

	.placeholder {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}

	/* Docked splitter: a 6px inner-edge grab strip. */
	.splitter {
		flex: none;
		width: 6px;
		cursor: col-resize;
		touch-action: none;
		background: var(--line);
	}

	.splitter:hover {
		background: var(--accent-line);
	}

	/* Collapsed docked tab: a 2.5rem edge strip with the glyph + expand. */
	.collapsed-tab {
		flex: none;
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 0.5rem;
		padding-top: 0.5rem;
	}

	.tab-glyph {
		font-size: var(--font-ui);
		color: var(--text-muted);
	}

	.expand {
		font: inherit;
		font-size: var(--font-ui);
		line-height: 1;
		width: 1.6rem;
		height: 1.6rem;
		display: grid;
		place-items: center;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}

	.expand:hover {
		background: var(--bg-hover);
		color: var(--text);
	}

	/* Collapsed floating panel: header only, with an expand button beneath it. */
	.float-expand {
		margin: 0.4rem auto;
	}

	/* Floating corner resize grip. */
	.grip {
		position: absolute;
		right: 0;
		bottom: 0;
		width: 14px;
		height: 14px;
		cursor: nwse-resize;
		touch-action: none;
		background: linear-gradient(
			135deg,
			transparent 0 55%,
			var(--line-strong) 55% 70%,
			transparent 70% 78%,
			var(--line-strong) 78% 93%,
			transparent 93%
		);
	}
</style>
