<script lang="ts">
	// DslFallback — mounts the OpenUI renderer over the captured DSL.
	//
	// The narrator emits OpenUI Lang; `$lib/openui/library` defines the component
	// contract. The renderer's parser is permissive, but when it produces no
	// usable tree (garbage, unknown components) or finishes with validation
	// errors we degrade to the raw-text `<pre>` so the user still sees something.
	// Parsing here is synchronous (`$derived`) so the choice is stable across SSR
	// and hydration; while `streaming`, partial payloads render progressively
	// because <Renderer> re-parses the cumulative `response` on every change.
	// A payload with no usable root yet (`pending`) keeps the same box with a
	// skeleton inside: rendering NOTHING until the parse succeeds made the panel
	// materialise out of nowhere and shove the transcript, and it read as a lost
	// panel for the whole draft. Interactive blocks inside the tree (the
	// Assistant card's options) fire `ActionEvent`s, which fill the composer
	// instead of sending a turn.
	import { Renderer } from '@openuidev/svelte-lang';
	import type { ActionEvent } from '@openuidev/svelte-lang';
	import { classifyDsl, library } from '#lib/openui/library';
	import { fillComposer } from '#lib/stores';

	let { dsl = '', streaming = false }: { dsl?: string; streaming?: boolean } = $props();

	const trimmed = $derived(dsl.trim());

	/**
	 * The openui parser only accepts `identifier = expression` — an unassigned
	 * component call is silently skipped, so a narrator that writes
	 * `DialogueSpotlight("Hobbs", …)` would show raw text. Segmentation keeps
	 * such a statement whole (one statement per segment); this gives it a
	 * throwaway identifier so it parses. The stored text is never rewritten.
	 */
	function asStatement(text: string): string {
		if (!text || /^\s*[A-Za-z_]\w*\s*=/.test(text)) return text;
		return `stmt = ${text}`;
	}

	const source = $derived(asStatement(trimmed));
	const state = $derived(classifyDsl(source, streaming));

	// Actionable blocks (the Assistant card's options) report clicks here. The
	// event FILLS the composer and stops: sending stays the user's decision
	// (UI-LANGUAGE §6 — never auto-send). `humanFriendlyMessage` is the label the
	// action declared; `params.message` is the fallback for unlabelled actions.
	function onAction(event: ActionEvent): void {
		const label = event.humanFriendlyMessage?.trim();
		const param = event.params?.message;
		const fallback = typeof param === 'string' ? param.trim() : '';
		fillComposer(label || fallback);
	}
</script>

{#if trimmed}
	<div class="dsl" class:streaming={streaming} data-dsl={state}>
		{#if state === 'pending'}
			<!-- Skeleton: the same footprint as a rendered panel, so a panel the
			     parse cannot see yet reserves its space instead of popping in. -->
			<div class="dsl-pending" data-dsl="pending" aria-hidden="true"></div>
		{:else if state === 'fallback'}
			<pre class="raw">{trimmed}</pre>
		{:else}
			<Renderer response={source} {library} {onAction} isStreaming={streaming} />
		{/if}
	</div>
{/if}

<style>
	.dsl {
		border: 1px dashed var(--line-strong);
		border-radius: 8px;
		background: var(--bg-inset);
		color: var(--text-muted);
		font-size: 0.72rem;
		line-height: 1.45;
	}
	.dsl[data-dsl='fallback'] {
		padding: 0.5rem 0.65rem;
	}
	.dsl[data-dsl='render'] {
		padding: 0.6rem;
	}
	.dsl.streaming {
		opacity: 0.65;
	}
	.dsl-pending {
		min-height: 2.5rem;
		background: repeating-linear-gradient(
			135deg,
			var(--bg-inset) 0 0.5rem,
			var(--bg-raised) 0.5rem 1rem
		);
	}
	.raw {
		margin: 0;
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: inherit;
		line-height: inherit;
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
