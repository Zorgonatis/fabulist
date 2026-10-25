<script lang="ts">
	// VTK flavor: Entrance — a first-appearance intro card.
	// OpenUI Lang call: Entrance(name, role, signature, options?).
	// Preset: "Visual Toolkit (VTK)" → Entrance. First appearance only; never
	// repeat the prose.
	//
	// A cinematic title card: the name is set in the display register, the role
	// is a stamped micro-label, and the signature line runs beneath in the
	// italic prose register. `options.glyph` is the signature OBJECT (hat, blade,
	// umbrella — never a face) stamped into the top-right corner.
	//
	// The optional `markers` string is the narrator's selection: a free-form
	// "Label: value; Label: value" list of the two-to-four visual details that
	// define this character on sight. Authority of usage lives with the model
	// (no vetted key set); every read is defensive — the card renders correctly
	// with or without it.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { EntranceProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<EntranceProps> = $props();

	const markerRows = $derived(
		(props.options?.markers ?? '')
			.split(';')
			.map((part) => part.trim())
			.filter(Boolean)
			.map((part) => {
				const at = part.indexOf(':');
				return at > 0
					? [part.slice(0, at).trim(), part.slice(at + 1).trim()]
					: ['', part];
			})
			.filter(([, value]) => value !== '')
	);
	const mark = $derived((props.options?.glyph ?? '').trim());
</script>

<article class="vtk-entrance ui-frame-diegetic" class:has-mark={Boolean(mark)} data-openui="Entrance">
	<span class="rule" aria-hidden="true"></span>
	{#if mark}
		<span class="mark" aria-hidden="true">{mark}</span>
	{/if}

	<span class="role ui-tag">{props.role}</span>
	<h3 class="name">{props.name}</h3>
	<p class="signature">{props.signature}</p>

	{#if markerRows.length}
		<div class="markers ui-inset">
			{#each markerRows as [label, value]}
				<div class="ui-stat">
					<span>{label}</span>
					<span>{value}</span>
				</div>
			{/each}
		</div>
	{/if}
</article>

<style>
	.vtk-entrance {
		position: relative;
		isolation: isolate;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		overflow: hidden;
		padding: 1.05rem 1.2rem 0.9rem;
		background:
			radial-gradient(130% 180% at 6% 0%, var(--tone-1-soft), transparent 56%),
			linear-gradient(180deg, var(--bg-raised), var(--bg-inset));
	}
	/* The corner mark needs its own lane, so the card clears it. */
	.vtk-entrance.has-mark {
		padding-right: 3.1rem;
	}

	/* Cinematic top rule that draws in on mount (transform only). */
	.rule {
		position: absolute;
		top: 0;
		left: 0;
		right: 0;
		z-index: 1;
		height: 2px;
		transform-origin: left center;
		background: linear-gradient(90deg, var(--tone-1), transparent);
		animation: entrance-draw 0.8s cubic-bezier(0.3, 1, 0.4, 1) both;
	}

	/* Signature object stamped into the corner. */
	.mark {
		position: absolute;
		top: 0.6rem;
		right: 0.75rem;
		font-size: clamp(1.5rem, 4vw, 2rem);
		line-height: 1;
		text-shadow: 0 0 10px var(--tone-1-soft);
		transform-origin: 62% 38%;
		animation: entrance-stamp 0.5s cubic-bezier(0.2, 1.5, 0.4, 1) 0.15s both;
	}

	/* Stamped micro-tag (system register): role as a mark, not a sentence. */
	.role {
		font-size: var(--font-micro);
		letter-spacing: 0.14em;
		border-width: 2px;
		color: var(--tone-1);
		transform: rotate(-1.2deg);
	}

	/* Display register — this is a title card. */
	.name {
		margin: 0.35rem 0 0;
		font-size: clamp(1.7rem, 5vw, 2.3rem);
		font-weight: 700;
		line-height: 1.05;
		letter-spacing: 0.01em;
		text-transform: uppercase;
		text-wrap: balance;
		color: var(--text);
		text-shadow: 0 0 22px var(--tone-1-soft);
		animation: entrance-rise 0.55s ease-out 0.05s both;
	}

	/* Prose register — the defining visual, in the narrator's own italics. */
	.signature {
		margin: 0.3rem 0 0;
		max-width: 46ch;
		font-size: var(--font-body);
		line-height: 1.5;
		font-style: italic;
		color: var(--text-muted);
		white-space: pre-line;
	}

	/* Structured visual markers: compact labelled rows, present only when the
	   narrator supplies them. */
	.markers {
		width: 100%;
		margin-top: 0.5rem;
		font-size: var(--font-body);
	}
	.markers .ui-stat + .ui-stat {
		border-top: 1px dashed var(--line);
	}
	.markers .ui-stat > :last-child {
		text-align: right;
		overflow-wrap: anywhere;
	}

	@keyframes entrance-draw {
		from {
			transform: scaleX(0);
			opacity: 0;
		}
		to {
			transform: scaleX(1);
			opacity: 1;
		}
	}
	@keyframes entrance-rise {
		from {
			opacity: 0;
			transform: translateY(8px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@keyframes entrance-stamp {
		from {
			opacity: 0;
			transform: scale(1.5) rotate(-14deg);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.rule,
		.name,
		.mark {
			animation: none;
		}
	}
</style>
