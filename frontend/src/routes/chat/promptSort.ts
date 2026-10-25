// Pure ordering helper for the prompt-module list (D18).
//
// The Prompt Manager's drag-drop and its up/down keyboard fallback both reduce
// to "move the item at `from` to index `to`". Keeping that as a pure function
// makes the reorder path trivially testable (there is no frontend test runner
// yet; this module is exported so one can be added later without touching the
// component).

/**
 * Return a new array with the element at `from` moved to index `to`.
 *
 * The input array is never mutated. Out-of-range indices (empty list, negative
 * index, index past the end) return a shallow copy unchanged, so a stray drag
 * event can never corrupt the module order. When `from === to` the order is
 * unchanged.
 */
export function reorder<T>(list: T[], from: number, to: number): T[] {
	const copy = [...list];
	if (copy.length === 0) return copy;
	if (from < 0 || from >= copy.length) return copy;
	if (to < 0 || to >= copy.length) return copy;
	if (from === to) return copy;
	const [item] = copy.splice(from, 1);
	copy.splice(to, 0, item);
	return copy;
}
