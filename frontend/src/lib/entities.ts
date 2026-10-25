// Entity kind descriptor table (D39 / W9.1) ------------------------------------
//
// One place decides which object types exist and which operations each one
// supports — the mechanism behind D39's "import and export in every layer".
// The three primitives (`EntityDrawer` / `EntityList` / `DefinitionControls`)
// stay per-type-agnostic; every per-kind decision (client function, list label,
// id key, import body) lives here and nowhere else.
//
// `actor` is a scope of the persona entity (D37b): it reuses the persona client
// functions verbatim. `chat` rides the same table so the rail can render a
// session row through the shared primitives, but a chat is not a portable
// definition — it has no `get`/`create` and therefore no import/export.
//
// Definitions are DATA (D4): a definition body is carried verbatim through the
// log, so the import adapters below coerce the wire object tolerantly rather
// than rejecting unknown keys.

import type { AssistantWrite, BookEntry, PersonaWrite } from '#lib/api';
import {
	createAssistant,
	createBook,
	createPersona,
	createWorld,
	deleteAssistant,
	deleteBook,
	deletePersona,
	deleteSession,
	deleteWorld,
	getBook,
	getPersona,
	getWorld,
	importCard,
	listAssistants,
	listBooks,
	listPersonas,
	listSessions,
	listWorlds,
	normalizeBookEntry,
	normalizeWorld,
	putBook,
	renameSession,
	updateAssistant,
	updatePersona,
	updateWorld
} from '#lib/api';

/** The object types the management surfaces can address. */
export type ObjectKind = 'persona' | 'actor' | 'assistant' | 'world' | 'book' | 'chat';

/** One catalog row, already labelled — what `EntityList` renders directly. */
export interface EntityRow {
	id: string;
	label: string;
	/** Compact secondary text (counts, refs); omitted when empty. */
	meta?: string;
}

/** A loosely-typed definition body (definitions ride through as data, D4). */
export type DefinitionBody = Record<string, unknown>;

/** The operations one kind exposes; an absent op means "not supported here". */
export interface EntityKindSpec {
	kind: ObjectKind;
	/** Singular human label (button copy, aria). */
	label: string;
	/** Plural human label (list head count). */
	plural: string;
	list: () => Promise<EntityRow[]>;
	get?: (id: string) => Promise<DefinitionBody>;
	create?: (body: DefinitionBody) => Promise<unknown>;
	update?: (id: string, body: DefinitionBody) => Promise<unknown>;
	remove?: (id: string) => Promise<void>;
	/** Download one definition, or the whole catalog when `id` is null. */
	export?: (id: string | null) => Promise<string>;
	/** Import a parsed definition; resolves to the created id. */
	import?: (source: unknown) => Promise<string>;
	/** The ONE import door (issue #60): import a FILE — a card PNG, a card JSON,
	 * an exported definition or a lore file — as this kind, whatever its format.
	 * Resolves to the created id + its name for the inline status line. */
	importFile?: (file: File) => Promise<ImportedDefinition>;
}

// --- tolerant field readers (definitions are data, D4) ----------------------

function objectOf(value: unknown): Record<string, unknown> {
	return value && typeof value === 'object' && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: {};
}

function stringOf(value: unknown): string {
	return typeof value === 'string' ? value : '';
}

function stringList(value: unknown): string[] {
	return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

/** Trigger a browser download of `value` as JSON; returns the filename. */
function downloadJson(filename: string, value: unknown): string {
	if (typeof window === 'undefined') return filename;
	const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' });
	const url = URL.createObjectURL(blob);
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	anchor.click();
	URL.revokeObjectURL(url);
	return filename;
}

// --- import body adapters ---------------------------------------------------

/** Coerce a parsed persona JSON into the `POST /api/personas` body. */
function asPersonaBody(source: unknown): PersonaWrite {
	const obj = objectOf(source);
	const body: PersonaWrite = {
		name: stringOf(obj.name),
		description: stringOf(obj.description),
		personality: stringOf(obj.personality),
		scenario: stringOf(obj.scenario),
		first_mes: stringOf(obj.first_mes),
		book_ids: stringList(obj.book_ids),
		alternate_greetings: stringList(obj.alternate_greetings),
		extensions: objectOf(obj.extensions),
		provenance: objectOf(obj.provenance)
	};
	const id = stringOf(obj.persona_id);
	if (id) body.persona_id = id;
	return body;
}

/** Coerce a parsed assistant JSON into the `POST /api/assistants` body. */
function asAssistantWrite(source: unknown): AssistantWrite {
	const obj = objectOf(source);
	const body: AssistantWrite = {
		name: stringOf(obj.name),
		persona: stringOf(obj.persona),
		style_notes: stringOf(obj.style_notes),
		agent: stringOf(obj.agent) || 'narrator',
		trigger: stringOf(obj.trigger) || 'manual',
		keywords: stringList(obj.keywords)
	};
	const id = stringOf(obj.id);
	if (id) body.id = id;
	// D47 / L2.d: absent stays absent. A real actor id rides through; an empty one
	// OMITS the key entirely instead of forcing `actor: ''`, which would read as an
	// explicit blank actor on the wire.
	const actor = stringOf(obj.actor);
	if (actor) body.actor = actor;
	return body;
}

/** Coerce a parsed world JSON into the `POST /api/worlds` body. */
function asWorldBody(source: unknown): {
	name: string;
	description: string;
	scenario: string;
	first_mes: string;
	alternate_greetings: string[];
	cast: string[];
	book_ids: string[];
	provenance: DefinitionBody;
} {
	const obj = objectOf(source);
	return {
		name: stringOf(obj.name),
		description: stringOf(obj.description),
		scenario: stringOf(obj.scenario),
		// The world's opening (issue #51) rides through untouched, like
		// `provenance`: an exported world re-imported here must keep its
		// first message + swipe openings, never resurrect one as `scenario`.
		first_mes: stringOf(obj.first_mes),
		alternate_greetings: stringList(obj.alternate_greetings),
		cast: stringList(obj.cast),
		book_ids: stringList(obj.book_ids),
		provenance: objectOf(obj.provenance)
	};
}

/** Coerce a parsed book JSON into the `POST /api/books` body. */
function asBookBody(source: unknown): { name: string; entries: BookEntry[]; provenance: DefinitionBody } {
	const obj = objectOf(source);
	return {
		name: stringOf(obj.name),
		entries: (Array.isArray(obj.entries) ? obj.entries : []) as BookEntry[],
		provenance: objectOf(obj.provenance)
	};
}

// --- the one import door (issue #60) -----------------------------------------
//
// Every import control in the UI is `DefinitionControls`, and every one of them
// runs exactly ONE handler: `EntityKindSpec.importFile`. It reads the file,
// detects WHAT it is — a card PNG, a card JSON, an exported definition, a lore
// file — and imports it as the kind whose pane it sits in. That is why the
// button reads simply `Import` everywhere: the location says what is being
// imported, so no control offers a format choice and no door has its own rules.

/** What a file import yielded: the created definition's id + its display name. */
export interface ImportedDefinition {
	id: string;
	/** Name for the inline “Imported …” line (omitted when the file has none). */
	label?: string;
}

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** True when `file` is a PNG: reported type, extension, or the magic bytes. */
async function isPngFile(file: File): Promise<boolean> {
	if ((file.type || '').toLowerCase() === 'image/png') return true;
	if (file.name.toLowerCase().endsWith('.png')) return true;
	try {
		const head = new Uint8Array(await file.slice(0, 8).arrayBuffer());
		return head.length === PNG_MAGIC.length && PNG_MAGIC.every((byte, index) => head[index] === byte);
	} catch {
		return false;
	}
}

/** Parse a JSON file, replacing the engine's SyntaxError with a plain sentence. */
async function readJsonFile(file: File): Promise<unknown> {
	const text = await file.text();
	try {
		return JSON.parse(text) as unknown;
	} catch {
		throw new Error(`${file.name || 'that file'} is not valid JSON`);
	}
}

/** An exported definition carries its own id; a character card never does. */
function carriesId(source: unknown, key: string): boolean {
	return stringOf(objectOf(source)[key]) !== '';
}

/** The `name` a definition/lore file declares, for the status line. */
function nameOf(source: unknown): string | undefined {
	return stringOf(objectOf(source).name) || undefined;
}

/**
 * Import one card (PNG or card JSON) as `kind`.
 *
 * Both card destinations ride the ONE entry point
 * (`importCard(source, { persona } | { world })`): the file format is detected
 * here and the type comes from the pane, never from a choice in the UI.
 */
async function importCardFile(
	file: File,
	kind: 'persona' | 'world'
): Promise<ImportedDefinition> {
	const result = await importCard(file, kind === 'world' ? { world: true } : { persona: true });
	if (!result.ok) throw new Error(result.error || 'could not import that file');
	if (kind === 'world') {
		const world = normalizeWorld(result.world);
		if (!world) throw new Error(result.error || 'could not import that file');
		return { id: world.world_id, label: world.name || undefined };
	}
	const persona = objectOf(result.persona);
	const id = stringOf(persona.persona_id);
	if (!id) throw new Error('the import did not create a persona');
	return { id, label: stringOf(persona.name) || undefined };
}

/** Import an exported/catalog definition body through the kind's own create path
 * (so its id survives the round trip — a card has none to lose). */
async function importDefinitionBody(
	kind: ObjectKind,
	source: unknown
): Promise<ImportedDefinition> {
	const runImport = ENTITY_KINDS[kind].import;
	if (!runImport) throw new Error(`importing a ${ENTITY_KINDS[kind].label} is not supported`);
	return { id: await runImport(source), label: nameOf(source) };
}

/** Pull the raw entry list out of any accepted world-info / book shape. */
function loreEntries(raw: unknown): Record<string, unknown>[] {
	const isRecord = (value: unknown): value is Record<string, unknown> =>
		typeof value === 'object' && value !== null && !Array.isArray(value);
	// Only objects that actually look like lore entries qualify, so a card or an
	// arbitrary JSON blob resolves to "no entries" instead of a junk book.
	const isEntry = (value: unknown): value is Record<string, unknown> =>
		isRecord(value) && ('content' in value || 'keys' in value || 'key' in value);
	let source: unknown = raw;
	if (isRecord(raw) && 'entries' in raw) source = raw.entries;
	if (Array.isArray(source)) return source.filter(isEntry);
	// A raw lore file is a numeric-id -> entry mapping; a card's `character_book`
	// may be that mapping too.
	if (isRecord(source)) return Object.values(source).filter(isEntry);
	return [];
}

/** Book name from the JSON, else the file stem. */
function loreName(raw: unknown, filename: string): string {
	const declared = nameOf(raw);
	if (declared) return declared.trim();
	return filename.replace(/\.json$/i, '').trim() || 'Imported lore';
}

/** Persona (and actor): a card in either format, or an exported persona. */
async function importPersonaFile(file: File): Promise<ImportedDefinition> {
	if (!(await isPngFile(file))) {
		const source = await readJsonFile(file);
		if (!carriesId(source, 'persona_id')) return importCardFile(file, 'persona');
		return importDefinitionBody('persona', source);
	}
	return importCardFile(file, 'persona');
}

/** World: a card in either format, or an exported world definition. */
async function importWorldFile(file: File): Promise<ImportedDefinition> {
	if (!(await isPngFile(file))) {
		const source = await readJsonFile(file);
		if (!carriesId(source, 'world_id')) return importCardFile(file, 'world');
		return importDefinitionBody('world', source);
	}
	return importCardFile(file, 'world');
}

/** Book: an exported book, or a bare lore file in either accepted layout. */
async function importBookFile(file: File): Promise<ImportedDefinition> {
	if (await isPngFile(file)) throw new Error('a book imports from a lore file — that one is an image');
	const source = await readJsonFile(file);
	if (carriesId(source, 'book_id')) return importDefinitionBody('book', source);
	const entries = loreEntries(source);
	if (entries.length === 0) throw new Error('no lore entries found in that file');
	const saved = await createBook({
		name: loreName(source, file.name),
		entries: entries.map((entry) => normalizeBookEntry(entry))
	});
	return { id: saved.book_id, label: saved.name };
}

/** Assistant: an exported assistant definition. */
async function importAssistantFile(file: File): Promise<ImportedDefinition> {
	if (await isPngFile(file)) {
		throw new Error('an assistant imports from a JSON file — that one is an image');
	}
	return importDefinitionBody('assistant', await readJsonFile(file));
}

// --- per-kind descriptors ---------------------------------------------------

const personaKind: EntityKindSpec = {
	kind: 'persona',
	label: 'persona',
	plural: 'personas',
	list: async () =>
		(await listPersonas()).map((persona) => {
			const row: EntityRow = {
				id: persona.persona_id,
				label: persona.name || persona.persona_id
			};
			if (persona.scenario) row.meta = persona.scenario;
			return row;
		}),
	get: async (id) => (await getPersona(id)) as unknown as DefinitionBody,
	create: (body) => createPersona(asPersonaBody(body)),
	update: (id, body) => updatePersona(id, asPersonaBody(body)),
	remove: (id) => deletePersona(id),
	export: async (id) => {
		if (!id) return downloadJson('personas.json', await listPersonas());
		return downloadJson(`${id}.persona.json`, await getPersona(id));
	},
	import: async (source) => (await createPersona(asPersonaBody(source))).persona_id,
	importFile: importPersonaFile
};

const assistantKind: EntityKindSpec = {
	kind: 'assistant',
	label: 'assistant',
	plural: 'assistants',
	list: async () =>
		(await listAssistants()).assistants.map((assistant) => {
			const row: EntityRow = {
				id: assistant.id,
				label: assistant.name || assistant.id
			};
			if (assistant.actor) row.meta = `as ${assistant.actor}`;
			return row;
		}),
	get: async (id) => {
		const found = (await listAssistants()).assistants.find((assistant) => assistant.id === id);
		if (!found) throw new Error(`unknown assistant: ${id}`);
		return found as unknown as DefinitionBody;
	},
	create: (body) => createAssistant(asAssistantWrite(body)),
	update: (id, body) => updateAssistant(id, asAssistantWrite(body)),
	remove: (id) => deleteAssistant(id),
	export: async (id) => {
		if (!id) return downloadJson('assistants.json', (await listAssistants()).assistants);
		const found = (await listAssistants()).assistants.find((assistant) => assistant.id === id);
		if (!found) throw new Error(`unknown assistant: ${id}`);
		return downloadJson(`${id}.assistant.json`, found);
	},
	import: async (source) => (await createAssistant(asAssistantWrite(source))).id,
	importFile: importAssistantFile
};

const worldKind: EntityKindSpec = {
	kind: 'world',
	label: 'world',
	plural: 'worlds',
	list: async () =>
		(await listWorlds()).map((world) => ({
			id: world.world_id,
			label: world.name || world.world_id,
			meta: `${world.cast.length} cast · ${world.book_ids.length} books`
		})),
	get: async (id) => (await getWorld(id)) as unknown as DefinitionBody,
	create: (body) => createWorld(asWorldBody(body)),
	update: (id, body) => updateWorld(id, asWorldBody(body)),
	remove: (id) => deleteWorld(id),
	export: async (id) => {
		if (!id) return downloadJson('worlds.json', await listWorlds());
		return downloadJson(`${id}.world.json`, await getWorld(id));
	},
	import: async (source) => (await createWorld(asWorldBody(source))).world_id,
	importFile: importWorldFile
};

const bookKind: EntityKindSpec = {
	kind: 'book',
	label: 'book',
	plural: 'books',
	list: async () =>
		(await listBooks()).map((book) => ({
			id: book.book_id,
			label: book.name || book.book_id,
			meta: `${book.entries.length} entries`
		})),
	get: async (id) => (await getBook(id)) as unknown as DefinitionBody,
	create: (body) => createBook(asBookBody(body)),
	update: (id, body) => putBook(id, asBookBody(body)),
	remove: (id) => deleteBook(id),
	export: async (id) => {
		if (!id) return downloadJson('books.json', await listBooks());
		return downloadJson(`${id}.book.json`, await getBook(id));
	},
	import: async (source) => (await createBook(asBookBody(source))).book_id,
	importFile: importBookFile
};

const chatKind: EntityKindSpec = {
	kind: 'chat',
	label: 'chat',
	plural: 'chats',
	list: async () =>
		(await listSessions()).map((session) => ({
			id: session.id,
			label: session.title || session.id,
			meta: `${session.turns} turns`
		})),
	update: (id, body) => renameSession(id, stringOf(body.title)),
	remove: (id) => deleteSession(id)
};

/** The kind table — the single place object operations are decided. */
export const ENTITY_KINDS: Record<ObjectKind, EntityKindSpec> = {
	persona: personaKind,
	// An actor is a persona seen at the actor scope (D37b): same operations.
	actor: { ...personaKind, kind: 'actor', label: 'actor', plural: 'actors' },
	assistant: assistantKind,
	world: worldKind,
	book: bookKind,
	chat: chatKind
};
