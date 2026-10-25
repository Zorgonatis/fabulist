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
	listAssistants,
	listBooks,
	listPersonas,
	listSessions,
	listWorlds,
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
	import: async (source) => (await createPersona(asPersonaBody(source))).persona_id
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
	import: async (source) => (await createAssistant(asAssistantWrite(source))).id
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
	import: async (source) => (await createWorld(asWorldBody(source))).world_id
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
	import: async (source) => (await createBook(asBookBody(source))).book_id
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
