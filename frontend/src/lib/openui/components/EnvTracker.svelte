<script lang="ts">
	// Authored env HUD asset (D38 diagram wave, Lane 2a): the "weather station".
	//
	// Philosophy (owner, verbatim): "build the asset (and detail it), give the
	// model ability to modulate" — the VISUAL IDENTITY is authored here; only the
	// five VALUES arrive per turn from the engine. This is chrome, not a
	// plain-text strip and not a narrator-openable flavor (D38b: the narrator
	// never learns this component exists).
	//
	// Wire contract (Lane 1f, src/fabulist/state/env_hud.py): the statement
	// `hud = EnvTracker(time, date, loc, weather, forecast)` — five positional
	// strings. An empty string means unknown and every slot degrades
	// independently: no time hides the clock + digital readout, no date hides the
	// date line, no weather shows a neutral dash, no forecast hides the "+6h"
	// chip, no location hides the pin. Pure presentational: props in, no fetch.
	//
	// Colour: existing theme tokens only (docs/UI-LANGUAGE.md §2/§4) — zero new
	// hex. The glyph map is deterministic (keyword -> authored SVG), so the same
	// descriptor always draws the same station.

	interface Props {
		time?: string;
		date?: string;
		loc?: string;
		weather?: string;
		forecast?: string;
	}

	let { time = '', date = '', loc = '', weather = '', forecast = '' }: Props = $props();

	// --- clock -------------------------------------------------------------
	// HH:MM 24h -> hand angles. The hour hand advances smoothly with the minutes.
	const TICKS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
	const clock = $derived(parseClock(time));
	const hourAngle = $derived(clock ? ((clock.h % 12) + clock.m / 60) * 30 : 0);
	const minuteAngle = $derived(clock ? clock.m * 6 : 0);

	function parseClock(raw: string): { h: number; m: number } | null {
		const match = /^(\d{1,2}):(\d{2})$/.exec(raw.trim());
		if (!match) return null;
		const h = Number(match[1]);
		const m = Number(match[2]);
		if (h > 23 || m > 59) return null;
		return { h, m };
	}

	// --- weather glyph map -------------------------------------------------
	// Deterministic keyword map. Order IS precedence: the first condition whose
	// keyword appears in the (lower-cased) descriptor wins, so "thunderstorm"
	// draws the storm before the rain. An unknown descriptor draws the neutral
	// ring; the descriptor text is rendered verbatim beside it either way.
	type Condition = 'storm' | 'deluge' | 'snow' | 'fog' | 'rain' | 'wind' | 'sun' | 'cloud';
	const CONDITIONS: Array<{ id: Condition; words: string[] }> = [
		{ id: 'storm', words: ['storm', 'thunder', 'lightning', 'tempest'] },
		{ id: 'deluge', words: ['deluge', 'downpour', 'torrent', 'monsoon', 'flood', 'pour'] },
		{ id: 'snow', words: ['snow', 'sleet', 'blizzard', 'frost', 'hail', 'flurr'] },
		{ id: 'fog', words: ['fog', 'mist', 'haze', 'smog', 'murk'] },
		{ id: 'rain', words: ['rain', 'drizzle', 'shower', 'sprinkle'] },
		{ id: 'wind', words: ['wind', 'gale', 'gust', 'breeze', 'squall'] },
		{ id: 'sun', words: ['sun', 'clear', 'fair', 'bright'] },
		{ id: 'cloud', words: ['cloud', 'overcast', 'grey', 'gray', 'gloom', 'dull'] }
	];

	function classify(raw: string): Condition | null {
		const text = raw.trim().toLowerCase();
		if (!text) return null;
		for (const { id, words } of CONDITIONS) {
			if (words.some((word) => text.includes(word))) return id;
		}
		return null;
	}

	const weatherCondition = $derived(classify(weather));
	const forecastCondition = $derived(classify(forecast));

	const CLOUD_PATH =
		'M7 17.5h9.5a3.5 3.5 0 0 0 0-7 5.5 5.5 0 0 0-10.4-1.2A3.9 3.9 0 0 0 7 17.5Z';
</script>

{#snippet wxGlyph(cond: Condition | null)}
	<svg class="wx-glyph" viewBox="0 0 24 24" aria-hidden="true">
		{#if cond === 'sun'}
			<circle cx="12" cy="12" r="4.4" />
			<g>
				<line x1="12" y1="2.5" x2="12" y2="5" />
				<line x1="12" y1="19" x2="12" y2="21.5" />
				<line x1="2.5" y1="12" x2="5" y2="12" />
				<line x1="19" y1="12" x2="21.5" y2="12" />
				<line x1="5.2" y1="5.2" x2="6.9" y2="6.9" />
				<line x1="17.1" y1="17.1" x2="18.8" y2="18.8" />
				<line x1="5.2" y1="18.8" x2="6.9" y2="17.1" />
				<line x1="17.1" y1="6.9" x2="18.8" y2="5.2" />
			</g>
		{:else if cond === 'cloud'}
			<path d={CLOUD_PATH} />
		{:else if cond === 'rain'}
			<path d={CLOUD_PATH} />
			<g class="precip">
				<line x1="9.6" y1="18.6" x2="8.6" y2="21.2" />
				<line x1="12.6" y1="18.6" x2="11.6" y2="21.2" />
				<line x1="15.6" y1="18.6" x2="14.6" y2="21.2" />
			</g>
		{:else if cond === 'deluge'}
			<path d={CLOUD_PATH} />
			<g class="precip heavy">
				<line x1="8.2" y1="18.4" x2="7.4" y2="22.4" />
				<line x1="11.2" y1="18.4" x2="10.4" y2="22.4" />
				<line x1="14.2" y1="18.4" x2="13.4" y2="22.4" />
				<line x1="17.2" y1="18.4" x2="16.4" y2="22.4" />
			</g>
		{:else if cond === 'storm'}
			<path d={CLOUD_PATH} />
			<path class="fill" d="M12.9 17.2 9.4 21.8h2.3l-1 3 4-4.6h-2.4l1.5-3.1Z" />
		{:else if cond === 'snow'}
			<path d={CLOUD_PATH} />
			<g class="flakes">
				<line x1="9" y1="19" x2="9" y2="22" />
				<line x1="7.7" y1="19.8" x2="10.3" y2="21.2" />
				<line x1="7.7" y1="21.2" x2="10.3" y2="19.8" />
				<line x1="15" y1="19" x2="15" y2="22" />
				<line x1="13.7" y1="19.8" x2="16.3" y2="21.2" />
				<line x1="13.7" y1="21.2" x2="16.3" y2="19.8" />
			</g>
		{:else if cond === 'fog'}
			<g class="fog">
				<line x1="3.5" y1="8.5" x2="20.5" y2="8.5" />
				<line x1="4.5" y1="12.5" x2="17" y2="12.5" />
				<line x1="3.5" y1="16.5" x2="20.5" y2="16.5" />
			</g>
		{:else if cond === 'wind'}
			<g class="wind">
				<path d="M3 8.5h9a2.5 2.5 0 1 0-2.5-2.5" />
				<path d="M3 15.5h12a2.5 2.5 0 1 1-2.5 2.5" />
				<path d="M3 12h6.5" />
			</g>
		{:else}
			<circle class="neutral-ring" cx="12" cy="12" r="7" />
			<circle class="fill" cx="12" cy="12" r="1.5" />
		{/if}
	</svg>
{/snippet}

<div class="env-tracker" role="group" aria-label="Environment">
	{#if clock}
		<svg class="clock" viewBox="0 0 40 40" role="img" aria-label="Clock showing {time}">
			<circle class="face" cx="20" cy="20" r="17" />
			{#each TICKS as t (t)}
				<line
					class="tick"
					class:major={t % 3 === 0}
					x1="20"
					y1="3.6"
					x2="20"
					y2={t % 3 === 0 ? 6 : 5}
					transform="rotate({t * 30} 20 20)"
				/>
			{/each}
			<line
				class="hand hour"
				x1="20"
				y1="20"
				x2="20"
				y2="10.5"
				transform="rotate({hourAngle} 20 20)"
			/>
			<line
				class="hand minute"
				x1="20"
				y1="20"
				x2="20"
				y2="7.5"
				transform="rotate({minuteAngle} 20 20)"
			/>
			<circle class="pin" cx="20" cy="20" r="1.7" />
		</svg>
		<div class="readout">
			<span class="digital">{time}</span>
			{#if date}<span class="date">{date}</span>{/if}
		</div>
		<span class="divider" aria-hidden="true"></span>
	{/if}

	<div class="wx" data-condition={weatherCondition ?? 'neutral'}>
		{#if weather}
			{@render wxGlyph(weatherCondition)}
			<span class="wx-text">{weather}</span>
		{:else}
			<span class="wx-dash" aria-hidden="true">—</span>
		{/if}
	</div>

	{#if forecast}
		<div class="forecast">
			<span class="forecast-tag">+6h</span>
			{@render wxGlyph(forecastCondition)}
			<span class="forecast-text">{forecast}</span>
		</div>
	{/if}

	{#if loc}
		<span class="divider" aria-hidden="true"></span>
		<div class="loc">
			<svg class="pin-icon" viewBox="0 0 24 24" aria-hidden="true">
				<path
					d="M12 2.5a6.5 6.5 0 0 0-6.5 6.5c0 4.7 6.5 12.5 6.5 12.5s6.5-7.8 6.5-12.5A6.5 6.5 0 0 0 12 2.5Zm0 8.8a2.3 2.3 0 1 1 0-4.6 2.3 2.3 0 0 1 0 4.6Z"
				/>
			</svg>
			<span class="loc-text">{loc}</span>
		</div>
	{/if}
</div>

<style>
	/* Compact header bar (~2.5-3rem): a station readout, not a text strip.
	   Docking the studio narrows the thread, and a row whose chips cannot shrink
	   pushed the forecast chip past the thread edge. Every text chip now has a
	   readable min-width and ellipsizes its own value, so the row stays one row
	   and no chip is ever cut off at the container edge. */
	.env-tracker {
		display: flex;
		align-items: center;
		gap: 0.7rem;
		min-height: 2.75rem;
		padding: 0.3rem 0.8rem;
		border: 1px solid var(--line);
		border-radius: 8px;
		background: var(--bg-panel);
		color: var(--text);
		font-size: var(--font-ui);
		min-width: 0;
	}

	.divider {
		width: 1px;
		align-self: stretch;
		margin: 0.3rem 0;
		background: var(--line);
		flex: 0 0 auto;
	}

	/* — clock — */
	.clock {
		width: 2rem;
		height: 2rem;
		flex: 0 0 auto;
	}
	.face {
		fill: var(--bg-inset);
		stroke: var(--line-strong);
		stroke-width: 1.4;
	}
	.tick {
		stroke: var(--text-dim);
		stroke-width: 1;
	}
	.tick.major {
		stroke: var(--text-muted);
		stroke-width: 1.4;
	}
	.hand {
		stroke: var(--text);
		stroke-linecap: round;
	}
	.hand.hour {
		stroke-width: 2.6;
	}
	.hand.minute {
		stroke-width: 1.8;
	}
	.pin {
		fill: var(--tone-1);
	}

	/* — digital readout + date line — */
	.readout {
		display: flex;
		flex-direction: column;
		line-height: 1.1;
		/* A floor, not zero: the clock stays legible and only the date truncates. */
		min-width: 2.75rem;
	}
	.digital {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-variant-numeric: tabular-nums;
		font-size: var(--font-ui);
		letter-spacing: 0.03em;
		color: var(--text);
		white-space: nowrap;
	}
	.date {
		font-size: var(--font-micro);
		letter-spacing: 0.02em;
		color: var(--text-muted);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* — weather — */
	.wx {
		display: inline-flex;
		align-items: center;
		gap: 0.4rem;
		/* The glyph plus a few characters of the descriptor stay visible. */
		min-width: 3rem;
	}
	.wx-glyph {
		width: 1.4rem;
		height: 1.4rem;
		flex: 0 0 auto;
		stroke: currentColor;
		fill: none;
		stroke-width: 1.6;
		stroke-linecap: round;
		stroke-linejoin: round;
	}
	.wx-glyph .fill {
		fill: currentColor;
		stroke: none;
	}
	.wx-glyph .precip {
		stroke-width: 1.6;
	}
	.wx-glyph .precip.heavy {
		stroke-width: 2.4;
	}
	.wx-glyph .neutral-ring {
		stroke-dasharray: 3 2.4;
	}
	.wx-text {
		font-size: var(--font-ui);
		color: var(--text-muted);
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.wx-dash {
		font-size: var(--font-ui);
		color: var(--text-dim);
	}
	/* Deterministic palette: existing tokens only, one colour per condition. */
	.wx[data-condition='sun'] {
		color: var(--warn);
	}
	.wx[data-condition='storm'] {
		color: var(--warn);
	}
	.wx[data-condition='rain'],
	.wx[data-condition='deluge'] {
		color: var(--tone-2);
	}
	.wx[data-condition='snow'] {
		color: var(--text);
	}
	.wx[data-condition='fog'] {
		color: var(--text-dim);
	}
	.wx[data-condition='wind'],
	.wx[data-condition='cloud'] {
		color: var(--text-muted);
	}
	.wx[data-condition='neutral'] {
		color: var(--text-dim);
	}

	/* — forecast chip — */
	.forecast {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		padding: 0.15rem 0.55rem 0.15rem 0.45rem;
		border: 1px solid var(--line);
		border-radius: 999px;
		background: var(--bg-raised);
		/* Shrink before it overflows the row; the text ellipsizes. */
		flex: 0 1 auto;
		min-width: 4rem;
	}
	.forecast-tag {
		font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
		font-size: var(--font-micro);
		letter-spacing: 0.05em;
		color: var(--tone-1);
	}
	.forecast .wx-glyph {
		width: 1.05rem;
		height: 1.05rem;
		color: var(--text-muted);
	}
	.forecast-text {
		font-size: var(--font-small);
		color: var(--text-muted);
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	/* — location — */
	.loc {
		display: inline-flex;
		align-items: center;
		gap: 0.35rem;
		/* The pin stays; only the place name truncates. */
		min-width: 2.5rem;
	}
	.pin-icon {
		width: 1rem;
		height: 1rem;
		flex: 0 0 auto;
		fill: var(--tone-1);
	}
	.loc-text {
		font-size: var(--font-ui);
		color: var(--text-muted);
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
</style>
