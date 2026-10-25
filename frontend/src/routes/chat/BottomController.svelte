<script lang="ts">
	// Bottom controller (owner ruling 2026-10-13, item 8): the chat rail's bottom
	// bar. It is THREE things:
	//
	//  1. a NEW-ASSET door — one menu over the ASSET kinds (personas / worlds /
	//     books / assistants / profiles), each opening the Design Studio straight
	//     on that kind's create form (`startNew`). Definition CRUD is the studio's;
	//     this is only the door.
	//  2. a STUDIO map — every kind the shell can show, grouped Home / Assets /
	//     Engine, one chip per kind. The list is `#lib/studio/kinds` verbatim, so
	//     the rail map can never fall behind the shell's tab strip again (it used
	//     to be a 5-kind mirror, which left flow / proposals / queries / catalog /
	//     templates / planner without a door from here).
	//  3. a GLOBAL search — no active-chat scope. It queries
	//     `GET /api/search/global` (all visible sessions + all definitions) and
	//     routes a hit back out: a session calls `onOpenSession`, a definition
	//     calls `onOpenStudio(kind, { focusId })` to focus that row in the studio.
	//
	// The query is debounced (300 ms) and only runs at >= 2 chars; Escape clears.
	// Presentational only — the parent owns selection and the studio request.
	import { globalSearch } from '#lib/api/globalSearch';
	import type { GlobalSearchResult } from '#lib/api/globalSearch';
	import { STUDIO_GROUPS, kindsInGroup, type StudioKind } from '#lib/studio/kinds';
	import { register } from '#lib/uiLayers';

	/** Which footer menu is open. One at a time: they open upward over the rail. */
	type OpenMenu = 'map' | 'new' | null;

	let {
		onOpenStudio,
		onOpenSession
	}: {
		/** Open the Design Studio on `kind`; a search hit also focuses `focusId`,
		 *  a New-asset door jumps to the create form (`startNew`). */
		onOpenStudio: (
			kind: StudioKind,
			opts?: { focusId?: string | null; startNew?: boolean }
		) => void;
		/** Open a session the global search matched. */
		onOpenSession: (sessionId: string) => void;
	} = $props();

	const MIN_CHARS = 2;
	const DEBOUNCE_MS = 300;

	let query = $state('');
	let results = $state<GlobalSearchResult | null>(null);
	let busy = $state(false);
	let error = $state('');
	let timer: ReturnType<typeof setTimeout> | null = null;

	let menu = $state<OpenMenu>(null);
	let doorsEl = $state<HTMLElement | undefined>(undefined);

	function toggleMenu(which: Exclude<OpenMenu, null>): void {
		menu = menu === which ? null : which;
	}

	// A menu is a surface: Escape closes it (through the shared stack, so it goes
	// before the studio underneath) and a pointer press outside closes it. The
	// listeners exist only while a menu is open.
	$effect(() => {
		if (menu === null || typeof window === 'undefined') return;
		const unregister = register('rail-footer-menu', () => (menu = null));
		function onPointerDown(event: PointerEvent): void {
			if (doorsEl && event.target instanceof Node && doorsEl.contains(event.target)) return;
			menu = null;
		}
		window.addEventListener('pointerdown', onPointerDown, true);
		return () => {
			window.removeEventListener('pointerdown', onPointerDown, true);
			unregister();
		};
	});

	/** Every door: close the menu first so the studio it opens is not covered. */
	function openStudio(kind: StudioKind, opts?: { focusId?: string | null; startNew?: boolean }): void {
		menu = null;
		clear();
		onOpenStudio(kind, opts);
	}

	/** Debounced re-query; below the minimum the results clear immediately. */
	function schedule(): void {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
		const q = query.trim();
		if (q.length < MIN_CHARS) {
			results = null;
			busy = false;
			error = '';
			return;
		}
		busy = true;
		timer = setTimeout(() => void run(q), DEBOUNCE_MS);
	}

	async function run(q: string): Promise<void> {
		const out = await globalSearch(q);
		// A newer keystroke superseded this request: drop the stale response.
		if (query.trim() !== q) return;
		busy = false;
		if ('error' in out) {
			error = out.error;
			results = null;
		} else {
			error = '';
			results = out;
		}
	}

	function clear(): void {
		if (timer) {
			clearTimeout(timer);
			timer = null;
		}
		query = '';
		results = null;
		busy = false;
		error = '';
	}

	/** Controlled input: read the DOM value explicitly so the debounce never
	 *  depends on binding-listener ordering. */
	function onInput(event: Event): void {
		query = (event.currentTarget as HTMLInputElement).value;
		schedule();
	}

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') clear();
	}

	function openSession(sessionId: string): void {
		clear();
		onOpenSession(sessionId);
	}

	function openDefinition(kind: StudioKind, id: string): void {
		openStudio(kind, { focusId: id });
	}

	const hasResults = $derived(
		results !== null && (results.sessions.length > 0 || results.definitions.length > 0)
	);
</script>

<div class="bottom-controller">
	<div class="bc-doors" bind:this={doorsEl}>
		<div class="bc-door">
			<button
				type="button"
				class="bc-btn bc-btn-primary"
				aria-haspopup="menu"
				aria-expanded={menu === 'new'}
				title="Create a definition in the studio"
				onclick={() => toggleMenu('new')}
			>
				＋ New asset <span class="bc-caret" aria-hidden="true">▾</span>
			</button>
			{#if menu === 'new'}
				<!-- The FIVE asset kinds: the definitions a chat composes from. -->
				<div class="bc-menu bc-menu-new" role="menu" aria-label="New definition">
					{#each kindsInGroup('assets') as spec (spec.kind)}
						<button
							type="button"
							class="bc-menu-item"
							role="menuitem"
							aria-label={`New ${spec.pane}`}
							onclick={() => openStudio(spec.kind, { startNew: true })}
						>
							{spec.pane}
						</button>
					{/each}
				</div>
			{/if}
		</div>

		<div class="bc-door">
			<button
				type="button"
				class="bc-btn"
				aria-haspopup="menu"
				aria-expanded={menu === 'map'}
				title="Every studio tab"
				onclick={() => toggleMenu('map')}
			>
				Studio <span class="bc-caret" aria-hidden="true">▾</span>
			</button>
			{#if menu === 'map'}
				<!-- The studio map: EVERY kind, grouped. Open upward — this bar is
				     pinned to the bottom of the rail, and 11 chips do not fit one
				     15rem column. -->
				<div class="bc-menu bc-menu-map" role="menu" aria-label="Studio tabs">
					{#each STUDIO_GROUPS as group (group.group)}
						<div class="bc-group" role="group" aria-label={group.label}>
							<span class="bc-group-label" aria-hidden="true">{group.label}</span>
							<div class="bc-chips">
								{#each kindsInGroup(group.group) as spec (spec.kind)}
									<button
										type="button"
										class="bc-chip"
										role="menuitem"
										title={`Open the ${spec.tab} studio tab`}
										onclick={() => openStudio(spec.kind)}
									>
										{spec.tab}
									</button>
								{/each}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	</div>

	<div class="bc-divider" aria-hidden="true"></div>

	<div class="bc-search">
		<input
			class="bc-input"
			type="search"
			placeholder="Search every chat and definition…"
			aria-label="Global search"
			value={query}
			oninput={onInput}
			onkeydown={onKeydown}
		/>
		{#if busy}<span class="bc-busy">searching…</span>{/if}

		{#if query.trim().length >= MIN_CHARS && (busy || results || error)}
			<div class="bc-results" aria-label="Global search results">
				{#if error}
					<p class="bc-empty">{error}</p>
				{:else if !busy && !hasResults}
					<p class="bc-empty">No matches</p>
				{:else if results}
					{#each results.sessions as hit (hit.session_id)}
						<button
							type="button"
							class="bc-result"
							onclick={() => openSession(hit.session_id)}
						>
							<span class="bc-kind">chat</span>
							<span class="bc-name">{hit.title || hit.session_id}</span>
							{#if hit.snippet}
								<span class="bc-snippet">{hit.snippet}</span>
							{/if}
						</button>
					{/each}
					{#each results.definitions as def (`${def.kind}:${def.id}`)}
						<button
							type="button"
							class="bc-result"
							onclick={() => openDefinition(def.kind, def.id)}
						>
							<span class="bc-kind">{def.kind}</span>
							<span class="bc-name">{def.name}</span>
						</button>
					{/each}
				{/if}
			</div>
		{/if}
	</div>
</div>

<style>
	.bottom-controller {
		flex: none;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
		padding: 0.5rem 0.75rem;
		border-top: 1px solid var(--line);
		background: var(--bg-raised);
	}
	.bc-doors {
		display: flex;
		align-items: center;
		gap: 0.35rem;
		flex-wrap: wrap;
	}
	/* Both doors are popover anchors; their menus open UPWARD (this bar is pinned
	   to the bottom of the rail). */
	.bc-door {
		position: relative;
	}
	.bc-btn {
		display: inline-flex;
		align-items: center;
		gap: 0.25rem;
		white-space: nowrap;
		background: var(--bg);
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.3rem 0.6rem;
		cursor: pointer;
		font-size: var(--font-ui);
	}
	.bc-btn:hover {
		background: var(--bg-hover);
	}
	.bc-btn[aria-expanded='true'] {
		border-color: var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.bc-btn-primary {
		border-color: var(--accent-line);
		font-weight: 600;
	}
	.bc-caret {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.bc-menu {
		position: absolute;
		bottom: calc(100% + 0.3rem);
		left: 0;
		z-index: 30;
		display: flex;
		flex-direction: column;
		gap: 0.25rem;
		padding: 0.4rem;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 8px;
		box-shadow: 0 6px 24px rgb(0 0 0 / 35%);
	}
	.bc-menu-new {
		min-width: 9rem;
	}
	/* Wider than the 15rem rail: the grouped map needs a row per band. */
	.bc-menu-map {
		min-width: 18rem;
	}
	.bc-menu-item {
		display: block;
		width: 100%;
		text-align: left;
		background: transparent;
		color: var(--text);
		border: none;
		border-radius: 6px;
		padding: 0.3rem 0.5rem;
		cursor: pointer;
		font-size: var(--font-small);
	}
	.bc-menu-item:hover {
		background: var(--bg-hover);
	}
	.bc-group {
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
	}
	/* Band caption: the mono-uppercase micro register the rail's layer heads use. */
	.bc-group-label {
		font-size: var(--font-micro);
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		color: var(--text-dim);
	}
	.bc-chips {
		display: flex;
		gap: 0.25rem;
		flex-wrap: wrap;
	}
	.bc-chip {
		background: var(--bg);
		color: var(--text-muted);
		border: 1px solid var(--line);
		border-radius: 999px;
		padding: 0.15rem 0.5rem;
		cursor: pointer;
		font-size: var(--font-small);
	}
	.bc-chip:hover {
		background: var(--accent-soft);
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	.bc-divider {
		height: 1px;
		background: var(--line);
	}
	.bc-search {
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}
	.bc-input {
		flex: 1;
		min-width: 0;
		background: var(--bg-inset, var(--bg));
		color: var(--text);
		border: 1px solid var(--line);
		border-radius: 6px;
		padding: 0.4rem 0.6rem;
		font-size: var(--font-ui);
	}
	.bc-busy {
		font-size: var(--font-small);
		color: var(--text-muted);
	}
	/* Results open UPWARD: this bar is pinned to the bottom of the pane. */
	.bc-results {
		position: absolute;
		bottom: calc(100% + 0.3rem);
		left: 0;
		right: 0;
		z-index: 30;
		max-height: 40vh;
		overflow: auto;
		display: flex;
		flex-direction: column;
		gap: 0.2rem;
		padding: 0.35rem;
		background: var(--bg-raised);
		border: 1px solid var(--line);
		border-radius: 8px;
		box-shadow: 0 6px 24px rgb(0 0 0 / 35%);
	}
	.bc-result {
		display: flex;
		align-items: baseline;
		gap: 0.5rem;
		width: 100%;
		text-align: left;
		background: transparent;
		color: var(--text);
		border: none;
		border-radius: 6px;
		padding: 0.35rem 0.5rem;
		cursor: pointer;
		font-size: var(--font-small);
	}
	.bc-result:hover {
		background: var(--bg-hover);
	}
	.bc-kind {
		flex: none;
		color: var(--accent);
		text-transform: uppercase;
		font-size: var(--font-micro);
		letter-spacing: 0.04em;
	}
	.bc-name {
		color: var(--text);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.bc-snippet {
		color: var(--text-muted);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}
	.bc-empty {
		color: var(--text-muted);
		font-size: var(--font-small);
		margin: 0;
		padding: 0.2rem 0.5rem;
	}
</style>
