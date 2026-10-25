// Pinned-surface component registry (D38) — the chat header HUD and the
// Assistant panel render ENGINE-EMITTED statements (never the narrator's)
// through this registry, deliberately SEPARATE from `library.ts`:
//
//   1. Nothing here enters the narrator doc, the generated spec, or the
//      transcript renderer — the narrator must never learn these components
//      exist (D38b). tests/test_library_spec.py asserts the narrator registry
//      file carries exactly the 14 narrator components, which is why this
//      registry lives in its own module.
//   2. Statements arrive as fixed-ident OpenUI calls (`asst = AssistantCard(...)`)
//      assembled or gated engine-side; the frontend only parses + renders.
//
// EnvTracker (env HUD) intentionally does NOT register here: ChatHud renders it
// from parsed props, not through the Renderer.

import { createLibrary, defineComponent } from '@openuidev/svelte-lang';

import AssistantCard from './components/AssistantCard.svelte';
import { AssistantCardSchema } from './schemas/assistant';

export const AssistantCardDef = defineComponent({
	name: 'AssistantCard',
	description:
		'Out-of-fiction assistant commentary card: the persona speaks to the user and offers clickable suggested replies.',
	props: AssistantCardSchema,
	component: AssistantCard
});

export const panelLibrary = createLibrary({
	components: [AssistantCardDef],
	root: 'AssistantCard'
});
