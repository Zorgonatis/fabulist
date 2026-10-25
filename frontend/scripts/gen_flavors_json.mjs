// Generate `src/lib/openui/schemas/flavors.json` from the zod schemas in
// `src/lib/openui/schemas/flavors.ts` (the single source of truth).
//
// The Python narrator-side validation gate (`fabulist.graph.validation`) and
// the model-facing library doc (`fabulist.prompts.library_mini`) read this
// plain-JSON mirror so they need no JS runtime. Run it from `frontend/`:
//
//     npm run gen:flavors
//
// Node type-strips the imported `.ts` module natively (Node >= 22.6, default
// from 23.6); no extra dependency is required. Never hand-edit the mirror — the
// header it writes says so.

import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { writeFileSync } from 'node:fs';
import { z } from 'zod';

// Import the schema + meta module directly (Node strips the TS types).
const { flavorSchemas, flavorMeta } = await import(
	new URL('../src/lib/openui/schemas/flavors.ts', import.meta.url).href
);

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = resolve(__dirname, '../src/lib/openui/schemas/flavors.json');

const PRESET_SECTION = 'Visual Toolkit (VTK)';

/** Flatten a zod-generated JSON-schema property into the mirror's `type`. */
function propType(prop) {
	if (!prop || typeof prop !== 'object') return 'unknown';
	if (prop.type === 'array') {
		const item = prop.items && prop.items.type ? prop.items.type : 'unknown';
		return `${item}[]`;
	}
	return prop.type ?? 'unknown';
}

const flavors = Object.keys(flavorSchemas).map((name) => {
	const schema = flavorSchemas[name];
	const meta = flavorMeta[name];
	if (!meta) throw new Error(`flavorMeta is missing an entry for ${name}`);
	const json = z.toJSONSchema(schema);
	const required = new Set(json.required ?? []);
	const props = Object.keys(json.properties ?? {}).map((propName) => {
		const prop = json.properties[propName];
		return {
			name: propName,
			type: propType(prop),
			required: required.has(propName),
			description: prop.description ?? ''
		};
	});
	return {
		component: name,
		flavor: meta.presetEntry,
		root: 'root',
		description: meta.bestFor,
		presetSection: meta.presetSection ?? PRESET_SECTION,
		presetEntry: meta.presetEntry,
		bestFor: meta.bestFor,
		exampleElements: meta.exampleElements,
		rules: [...meta.rules],
		docHint: meta.docHint,
		props
	};
});

const mirror = {
	$comment:
		'GENERATED MIRROR of flavors.ts (Wave 2a cull). Regenerate with ' +
		'`npm run gen:flavors` (frontend/scripts/gen_flavors_json.mjs); do not edit by hand. ' +
		'Read by the Python narrator-side validation gate so it needs no JS runtime.',
	preset: 'Stabs-EDH-3.0.1 + Stabs-GLM5.1-Directives-v4.0.0.json (VTK, Wave 2a cull)',
	presetSection: PRESET_SECTION,
	root: 'root',
	orderedArgs: true,
	flavors
};

writeFileSync(OUT, JSON.stringify(mirror, null, 2) + '\n', 'utf8');
console.log(`wrote ${OUT} (${flavors.length} flavors)`);
