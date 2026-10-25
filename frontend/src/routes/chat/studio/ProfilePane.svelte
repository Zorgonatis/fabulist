<script lang="ts">
	// Studio Profiles pane (D51 §3 L2.e) — the ProfileDrawer BODY, migrated.
	//
	// Experience-profile export / import (Wave D, D33): a portable
	// definitions-only bundle (a world + its cast + books, a prompt preset, and
	// optionally the assistant roster). Profiles are log-owned entities; this
	// pane owns only the draft form state and the API calls — the studio shell
	// owns the frame (title, close, Escape, dirty guard), so the markup below is
	// the former `EntityDrawer` body with its shell removed.
	//
	// Assistant semantics (owner-ratified amendment to D33): ABSENT != EMPTY. An
	// envelope without an `assistants` key never touches the local roster on
	// import; only an explicit `assistants: []` clears it. Two UI consequences:
	// the "Include assistants" toggle defaults ON (OFF omits the key), and an
	// explicit-`[]` envelope is gated behind a roster-clear confirmation. A
	// pre-rename file's roster is folded on read by `envelopeAssistantRoster`, so
	// the pre-flight summary matches what the import will actually do.
	//
	// PNG representation (owner ruling #7): the
	// export block offers the SAME envelope as PNG — the profile's assigned image
	// as the PNG body, the envelope JSON in a `fabulist_profile` tEXt chunk
	// (`GET /api/profiles/export.png`, same query params as the JSON route). The
	// import file picker accepts `.json` AND `.png`: a PNG's chunk is decoded
	// here (the browser has no PNG reader, and the roster-clear gate needs the
	// envelope BEFORE the request is made) and then travels the ordinary import
	// path. The backend also accepts a raw PNG body on the import route.
	import { onMount } from 'svelte';
	import type {
		ImportResult,
		ImportRow,
		ProfileEnvelope,
		ProfileWarning,
		PromptPreset,
		World
	} from '#lib/api';
	import {
		API_BASE,
		USE_MOCK,
		envelopeAssistantRoster,
		exportProfile,
		getActivePreset,
		getDefaultPreset,
		importProfile,
		importProfilePng,
		listPresets,
		listAssistants,
		listWorlds
	} from '#lib/api';

	/** Props every studio pane receives from the shell (D51 §2.3). */
	interface StudioPaneProps {
		focusId?: string | null;
		startNew?: boolean;
		requestSeq?: number | null;
		onChanged?: () => void;
		onDirty?: (dirty: boolean) => void;
	}

	// `focusId`, `startNew` and `requestSeq` are part of the shell's pane contract
	// (D51 §2.3 / §3 L3.c), but a profile is a whole-log envelope: there is no
	// per-instance list entry to focus and nothing to draft ahead of time, so this
	// pane no-ops all three (documented no-op). They stay declared in
	// `StudioPaneProps` so the shell can hand every pane the same props (that is
	// the whole point of the uniform contract).
	let { onChanged, onDirty }: StudioPaneProps = $props();

	/** The pane's two surfaces. */
	type Tab = 'export' | 'import';

	/** One selectable preset (id + display name). */
	interface PresetOption {
		id: string;
		name: string;
	}

	/** How the envelope treats the local assistant roster. */
	type AssistantPresence = 'absent' | 'empty' | 'present';

	/** The pre-flight read of a parsed envelope. */
	interface EnvelopeSummary {
		name: string;
		worldName: string | null;
		castCount: number;
		bookCount: number;
		presetName: string | null;
		assistants: AssistantPresence;
		assistantCount: number;
	}

	let tab = $state<Tab>('export');
	let loading = $state(true);
	let error = $state('');

	// --- Export tab ---------------------------------------------------------
	let presetOptions = $state<PresetOption[]>([]);
	let worlds = $state<World[]>([]);
	let presetId = $state('');
	let worldId = $state('');
	// A full profile ships the roster; absent != empty, so OFF must be explicit.
	let includeAssistants = $state(true);
	let profileName = $state('');
	let exportBusy = $state(false);
	let exportStatus = $state('');
	let pngBusy = $state(false);

	// --- Import tab ---------------------------------------------------------
	/** Non-demo assistants the local log owns (built-in placeholders excluded). */
	let localRosterCount = $state(0);
	let envelopeText = $state('');
	let parsed = $state<ProfileEnvelope | null>(null);
	let parseError = $state('');
	/** Acknowledgement gate for an explicit-empty roster clear. */
	let rosterConfirmed = $state(false);
	let importBusy = $state(false);
	let result = $state<ImportResult | null>(null);
	let importFileInput = $state<HTMLInputElement | null>(null);
	/**
	 * The raw bytes of a chosen profile PNG, when the source was a PNG.
	 *
	 * The envelope is decoded client-side for the pre-flight gate, but the PNG
	 * body is the profile's assigned image — importing through the JSON path
	 * would drop it. When set, `doImport` posts the bytes to the import route's
	 * PNG representation instead (cleared when a JSON source is read).
	 */
	let importPngBytes = $state<Uint8Array | null>(null);

	// --- Pane-contract dirty flag ------------------------------------------
	// The drawer tracked no dirtiness; the pane contract needs one. The honest
	// reading for this pane: a parsed envelope that has NOT been imported yet is
	// unsaved work — switching studio tabs unmounts this pane (keyed) and the
	// staged envelope is gone, so the shell's guard is the only thing that can
	// protect it. Set true when a source parses, false once it is imported.
	let importStaged = $state(false);

	$effect(() => {
		onDirty?.(importStaged);
	});

	const summary = $derived(parsed ? describeEnvelope(parsed) : null);
	const clearsRoster = $derived(
		summary !== null && summary.assistants === 'empty' && localRosterCount > 0
	);

	function message(err: unknown): string {
		return err instanceof Error ? err.message : String(err);
	}

	function strField(value: unknown): string {
		return typeof value === 'string' ? value : '';
	}

	onMount(() => {
		void load();
	});

	async function load(): Promise<void> {
		loading = true;
		try {
			const [presets, active, fallback, loadedWorlds, roster] = await Promise.all([
				listPresets(),
				getActivePreset(),
				getDefaultPreset(),
				listWorlds(),
				listAssistants()
			]);
			presetOptions = mergePresets(presets, active, fallback);
			worlds = loadedWorlds;
			localRosterCount = roster.assistants.filter((assistant) => !assistant.demo).length;
			presetId = active?.preset_id ?? presetOptions[0]?.id ?? '';
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			loading = false;
		}
	}

	/**
	 * The picker's option list: the named presets, then the active preset, then
	 * the built-in default pipeline — deduped by id, first-seen order. The
	 * default is always offered so a fresh/mock install (no named presets yet)
	 * can still export the running pipeline.
	 */
	function mergePresets(
		presets: PromptPreset[],
		active: PromptPreset | null,
		fallback: PromptPreset | null
	): PresetOption[] {
		const options: PresetOption[] = [];
		const seen = new Set<string>();
		for (const preset of [active, ...presets, fallback]) {
			if (!preset || seen.has(preset.preset_id)) continue;
			seen.add(preset.preset_id);
			options.push({ id: preset.preset_id, name: preset.name || preset.preset_id });
		}
		return options;
	}

	async function doExport(): Promise<void> {
		if (exportBusy) return;
		if (!presetId) {
			error = 'Choose a preset to export.';
			return;
		}
		exportBusy = true;
		exportStatus = '';
		try {
			const { filename } = await exportProfile({
				preset: presetId,
				world: worldId || null,
				assistants: includeAssistants,
				name: profileName.trim() || null
			});
			exportStatus = `Exported ${filename}`;
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			exportBusy = false;
		}
	}

	/**
	 * The export URL for the PNG representation: the JSON export's query params,
	 * one different representation (`GET /api/profiles/export.png`). Mirrors the
	 * `PROFILE_ENDPOINTS.export` builder, which is JSON-only.
	 */
	function pngExportUrl(): string {
		const query = new URLSearchParams();
		query.set('preset', presetId);
		if (worldId) query.set('world', worldId);
		if (includeAssistants) query.set('assistants', '1');
		const name = profileName.trim();
		if (name) query.set('name', name);
		return `${API_BASE}/api/profiles/export.png?${query.toString()}`;
	}

	/** The backend's error reason (`detail`), or an empty string. */
	async function errorReason(res: Response): Promise<string> {
		try {
			const body = (await res.json()) as { detail?: unknown };
			return typeof body.detail === 'string' ? body.detail : '';
		} catch {
			return '';
		}
	}

	/** `filename="…"` from a Content-Disposition header, or null. */
	function filenameFromHeader(header: string | null): string | null {
		const match = header?.match(/filename="?([^";]+)"?/);
		return match ? match[1] : null;
	}

	/**
	 * Export the profile as PNG (item G): the assigned image as the body, the full
	 * envelope in a text chunk. Requires the engine — the image bytes live in the
	 * log — so mock mode reports why instead of downloading nothing.
	 */
	async function doExportPng(): Promise<void> {
		if (!presetId) {
			error = 'Choose a preset to export.';
			return;
		}
		if (pngBusy) return;
		pngBusy = true;
		exportStatus = '';
		try {
			if (USE_MOCK) {
				throw new Error("PNG export needs the engine — the profile's image lives in the log");
			}
			const res = await fetch(pngExportUrl(), { headers: { Accept: 'image/png' } });
			if (!res.ok) {
				const reason = await errorReason(res);
				throw new Error(reason || `PNG export failed: ${res.status} ${res.statusText}`);
			}
			const blob = await res.blob();
			const filename =
				filenameFromHeader(res.headers.get('content-disposition')) ?? 'profile.profile.png';
			const url = URL.createObjectURL(blob);
			const anchor = document.createElement('a');
			anchor.href = url;
			anchor.download = filename;
			anchor.click();
			URL.revokeObjectURL(url);
			exportStatus = `Exported ${filename}`;
			error = '';
		} catch (err) {
			error = message(err);
		} finally {
			pngBusy = false;
		}
	}

	/** Clear every staged-import field (no source loaded). */
	function resetSource(): void {
		result = null;
		rosterConfirmed = false;
		parsed = null;
		parseError = '';
		importStaged = false;
		importPngBytes = null;
	}

	/** Parse the pasted/file source into a validated envelope (or a parse error). */
	function loadSource(text: string): void {
		if (!text.trim()) {
			resetSource();
			return;
		}
		result = null;
		rosterConfirmed = false;
		try {
			const value = JSON.parse(text) as ProfileEnvelope;
			if (!value || typeof value !== 'object') throw new Error('the envelope is not an object');
			if (value.format !== 'fabulist-profile') {
				throw new Error(`not a fabulist-profile envelope (format: ${String(value.format ?? 'missing')})`);
			}
			if (typeof value.name !== 'string' || !value.name.trim()) {
				throw new Error("the envelope is missing a non-empty 'name'");
			}
			parsed = value;
			parseError = '';
			importStaged = true;
		} catch (err) {
			parsed = null;
			parseError = message(err);
			importStaged = false;
		}
	}

	function pickImportFile(): void {
		importFileInput?.click();
	}

	/** The 8-byte PNG magic (mirrors the backend's signature-detected import). */
	function isPngBytes(bytes: Uint8Array): boolean {
		const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
		return bytes.length >= signature.length && signature.every((b, i) => bytes[i] === b);
	}

	/**
	 * The envelope JSON text inside a profile PNG (item G).
	 *
	 * The backend writes the full envelope into one `fabulist_profile` tEXt chunk
	 * as base64 UTF-8 JSON; this reads the first such chunk, mirroring the
	 * backend reader's rule. Throws with the backend's wording when the file is not
	 * a PNG or carries no chunk, so a wrong file reports the same reason a PNG
	 * posted to the import route would.
	 */
	function readProfilePng(bytes: Uint8Array): string {
		if (!isPngBytes(bytes)) {
			throw new Error('not a PNG: missing the 8-byte PNG signature');
		}
		const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
		const ascii = new TextDecoder('latin1');
		let offset = 8;
		while (offset + 8 <= bytes.length) {
			const length = view.getUint32(offset);
			const type = ascii.decode(bytes.subarray(offset + 4, offset + 8));
			const dataStart = offset + 8;
			const dataEnd = dataStart + length;
			if (dataEnd + 4 > bytes.length) {
				throw new Error('unreadable PNG: chunk extends past end of file');
			}
			if (type === 'tEXt') {
				const text = ascii.decode(bytes.subarray(dataStart, dataEnd));
				const nul = text.indexOf('\u0000');
				if (nul !== -1 && text.slice(0, nul) === 'fabulist_profile') {
					return decodeProfileChunk(text.slice(nul + 1));
				}
			}
			offset = dataEnd + 4;
			if (type === 'IEND') break;
		}
		throw new Error(
			"PNG carries no 'fabulist_profile' tEXt chunk (not a profile exported by this app?)"
		);
	}

	/** Base64 + UTF-8 decode of one chunk payload (throws the backend's wording). */
	function decodeProfileChunk(payload: string): string {
		let binary: string;
		try {
			binary = atob(payload.replace(/\s+/g, ''));
		} catch (err) {
			throw new Error(`'fabulist_profile' chunk is not valid base64: ${message(err)}`);
		}
		const decoded = new TextDecoder().decode(
			Uint8Array.from(binary, (char) => char.charCodeAt(0))
		);
		if (!decoded.trim()) throw new Error("'fabulist_profile' chunk is empty");
		return decoded;
	}

	async function onFileChosen(event: Event): Promise<void> {
		const input = event.target as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file) return;
		const bytes = new Uint8Array(await file.arrayBuffer());
		importPngBytes = null;
		let text: string;
		try {
			if (isPngBytes(bytes)) {
				text = readProfilePng(bytes);
				// Keep the body: it is the profile's assigned image and must ride the
				// PNG import so the target log can persist it (item G).
				importPngBytes = bytes;
			} else {
				text = new TextDecoder().decode(bytes);
			}
		} catch (err) {
			resetSource();
			envelopeText = '';
			parseError = message(err);
			return;
		}
		envelopeText = text;
		loadSource(text);
	}

	function describeEnvelope(envelope: ProfileEnvelope): EnvelopeSummary {
		const bundle = envelope.world;
		const worldName = bundle?.world ? strField(bundle.world.name) || null : null;
		const castCount = Array.isArray(bundle?.cast) ? bundle.cast.length : 0;
		const bookCount = Array.isArray(bundle?.books) ? bundle.books.length : 0;
		const presetName = envelope.preset
			? strField(envelope.preset.name) || strField(envelope.preset.preset_id) || null
			: null;
		let assistants: AssistantPresence = 'absent';
		let assistantCount = 0;
		const roster = envelopeAssistantRoster(envelope);
		if (roster) {
			assistantCount = roster.length;
			assistants = assistantCount === 0 ? 'empty' : 'present';
		}
		return {
			name: envelope.name,
			worldName,
			castCount,
			bookCount,
			presetName,
			assistants,
			assistantCount
		};
	}

	function assistantSummary(presence: AssistantPresence, count: number): string {
		if (presence === 'absent') return 'not included — roster untouched on import';
		if (presence === 'empty') return 'explicitly empty — CLEARS the local roster';
		return `included (${count})`;
	}

	async function doImport(): Promise<void> {
		if (!parsed || importBusy) return;
		if (clearsRoster && !rosterConfirmed) return;
		importBusy = true;
		result = null;
		try {
			// A staged PNG goes back as bytes so its body image is persisted on the
			// target; a JSON/text source (mock always) uses the envelope path.
			result =
				importPngBytes && !USE_MOCK
					? await importProfilePng(importPngBytes)
					: await importProfile(parsed);
			error = '';
			// The staged envelope has landed; nothing left for the dirty guard.
			importStaged = false;
			onChanged?.();
		} catch (err) {
			error = message(err);
		} finally {
			importBusy = false;
		}
	}

	/** Render one `{kind, id, name?, count?}` result row as a readable line. */
	function rowLabel(row: ImportRow): string {
		const label = row.name || row.id;
		const count = typeof row.count === 'number' ? ` (${row.count})` : '';
		return `${row.kind}: ${label}${count}`;
	}

	/** Render one warning in plain language (discriminated on `type`). */
	function warningText(warning: ProfileWarning): string {
		switch (warning.type) {
			case 'agent_binding': {
				const fallback = warning.fell_back_to
					? `fell back to '${warning.fell_back_to}'`
					: 'dropped (no local fallback)';
				return `${warning.interaction ?? 'unknown'} binding '${warning.wanted ?? '?'}' not known locally — ${fallback}`;
			}
			case 'preset_id':
				return `preset '${warning.wanted ?? 'default'}' cannot be superseded — imported as '${warning.assigned ?? '?'}'`;
			default:
				return JSON.stringify(warning);
		}
	}
</script>

<div class="profile-pane">
	<p class="sub">
		An experience profile bundles a world, its cast and books, and a prompt preset into one
		portable file.
	</p>

	<div class="tabs" role="tablist">
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'export'}
			class:active={tab === 'export'}
			onclick={() => (tab = 'export')}
		>
			Export
		</button>
		<button
			type="button"
			role="tab"
			aria-selected={tab === 'import'}
			class:active={tab === 'import'}
			onclick={() => (tab = 'import')}
		>
			Import
		</button>
	</div>

	{#if error}
		<p class="error" role="alert">{error}</p>
	{/if}

	{#if loading}
		<p class="hint">Loading…</p>
	{:else if tab === 'export'}
		<form
			class="form"
			onsubmit={(event) => {
				event.preventDefault();
				void doExport();
			}}
		>
			<label class="field">
				<span>Preset</span>
				{#if presetOptions.length === 0}
					<p class="hint">No presets available — create one in Prompts first.</p>
				{:else}
					<select bind:value={presetId}>
						{#each presetOptions as option (option.id)}
							<option value={option.id}>{option.name}</option>
						{/each}
					</select>
				{/if}
			</label>

			<label class="field">
				<span>World (optional)</span>
				<select bind:value={worldId}>
					<option value="">No world — preset only</option>
					{#each worlds as world (world.world_id)}
						<option value={world.world_id}>{world.name || world.world_id}</option>
					{/each}
				</select>
			</label>

			<label class="check">
				<input type="checkbox" bind:checked={includeAssistants} />
				<span>Include assistants</span>
			</label>
			{#if includeAssistants}
				<p class="hint">The roster rides the envelope and replaces it on import.</p>
			{:else}
				<p class="hint">
					Off — the envelope omits the assistant key, so the recipient's roster is untouched
					on import.
				</p>
			{/if}

			<label class="field">
				<span>Profile name (optional)</span>
				<input bind:value={profileName} placeholder="Defaults to the world name" autocomplete="off" />
			</label>

			<div class="form-actions">
				<button type="submit" class="primary" disabled={exportBusy || presetOptions.length === 0}>
					{exportBusy ? 'Exporting…' : 'Export profile'}
				</button>
				<button
					type="button"
					class="ghost"
					disabled={pngBusy || exportBusy || presetOptions.length === 0}
					onclick={() => void doExportPng()}
				>
					{pngBusy ? 'Exporting…' : 'Export PNG'}
				</button>
			</div>
			<p class="hint">
				PNG: the profile's assigned image (world, else cast) as the image body, the full profile in
				a text chunk — needs a world or cast image, and only a PNG image.
			</p>
			{#if exportStatus}
				<p class="status" role="status">{exportStatus}</p>
			{/if}
		</form>
	{:else}
		<div class="form">
			<div class="source-row">
				<input
					type="file"
					accept=".json,.png,application/json,image/png"
					hidden
					bind:this={importFileInput}
					onchange={(event) => void onFileChosen(event)}
				/>
				<button type="button" class="ghost" onclick={pickImportFile}>
					Choose profile file (JSON or PNG)…
				</button>
			</div>
			<label class="field">
				<span>…or paste an envelope</span>
				<textarea
					bind:value={envelopeText}
					rows="5"
					placeholder={'{ "format": "fabulist-profile", "format_version": 1, "name": "…", "world": …, "preset": … }'}
				></textarea>
			</label>
			<div class="form-actions">
				<button
					type="button"
					class="ghost"
					onclick={() => {
						importPngBytes = null;
						loadSource(envelopeText);
					}}
				>
					Read profile
				</button>
			</div>

			{#if parseError}
				<p class="error" role="alert">{parseError}</p>
			{/if}

			{#if summary}
				<div class="preflight">
					<h3>Pre-flight</h3>
					<dl>
						<div><dt>Name</dt><dd>{summary.name}</dd></div>
						<div><dt>World</dt><dd>{summary.worldName ?? '— none —'}</dd></div>
						<div><dt>Cast</dt><dd>{summary.castCount}</dd></div>
						<div><dt>Books</dt><dd>{summary.bookCount}</dd></div>
						<div><dt>Preset</dt><dd>{summary.presetName ?? '— none —'}</dd></div>
						<div>
							<dt>Assistants</dt>
							<dd>{assistantSummary(summary.assistants, summary.assistantCount)}</dd>
						</div>
					</dl>
				</div>

				{#if clearsRoster}
					<div class="gate" role="alert">
						<p>
							This profile carries an <strong>empty</strong> assistant list. Importing it will
							<strong>CLEAR your assistant roster</strong> ({localRosterCount}
							{localRosterCount === 1 ? 'assistant' : 'assistants'}).
						</p>
						<label class="check">
							<input type="checkbox" bind:checked={rosterConfirmed} />
							<span>I understand — clear my assistant roster.</span>
						</label>
					</div>
				{/if}

				<div class="form-actions">
					<button
						type="button"
						class="primary"
						disabled={importBusy || (clearsRoster && !rosterConfirmed)}
						onclick={() => void doImport()}
					>
						{importBusy ? 'Importing…' : 'Import profile'}
					</button>
				</div>
			{/if}

			{#if result}
				<div class="result">
					<h3>Import result</h3>
					<p class="result-line">
						{result.imported.length} imported · {result.skipped.length} skipped ·
						{result.warnings.length}
						{result.warnings.length === 1 ? 'warning' : 'warnings'}
					</p>
					{#if result.imported.length}
						<h4>Imported</h4>
						<ul class="rows">
							{#each result.imported as row (`${row.kind}:${row.id}`)}
								<li>{rowLabel(row)}</li>
							{/each}
						</ul>
					{/if}
					{#if result.skipped.length}
						<h4>Skipped (already up to date)</h4>
						<ul class="rows">
							{#each result.skipped as row (`${row.kind}:${row.id}`)}
								<li>{rowLabel(row)}</li>
							{/each}
						</ul>
					{/if}
					{#if result.warnings.length}
						<h4>Warnings</h4>
						<ul class="warnings">
							{#each result.warnings as warning, i (i)}
								<li>{warningText(warning)}</li>
							{/each}
						</ul>
					{/if}
				</div>
			{/if}
		</div>
	{/if}
</div>

<style>
	/* The pane is the former EntityDrawer body, minus the shell: the studio's
	   `.studio-body` supplies the padding/scroll, this div restores the body's
	   own column rhythm. */
	.profile-pane {
		display: flex;
		flex-direction: column;
		gap: 0.65rem;
	}
	h3 {
		margin: 0;
		font-size: var(--font-ui);
	}
	h4 {
		margin: 0.4rem 0 0.1rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.sub {
		margin: 0;
		font-size: var(--font-small);
		line-height: 1.4;
		color: var(--text-muted);
	}
	.tabs {
		display: flex;
		gap: 0.35rem;
	}
	.tabs button {
		font: inherit;
		font-size: var(--font-small);
		padding: 0.3rem 0.8rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.tabs button.active {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.error {
		margin: 0;
		padding: 0.45rem 0.6rem;
		font-size: var(--font-ui);
		border-radius: 6px;
		background: var(--danger-soft);
		color: var(--danger-text);
	}
	.hint {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
	.status {
		margin: 0.4rem 0 0;
		font-size: var(--font-small);
		color: var(--accent-strong);
	}
	.form {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
	}
	.field {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.field input,
	.field select,
	.field textarea {
		font: inherit;
		font-size: var(--font-ui);
		background: var(--bg-inset);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.35rem 0.5rem;
		resize: vertical;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 0.4rem;
		font-size: var(--font-ui);
		padding: 0.15rem 0.1rem;
		border-radius: 6px;
		cursor: pointer;
	}
	.source-row {
		display: flex;
		gap: 0.4rem;
	}
	.form-actions {
		display: flex;
		justify-content: flex-end;
		gap: 0.4rem;
		margin-top: 0.15rem;
	}
	button {
		font: inherit;
		font-size: var(--font-small);
		border-radius: 6px;
		cursor: pointer;
		padding: 0.35rem 0.7rem;
	}
	button:disabled {
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
	.preflight dl {
		margin: 0.3rem 0 0;
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
		font-size: var(--font-small);
	}
	.preflight dl > div {
		display: flex;
		gap: 0.5rem;
	}
	.preflight dt {
		flex: none;
		width: 6rem;
		color: var(--text-dim);
	}
	.preflight dd {
		margin: 0;
		color: var(--text);
	}
	.gate {
		margin-top: 0.6rem;
		padding: 0.55rem 0.65rem;
		border: 1px solid var(--danger-text);
		border-radius: 8px;
		background: var(--danger-soft);
		color: var(--danger-text);
		font-size: var(--font-small);
	}
	.gate p {
		margin: 0 0 0.35rem;
		line-height: 1.4;
	}
	.result {
		margin-top: 0.8rem;
		border-top: 1px solid var(--line);
		padding-top: 0.5rem;
	}
	.result-line {
		margin: 0.2rem 0;
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	.rows,
	.warnings {
		margin: 0.15rem 0;
		padding-left: 1.1rem;
		font-size: var(--font-small);
		line-height: 1.45;
	}
	.warnings {
		color: var(--danger-text);
	}
</style>
