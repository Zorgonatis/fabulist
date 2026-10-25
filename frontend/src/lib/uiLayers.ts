// Surface stack (D51 §2.4).
//
// Coexisting overlay surfaces (the Design Studio, drawers, floating panes) each
// register themselves while visible. ONE `window` keydown listener is installed
// on the first registration and removed on the last unregister, so Escape
// closes exactly the TOP surface instead of every surface at once — the
// behaviour every drawer used to re-implement, which used to pile up.
//
// Plain TS: no Svelte imports, no runes, usable from any component / module.
// Every `window` touch is guarded so this module is safe to import during SSR
// (register/unregister are only ever called from client-only effect bodies).

export interface LayerEntry {
	/** Stable per-surface id (e.g. `'studio'`). Re-registering the same id replaces it. */
	id: string;
	/** Escape handler for this surface; called only while it is topmost. */
	onClose: () => void;
}

// Insertion-ordered stack; the LAST entry is the topmost surface.
const stack: LayerEntry[] = [];
let listening = false;

function onKeydown(event: KeyboardEvent): void {
	if (event.key !== 'Escape' || event.defaultPrevented) return;
	const top = stack[stack.length - 1];
	if (!top) return;
	event.preventDefault();
	top.onClose();
}

function attach(): void {
	if (typeof window === 'undefined' || listening) return;
	window.addEventListener('keydown', onKeydown);
	listening = true;
}

function detachIfEmpty(): void {
	if (typeof window === 'undefined' || !listening || stack.length > 0) return;
	window.removeEventListener('keydown', onKeydown);
	listening = false;
}

/**
 * Push a surface onto the stack. Returns an idempotent unregister function.
 * An id already on the stack is replaced in place, so a re-register cannot
 * leave a stale entry behind.
 */
export function register(id: string, onClose: () => void): () => void {
	const existing = stack.findIndex((entry) => entry.id === id);
	if (existing >= 0) stack.splice(existing, 1);
	stack.push({ id, onClose });
	attach();

	let done = false;
	return () => {
		if (done) return;
		done = true;
		const index = stack.findIndex((entry) => entry.id === id);
		if (index >= 0) stack.splice(index, 1);
		detachIfEmpty();
	};
}

/** Registered surface ids, bottom-first — for debugging / smoke assertions. */
export function layers(): string[] {
	return stack.map((entry) => entry.id);
}
