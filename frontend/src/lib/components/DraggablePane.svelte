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
</script>

<script lang="ts">
	// Floating draggable/resizable pane shell (SURFACE #12 / R13).
	//
	// A self-contained absolutely-positioned card: its header is a drag handle
	// (`role="toolbar"`), the corner grip resizes it, and a close button fires
	// `onclose`. `x/y/w/h` seed the uncontrolled position/size once on mount and
	// are otherwise owned by the component — callers do not get live updates back.
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
		onclose?: () => void;
		children: Snippet;
	};

	let { title, x = 24, y = 24, w = 340, h, onclose, children }: Props = $props();

	// W9.16: every floating pane joins the Escape stack (D51 §2.4). The
	// drawers already do; these panes did not, so Escape left them open over
	// the header and blocked clicks on the controls beneath.
	onMount(() => register('pane', () => onclose?.()));

	// Props are INITIAL values only: snapshot them once (untrack marks that we
	// deliberately do not follow later prop changes) and own the state ourselves.
	const seed = untrack(() => ({ x, y, w, h }));
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

	let headerEl = $state<HTMLElement | undefined>(undefined);

	type DragState = {
		pointerId: number;
		startX: number;
		startY: number;
		originX: number;
		originY: number;
	};
	type ResizeState = {
		pointerId: number;
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

	/** Double-clicking the header snaps the pane back to its initial position. */
	function resetPosition(): void {
		posX = clampX(initial.x);
		posY = clampY(initial.y);
	}

	function onResizePointerDown(event: PointerEvent): void {
		if (event.button !== 0 || height === undefined) return;
		const el = event.currentTarget as HTMLElement;
		el.setPointerCapture(event.pointerId);
		resize = {
			pointerId: event.pointerId,
			startX: event.clientX,
			startY: event.clientY,
			originW: width,
			originH: height
		};
	}

	function onResizePointerMove(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		width = Math.max(MIN_W, resize.originW + (event.clientX - resize.startX));
		height = Math.max(MIN_H, resize.originH + (event.clientY - resize.startY));
		posX = clampX(posX);
	}

	function onResizePointerEnd(event: PointerEvent): void {
		if (!resize || resize.pointerId !== event.pointerId) return;
		const el = event.currentTarget as HTMLElement;
		if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
		resize = null;
	}
</script>

<section
	class="pane"
	role="group"
	aria-label={title}
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
		ondblclick={resetPosition}
	>
		<span class="pane-title">{title}</span>
		<button type="button" class="pane-close" aria-label="close pane" onclick={onclose}>×</button>
	</header>

	<div class="pane-body">
		{@render children()}
	</div>

	{#if height !== undefined}
		<div
			class="pane-resize"
			role="separator"
			aria-label="resize pane"
			onpointerdown={onResizePointerDown}
			onpointermove={onResizePointerMove}
			onpointerup={onResizePointerEnd}
			onpointercancel={onResizePointerEnd}
		></div>
	{/if}
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
