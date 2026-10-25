<script lang="ts">
	// Appearance drawer (issue #12 + #13, ADR-027): the whole reader-facing theme surface.
	//
	// Deliberately MINIMAL — a preset selector, ONE stylesheet paste box, ONE image
	// behind the prose, and the one sentence about the tone/!important precedence.
	// No theme editor, no per-token controls: that was ruled out on the issue, and
	// the custom CSS box is the escape hatch that makes the editor unnecessary.
	//
	// Where the state lives: `GET/PATCH /api/appearance` (preset + CSS) and
	// `POST/GET/DELETE /api/appearance/wallpaper` (the image bytes) — per-install
	// user state (`data/appearance.json` + `data/appearance/`), NOT the event log
	// and NOT session-scoped. Applying is `#lib/theme/apply` (preset attribute +
	// one style element last in head + the wallpaper attribute and image URL).
	//
	// The backend guard has already reduced whatever is pasted to the layout-safe
	// allowlist before it is stored; the response's `dropped[]` is the report we
	// show. This component does NOT sanitise locally — one place decides, so what
	// the reader sees in "not applied" is always exactly what the browser skipped.
	// The wallpaper's caps work the same way: the server refuses with a sentence
	// and this component shows it verbatim (`error`), never a local pre-check that
	// could drift from what the door enforces.
	import { onMount } from 'svelte';
	import {
		getAppearance,
		patchAppearance,
		removeWallpaper,
		uploadWallpaper,
		type AppearanceState,
		type DroppedDeclaration
	} from '#lib/api/appearance';
	import { applyAlign, applyAppearance } from '#lib/theme/apply';
	import { register } from '#lib/uiLayers';

	let { onClose }: { onClose: () => void } = $props();

	/** The loaded catalog + guard (null until the first read lands). */
	let catalog = $state<AppearanceState | null>(null);
	/** The live preset id (optimistic; the response re-syncs it). */
	let preset = $state<string>('dark');
	/** The live alignment id (optimistic; the response re-syncs it). */
	let align = $state<string>('center');
	/** The paste box's draft — saved explicitly, so a half-typed rule never lands. */
	let draftCss = $state<string>('');
	/** What the guard refused on the last save. */
	let dropped = $state<DroppedDeclaration[]>([]);
	/** The hidden file input (the visible affordance is the button below). */
	let fileInput = $state<HTMLInputElement | null>(null);
	let busy = $state(false);
	let error = $state('');
	let status = $state('');
	let loaded = $state(false);

	onMount(() => {
		// Escape closes only the topmost surface (uiLayers owns the one listener).
		const unregister = register('appearance', () => onClose());
		void load();
		return () => unregister();
	});

	async function load(): Promise<void> {
		try {
			const read = await getAppearance();
			catalog = read;
			preset = read.preset;
			align = read.align;
			draftCss = read.custom_css;
			applyAppearance(read);
		} catch (err) {
			error = err instanceof Error ? err.message : 'could not read the appearance';
		} finally {
			loaded = true;
		}
	}

	async function choose(next: string): Promise<void> {
		if (busy || next === preset) return;
		const previous = preset;
		preset = next;
		busy = true;
		error = '';
		try {
			const saved = await patchAppearance({ preset: next });
			catalog = saved;
			preset = saved.preset;
			applyAppearance(saved);
			status = `${saved.presets.find((p) => p.id === saved.preset)?.label ?? saved.preset} preset applied.`;
		} catch (err) {
			preset = previous;
			error = err instanceof Error ? err.message : 'could not save the preset';
		} finally {
			busy = false;
		}
	}

	/** Issue #41: move the reading column. Applied to the DOM BEFORE the write
	 *  lands (the reader sees the move immediately) and reverted if the store
	 *  refuses it; the response is the truth (`saved.align`). */
	async function chooseAlign(next: string): Promise<void> {
		if (busy || next === align) return;
		const previous = align;
		align = next;
		applyAlign(next);
		busy = true;
		error = '';
		try {
			const saved = await patchAppearance({ align: next });
			catalog = saved;
			align = saved.align;
			applyAppearance(saved);
			const label = saved.alignments.find((option) => option.id === saved.align)?.label;
			status = `Alignment: ${label ?? saved.align}.`;
		} catch (err) {
			align = previous;
			applyAlign(previous);
			error = err instanceof Error ? err.message : 'could not save the alignment';
		} finally {
			busy = false;
		}
	}

	/** Open the (hidden) file picker: a real input, so the browser's own dialog
	 *  is what the reader sees, filtered by the accepted types from the caps. */
	function pickWallpaper(): void {
		if (busy) return;
		fileInput?.click();
	}

	async function onWallpaperChosen(event: Event): Promise<void> {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		// Clear the input immediately: re-picking the SAME file must still fire a
		// change event (a re-pick is how a reader replaces a bad crop).
		input.value = '';
		if (!file || busy) return;
		busy = true;
		error = '';
		status = '';
		try {
			const saved = await uploadWallpaper(file);
			catalog = saved;
			preset = saved.preset;
			draftCss = saved.custom_css;
			applyAppearance(saved);
			status = `Wallpaper applied — ${file.name}.`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'could not store the wallpaper';
		} finally {
			busy = false;
		}
	}

	async function clearWallpaper(): Promise<void> {
		if (busy) return;
		busy = true;
		error = '';
		status = '';
		try {
			const saved = await removeWallpaper();
			catalog = saved;
			preset = saved.preset;
			applyAppearance(saved);
			status = 'Wallpaper removed.';
		} catch (err) {
			error = err instanceof Error ? err.message : 'could not remove the wallpaper';
		} finally {
			busy = false;
		}
	}

	/** Fit has one value today (cover); the handler exists so a second mode is a
	 *  data change (the backend's FITS) rather than a new control. */
	async function chooseFit(next: string): Promise<void> {
		if (busy || !catalog?.wallpaper || next === catalog.fit) return;
		busy = true;
		error = '';
		try {
			const saved = await patchAppearance({ fit: next });
			catalog = saved;
			applyAppearance(saved);
			status = `Fit set to ${next}.`;
		} catch (err) {
			error = err instanceof Error ? err.message : 'could not save the fit';
		} finally {
			busy = false;
		}
	}

	async function saveCss(): Promise<void> {
		if (busy) return;
		busy = true;
		error = '';
		status = '';
		try {
			const saved = await patchAppearance({ custom_css: draftCss });
			catalog = saved;
			preset = saved.preset;
			// The server stores the GUARDED text, so the box now shows exactly what
			// the browser was handed — and the dropped list explains the difference.
			draftCss = saved.custom_css;
			dropped = saved.dropped;
			applyAppearance(saved);
			status = saved.dropped.length
				? `Saved — ${saved.dropped.length} declaration${saved.dropped.length === 1 ? '' : 's'} not applied (listed below).`
				: 'Saved.';
		} catch (err) {
			error = err instanceof Error ? err.message : 'could not save the custom CSS';
		} finally {
			busy = false;
		}
	}

	async function clearCss(): Promise<void> {
		draftCss = '';
		await saveCss();
	}

	/** The example shown in the empty paste box (kept out of the markup: the CSS
	 * braces inside an attribute expression are easy for the Svelte parser to
	 * mistake for markup, and the example is constant anyway). */
	const CSS_PLACEHOLDER =
		"/* e.g.\n" +
		":root { --accent-hue: 200; }\n" +
		"body { font-family: 'Iowan Old Style', serif; }\n" +
		"*/";

	const guard = $derived(catalog?.guard ?? null);
	const presets = $derived(catalog?.presets ?? []);
	const alignments = $derived(catalog?.alignments ?? []);
	const blockedFamilies = $derived(Object.entries(guard?.blocked ?? {}));
	const wallpaper = $derived(catalog?.wallpaper ?? null);
	const limits = $derived(catalog?.wallpaper_limits ?? null);
	const fits = $derived(catalog?.fits ?? []);
	/** The `<input accept>` list, built from the caps the backend enforces. */
	const accept = $derived((limits?.formats ?? []).map((format) => format.mime).join(','));
	const limitsLine = $derived(
		limits
			? `${limits.formats.map((format) => format.label).join(', ')} · up to ${limits.max_bytes_label} · ${limits.max_edge}px on each edge.`
			: ''
	);
</script>

<div class="scrim" role="presentation" onclick={onClose}></div>
<aside class="appearance-drawer" aria-label="Appearance">
	<header>
		<div>
			<h2>Appearance</h2>
			<p class="sub">
				One preset for the whole app, one image behind the prose, plus one stylesheet if you
				want to change more. Per install — not per chat.
			</p>
		</div>
		<button type="button" class="close" aria-label="Close appearance" onclick={onClose}>×</button>
	</header>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}
	{#if status}
		<p class="status">{status}</p>
	{/if}

	<div class="body">
		<section class="block">
			<h3>Preset</h3>
			<div class="presets" role="radiogroup" aria-label="Theme preset">
				{#each presets as option (option.id)}
					<button
						type="button"
						class="preset"
						class:active={preset === option.id}
						role="radio"
						aria-checked={preset === option.id}
						disabled={busy}
						onclick={() => void choose(option.id)}
					>
						<span class="preset-name">{option.label}</span>
						<!-- The scheme is a visual hint only; keeping it out of the
						     accessibility tree keeps the radio's name exactly its label. -->
						<span class="preset-scheme" aria-hidden="true">{option.color_scheme}</span>
					</button>
				{/each}
			</div>
			{#if !loaded}
				<p class="hint">Reading the saved appearance…</p>
			{/if}
			<p class="hint">
				Light is the strict preset (every divider holds 3:1). Dark is the default. OLED is
				true black, driven by the display; Savilen is the reference teal.
			</p>
		</section>

		<!-- Issue #41: where the reading column sits in the window. Same install-wide
		     document as the preset (data/appearance.json), applied immediately: the
		     measure keeps its width, only its inline margins move. -->
		<section class="block">
			<h3>Alignment</h3>
			<div class="aligns" role="radiogroup" aria-label="Content alignment">
				{#each alignments as option (option.id)}
					<button
						type="button"
						class="align"
						class:active={align === option.id}
						role="radio"
						aria-checked={align === option.id}
						disabled={busy}
						onclick={() => void chooseAlign(option.id)}
					>
						{option.label}
					</button>
				{/each}
			</div>
			<p class="hint">
				Where the reading column sits. Center is the default; the measure is the same
				either way.
			</p>
		</section>

		<!-- Issue #13: the image behind the prose. Sits BELOW the preset because it
		     is the second thing a reader reaches for after the palette, and it is
		     the one control that changes what the whole page looks like at once. -->
		<section class="block">
			<h3>Wallpaper</h3>
			<p class="hint">
				An image behind the prose. The reading surfaces keep a scrim over it, so text stays
				legible whatever you pick — the room shows through, the words do not compete with it.
			</p>
			{#if wallpaper}
				<div class="wallpaper">
					<img class="wallpaper-preview" src={wallpaper.url} alt="The current wallpaper" />
					<div class="wallpaper-meta">
						<span class="wallpaper-name">{wallpaper.name}</span>
						<span class="count">{wallpaper.width}×{wallpaper.height}px · {wallpaper.format}</span>
					</div>
				</div>
			{:else}
				<p class="hint">None — the shipped page colour.</p>
			{/if}
			<input
				class="sr-only"
				bind:this={fileInput}
				type="file"
				accept={accept}
				onchange={(event) => void onWallpaperChosen(event)}
			/>
			<div class="row">
				<button type="button" class="primary" disabled={busy} onclick={pickWallpaper}>
					{busy ? 'Saving…' : wallpaper ? 'Replace image…' : 'Choose image…'}
				</button>
				{#if wallpaper}
					<button type="button" class="ghost" disabled={busy} onclick={() => void clearWallpaper()}>
						Remove
					</button>
				{/if}
				{#if fits.length > 1 && wallpaper}
					<label class="fit">
						<span>Fit</span>
						<select
							value={wallpaper.fit}
							disabled={busy}
							onchange={(event) => void chooseFit(event.currentTarget.value)}
						>
							{#each fits as option (option)}
								<option value={option}>{option}</option>
							{/each}
						</select>
					</label>
				{:else if wallpaper}
					<!-- One mode ships (cover). The line still states what it is, so the
					     setting is visible rather than implied by a missing control. -->
					<span class="count">Fit: cover</span>
				{/if}
			</div>
			{#if limitsLine}
				<p class="note">{limitsLine}</p>
			{/if}
		</section>

		<section class="block">
			<h3>Custom CSS</h3>
			<label class="sr-only" for="appearance-custom-css">Your stylesheet</label>
			<textarea
				id="appearance-custom-css"
				class="css"
				spellcheck="false"
				rows="12"
				placeholder={CSS_PLACEHOLDER}
				bind:value={draftCss}
				disabled={busy}
			></textarea>
			<div class="row">
				<button type="button" class="primary" disabled={busy} onclick={() => void saveCss()}>
					{busy ? 'Saving…' : 'Apply CSS'}
				</button>
				<button type="button" class="ghost" disabled={busy || !draftCss} onclick={() => void clearCss()}>
					Clear
				</button>
				{#if guard}
					<span class="count">{draftCss.length} / {guard.max_custom_css_chars}</span>
				{/if}
			</div>
			<!-- Owner ruling D-D, one sentence (the exact text comes from the backend,
			     so the drawer and the docs cannot drift apart). -->
			<p class="note">{guard?.important_note ?? ''}</p>

			{#if dropped.length}
				<div class="dropped" role="status">
					<p class="dropped-title">Not applied</p>
					<ul>
						{#each dropped as item, index (index)}
							<li>
								<code>{item.declaration}</code>
								<span class="why">{item.reason}</span>
							</li>
						{/each}
					</ul>
				</div>
			{/if}
		</section>

		<section class="block">
			<h3>What this box may change</h3>
			<p class="hint">
				<strong>Allowed:</strong> colours, backgrounds, border colours and radii, fonts at 14px or
				larger, and decoration (shadows, spacing between letters, case).
			</p>
			<p class="hint">
				<strong>Refused on save</strong> — the layout is not yours to move:
			</p>
			<ul class="families">
				{#each blockedFamilies as [family, description] (family)}
					<li><span class="family">{family}</span> — {description}</li>
				{/each}
			</ul>
			<p class="hint">
				A refusal is listed under “Not applied” after you save; nothing is dropped silently.
			</p>
		</section>
	</div>
</aside>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 22;
	}
	.appearance-drawer {
		position: fixed;
		top: 0;
		right: 0;
		height: 100vh;
		width: 30rem;
		max-width: 96vw;
		z-index: 23;
		display: flex;
		flex-direction: column;
		background: var(--bg-panel);
		border-left: 1px solid var(--line-strong);
		box-shadow: -8px 0 24px var(--shadow);
		color: var(--text);
	}
	header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.85rem 1rem;
		border-bottom: 1px solid var(--line);
	}
	h2 {
		margin: 0;
		font-size: var(--font-lg);
	}
	h3 {
		margin: 0 0 0.4rem;
		font-size: var(--font-ui);
	}
	.sub,
	.hint,
	.note {
		margin: 0.2rem 0 0;
		font-size: var(--font-small);
		line-height: 1.5;
		color: var(--text-muted);
	}
	.note {
		margin-top: 0.5rem;
		color: var(--text-dim);
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
	.error,
	.status {
		margin: 0.6rem 1rem 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
	}
	.error {
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.status {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.body {
		flex: 1;
		min-height: 0;
		overflow-y: auto;
		padding: 0.9rem 1rem 2rem;
		display: flex;
		flex-direction: column;
		gap: 1.2rem;
	}
	.block {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
	}
	.presets {
		display: flex;
		gap: 0.5rem;
		flex-wrap: wrap;
	}
	/* Issue #41: a segmented control — the three positions read as one row, not
	 * as three independent toggles, because exactly one mode is live. */
	.aligns {
		display: flex;
		align-self: flex-start;
		border: 1px solid var(--line-strong);
		border-radius: 8px;
		overflow: hidden;
		background: var(--bg-raised);
	}
	.align {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.4rem 0.9rem;
		border: none;
		border-left: 1px solid var(--line-strong);
		background: none;
		color: var(--text-muted);
		cursor: pointer;
	}
	.align:first-child {
		border-left: none;
	}
	.align:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--accent-strong);
	}
	.align.active {
		background: var(--bg-active);
		color: var(--text);
	}
	.align:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: -2px;
	}
	.preset {
		font: inherit;
		flex: 1 1 6rem;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		padding: 0.5rem 0.65rem;
		border: 1px solid var(--line-strong);
		border-radius: 8px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
		text-align: left;
	}
	.preset:hover:not(:disabled) {
		background: var(--bg-hover);
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.preset.active {
		background: var(--bg-active);
		border-color: var(--accent);
		color: var(--text);
	}
	.preset:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.preset-name {
		font-size: var(--font-ui);
		font-weight: 600;
	}
	.preset-scheme {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.css {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-small);
		line-height: 1.45;
		min-height: 10rem;
		resize: vertical;
		padding: 0.55rem 0.65rem;
		border: 1px solid var(--line-strong);
		border-radius: 8px;
		background: var(--bg-inset);
		color: var(--text);
	}
	.css:focus-visible {
		outline: 2px solid var(--accent-strong);
		outline-offset: 1px;
	}
	.wallpaper {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		padding: 0.4rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
	}
	.wallpaper-preview {
		width: 5rem;
		height: 3.25rem;
		object-fit: cover;
		border-radius: 5px;
		border: 1px solid var(--line);
		flex: 0 0 auto;
	}
	.wallpaper-meta {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		min-width: 0;
	}
	.wallpaper-name {
		font-size: var(--font-ui);
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.fit {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.fit select {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.25rem 0.4rem;
		border: 1px solid var(--line-strong);
		border-radius: 6px;
		background: var(--bg-inset);
		color: var(--text);
	}
	.row {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		margin-top: 0.45rem;
	}
	.primary,
	.ghost {
		font: inherit;
		font-size: var(--font-ui);
		padding: 0.4rem 0.8rem;
		border-radius: 8px;
		cursor: pointer;
		border: 1px solid var(--line-strong);
		background: var(--bg-raised);
		color: var(--text);
	}
	.primary {
		border-color: var(--accent);
		background: var(--accent);
		color: var(--on-accent);
	}
	.primary:hover:not(:disabled) {
		background: var(--accent-hover);
	}
	.ghost {
		color: var(--text-muted);
	}
	.ghost:hover:not(:disabled) {
		background: var(--bg-hover);
		color: var(--accent-strong);
	}
	.primary:disabled,
	.ghost:disabled,
	.preset:disabled,
	.align:disabled {
		opacity: 0.6;
		cursor: default;
	}
	.count {
		margin-left: auto;
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.dropped {
		margin-top: 0.6rem;
		padding: 0.5rem 0.65rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
	}
	.dropped-title {
		margin: 0 0 0.3rem;
		font-size: var(--font-ui);
		color: var(--warn);
	}
	.dropped ul,
	.families {
		margin: 0;
		padding-left: 1.1rem;
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		font-size: var(--font-small);
		line-height: 1.45;
		color: var(--text-muted);
	}
	.dropped code {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		color: var(--text);
	}
	.why {
		display: block;
		color: var(--text-dim);
	}
	.family {
		color: var(--text);
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
