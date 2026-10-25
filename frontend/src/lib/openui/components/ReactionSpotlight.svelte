<script lang="ts">
	// VTK flavor (restored, 3.0.1): Reaction Spotlight — one enlarged emoji plus
	// one internal thought. Rare (at most ~1 per turn).
	// OpenUI Lang call: ReactionSpotlight(face, thought).
	//
	// W6c: leaned into the beat — the face sits on a soft accent radial backdrop
	// and pops in (scale/opacity overshoot); the thought arrives a beat later in
	// the muted register. No animated box-shadow; the glow is a static gradient.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { ReactionSpotlightProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<ReactionSpotlightProps> = $props();
</script>

<div class="vtk-reaction" data-openui="ReactionSpotlight">
	<span class="stage" aria-hidden="true">
		<span class="face">{props.face}</span>
	</span>
	<p class="thought">{props.thought}</p>
</div>

<style>
	.vtk-reaction {
		--h: calc(var(--accent-hue, 262) + 148);
		--vtk-accent: hsl(var(--h), var(--accent-sat, 72%), 74%);
		--vtk-line: hsl(var(--h), var(--accent-sat, 72%), 62%, 0.4);
		--vtk-soft: hsl(var(--h), var(--accent-sat, 72%), 62%, 0.18);
		--vtk-glow: hsl(var(--h), var(--accent-sat, 72%), 60%, 0.42);
		display: flex;
		align-items: center;
		gap: 1rem;
		padding: 0.75rem 0.95rem;
		font-size: 14px;
		color: var(--text);
		background:
			radial-gradient(circle at 12% 50%, var(--vtk-soft), transparent 42%),
			var(--bg-inset);
		border: 1px solid var(--vtk-line);
		border-radius: 12px;
		overflow: hidden;
	}
	/* The backdrop earns the emoji its place: a soft accent halo behind the
	   glyph, so it reads as staged art rather than clip-art dropped on a card. */
	.stage {
		position: relative;
		flex: none;
		display: grid;
		place-items: center;
		min-width: 3.5rem;
		width: max-content;
		height: 3.5rem;
		padding: 0 0.55rem;
		border-radius: 999px;
		background:
			radial-gradient(circle at 50% 44%, var(--vtk-glow), transparent 66%),
			radial-gradient(circle at 50% 50%, var(--vtk-soft), transparent 72%);
		animation: reaction-stage 0.5s cubic-bezier(0.2, 1.6, 0.4, 1) both;
	}
	.face {
		font-size: 2.3rem;
		line-height: 1;
		/* D48: a spaced multi-glyph cluster (e.g. `( ¬_¬)`) must stay on one line —
		   the fixed 3.5rem stage clips a wrapped second line. */
		white-space: nowrap;
		transform: translateY(0.02em);
	}
	.thought {
		margin: 0;
		font-size: 14px;
		line-height: 1.5;
		color: var(--text-muted);
		white-space: pre-line;
		animation: reaction-thought 0.5s ease-out 0.18s both;
	}
	.thought::before {
		content: '\201C';
		color: var(--vtk-accent);
	}
	.thought::after {
		content: '\201D';
		color: var(--vtk-accent);
	}
	@keyframes reaction-stage {
		0% {
			opacity: 0;
			transform: scale(0.3) rotate(-12deg);
		}
		70% {
			opacity: 1;
			transform: scale(1.12) rotate(4deg);
		}
		100% {
			opacity: 1;
			transform: none;
		}
	}
	@keyframes reaction-thought {
		from {
			opacity: 0;
			transform: translateX(-6px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.stage,
		.thought {
			animation: none;
		}
	}
</style>
