<script lang="ts">
	// Component gallery (V5; artifact-components 2026-10-14): exercises ALL
	// twenty components — four defaults, ten scene flavors, Custom, five
	// artifact reproductions — on one page so the visuals iterate without
	// model turns.
	//
	// DEV-ONLY ROUTE. `/components` is absent from the production build: the
	// build routes from a staged `src/routes` overlay that leaves it out (see
	// DEV_ONLY_ROUTES in vite.config.ts), so this file only ever reaches a
	// browser through `npm run dev`. It talks to no API — the payload below is
	// rendered through the real library — so it needs neither `VITE_MOCK=1`
	// nor a running engine. `scripts/render_shots.py` screenshots this page.
	//
	// The theme buttons re-tint the live `--accent-hue`/`--accent-sat` tokens —
	// exactly what the R4 theme pipeline sets on :root per session — so every
	// flavor's palette (all derived from those vars) re-themes in place.
	import { Renderer } from '@openuidev/svelte-lang';
	import { library } from '#lib/openui/library';

	const HUES: { label: string; hue: number; sat: number; warmth: number }[] = [
		{ label: 'Violet (default)', hue: 262, sat: 72, warmth: 0 },
		{ label: 'Crimson', hue: 350, sat: 66, warmth: -1 },
		{ label: 'Teal', hue: 190, sat: 58, warmth: -2 },
		{ label: 'Amber', hue: 38, sat: 74, warmth: 2 }
	];

	let active = $state(0);
	const theme = $derived(HUES[active]);

	// Every flavor, anchored as an inline statement; the single structural root
	// line closes the set (the placement rule the narrator follows).
	const DSL = `mind = Mindscape("Cracks in the Facade", [{text: "Hold it together", weight: 45, kind: "thought"}, {text: "IT IS ALREADY OPEN", weight: 95, kind: "scream"}, {text: "they can smell fear", weight: 20, kind: "aside"}, {text: "I am fine", weight: 70, kind: "thought"}], "grain", "", "dread", {glyph: "(・ε・)"})
term = Interface("terminal", "LANTERN//2", ["handshake ok", "1 message waiting", "> trace route …", "route refused"], {art: "┌─[ LANTERN ]───┐\\n│ signal: none  │\\n│ route: closed │\\n└───────────────┘", glyph: "📡"})
letter = Document("letter", "To the Harbourmaster", "The tide took the lamps before dawn, and the fog has opinions about the eastern pier. Do not send the boy.\\n\\nHe is the last of the line, and the water already knows his name.", "— A friend", {glyph: "📜"})
relic = Artifact("The Cursed Compass", "Relic", "cursed", "It points at whoever last told a lie. It has never once pointed north, and it has never yet been wrong.", ["Truthfulness: 2/10", "Weight: 0.4kg", "Cursed: extremely", "Favours owed: −3"], {glyph: "🧭"})
spot = DialogueSpotlight("The Innkeeper", "Of course I'm fine.", "Flat and level — the answer she rehearsed, and the door already closing.", {glyph: "🎙️"})
react = ReactionSpotlight("😬", "That was not the question I meant to ask.")
fail = FailureAchievement("🏆", "Diplomacy, Attempted", "You called the duke 'the tall one'. He is not tall.", "Some careers end in a single syllable.")
mem = Memory("the summer of ash", ["Chalk dust in the rafters", "Her laugh, two rooms away", "The smell of lamp oil"], "The faces keep sliding off the frame.", {glyph: "🕯️"})
entrance = Entrance("Voss", "The Duke's Enforcer", "A coat the colour of knife-scars, and eyes that never blink first.", {markers: "Eyes: pale grey, unblinking; Coat: knife-scar colours", glyph: "🎩"})
fight = Clash("Innkeeper", "You", 88, "a wild haymaker, all shoulder and no plan", {glyph: "⚡"})
meter = MeterBar("Tension", 68, 100, {glyph: "⏳"})
card = ArtifactCard("Brass Lantern", "Quest item", "Picked up in the market square; the flame leans toward the coast.", {glyph: "🗝️"})
beat = BeatCard("The innkeeper avoids your eyes when the name is spoken.", {glyph: "❗"})
band = Phone("NO ENCORE — band chat", [{from: "Marisol", text: "soundcheck in 20. bring the good amp", time: "18:02", status: "read", reactions: [{emoji: "🎸", count: 3}]}, {from: "Dev", text: "the good amp is the one that caught fire", time: "18:04", status: "delivered"}, {from: "You", text: "bring BOTH amps", time: "18:05", status: "sent"}], {me: "You", typing: "Marisol", clock: "18:05"})
buzz = SMS("banner", "Unknown Number", [{from: "Unknown Number", text: "we have the boy. bring the compass. midnight.", time: "now"}], {glyph: "❓"})
texts = SMS("thread", "Dev", [{from: "Dev", text: "you kept the compass.", time: "02:11", status: "read"}, {from: "You", text: "i kept the boy breathing too. we are not even.", time: "02:13", status: "read"}, {from: "Dev", text: "midnight. pier 4. bring earplugs.", time: "02:14", status: "delivered", reactions: [{emoji: "🫤"}]}], {me: "You"})
crash = Monitor("bsod", "CRITICAL_PROCESS_DIED", ["27% complete", "collecting error info — do not turn the rig off", "for fixes, ask the harbourmaster's ghost"], {theme: "crt"})
fault = Monitor("error", "REACTOR CORE — UNHANDLED FAULT", ["coolant loop 2: REFUSED", "containment: holding (for now)", "recommend: do not greet it"], {theme: "digital"})
notes = PostIt([{text: "CALL HER BACK"}, {text: "gear: 2 amps, no fire"}, {text: "midnight ≠ late"}])
post = SocialPost("Marisol Vega", "marivv", "the amp that caught fire is not in the setlist photos because it IS the setlist. as a metaphor.", {likes: 214, reposts: 19, comments: 7}, {author: "Dev Okafor", handle: "devnoise", text: "the metaphor is also on fire"})
scrap = Custom("Setlist — Friday", "<div class='set'><h3>SETLIST — FRIDAY</h3><ol><li>Cold Open</li><li>Harbour Lights</li><li>NO ENCORE</li></ol><p class='note'>encore listed for legal reasons only</p></div>", ".set{border:1px solid var(--line);padding:0.9rem;background:var(--bg-panel)} h3{color:var(--tone-1);letter-spacing:0.08em} ol{margin:0.4rem 0 0 1.2rem} .note{font-size:14px;opacity:0.7}")
root = ScenePane("VTK — every component on one wall", [meter, card, beat, mind, term, letter, relic, spot, react, fail, mem, entrance, fight, band, buzz, texts, crash, fault, notes, post, scrap], {glyph: "⚓"})`;
</script>

<svelte:head>
	<title>Fabulist — component gallery</title>
</svelte:head>

<main
	class="gallery"
	style="--accent-hue: {theme.hue}; --accent-sat: {theme.sat}%; --bg-warmth: {theme.warmth}%;"
>
	<header class="bar">
		<div>
			<h1>Component gallery</h1>
			<p>
				All twenty components (4 defaults + 10 flavors + Custom + 5 reproductions),
				rendered from one DSL payload. Palettes derive from the live accent tokens —
				switch the theme to watch every panel re-colour.
			</p>
		</div>
		<div class="themes" role="group" aria-label="Theme">
			{#each HUES as option, i (option.label)}
				<button type="button" class:active={i === active} onclick={() => (active = i)}>
					{option.label}
				</button>
			{/each}
		</div>
	</header>
	<div class="wall">
		<Renderer response={DSL} {library} />
	</div>
</main>

<style>
	.gallery {
		min-height: 100vh;
		padding: 1.4rem clamp(1rem, 4vw, 3rem) 3rem;
		background: var(--bg);
		color: var(--text);
	}
	.bar {
		display: flex;
		flex-wrap: wrap;
		gap: 1rem;
		align-items: flex-end;
		justify-content: space-between;
		margin-bottom: 1.4rem;
		border-bottom: 1px solid var(--line);
		padding-bottom: 1rem;
	}
	h1 {
		margin: 0 0 0.3rem;
		font-size: 1.25rem;
	}
	.bar p {
		margin: 0;
		max-width: 46rem;
		font-size: 0.82rem;
		line-height: 1.5;
		color: var(--text-muted);
	}
	.themes {
		display: flex;
		flex-wrap: wrap;
		gap: 0.4rem;
	}
	.themes button {
		font: inherit;
		font-size: 0.78rem;
		padding: 0.35rem 0.7rem;
		border-radius: 999px;
		border: 1px solid var(--line-strong);
		background: var(--bg-raised);
		color: var(--text-muted);
		cursor: pointer;
	}
	.themes button:hover {
		background: var(--bg-hover);
		color: var(--text);
	}
	.themes button.active {
		background: var(--accent-soft);
		border-color: var(--accent-line);
		color: var(--accent-strong);
	}
	/* Each panel needs room to breathe and a floor width so the grid reads as a
	   gallery, not a column. */
	.wall {
		max-width: 900px;
	}
</style>
