<script lang="ts">
	// VTK flavor: Failure Achievement — comedic failure trophy.
	// OpenUI Lang call: FailureAchievement(icon, title, description, footer).
	// Preset: "Visual Toolkit (VTK)" → Failure Achievement.
	// Constraints: max 1/response; skip for tragic moments (enforced narrator-side).
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';
	import type { FailureAchievementProps } from '../schemas/flavors';

	let { props }: ComponentRenderProps<FailureAchievementProps> = $props();
</script>

<div class="vtk-achievement" data-openui="FailureAchievement">
	<span class="icon" aria-hidden="true">{props.icon}</span>
	<div class="body">
		<span class="unlocked">Achievement Unlocked</span>
		<h3 class="title">&ldquo;{props.title}&rdquo;</h3>
		<p class="desc">{props.description}</p>
		<p class="footer">{props.footer}</p>
	</div>
</div>

<style>
	.vtk-achievement {
		--h: calc(var(--accent-hue, 262) + 143);
		--vtk-accent: hsl(var(--h), var(--accent-sat, 72%), 66%);
		--vtk-line: hsl(var(--h), var(--accent-sat, 72%), 60%, 0.45);
		display: flex;
		align-items: flex-start;
		gap: 0.7rem;
		padding: 0.75rem 0.85rem;
		font-size: 14px;
		color: var(--text);
		background:
			linear-gradient(140deg, hsl(var(--h), var(--accent-sat, 72%), 60%, 0.14), transparent 65%),
			var(--bg-inset);
		border: 1px solid var(--vtk-line);
		border-radius: 10px;
		animation: achievement-pop 0.5s cubic-bezier(0.2, 1.4, 0.5, 1) both;
	}
	.icon {
		font-size: 34px;
		line-height: 1;
		animation: achievement-pulse 2.4s ease-in-out infinite;
	}
	.body {
		display: flex;
		flex-direction: column;
		gap: 0.15rem;
	}
	.unlocked {
		font-size: 14px;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--vtk-accent);
	}
	.title {
		margin: 0;
		font-size: 16px;
	}
	.desc {
		margin: 0.15rem 0 0;
		font-size: 14px;
		line-height: 1.45;
		white-space: pre-line;
	}
	.footer {
		margin: 0.35rem 0 0;
		font-size: 14px;
		font-style: italic;
		color: var(--text-dim);
		white-space: pre-line;
	}
	@keyframes achievement-pop {
		from {
			opacity: 0;
			transform: scale(0.92) translateY(6px);
		}
		to {
			opacity: 1;
			transform: none;
		}
	}
	@keyframes achievement-pulse {
		0%,
		100% {
			transform: rotate(-6deg) scale(1);
		}
		50% {
			transform: rotate(6deg) scale(1.08);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.vtk-achievement,
		.icon {
			animation: none;
		}
	}
</style>
