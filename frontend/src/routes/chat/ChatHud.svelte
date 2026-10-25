<script module lang="ts">
	// Parsed env HUD values, in wire order (Lane 1f: time, date, loc, weather,
	// forecast). Empty string = unknown; the renderer degrades per slot.
	export interface HudValues {
		time: string;
		date: string;
		loc: string;
		weather: string;
		forecast: string;
	}

	// Tolerant local parser for the wire statement
	// `hud = EnvTracker("t", "d", "l", "w", "f")` — five positional double-quoted
	// JSON string literals, mirroring `src/fabulist/state/env_hud.py`. Scans the
	// call to its matching close paren (strings copied whole, so a stray paren or
	// comma inside a descriptor can never split an argument). Anything that is not
	// exactly five string literals returns `null`, so the caller renders nothing
	// rather than an error frame.
	export function parseHudStatement(statement: string | null | undefined): HudValues | null {
		if (!statement) return null;
		const call = /\bEnvTracker\s*\(/.exec(statement);
		if (!call) return null;

		const args: string[] = [];
		let current = '';
		let inString = false;
		let escaped = false;
		let depth = 1;
		let closed = false;
		for (let i = call.index + call[0].length; i < statement.length; i++) {
			const ch = statement[i];
			if (inString) {
				current += ch;
				if (escaped) escaped = false;
				else if (ch === '\\') escaped = true;
				else if (ch === '"') inString = false;
				continue;
			}
			if (ch === '"') {
				inString = true;
				current += ch;
				continue;
			}
			if (ch === '(') {
				depth++;
				current += ch;
				continue;
			}
			if (ch === ')') {
				depth--;
				if (depth === 0) {
					args.push(current);
					closed = true;
					break;
				}
				current += ch;
				continue;
			}
			if (ch === ',' && depth === 1) {
				args.push(current);
				current = '';
				continue;
			}
			current += ch;
		}
		if (!closed || args.length !== 5) return null;

		const parsed = args.map(parseLiteral);
		if (parsed.some((value) => value === null)) return null;
		const [time, date, loc, weather, forecast] = parsed as string[];
		return { time, date, loc, weather, forecast };
	}

	function parseLiteral(raw: string): string | null {
		const text = raw.trim();
		if (text.length < 2 || !text.startsWith('"') || !text.endsWith('"')) return null;
		try {
			const value = JSON.parse(text);
			return typeof value === 'string' ? value : null;
		} catch {
			// The OpenUI tokenizer's own fallback: strip the surrounding quotes.
			return text.slice(1, -1);
		}
	}
</script>

<script lang="ts">
	// Chat HUD bar (D38 diagram wave, Lane 2a). Self-contained: reads the current
	// session from the shared stores, fetches the engine's env HUD statement, and
	// renders the authored EnvTracker asset inside a bar row built to host future
	// pinned modules (today: the env station only).
	//
	// Refresh trigger mirrors StateSidebar: the parent replaces the `facts`
	// object after each kept turn (the engine has already stamped the new
	// `system`/`env_hud` statement server-side), and the session id changes on
	// switch/create. Read the `$`-store values directly — `get()` inside `$effect`
	// does not track (Svelte 5).
	import { facts, sessionId } from '#lib/stores';
	import { getHud } from '#lib/hudApi';
	import { clearHud, hud, setHud } from '#lib/hudStore';
	import EnvTracker from '#lib/openui/components/EnvTracker.svelte';

	$effect(() => {
		const id = $sessionId;
		void $facts;
		if (!id) {
			clearHud();
			return;
		}
		let cancelled = false;
		void getHud(id).then((statement) => {
			if (cancelled) return;
			if (statement) setHud(id, statement);
			else clearHud();
		});
		return () => {
			cancelled = true;
		};
	});

	// Only render a statement that belongs to the session currently shown.
	const values = $derived.by(() => {
		const current = $hud;
		if (!current.statement || current.sessionId !== $sessionId) return null;
		return parseHudStatement(current.statement);
	});
</script>

<!-- The env station (issue #22): it now rides the THREAD HEAD — `<ChatHud />` is
     rendered inside `header.thread-head` and this element is that grid's `env`
     band. It is the ONE owner of time / date / location / weather / forecast; no
     other surface reprints them. The station renders nothing at all until the
     engine has emitted a statement for THIS session, so an empty chat pays no
     height for it. -->
{#if values}
	<div class="hud-bar" data-hud="1" role="region" aria-label="Session HUD">
		<EnvTracker
			time={values.time}
			date={values.date}
			loc={values.loc}
			weather={values.weather}
			forecast={values.forecast}
		/>
	</div>
{/if}

<style>
	.hud-bar {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		width: 100%;
		min-width: 0;
	}
</style>
