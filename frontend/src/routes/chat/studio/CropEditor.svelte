<script lang="ts">
	// Definition-image cropper (T4a): a user-facing CIRCLE MASK.
	//
	// Opens over the studio with a chosen file and exports a SQUARE PNG master
	// (max 1024px) that the circular thumbnail renders with `object-fit: cover`.
	// The circular guide sits on a dark field; the export is the largest square
	// the current selection allows (cover-fit keeps the image filling the square
	// at every zoom, so no empty corners can be cropped in).
	//
	// Controls are deliberately simple and dependency-free (plain canvas): a zoom
	// slider (keyboard operable), drag-to-reposition on the pointer, and nudge
	// buttons so the same repositioning is reachable from the keyboard. Escape
	// cancels through the shared surface stack (`#lib/uiLayers`), so it closes
	// the crop dialog without also closing the studio beneath it.
	import { onMount } from 'svelte';
	import { register } from '#lib/uiLayers';

	let {
		file,
		saving = false,
		error = '',
		onSave,
		onCancel
	}: {
		/** The image the user picked (`accept="image/*"`). */
		file: File;
		/** Upload in flight — disables every control. */
		saving?: boolean;
		/** Upload failure to surface inline, if any. */
		error?: string;
		/** Receives the exported square PNG. */
		onSave: (blob: Blob) => void;
		onCancel: () => void;
	} = $props();

	/** Square viewport, in CSS px. */
	const VIEW = 320;
	/** Circle-guide diameter as a fraction of the viewport. */
	const CIRCLE = 0.84;
	/** Hard cap on the exported master's side (task: max 1024px). */
	const MAX_OUT = 1024;
	/** Zoom slider runs from cover-fit (1x) to 4x. */
	const ZOOM_MAX = 4;

	let canvas = $state<HTMLCanvasElement | null>(null);
	/** Source-space coordinate pinned to the viewport centre. */
	let centreX = $state(0);
	let centreY = $state(0);
	/** Source px per CSS px; never below `minScale` (keeps the square covered). */
	let scale = $state(1);
	/** The cover-fit scale for this image — the 1x floor. */
	let minScale = $state(1);
	let ready = $state(false);
	let loadError = $state('');
	let dragging = false;
	let lastX = 0;
	let lastY = 0;

	let natW = 0;
	let natH = 0;
	let image: HTMLImageElement | null = null;

	const zoom = $derived(minScale ? scale / minScale : 1);

	/** Keep the viewport centre inside the image (cover fit => always possible). */
	function clampCentre(): void {
		const halfX = VIEW / 2 / scale;
		const halfY = VIEW / 2 / scale;
		centreX = Math.min(Math.max(centreX, halfX), Math.max(halfX, natW - halfX));
		centreY = Math.min(Math.max(centreY, halfY), Math.max(halfY, natH - halfY));
	}

	/** Draw the dark field, the transformed image, the dimmed outside, the ring. */
	function render(): void {
		const ctx = canvas?.getContext('2d');
		if (!ctx) return;
		ctx.setTransform(1, 0, 0, 1, 0, 0);
		ctx.clearRect(0, 0, VIEW, VIEW);
		ctx.fillStyle = '#10131a';
		ctx.fillRect(0, 0, VIEW, VIEW);
		if (image) {
			const dx = VIEW / 2 - centreX * scale;
			const dy = VIEW / 2 - centreY * scale;
			ctx.drawImage(image, dx, dy, natW * scale, natH * scale);
		}
		const centre = VIEW / 2;
		const radius = (VIEW * CIRCLE) / 2;
		// Dim everything outside the circle (even-odd: rect minus circle).
		ctx.beginPath();
		ctx.rect(0, 0, VIEW, VIEW);
		ctx.arc(centre, centre, radius, 0, Math.PI * 2);
		ctx.fillStyle = 'rgba(8, 10, 14, 0.62)';
		ctx.fill('evenodd');
		// Ring.
		ctx.beginPath();
		ctx.arc(centre, centre, radius, 0, Math.PI * 2);
		ctx.lineWidth = 2;
		ctx.strokeStyle = 'rgba(255, 255, 255, 0.85)';
		ctx.stroke();
	}

	onMount(() => {
		const unregister = register('studio-crop', () => onCancel());
		const url = URL.createObjectURL(file);
		const img = new Image();
		img.onload = () => {
			natW = img.naturalWidth || VIEW;
			natH = img.naturalHeight || VIEW;
			image = img;
			minScale = Math.max(VIEW / natW, VIEW / natH);
			scale = minScale;
			centreX = natW / 2;
			centreY = natH / 2;
			ready = true;
			render();
		};
		img.onerror = () => {
			loadError = 'Could not read that image.';
		};
		img.src = url;
		return () => {
			unregister();
			URL.revokeObjectURL(url);
		};
	});

	function onPointerDown(event: PointerEvent): void {
		if (!ready || saving) return;
		dragging = true;
		lastX = event.clientX;
		lastY = event.clientY;
		(event.currentTarget as HTMLCanvasElement).setPointerCapture(event.pointerId);
	}

	function onPointerMove(event: PointerEvent): void {
		if (!dragging) return;
		// The canvas may be displayed below its `VIEW` attribute size on narrow
		// screens, so map screen px -> viewport px before -> source px.
		const rect = canvas?.getBoundingClientRect();
		const ratio = rect && rect.width ? VIEW / rect.width : 1;
		const dx = (event.clientX - lastX) * ratio;
		const dy = (event.clientY - lastY) * ratio;
		lastX = event.clientX;
		lastY = event.clientY;
		centreX -= dx / scale;
		centreY -= dy / scale;
		clampCentre();
		render();
	}

	function endDrag(event: PointerEvent): void {
		if (!dragging) return;
		dragging = false;
		(event.currentTarget as HTMLCanvasElement).releasePointerCapture(event.pointerId);
	}

	function onZoom(event: Event): void {
		const next = Number((event.currentTarget as HTMLInputElement).value);
		if (!Number.isFinite(next)) return;
		scale = Math.min(Math.max(minScale * next, minScale), minScale * ZOOM_MAX);
		clampCentre();
		render();
	}

	/** Keyboard repositioning (the drag affordance's accessible twin). */
	function nudge(dirX: number, dirY: number): void {
		if (!ready || saving) return;
		const step = 12 / scale;
		centreX += dirX * step;
		centreY += dirY * step;
		clampCentre();
		render();
	}

	/** Rasterize the square selection at up to {@link MAX_OUT} px. */
	function exportBlob(): Promise<Blob | null> {
		const out = document.createElement('canvas');
		const region = VIEW / scale; // source px across the square selection
		const side = Math.max(1, Math.min(MAX_OUT, Math.round(region)));
		out.width = side;
		out.height = side;
		const ctx = out.getContext('2d');
		if (!ctx || !image) return Promise.resolve(null);
		const origin = VIEW / 2 / scale;
		ctx.drawImage(image, centreX - origin, centreY - origin, region, region, 0, 0, side, side);
		return new Promise((resolve) => out.toBlob((blob) => resolve(blob), 'image/png'));
	}

	async function save(): Promise<void> {
		if (!ready || saving) return;
		const blob = await exportBlob();
		if (blob) onSave(blob);
		else loadError = 'Could not render the crop.';
	}
</script>

<div class="scrim" role="presentation" onclick={() => !saving && onCancel()}></div>
<div class="crop" role="dialog" aria-modal="true" aria-label="Crop definition image">
	<h2>Crop image</h2>
	<p class="hint">Drag to reposition. The circle previews how the studio shows it.</p>

	<div class="stage" style="width: {VIEW}px; height: {VIEW}px;">
		<canvas
			bind:this={canvas}
			width={VIEW}
			height={VIEW}
			class="preview"
			onpointerdown={onPointerDown}
			onpointermove={onPointerMove}
			onpointerup={endDrag}
			onpointercancel={endDrag}
		></canvas>
		{#if !ready && !loadError}
			<span class="loading">Loading image…</span>
		{/if}
	</div>

	<div class="controls">
		<label class="zoom">
			<span>Zoom</span>
			<input
				type="range"
				min="1"
				max={ZOOM_MAX}
				step="0.01"
				value={zoom}
				oninput={onZoom}
				disabled={!ready || saving}
			/>
		</label>
		<div class="nudge" role="group" aria-label="Reposition image">
			<button type="button" aria-label="Move up" onclick={() => nudge(0, -1)} disabled={!ready || saving}>↑</button>
			<button type="button" aria-label="Move left" onclick={() => nudge(-1, 0)} disabled={!ready || saving}>←</button>
			<button type="button" aria-label="Move right" onclick={() => nudge(1, 0)} disabled={!ready || saving}>→</button>
			<button type="button" aria-label="Move down" onclick={() => nudge(0, 1)} disabled={!ready || saving}>↓</button>
		</div>
	</div>

	{#if loadError}
		<p class="error" role="alert">{loadError}</p>
	{/if}
	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	<div class="actions">
		<button type="button" class="ghost" onclick={onCancel} disabled={saving}>Cancel</button>
		<button type="button" class="primary" onclick={() => void save()} disabled={!ready || saving}>
			{saving ? 'Saving…' : 'Save'}
		</button>
	</div>
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.45);
		z-index: var(--z-scrim, 20);
	}
	.crop {
		position: fixed;
		top: 50%;
		left: 50%;
		transform: translate(-50%, -50%);
		z-index: var(--z-drawer-top, 23);
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		width: min(24rem, 92vw);
		padding: 0.9rem;
		border: 1px solid var(--line);
		border-radius: 10px;
		background: var(--bg-raised);
		color: var(--text);
		box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4);
	}
	h2 {
		margin: 0;
		font-size: var(--font-ui);
	}
	.hint {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.stage {
		position: relative;
		align-self: center;
		max-width: 100%;
	}
	.preview {
		display: block;
		width: 100%;
		height: 100%;
		border-radius: 8px;
		cursor: grab;
		touch-action: none;
	}
	.preview:active {
		cursor: grabbing;
	}
	.loading {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.controls {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.6rem;
	}
	.zoom {
		flex: 1;
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.zoom input {
		flex: 1;
	}
	.nudge {
		display: grid;
		grid-template-columns: repeat(3, 1.6rem);
		grid-template-rows: repeat(2, 1.6rem);
		gap: 0.15rem;
	}
	.nudge button {
		font: inherit;
		font-size: var(--font-small);
		border: 1px solid var(--line);
		border-radius: 5px;
		background: var(--bg);
		color: var(--text);
		cursor: pointer;
		padding: 0;
	}
	.nudge button:first-child {
		grid-column: 2;
	}
	.nudge button:nth-child(2) {
		grid-column: 1;
		grid-row: 2;
	}
	.nudge button:nth-child(3) {
		grid-column: 2;
		grid-row: 2;
	}
	.nudge button:nth-child(4) {
		grid-column: 3;
		grid-row: 2;
	}
	.nudge button:hover:not(:disabled) {
		background: var(--bg-hover);
	}
	.error {
		margin: 0;
		padding: 0.4rem 0.55rem;
		font-size: var(--font-small);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.4rem;
	}
	.actions button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	.actions button:disabled {
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
</style>
