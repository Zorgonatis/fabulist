<script lang="ts">
	// Card details (Wave B / D28/D30): the read-only side of a parsed V2 card.
	//
	// The import parser keeps EVERY V2 field (D28), but the transcript only ever
	// showed name/description/greeting. This overlay surfaces the rest verbatim —
	// creator, character_version, creator_notes, the raw `extensions` JSON object,
	// and the retained card image (when the import carried one) — so a parsed-but-
	// dead field is observable. It renders whatever the imported card dict holds;
	// missing keys simply omit their row.
	import { API_BASE } from '#lib/api';

	let {
		card,
		sessionId = null,
		cardAsset = null,
		onClose
	}: {
		card: Record<string, unknown>;
		/** Session owning the card image asset (needed for the image URL). */
		sessionId?: string | null;
		/** Retained card-image asset id (PNG import), or null. */
		cardAsset?: string | null;
		onClose: () => void;
	} = $props();

	function text(value: unknown): string {
		return typeof value === 'string' ? value : '';
	}

	const name = $derived(text(card.name) || 'Untitled card');
	const creator = $derived(text(card.creator));
	const version = $derived(text(card.character_version));
	const creatorNotes = $derived(text(card.creator_notes));
	const tags = $derived(Array.isArray(card.tags) ? card.tags.map((t) => String(t)) : []);

	//: Verbatim extensions JSON — the spec requires unknown keys survive (D28),
	//: so this renders the object as-is, never a re-typed summary.
	const extensionsJson = $derived(
		card.extensions && typeof card.extensions === 'object'
			? JSON.stringify(card.extensions, null, 2)
			: ''
	);

	const imageUrl = $derived(
		sessionId && cardAsset
			? `${API_BASE}/api/sessions/${encodeURIComponent(sessionId)}/attachments/${encodeURIComponent(cardAsset)}/raw`
			: null
	);
</script>

<div class="scrim" role="presentation" onclick={onClose}></div>
<div class="card-details" role="dialog" aria-modal="true" aria-label="Card details">
	<header>
		<h2>{name}</h2>
		<button type="button" class="close" onclick={onClose} aria-label="Close card details">×</button>
	</header>

	{#if imageUrl}
		<img class="card-image" src={imageUrl} alt={`${name} card art`} />
	{/if}

	<dl class="fields">
		{#if creator}
			<dt>Creator</dt>
			<dd>{creator}</dd>
		{/if}
		{#if version}
			<dt>Version</dt>
			<dd>{version}</dd>
		{/if}
		{#if creatorNotes}
			<dt>Creator notes</dt>
			<dd class="notes">{creatorNotes}</dd>
		{/if}
		{#if tags.length}
			<dt>Tags</dt>
			<dd class="tags">
				{#each tags as tag (tag)}<span class="tag">{tag}</span>{/each}
			</dd>
		{/if}
	</dl>

	{#if extensionsJson}
		<h3>Extensions</h3>
		<pre class="extensions" aria-label="Extensions JSON">{extensionsJson}</pre>
	{:else}
		<p class="hint">No extensions on this card.</p>
	{/if}
</div>

<style>
	.scrim {
		position: fixed;
		inset: 0;
		background: var(--scrim);
		z-index: 24;
	}
	.card-details {
		position: fixed;
		top: 8vh;
		left: 50%;
		transform: translateX(-50%);
		width: min(30rem, 92vw);
		max-height: 80vh;
		overflow-y: auto;
		background: var(--bg-panel);
		border: 1px solid var(--line);
		border-radius: 12px;
		padding: 1rem;
		z-index: 25;
		box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
	}
	.card-details header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
	}
	.card-details h2 {
		margin: 0;
		font-size: 1rem;
		color: var(--text);
	}
	.card-details h3 {
		margin: 0.9rem 0 0.3rem;
		font-size: 0.72rem;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--text-dim);
	}
	.close {
		font: inherit;
		font-size: 1rem;
		line-height: 1;
		border: 1px solid var(--line);
		background: var(--bg-raised);
		color: var(--text-muted);
		border-radius: 6px;
		padding: 0.15rem 0.45rem;
		cursor: pointer;
	}
	.card-image {
		display: block;
		max-width: 100%;
		max-height: 16rem;
		margin: 0.6rem auto 0;
		border-radius: 8px;
		border: 1px solid var(--line);
	}
	.fields {
		margin: 0.6rem 0 0;
		display: grid;
		grid-template-columns: auto 1fr;
		gap: 0.2rem 0.7rem;
		font-size: 0.78rem;
	}
	.fields dt {
		color: var(--text-dim);
	}
	.fields dd {
		margin: 0;
		color: var(--text);
	}
	.notes {
		white-space: pre-wrap;
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 0.25rem;
	}
	.tag {
		border: 1px solid var(--accent-line);
		background: var(--accent-soft);
		color: var(--accent-strong);
		border-radius: 999px;
		padding: 0 0.45rem;
		font-size: var(--font-micro);
	}
	.extensions {
		margin: 0;
		padding: 0.5rem;
		background: var(--bg-inset);
		border: 1px solid var(--line);
		border-radius: 8px;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: 0.68rem;
		line-height: 1.45;
		color: var(--text-muted);
		white-space: pre-wrap;
		word-break: break-word;
	}
	.hint {
		margin: 0.4rem 0 0;
		font-size: 0.72rem;
		color: var(--text-dim);
	}
</style>
