<script lang="ts">
	// VTK flavor: Custom — the raw-HTML escape hatch (plan §1.1, listed first for
	// salience).
	// OpenUI Lang call: Custom(title, html, css?[, {glyph}]).
	// Preset: "Visual Toolkit (VTK)" → Custom.
	//
	// Typed components are always the better first choice; Custom exists for the
	// beat none of them is adequate or specific enough for. The narrator hands us
	// a title, an HTML fragment and an optional CSS string, and we mount them in
	// an OPEN SHADOW ROOT on this host div — shadow DOM is the whole point:
	// bidirectional isolation. Model CSS cannot restyle the app, and app CSS
	// cannot break the artifact. CSS custom properties DO inherit through the
	// boundary, so the model may consume var(--tone-1|-2|-3, -soft, -line),
	// var(--bg-panel), var(--text) and var(--line) — that is the harmonization
	// lever; nothing else crosses.
	//
	// Injection order is load-bearing: (a) a style element whose textContent is the
	// SCRUBBED css string, then (b) the SANITIZED html fragment. CSS is never
	// parsed as markup (a style element inside html is forbidden) and HTML never
	// becomes CSS.
	//
	// SSR / pre-hydration / sanitize-failure all render the paper-scrap: the raw
	// source HTML-escaped in a pre block inside a .ui-frame-diegetic scrap titled by
	// `title` (the markdown.ts SSR pattern — DOMPurify needs a DOM, so the server
	// cannot sanitize). A client-only $effect swaps the scrap for the shadow host
	// after hydration, so there is no hydration mismatch.
	//
	// Caps: the engine gate enforces html ≤ 4000 / css ≤ 2000 chars. This
	// renderer is display-only — it renders whatever it is handed and does NOT
	// clamp; a display clamp is deliberately not this component's concern.
	//
	// Colour: the model's CSS inside the shadow; the scrap and host use standard
	// tokens. Motion: ONE ui-rise-in 300ms fade/rise on the host (scrap
	// identical); nothing is animated inside the model's CSS by policy copy.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import { sanitizeCustomHtml, scrubCustomCss } from '../customSanitize';

	// Local prop types matching plan §1.1 exactly (Wave 2 swaps to schema imports).
	type CustomOptions = { glyph?: string; art?: string };
	type CustomProps = { title: string; html: string; css?: string; options?: CustomOptions };

	let { props }: ComponentRenderProps<CustomProps> = $props();

	const glyph = $derived((props.options?.glyph ?? '').trim());

	// Raw source shown by the scrap: exactly what the narrator wrote, so a beat
	// that could not render still reads truthfully.
	const rawSource = $derived(
		[props.html?.trim(), props.css?.trim()].filter((part) => !!part && part.length > 0).join('\n\n')
	);

	/** HTML-escape a plain string — the scrap renders through `{@html}` (markdown.ts pattern). */
	function escapeHtml(text: string): string {
		return text
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	}

	let hostEl = $state<HTMLDivElement | null>(null);
	// `mounted` flips true in a client-only effect so the first client render
	// matches the SSR scrap; `failed` keeps that scrap when sanitizing yields
	// nothing (empty or throw).
	let mounted = $state(false);
	let failed = $state(false);

	$effect(() => {
		mounted = true;
	});

	$effect(() => {
		const host = hostEl;
		if (!host) return;
		const html = props.html ?? '';
		const css = props.css ?? '';
		let clean = '';
		try {
			clean = sanitizeCustomHtml(html);
		} catch {
			clean = '';
		}
		if (!clean.trim()) {
			failed = true;
			return;
		}
		failed = false;
		const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' });
		root.replaceChildren();
		const styleEl = document.createElement('style');
		styleEl.textContent = scrubCustomCss(css);
		root.append(styleEl);
		const template = document.createElement('template');
		template.innerHTML = clean;
		root.append(template.content);
	});
</script>

{#if mounted && !failed}
	<div
		class="custom-host"
		data-openui="Custom"
		data-custom="host"
		data-title={props.title}
		bind:this={hostEl}
	></div>
{:else}
	<div
		class="custom-scrap ui-frame-diegetic"
		data-openui="Custom"
		data-custom="scrap"
		data-title={props.title}
	>
		<header class="scrap-head ui-head">
			{#if glyph}
				<span class="scrap-glyph ui-glyph" aria-hidden="true">{glyph}</span>
			{/if}
			<span class="scrap-title">{props.title}</span>
		</header>
		<div class="scrap-body ui-inset">
			<pre class="scrap-source">{@html escapeHtml(rawSource)}</pre>
		</div>
	</div>
{/if}

<style>
	/* The host is the isolation boundary: app CSS styles ONLY this box, never the
	   artifact inside. One ui-rise-in 300ms entrance, identical to the scrap. */
	.custom-host {
		display: block;
		animation: ui-rise-in 300ms ease-out both;
	}

	.custom-scrap {
		display: flex;
		flex-direction: column;
		gap: 0.55rem;
		animation: ui-rise-in 300ms ease-out both;
	}
	.scrap-head {
		gap: 0.45rem;
		padding-bottom: 0.45rem;
	}
	.scrap-glyph {
		flex: none;
	}
	.scrap-title {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-weight: 600;
	}
	.scrap-body {
		max-height: 22rem;
		overflow: auto;
	}
	.scrap-source {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-ui, 14px);
		line-height: 1.42;
		white-space: pre-wrap;
		overflow-wrap: anywhere;
		color: var(--text-muted);
	}

	@media (prefers-reduced-motion: reduce) {
		.custom-host,
		.custom-scrap {
			animation: none;
		}
	}
</style>
