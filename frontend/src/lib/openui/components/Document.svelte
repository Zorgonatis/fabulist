<script lang="ts">
	// VTK flavor: Document — letters, ledgers, notices, newspapers, notes.
	// OpenUI Lang call: Document(docType, title, body, signature?[, {glyph, art}]).
	// Preset: "Visual Toolkit (VTK)" → Document.
	//
	// Authored to READ as a letter: a letterhead (small-caps title over a rule),
	// the body verbatim in the prose register, and a right-aligned sign-off under
	// its own rule. `docType` re-dresses that same structure with the same tone
	// tokens — a stamped notice, a ruled ledger, a masthead newspaper, a taped
	// note — and never spends a new hue (docs/UI-LANGUAGE.md §2). An optional
	// {glyph} presses the wax seal; {art} is a monospace stamp block.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { DocumentProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<DocumentProps> = $props();

	const kind = $derived((props.docType || 'letter').toLowerCase());
	const glyph = $derived(props.options?.glyph ?? '');
	const art = $derived(props.options?.art ?? '');
</script>

<article class="vtk-document ui-frame-diegetic" data-openui="Document" data-doc={kind}>
	<header class="letterhead">
		<span class="kind">{props.docType}</span>
		<h3 class="title">{props.title}</h3>
	</header>

	<p class="body">{props.body}</p>

	{#if art}
		<pre class="art" aria-hidden="true">{art}</pre>
	{/if}

	{#if props.signature || glyph}
		<footer class="signoff">
			{#if props.signature}
				<p class="signature">{props.signature}</p>
			{/if}
			{#if glyph}
				<span class="seal ui-glyph" aria-hidden="true">{glyph}</span>
			{/if}
		</footer>
	{/if}
</article>

<style>
	.vtk-document {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
		padding: 1rem 1.05rem 0.9rem;
		font-size: var(--font-body);
		color: var(--text);
		/* Warm paper: a tone-soft wash over the neutral panel, no hue offset. */
		background:
			linear-gradient(180deg, var(--tone-1-soft), transparent 68%),
			var(--bg-panel);
	}

	/* ── letterhead: small-caps title over a rule ───────────────────── */
	.letterhead {
		display: flex;
		flex-direction: column;
		gap: 0.3rem;
		padding-bottom: 0.5rem;
		border-bottom: 1px solid var(--tone-1-line);
	}
	.kind {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.22em;
		text-transform: uppercase;
		color: var(--tone-1);
	}
	.title {
		margin: 0;
		font-size: clamp(1.05rem, 2.4vw, 1.35rem);
		font-weight: 600;
		font-variant-caps: small-caps;
		letter-spacing: 0.05em;
		line-height: 1.15;
		text-wrap: balance;
		color: var(--text);
	}

	/* ── body: verbatim, prose register, pre-wrap ───────────────────── */
	.body {
		margin: 0;
		font-size: var(--font-body);
		line-height: 1.62;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		color: var(--text);
	}

	.art {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-ui);
		line-height: 1.2;
		white-space: pre;
		overflow-x: auto;
		text-align: center;
		color: var(--text-muted);
	}

	/* ── sign-off: its own rule, right-aligned, sealed ──────────────── */
	.signoff {
		display: flex;
		align-items: flex-end;
		justify-content: flex-end;
		gap: 0.7rem;
		padding-top: 0.6rem;
		border-top: 1px solid var(--tone-1-line);
	}
	.signature {
		margin: 0;
		font-size: var(--font-body);
		font-style: italic;
		line-height: 1.4;
		text-align: right;
		color: var(--text-muted);
	}
	.seal {
		flex: none;
		display: grid;
		place-items: center;
		width: 2.1rem;
		height: 2.1rem;
		font-size: 1.05rem;
		line-height: 1;
		color: var(--tone-1);
		background: var(--tone-1-soft);
		border: 1.5px solid var(--tone-1-line);
		border-radius: 50%;
		box-shadow: inset 0 0 0 2px var(--bg-panel);
		transform: rotate(-7deg);
	}

	/* ── docType variants — same tokens, different structure ────────── */

	/* notice: a rotated bordered stamp in the corner. */
	.vtk-document[data-doc='notice'] {
		background:
			repeating-linear-gradient(0deg, var(--tone-2-soft) 0 1px, transparent 1px 1.5rem),
			var(--bg-panel);
	}
	.vtk-document[data-doc='notice'] .letterhead {
		padding-right: 6.5rem;
		border-bottom-color: var(--tone-2-line);
	}
	.vtk-document[data-doc='notice'] .kind {
		position: absolute;
		top: -0.85rem;
		right: -0.6rem;
		padding: 0.2rem 0.6rem;
		color: var(--tone-1);
		background: var(--bg-raised);
		border: 2px solid var(--tone-1-line);
		border-radius: 3px;
		box-shadow: 0 0 0 3px var(--bg-panel);
		transform: rotate(-11deg);
	}

	/* ledger: ruled rows the body sits on. */
	.vtk-document[data-doc='ledger'] .body {
		line-height: 2rem;
		font-variant-numeric: tabular-nums;
		background-image: repeating-linear-gradient(
			to bottom,
			transparent 0 calc(2rem - 1px),
			var(--tone-1-line) calc(2rem - 1px)
		);
	}

	/* newspaper: centered masthead over a double rule, two columns. */
	.vtk-document[data-doc='newspaper'] .letterhead {
		align-items: center;
		text-align: center;
		border-bottom: 3px double var(--tone-1-line);
	}
	.vtk-document[data-doc='newspaper'] .title {
		font-size: clamp(1.35rem, 3.4vw, 1.95rem);
		text-transform: uppercase;
		font-variant-caps: normal;
		letter-spacing: 0.05em;
	}
	.vtk-document[data-doc='newspaper'] .body {
		column-width: 13rem;
		column-gap: 1.2rem;
		column-rule: 1px solid var(--tone-1-line);
		text-align: justify;
	}

	/* note: a slight tilt with a tape strip across the top. */
	.vtk-document[data-doc='note'] {
		margin-top: 0.7rem;
		transform: rotate(-1.2deg);
		border-radius: 2px;
		background:
			linear-gradient(165deg, var(--tone-3-soft), transparent 68%),
			var(--bg-raised);
	}
	.vtk-document[data-doc='note']::before {
		content: '';
		position: absolute;
		top: -0.6rem;
		left: 50%;
		width: 4.5rem;
		height: 1.1rem;
		transform: translateX(-50%) rotate(-2.2deg);
		background: color-mix(in srgb, var(--text) 12%, transparent);
		border-left: 1px dashed var(--line-strong);
		border-right: 1px dashed var(--line-strong);
		pointer-events: none;
	}
</style>
