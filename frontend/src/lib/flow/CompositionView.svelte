<script lang="ts">
	// A node's COMPOSITION as a nested graph (W9.8, R13).
	//
	// R13 verbatim: "you go to narrator which is itself a graph … showing each of
	// the rules as a fan out to its group, fan into an assembled prompt — objects
	// representing the state of e.g. the Stable Prefix broken down." This renders
	// exactly that: the composition's REGIONS as colour-keyed bands (the cache
	// contract becomes visible, R13b), each region holding its module objects,
	// every module fanning into the composition's sink (`assembled_prompt`).
	//
	// View-only: it reads the manifest's `FlowSubGraph` and opens nothing. The
	// only intent it reports is `onselect` (view-only today; the W9.5 editor will
	// make sub-nodes editable, R13d — one source, two editors).
	//
	// Layout is component-owned and deterministic, like `FlowGraph` (no hand
	// coordinates): bands are stacked in region `order` (top = start of the
	// assembled prompt), module rows and the sink are derived from the
	// node/region data, and the sink sits in a right-hand column so every fan
	// edge is crossing-free. Card width is the one measured input — it follows the
	// host width within fixed bounds, so the same layout reads in the wide chat
	// drop-down and the narrow Studio pane.

	import type { FlowNode, FlowRegion, FlowSubGraph, RegionKind } from './types';

	let {
		composition,
		onselect
	}: {
		composition: FlowSubGraph;
		/** Reports the clicked module id. View-only: nothing opens. */
		onselect?: (id: string) => void;
	} = $props();

	// --- geometry ------------------------------------------------------------
	// Compact on purpose: this renders INSIDE a panel (the chat drop-down and the
	// Studio graph home), not as a full-width board.
	const CARD_H = 44;
	const CARD_GAP = 6;
	const BAND_PAD = 7;
	/** Band header: the region id line plus its caption line. */
	const BAND_HEAD = 32;
	const BAND_GAP = 12;
	const PAD = 10;
	const FAN_GAP = 26;
	const SINK_W = 150;
	const SINK_H = 88;
	const MIN_CARD_W = 150;
	const MAX_CARD_W = 300;
	/** Card width before the host has been measured (SSR and first paint). */
	const NATURAL_CARD_W = 260;

	/** The host width, measured client-side. `0` until the first measurement. */
	let hostW = $state(0);
	let frame = $state<HTMLElement | null>(null);

	$effect(() => {
		const element = frame;
		if (!element) return;
		const measure = () => (hostW = element.clientWidth);
		measure();
		const observer = new ResizeObserver(measure);
		observer.observe(element);
		return () => observer.disconnect();
	});

	/**
	 * The card width that fits a host of `hostW` px: the widest card that still
	 * leaves room for the region column padding, the fan gap and the sink column.
	 * Clamped to readable bounds — a host narrower than the clamped minimum scrolls
	 * (the board keeps `overflow: auto`), it never silently drops a module.
	 */
	function cardWidth(hostW: number): number {
		if (!hostW) return NATURAL_CARD_W;
		const available = hostW - (PAD * 2 + BAND_PAD * 2 + FAN_GAP + SINK_W);
		return Math.max(MIN_CARD_W, Math.min(MAX_CARD_W, available));
	}

	/** Short captions that make each region's meaning legible (R13b/C4). */
	const REGION_CAPTION: Record<RegionKind, string> = {
		stable_prefix: 'sent identically every turn',
		neutral: 'conversation',
		dynamic_tail: 'changes every turn'
	};

	/** Shown for a module the manifest leaves outside every declared region. */
	const UNPLACED_CAPTION = 'not part of any named block';

	/** Fan-edge tones. A composition edge is a plain `flow`; `attach` is the dim
	 * dashed variant `FlowGraph` uses (here: an edge the manifest leaves implied). */
	const TONES = ['flow', 'attach'];

	function toneColor(tone: string): string {
		return tone === 'attach' ? 'var(--text-dim)' : 'var(--text-muted)';
	}

	/**
	 * A module's region. The manifest names it in the node's `meta.description`
	 * (`Region: <id>`, authored next to `REGION_OF_MODULE_TYPE`); there is no
	 * dedicated field, so this is the one machine-readable spelling of it.
	 */
	function regionTag(node: FlowNode): string | null {
		const match = /^Region:\s*(\S+)/.exec(node.meta.description.trim());
		return match ? match[1] : null;
	}

	/**
	 * The module's one-line description: its own `runs.description`, else its
	 * `meta.description` with the region tag stripped. Never invented — a node
	 * that carries neither renders without a caption.
	 */
	function describe(node: FlowNode): string {
		const runs = node.runs.description.trim();
		if (runs) return runs;
		return node.meta.description.replace(/^Region:\s*\S+\s*/, '').trim();
	}

	/** The kind chip label — the same mapping `FlowNode` uses. */
	function kindLabel(node: FlowNode): string {
		if (node.kind === 'gate') return 'gate';
		switch (node.runs.kind) {
			case 'model_pass':
				return 'model pass';
			case 'fixed':
				return 'fixed';
			case 'engine':
				return 'engine';
			case 'hook':
				return 'hook';
			case 'embedder':
				return 'embedder';
		}
	}

	// --- layout --------------------------------------------------------------
	interface CardView {
		node: FlowNode;
		/** Top of the card, relative to its band. */
		y: number;
	}
	interface BandView {
		id: string;
		/** `RegionKind`, or `unplaced` for a module no region claims. */
		kind: string;
		caption: string;
		y: number;
		height: number;
		cards: CardView[];
	}
	interface FanView {
		id: string;
		d: string;
		tone: string;
		dashed: boolean;
		title: string;
	}
	interface Board {
		bands: BandView[];
		fans: FanView[];
		sink: { node: FlowNode; y: number; caption: string } | null;
		/** Card, band and sink columns, derived from the measured host width. */
		cardW: number;
		bandW: number;
		sinkX: number;
		width: number;
		height: number;
		blocks: number;
	}

	function bandOf(id: string, kind: string, caption: string, nodes: FlowNode[], top: number): BandView {
		const height =
			BAND_PAD * 2 + BAND_HEAD + nodes.length * CARD_H + (nodes.length - 1) * CARD_GAP;
		return {
			id,
			kind,
			caption,
			y: top,
			height,
			cards: nodes.map((node, index) => ({
				node,
				y: BAND_PAD + BAND_HEAD + index * (CARD_H + CARD_GAP)
			}))
		};
	}

	const board = $derived.by((): Board => {
		const cardW = cardWidth(hostW);
		const bandW = cardW + BAND_PAD * 2;
		const sinkX = PAD + bandW + FAN_GAP;
		const cardRight = PAD + BAND_PAD + cardW;
		const regions: FlowRegion[] = [...composition.regions].sort((a, b) => a.order - b.order);
		const sinkNode = composition.nodes.find((node) => node.id === composition.sink) ?? null;

		// Duplicate node ids would throw a keyed `{#each}`; they are a manifest
		// bug, so drop the repeat rather than crash the panel.
		const seen = new Set<string>();
		const modules: FlowNode[] = [];
		for (const node of composition.nodes) {
			if (node.id === composition.sink || seen.has(node.id)) continue;
			seen.add(node.id);
			modules.push(node);
		}

		const bands: BandView[] = [];
		const placed = new Set<string>();
		let y = PAD;
		for (const region of regions) {
			const claimed = modules.filter((node) => regionTag(node) === region.id);
			if (!claimed.length) continue;
			for (const node of claimed) placed.add(node.id);
			const band = bandOf(
				region.id,
				region.kind,
				REGION_CAPTION[region.kind] ?? region.title,
				claimed,
				y
			);
			bands.push(band);
			y += band.height + BAND_GAP;
		}
		const unplaced = modules.filter((node) => !placed.has(node.id));
		if (unplaced.length) {
			const band = bandOf('unplaced', 'unplaced', UNPLACED_CAPTION, unplaced, y);
			bands.push(band);
			y += band.height + BAND_GAP;
		}

		const contentBottom = bands.length ? y - BAND_GAP : PAD;
		const sinkY = Math.max(PAD, (PAD + contentBottom) / 2 - SINK_H / 2);

		// Fan-in: every module feeds the sink. A declared edge sets its tone; a
		// module the manifest leaves unwired to the sink is still drawn, dashed, so
		// the fan is complete (R20) instead of silently missing a block.
		const fans: FanView[] = [];
		for (const band of bands) {
			for (const card of band.cards) {
				const declared = composition.edges.find(
					(edge) => edge.from === card.node.id && edge.to === composition.sink
				);
				const sx = cardRight;
				const sy = band.y + card.y + CARD_H / 2;
				const tx = sinkX;
				const ty = sinkY + SINK_H / 2;
				const dx = Math.max(16, (tx - sx) * 0.55);
				fans.push({
					id: `${band.id}:${card.node.id}`,
					d: `M ${sx} ${sy} C ${sx + dx} ${sy}, ${tx - dx} ${ty}, ${tx} ${ty}`,
					tone: declared?.kind === 'attach' ? 'attach' : 'flow',
					dashed: !declared || declared.kind === 'attach',
					title: declared
						? `${card.node.id} → ${composition.sink} (${declared.kind})`
						: `${card.node.id}: no declared edge — implied into ${composition.sink}`
				});
			}
		}

		const height = Math.max(contentBottom, sinkY + SINK_H) + PAD;
		return {
			bands,
			fans,
			sink: sinkNode ? { node: sinkNode, y: sinkY, caption: describe(sinkNode) } : null,
			cardW,
			bandW,
			sinkX,
			width: sinkX + SINK_W + PAD,
			height,
			blocks: modules.length
		};
	});

	/** View-only selection: highlight the module that was clicked. */
	let picked = $state<string | null>(null);

	function pick(id: string): void {
		picked = id;
		onselect?.(id);
	}
</script>

<section class="composition" bind:this={frame} aria-label={`${composition.title} — what it is made of`}>
	<header class="comp-head">
		<span class="comp-title">{composition.title}</span>
		<span class="comp-chip">composition</span>
		<span class="comp-count">
			{board.blocks} block{board.blocks === 1 ? '' : 's'} → {composition.sink}
		</span>
	</header>

	{#if composition.nodes.length}
		<div class="board" style="width:{board.width}px; height:{board.height}px;">
			<svg class="fans" width={board.width} height={board.height} aria-hidden="true">
				<defs>
					{#each TONES as tone (tone)}
						<marker
							id={`composition-arrow-${tone}`}
							markerWidth="9"
							markerHeight="9"
							refX="7.5"
							refY="4.5"
							orient="auto"
						>
							<path class="arrow" style="fill:{toneColor(tone)}" d="M0,0 L9,4.5 L0,9 z" />
						</marker>
					{/each}
				</defs>
				{#each board.fans as fan (fan.id)}
					<path
						class="fan"
						class:dashed={fan.dashed}
						style="stroke:{toneColor(fan.tone)}"
						d={fan.d}
						marker-end={`url(#composition-arrow-${fan.tone})`}
					>
						<title>{fan.title}</title>
					</path>
				{/each}
			</svg>

			{#each board.bands as band (band.id)}
				<div
					class="band"
					data-region={band.kind}
					style="left:{PAD}px; top:{band.y}px; width:{board.bandW}px; height:{band.height}px;"
				>
					<div class="band-head">
						<span class="band-id">{band.id}</span>
						<span class="band-caption">{band.caption}</span>
					</div>
					{#each band.cards as card (card.node.id)}
						<div
							class="cell"
							style="left:{BAND_PAD}px; top:{card.y}px; width:{board.cardW}px; height:{CARD_H}px;"
						>
							<button
								type="button"
								class="module"
								class:picked={picked === card.node.id}
								aria-current={picked === card.node.id ? 'true' : undefined}
								title={`${card.node.title} (${card.node.kind})${describe(card.node) ? ` — ${describe(card.node)}` : ''}`}
								onclick={() => pick(card.node.id)}
							>
								<span class="module-head">
									<span class="module-title">{card.node.title}</span>
									{#if card.node.id === composition.entry}
										<span class="entry-chip" title="Composition entry">entry</span>
									{/if}
									<span class="kind" data-kind={card.node.kind}>{kindLabel(card.node)}</span>
								</span>
								{#if describe(card.node)}
									<span class="module-desc">{describe(card.node)}</span>
								{/if}
							</button>
						</div>
					{/each}
				</div>
			{/each}

			{#if board.sink}
				<div
					class="sink"
					style="left:{board.sinkX}px; top:{board.sink.y}px; width:{SINK_W}px; height:{SINK_H}px;"
				>
					<span class="sink-title">{board.sink.node.title}</span>
					<span class="sink-meta">
						<span class="kind" data-kind={board.sink.node.kind}>
							{kindLabel(board.sink.node)}
						</span>
						<span class="sink-subkind">{board.sink.node.kind}</span>
					</span>
					{#if board.sink.caption}
						<span class="sink-desc">{board.sink.caption}</span>
					{/if}
				</div>
			{/if}
		</div>
	{:else}
		<p class="empty">Nothing inside this step.</p>
	{/if}
</section>

<style>
	.composition {
		display: flex;
		flex-direction: column;
		gap: 0.35rem;
		/* The measured width the board lays out to. */
		width: 100%;
		min-width: 0;
	}
	.comp-head {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	.comp-title {
		font-size: var(--font-small);
		font-weight: 600;
	}
	.comp-chip {
		padding: 0.05rem 0.35rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.comp-count {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.board {
		position: relative;
		box-sizing: border-box;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-inset);
		/* The board is a deterministic size; a narrower host than the clamped
		   minimum scrolls it rather than hiding a module (R20). */
		max-width: 100%;
		overflow: auto;
	}
	.fans {
		position: absolute;
		inset: 0;
		pointer-events: none;
	}
	.fan {
		fill: none;
		stroke-width: 1.5;
		/* Hoverable despite the non-interactive layer, so the edge's own
		   `<title>` names the module it comes from. */
		pointer-events: stroke;
	}
	.fan.dashed {
		stroke-dasharray: 5 4;
	}
	.arrow {
		stroke: none;
	}
	/* Region bands: the cache contract made visible (R13b). Colour is keyed by
	   region KIND — the same accent/warn language FlowNode's region badges use. */
	.band {
		position: absolute;
		box-sizing: border-box;
		border: 1px solid var(--line);
		border-left: 3px solid var(--line-strong);
		border-radius: 7px;
		background: var(--bg-raised);
	}
	.band[data-region='stable_prefix'] {
		border-left-color: var(--accent-line);
		background: color-mix(in srgb, var(--accent) 10%, var(--bg-raised));
	}
	.band[data-region='stable_prefix'] .band-id {
		color: var(--accent-strong);
	}
	.band[data-region='dynamic_tail'] {
		border-left-color: var(--warn);
		background: color-mix(in srgb, var(--warn) 8%, var(--bg-raised));
	}
	.band[data-region='dynamic_tail'] .band-id {
		color: var(--warn);
	}
	.band[data-region='neutral'] {
		border-left-color: var(--line-strong);
	}
	.band[data-region='unplaced'] {
		border-style: dashed;
		border-left-style: solid;
		border-left-color: var(--danger-text);
	}
	.band[data-region='unplaced'] .band-id {
		color: var(--danger-text);
	}
	.band-head {
		display: flex;
		flex-direction: column;
		gap: 0.05rem;
		height: 32px;
		box-sizing: border-box;
		padding: 0.3rem 0.45rem 0;
		overflow: hidden;
	}
	.band-id {
		font-size: var(--font-micro);
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		line-height: 1.2;
		color: var(--text-muted);
	}
	.band-caption {
		font-size: var(--font-micro);
		line-height: 1.2;
		color: var(--text-dim);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.cell {
		position: absolute;
	}
	.module {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		gap: 0.1rem;
		width: 100%;
		height: 100%;
		box-sizing: border-box;
		padding: 0.25rem 0.4rem;
		border: 1px solid var(--line);
		border-radius: 6px;
		background: var(--bg-raised);
		color: inherit;
		text-align: left;
		font: inherit;
		overflow: hidden;
		cursor: pointer;
	}
	.module:hover {
		background: var(--bg-hover);
	}
	.module.picked {
		border-color: var(--accent);
	}
	.module-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.3rem;
	}
	.module-title {
		flex: 0 1 auto;
		font-size: var(--font-small);
		font-weight: 600;
		line-height: 1.15;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.module-desc {
		flex: 0 0 auto;
		font-size: var(--font-micro);
		line-height: 1.25;
		color: var(--text-dim);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.kind {
		flex: 0 0 auto;
		padding: 0.05rem 0.3rem;
		border-radius: 4px;
		font-size: var(--font-micro);
		text-transform: uppercase;
		letter-spacing: 0.03em;
		background: var(--bg-inset);
		color: var(--text-muted);
	}
	.kind[data-kind='gate'] {
		background: var(--accent-soft);
		color: var(--accent-strong);
	}
	.kind[data-kind='terminal'] {
		border: 1px solid var(--line);
	}
	.entry-chip {
		flex: 0 0 auto;
		padding: 0 0.3rem;
		border: 1px solid var(--accent-line);
		border-radius: 999px;
		background: var(--accent-soft);
		color: var(--accent-strong);
		font-size: var(--font-micro);
		line-height: 1.5;
	}
	.sink {
		position: absolute;
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
		box-sizing: border-box;
		padding: 0.35rem 0.45rem;
		border: 1px solid var(--accent-line);
		border-radius: 7px;
		background: var(--bg-raised);
		overflow: hidden;
	}
	.sink-title {
		font-size: var(--font-small);
		font-weight: 600;
		line-height: 1.15;
	}
	.sink-meta {
		display: flex;
		align-items: center;
		gap: 0.3rem;
	}
	.sink-subkind {
		font-size: var(--font-micro);
		color: var(--text-dim);
	}
	.sink-desc {
		font-size: var(--font-micro);
		line-height: 1.25;
		color: var(--text-dim);
		overflow: hidden;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
	}
	.empty {
		margin: 0;
		font-size: var(--font-small);
		color: var(--text-dim);
	}
</style>
