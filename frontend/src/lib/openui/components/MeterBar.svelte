<script lang="ts">
	// Compact meter: a labelled bar of `value` out of `max`.
	// OpenUI Lang call: MeterBar(label, value, max).
	//
	// W6d: reads as a gauge, not a loader — an emphasised uppercase label and a
	// bold tabular value sit above the track, and quarter tick marks make the
	// scale legible. No text rides the fill.
	import type { ComponentRenderProps } from '@openuidev/svelte-lang';

	type MeterProps = { label: string; value: number; max: number };
	let { props }: ComponentRenderProps<MeterProps> = $props();

	const pct = $derived(
		props.max > 0 ? Math.min(100, Math.max(0, (props.value / props.max) * 100)) : 0
	);
</script>

<div
	class="meter-bar"
	data-openui="MeterBar"
	role="meter"
	aria-label={props.label}
	aria-valuenow={props.value}
	aria-valuemin="0"
	aria-valuemax={props.max}
>
	<div class="row">
		<span class="label">{props.label}</span>
		<span class="value">{props.value}<span class="max">/{props.max}</span></span>
	</div>
	<div class="track">
		<div class="fill" style="width: {pct}%"></div>
		<span class="ticks" aria-hidden="true"></span>
	</div>
</div>

<style>
	.meter-bar {
		border: 1px dashed var(--accent-line);
		border-radius: 8px;
		padding: 0.5rem 0.65rem 0.55rem;
		display: flex;
		flex-direction: column;
		gap: 0.4rem;
	}
	.row {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 0.6rem;
		color: inherit;
	}
	.label {
		font-size: 0.7rem;
		font-weight: 700;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--accent-strong);
	}
	.value {
		font-size: 0.8rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
		color: var(--text);
	}
	.value .max {
		font-weight: 500;
		color: var(--text-muted);
	}
	.track {
		position: relative;
		height: 8px;
		border-radius: 999px;
		background: var(--accent-soft);
		overflow: hidden;
		box-shadow: inset 0 0 0 1px var(--accent-line);
	}
	.fill {
		height: 100%;
		background: linear-gradient(
			90deg,
			var(--accent),
			hsl(calc(var(--accent-hue, 262) + 56), var(--accent-sat, 72%), 58%)
		);
	}
	/* Quarter ticks: the scale is visible, so the bar measures rather than spins. */
	.ticks {
		position: absolute;
		inset: 0;
		background: repeating-linear-gradient(
			90deg,
			transparent 0,
			transparent calc(25% - 1px),
			var(--accent-line) calc(25% - 1px),
			var(--accent-line) 25%
		);
		pointer-events: none;
	}
</style>
