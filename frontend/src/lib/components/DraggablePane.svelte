<script module lang="ts">
	// Module-level z-order counter shared by every DraggablePane instance.
	// Each pane that gets interacted with claims the next slot, so the touched
	// card rises above its siblings (classic window-stack behaviour) without
	// every pane having to know about the others.
	//
	// UX shell pass 1 (U5): panes live in a single bounded lane just above the
	// docks and strictly BELOW the scrims/drawers, so a busy stack can never
	// climb over an open modal. The counter wraps inside the band; the values
	// mirror --z-pane/--z-pane-max in app.css.
	const Z_PANE_MIN = 10;
	const Z_PANE_MAX = 17;
	let zCounter = Z_PANE_MIN;

	/** Claim and return the next z-index slot for a pane being interacted with. */
	export function paneZ(): number {
		zCounter = zCounter >= Z_PANE_MAX ? Z_PANE_MIN + 1 : zCounter + 1;
		return zCounter;
	}

	/** localStorage prefix for a pane's remembered size (issue #49). */
	const SIZE_KEY_PREFIX = 'fabulist.pane.size.';

	/** Title -> stable storage key (`Memories (Used this turn)` -> `memories-used-this-turn`). */
	function paneSlug(name: string): string {
		return name
			.toLowerCase()
			.replace(/[^a-z0-9]+/g, '-')
			.replace(/^-+|-+$/g, '');
	}
</script>

<script lang="ts">
	// Floating draggable/resizable pane shell (SURFACE #12 / R13).
	//
	// A self-contained absolutely-positioned card: its header is a drag handle
	// (`role="toolbar"`), the right/bottom edges and the corner grip resize it
	// (issue #49 — previously the grip only existed when the caller passed `h`,
	// which no caller did), and a close button fires `onclose`. `x/y/w/h` seed the
	// uncontrolled position/size once on mount and are otherwise owned by the
	// component — callers do not get live updates back.
	//
	// A size the user dragged is remembered per panel in localStorage (keyed by
	// `id`, else the title slug). A pane size is browser-local user state, not
	// narrative history, so it never goes through the engine API.
	//
	// Dragging uses pointer events + setPointerCapture so the gesture keeps
	// tracking even when the cursor races ahead of the element (no window-level
	// listeners to leak). Position is clamped so the header never fully leaves
	// the viewport — at least MIN_VISIBLE px stay reachable in both axes.
	import { untrack } from 'svelte';
	import { onMount } from 'svelte';
	import { register } from '#lib/uiLayers';
	import type { Snippet } from 'svelte';

	type Props = {
		title: string;
		x?: number;
		y?: number;
		w?: number;
		h?: number;
		/** Stable per-panel storage key for the remembered size; defaults to the title slug. */
		id?: string;
		onclose?: () => void;
		children: Snippet;
	};

	let { title, x = 24, y = 24, w = 340, h, id, onclose, children }: Props = $props();

	// W9.16: every floating pane joins the Escape stack (D51 §2.4). The
	// drawers already do; these panes did not, so Escape left them open over
	// the header and blocked clicks on the controls beneath.
	onMount(() => {
		// Client-only work: the remembered size (if any), then the Escape-stack slot.
		restoreSize();
		return register('pane', () => onclose?.());
	});

	// Props are INITIAL values only: snapshot them once (untrack marks that we
	// deliberately do not follow later prop changes) and own the state ourselves.
	const seed = untrack(() => ({ x, y, w, h, sizeKey: SIZE_KEY_PREFIX + (id ?? paneSlug(title)) }));
	let posX = $state(seed.x);
	let posY = $state(seed.y);
	let width = $state(seed.w);
	let height = $state<number | undefined>(seed.h);
	let z = $state(paneZ());

	// Remember where we started so double-clicking the header can snap back.
	const initial = { x: seed.x, y: seed.y };

	// Keep this much of the header on screen in each axis while dragging.
	const MIN_VISIBLE = 80;
	const MIN_W = 260;
	const MIN_H = 140;
	// Leave a gap at the viewport edge so a resize handle stays grabbable.
	const VIEWPORT_INSET = 8;

	let headerEl = $state<HTMLElement | undefined>(undefined);
	let sectionEl = $state<HTMLElement | undefined>(undefined);

	type DragState = {
		pointerId: number;
		startX: number;
		startY: number;
		originX: number;
		originY: number;
	};
	/** Which axes a resize gesture drives; the edge handles each drive one. */
	type ResizeAxis = 'x' | 'y' | 'both';
	type ResizeState = {
		pointerId: number;
		axis: ResizeAxis;
		startX: number;
		startY: number;
		originW: number;
		originH: number;
	};

	let drag = $state<DragState | null>(null);
	let resize = $state<ResizeState | null>(null);

	/** Clamp horizontal position so MIN_VISIBLE px of the header stay on screen. */
	function clampX(value: number): number {
		if (typeof window === 'undefined') return value;
		const vw = window.innerWidth;
		const lo = -(width - MIN_VISIBLE);
		const hi = Math.max(0, vw - MIN_VISIBLE);
		return Math.min(Math.max(value, lo), hi);
	}

	/** Clamp vertical position so the header bar itself stays on screen. */
	function clampY(value: number): number {
		if (typeof window === 'undefined') return value;
		const headerH = headerEl?.offsetHeight ?? 40;
		const vh = window.innerHeight;
		return Math.min(Math.max(value, 0), Math.max(0, vh - headerH));
	}

	/** Raise this pane above its siblings. Called on any pointerdown inside it. */
	function bringToFront(): void {
		z = paneZ();
	}

	function onHeaderPointerDown(event: PointerEvent): void {
		if (event.button !== 0) return;
		// Buttons INSIDE the header (close/anything clickable) must receive their
		// own pointer events: pointer-capturing the header would swallow the
		// button's pointerup -> no click -> onclick/onclose never fire.
		const target = event.target as HTMLElement;
		if (target !== event.currentTarget && target.closest?.('button')) return;
		const el = event.currentTarget as HTMLElement;
		el.setPointerCapture(event.pointerId);
		drag = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			originX: posX,
			originY: posY
		};
	}

	function onHeaderPointerMove(event: PointerEvent): void {
		if (!drag || drag.pointerId !== event.pointerId) return;
		const nextX = drag.originX + (event.clientX - drag.startX);
		const nextY = drag.originY + (event.clientY - drag.startY);
		posX = clampX(nextX);
		posY = clampY(nextY);
	}

	function onHeaderPointerEnd(event: PointerEvent): void {
		if (!drag || drag.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		drag = null;
	}

	/**
	 * Double-clicking the header snaps the pane back to its SEED geometry —
	 * position and size both, so a pane can never get stuck at a size the user
	 * has no way to undo.
	 */
	function resetGeometry(): void {
		posX = clampX(initial.x);
		posY = clampY(initial.y);
		width = clampW(seed.w);
		height = seed.h === undefined ? undefined : clampH(seed.h);
		keepInViewport();
		forgetSize();
	}

	function onResizePointerDown(event: PointerEvent, axis: ResizeAxis): void {
		if (event.button !== 0) return;
		// An auto-height pane has no baseline to add deltas to: pin its rendered
		// height at grab time, so the first vertical nudge converts auto-height
		// into an explicit height instead of snapping to MIN_H.
		let originH = height;
		if (axis !== 'x' && originH === undefined) {
			originH = clampH(sectionEl?.getBoundingClientRect().height ?? MIN_H);
			height = originH;
		}
		const el = event.currentTarget as HTMLElement;
		el.setPointerCapture(event.pointerId);
		resize = {
			pointerId: event.pointerId,
			axis,
			startX: event.clientX,
			startY: event.clientY,
			originW: width,
			originH: originH ?? MIN_H
		};
	}

	function onResizePointerMove(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		if (resize.axis !== 'y') width = clampW(resize.originW + (event.clientX - resize.startX));
		if (resize.axis !== 'x') height = clampH(resize.originH + (event.clientY - resize.startY));
		keepInViewport();
		posX = clampX(posX);
	}

	function onResizePointerEnd(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		resize = null;
		// Remember the size the user settled on (issue #49).
		persistSize();
	}

	/** Never narrower than MIN_W, never wider than the viewport (minus an inset). */
	function clampW(value: number): number {
		if (typeof window === 'undefined') return Math.max(MIN_W, value);
		return Math.min(Math.max(value, MIN_W), Math.max(MIN_W, window.innerWidth - VIEWPORT_INSET));
	}

	/** Never shorter than MIN_H, never taller than the viewport (minus an inset). */
	function clampH(value: number): number {
		if (typeof window === 'undefined') return Math.max(MIN_H, value);
		return Math.min(Math.max(value, MIN_H), Math.max(MIN_H, window.innerHeight - VIEWPORT_INSET));
	}

	/**
	 * Growing a pane must never push its own right/bottom edge (the handle the
	 * user is dragging, and the content they are widening for) off screen: the
	 * pane slides back instead, so the dragged edge stays under the cursor.
	 * Without this a right-docked pane could only ever grow 30px.
	 */
	function keepInViewport(): void {
		if (typeof window === 'undefined') return;
		const maxX = window.innerWidth - VIEWPORT_INSET - width;
		if (posX > maxX) posX = maxX;
		if (height !== undefined) {
			const maxY = window.innerHeight - VIEWPORT_INSET - height;
			if (posY > maxY) posY = maxY;
		}
	}

	/**
	 * Remember the dragged size (issue #49). Written on gesture END, not on every
	 * pointermove: one storage write per resize, never one per pixel. A pane size
	 * is browser-local user state — it must never reach the engine API.
	 */
	function persistSize(): void {
		// Guarded: no localStorage during SSR, and private mode / quota can throw.
		if (typeof localStorage === 'undefined') return;
		try {
			localStorage.setItem(
				seed.sizeKey,
				JSON.stringify({
					w: Math.round(width),
					h: height === undefined ? null : Math.round(height)
				})
			);
		} catch {
			// A remembered size is a nicety — never worth breaking the pane over.
		}
	}

	/** Apply a remembered size, if any. Client-only (called from onMount). */
	function restoreSize(): void {
		if (typeof localStorage === 'undefined') return;
		try {
			const raw = localStorage.getItem(seed.sizeKey);
			if (!raw) return;
			const saved: { w?: unknown; h?: unknown } | null = JSON.parse(raw);
			if (typeof saved?.w === 'number' && Number.isFinite(saved.w)) width = clampW(saved.w);
			if (typeof saved?.h === 'number' && Number.isFinite(saved.h)) height = clampH(saved.h);
			// A remembered size from a wider window must not hang off this one.
			keepInViewport();
		} catch {
			// Unreadable/corrupt entry: keep the caller's seed rather than a guess.
		}
	}

	/** Forget the remembered size (the double-click reset restores seed geometry). */
	function forgetSize(): void {
		if (typeof localStorage === 'undefined') return;
		try {
			localStorage.removeItem(seed.sizeKey);
		} catch {
			// see persistSize
		}
	}
</script>

<section
	class="pane"
	role="group"
	aria-label={title}
	bind:this={sectionEl}
	style="left: {posX}px; top: {posY}px; width: {width}px;{height !== undefined
		? ` height: ${height}px;`
		: ''} z-index: {z};"
	onpointerdown={bringToFront}
>
	<header
		class="pane-header"
		role="toolbar"
		aria-label={title}
		tabindex="-1"
		bind:this={headerEl}
		onpointerdown={onHeaderPointerDown}
		onpointermove={onHeaderPointerMove}
		onpointerup={onHeaderPointerEnd}
		onpointercancel={onHeaderPointerEnd}
		ondblclick={resetGeometry}
	>
		<span class="pane-title">{title}</span>
		<button type="button" class="pane-close" aria-label="close pane" onclick={onclose}>×</button>
	</header>

	<div class="pane-body">
		{@render children()}
	</div>

	<!-- Issue #49: every pane is resizable on BOTH axes. The right and bottom
	     edges are thin grab strips (so a width-only or height-only drag is
	     possible); the corner keeps the classic grip and drives both at once. -->
	<div
		class="pane-resize pane-resize-x"
		role="separator"
		aria-orientation="vertical"
		aria-label="resize pane width"
		onpointerdown={(event) => onResizePointerDown(event, 'x')}
		onpointermove={onResizePointerMove}
		onpointerup={onResizePointerEnd}
		onpointercancel={onResizePointerEnd}
	></div>
	<div
		class="pane-resize pane-resize-y"
		role="separator"
		aria-orientation="horizontal"
		aria-label="resize pane height"
		onpointerdown={(event) => onResizePointerDown(event, 'y')}
		onpointermove={onResizePointerMove}
		onpointerup={onResizePointerEnd}
		onpointercancel={onResizePointerEnd}
	></div>
	<div
		class="pane-resize pane-resize-corner"
		role="separator"
		aria-label="resize pane"
		onpointerdown={(event) => onResizePointerDown(event, 'both')}
		onpointermove={onResizePointerMove}
		onpointerup={onResizePointerEnd}
		onpointercancel={onResizePointerEnd}
	></div>
</section>

<style>
	.pane {
		position: absolute;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border: 1px solid var(--line);
		border-radius: 10px;
		overflow: hidden;
		box-shadow: 0 12px 32px var(--shadow);
		color: var(--text);
	}

	.pane-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.35rem 0.4rem 0.35rem 0.7rem;
		background: var(--bg-raised);
		border-bottom: 1px solid var(--line);
		cursor: grab;
		touch-action: none;
		user-select: none;
	}

	.pane-header:active {
		cursor: grabbing;
	}

	.pane-title {
		flex: 1 1 auto;
		min-width: 0;
		font-size: var(--font-ui);
		font-weight: 600;
		letter-spacing: 0.02em;
		color: var(--text);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.pane-close {
		flex: 0 0 auto;
		width: 1.4rem;
		height: 1.4rem;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		margin: 0;
		padding: 0;
		font-size: 1rem;
		line-height: 1;
		color: var(--text-muted);
		background: transparent;
		border: none;
		border-radius: 6px;
		cursor: pointer;
	}

	.pane-close:hover {
		color: var(--text);
		background: var(--bg-hover);
	}

	.pane-body {
		flex: 1 1 auto;
		min-height: 0;
		overflow: auto;
		padding: 0.6rem 0.7rem;
		color: var(--text);
	}

	.pane-resize {
		position: absolute;
		touch-action: none;
		z-index: 1;
	}

	/* Thin grab strips: 5px keeps the pane body's own scrollbar usable while
	   still being easy to hit, and they highlight on hover so the affordance is
	   discoverable without adding chrome. */
	.pane-resize-x,
	.pane-resize-y {
		background: transparent;
	}

	.pane-resize-x:hover,
	.pane-resize-y:hover {
		background: var(--accent, #6b8afd);
		opacity: 0.25;
	}

	.pane-resize-x {
		top: 0;
		right: 0;
		bottom: 14px;
		width: 5px;
		cursor: ew-resize;
	}

	.pane-resize-y {
		left: 0;
		right: 14px;
		bottom: 0;
		height: 5px;
		cursor: ns-resize;
	}

	.pane-resize-corner {
		right: 0;
		bottom: 0;
		width: 14px;
		height: 14px;
		cursor: nwse-resize;
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
