<script lang="ts">
	// VTK flavor (restored, 3.0.1): Dialogue Spotlight — a signature line of
	// dialogue styled to its voice; `tell` is the delivery detail framing it.
	// OpenUI Lang call: DialogueSpotlight(speaker, line, tell).
	//
	// W6b: the line is a real pull-quote — oversized serif set (a serif stack),
	// hanging quote glyphs, a speaker rule and generous air — inside one
	// accent-derived card, never bare transcript text.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { DialogueSpotlightProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<DialogueSpotlightProps> = $props();
</script>

<figure class="vtk-dialogue" data-openui="DialogueSpotlight">
	<div class="speaker">{props.speaker}</div>
	<blockquote class="line">
		<span class="glyph glyph-open" aria-hidden="true">&ldquo;</span>
		<p class="words">
			{props.line}<span class="glyph glyph-close" aria-hidden="true">&rdquo;</span>
		</p>
	</blockquote>
	<figcaption class="tell">{props.tell}</figcaption>
</figure>

<style>
	.vtk-dialogue {
		--h: calc(var(--accent-hue, 262) + 6);
		--vtk-accent: hsl(var(--h), var(--accent-sat, 72%), 78%);
		--vtk-line: hsl(var(--h), var(--accent-sat, 72%), 62%, 0.42);
		--vtk-soft: hsl(var(--h), var(--accent-sat, 72%), 62%, 0.16);
		--vtk-quote: hsl(var(--h), var(--accent-sat, 72%), 82%);
		--vtk-serif: 'Iowan Old Style', 'Palatino Linotype', Palatino, 'Book Antiqua', Georgia,
			'Times New Roman', serif;
		position: relative;
		display: flex;
		flex-direction: column;
		gap: 0.95rem;
		margin: 0;
		padding: 1.4rem 1.6rem 1.1rem 1.9rem;
		font-size: 14px;
		color: var(--text);
		background:
			linear-gradient(125deg, var(--vtk-soft), transparent 60%),
			var(--bg-raised);
		border: 1px solid var(--vtk-line);
		border-radius: 12px;
		overflow: hidden;
	}
	/* Accent spine: anchors the card as a quotable object, not a text blob. */
	.vtk-dialogue::before {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 4px;
		background: linear-gradient(180deg, var(--vtk-accent), transparent 92%);
	}
	.vtk-dialogue::after {
		content: '';
		position: absolute;
		inset: 0 0 auto auto;
		width: 9rem;
		height: 9rem;
		background: radial-gradient(circle at 100% 0%, var(--vtk-soft), transparent 70%);
		pointer-events: none;
	}
	.speaker {
		position: relative;
		display: flex;
		align-items: center;
		gap: 0.7rem;
		font-size: 0.72rem;
		font-weight: 700;
		letter-spacing: 0.24em;
		text-transform: uppercase;
		color: var(--vtk-accent);
	}
	.speaker::after {
		content: '';
		flex: 1;
		height: 1px;
		background: linear-gradient(90deg, var(--vtk-line), transparent);
	}
	.line {
		position: relative;
		margin: 0;
		padding-left: 2.3rem;
	}
	.words {
		position: relative;
		margin: 0;
		font-family: var(--vtk-serif);
		font-size: clamp(1.4rem, 1.05rem + 1.7vw, 2.05rem);
		font-weight: 500;
		line-height: 1.26;
		letter-spacing: -0.012em;
		color: var(--text);
		text-wrap: balance;
		/* A line that breaks is still the same line: keep the model's breaks. */
		white-space: pre-line;
		animation: dialogue-settle 0.55s ease-out both;
	}
	.glyph {
		font-family: var(--vtk-serif);
		color: var(--vtk-quote);
		pointer-events: none;
		user-select: none;
	}
	.glyph-open {
		position: absolute;
		left: -2.2rem;
		top: -0.55em;
		font-size: 3.6em;
		line-height: 1;
		opacity: 0.55;
	}
	.glyph-close {
		font-size: 1.4em;
		line-height: 0;
		vertical-align: -0.28em;
		opacity: 0.7;
		margin-left: 0.04em;
	}
	.tell {
		position: relative;
		margin: 0;
		font-size: 0.82rem;
		font-style: italic;
		line-height: 1.5;
		color: var(--text-muted);
		border-top: 1px dashed var(--vtk-line);
		padding-top: 0.6rem;
		white-space: pre-line;
	}
	@keyframes dialogue-settle {
		from {
			opacity: 0;
			transform: translateY(6px);
			letter-spacing: 0.02em;
		}
		to {
			opacity: 1;
			transform: none;
			letter-spacing: -0.012em;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.words {
			animation: none;
		}
	}
</style>
