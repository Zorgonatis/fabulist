// Fabulist HTTP/SSE API client.
//
// Backend contract (fastapi app in `src/fabulist/api/app.py`):
//
//   POST {API_BASE}/api/sessions                         -> {"session_id", "title"}
//   GET  {API_BASE}/api/sessions                         -> {"sessions": [{session_id, title, updated_at, turns}]}
//   PATCH {API_BASE}/api/sessions/{id}  {"title": str}   -> {"session_id", "title"} (append-only revision)
//   DELETE {API_BASE}/api/sessions/{id}                  -> {"session_id", "hidden": true} (soft hide)
//   GET  {API_BASE}/api/sessions/{id}/events             -> projection (JSON)
//   POST {API_BASE}/api/sessions/{id}/turns              -> text/event-stream
//        body {"content": string, "kind": "in_character" | "ooc"}
//   GET  {API_BASE}/api/sessions/{id}/stream?lastEventId=N -> text/event-stream (resume)
//   POST {API_BASE}/api/sessions/{id}/stop               -> 202 (cooperative stop)
//   GET  {API_BASE}/api/component-sets?session_id={id}    -> {active_set_id, default_set_id, sets}
//   POST {API_BASE}/api/sessions/{id}/component-set {set_id} -> per-session selection
//        (a `set_swapped` log fact; applies on the NEXT turn; 422 + unrenderable names)
//
// Branching (W2.2, design notes kept privately). A swipe is a branch session
// forked at the user message before the reply:
//
//   GET  {API_BASE}/api/sessions/{id}/branches  -> sibling context for {id}
//        {session_id, parent: {session_id, base_seq, snippet}|null,
//         siblings: [{session_id, base_seq, created_at, snippet, is_self}],
//         children: [...]}. `siblings` is parent-first ordered (the parent's own
//         reply is position 1) and includes self. A children-only payload (the
//         landed shape) is accepted as a fallback.
//   POST {API_BASE}/api/sessions/{id}/fork     -> {session_id, base_session, base_seq}
//        body {"at_seq": user_seq}: creates the O(1) fork row.
//   POST {API_BASE}/api/sessions/{newId}/turns -> text/event-stream
//        body {"content", "kind", "regenerate": true}: streams the fresh
//        branch reply; `regenerate` skips the user-event append (F2).
//
// `forkTurn` composes the last two. A missing branches endpoint (legacy
// sessions) or mock mode yields `null` and renders exactly as today.
//
// SSE named events carry a JSON `StreamEvent` and an `id:` line used for resume:
//
//   id: 12  event: reasoning  data: {"type":"reasoning","delta":"..."}
//   id: 13  event: content    data: {"type":"content","delta":"..."}
//   id: 14  event: done       data: {"type":"done","meta":{...}}
//   id: 15  event: error      data: {"type":"error","meta":{"reason":"interrupted"}}
//
// The stream is read with `fetch` + `ReadableStream` (no EventSource: we need a
// POST body and an AbortSignal). If a stream ends before `done` (a drop), the
// client resumes once via `GET .../stream?lastEventId=N`; the backend replays the
// missed suffix from its replay buffer and continues the live tail.
//
// When `VITE_MOCK=1`, every function delegates to `#lib/mock/mockApi`, a
// scripted in-browser fake that honours the same signatures and stop semantics
// so `/chat` is fully demonstrable without any backend.


export type { BranchInfo, BranchSibling, BranchSiblingKind, ChainRow, CompactionJob, CompactionJobStatus, CompactionMarker, CompactionMarkerPayload, CompactionStats, ContextLowPayload, ContextLowRow, FeatureDefinition, ForkOutcome, Projection, ProjectionItem, RevisionFrame, SessionSummary, SettingsCatalog, StageFrame, StreamEvent, StreamEventType, StreamOutcome, StreamTurnHandlers, StreamTurnOptions, ToggleDefinition, TurnKind, ValidationNotice, ValidationNoticeFlag } from './api/types';
export { API_BASE, ENDPOINTS, MOCK_SEED_SESSIONS, StreamError, USE_MOCK, mockSessions } from './api/shared';
export { createSseParser, normalizeStreamEvent, parseSseStream, streamTurn } from './api/streaming';
export type { SseEvent } from './api/streaming';
export { createSession, deleteSession, forkTurn, getCompactionJob, getProjection, listBranches, listSessions, pollCompactionJob, renameSession, startCompaction, stopTurn, COMPACTION_POLL_MS, COMPACTION_POLL_TIMEOUT_MS } from './api/sessions';
export { PERSONA_ENDPOINTS, assignChatPersona, createPersona, createSessionWithPersona, deletePersona, getChatConfig, getPersona, listPersonas, setChatMemory, updatePersona } from './api/personas';
export type { ChatConfig, Persona, PersonaWrite, SessionCreated } from './api/personas';
export { getSettings, patchDefaultPersona, patchFeatures, patchSettings } from './api/settings';
export { CARD_IMPORT_ENDPOINT, importCard } from './api/cards';
export type { CardImportResult, ImportedCard } from './api/cards';
export { WORLD_BOOK_ENDPOINTS, attachSessionBooks, createBook, createWorld, deleteBook, deleteWorld, getBook, getSessionWorld, getWorld, importWorldCard, listBooks, listWorlds, normalizeBook, normalizeBookEntry, normalizeWorld, putBook, startWorldChat, updateWorld } from './api/worlds';
export type { Book, BookEntry, SessionBooksResult, SessionWorld, World, WorldCardImportResult, WorldChatCreated } from './api/worlds';
export { PROMPT_ENDPOINTS, PROMPT_FLOWS, activatePreset, commitDefaultPreset, createPreset, deactivatePreset, getActivePreset, getDefaultPreset, getDefaultPromptStatus, listPresets, listPromptVariables, normalizePromptPreset, normalizePromptSurfaces, previewPrompt, resetDefaultPreset, seedFromDefault, updatePreset } from './api/prompts';
export { COMPONENT_SET_ENDPOINTS, listComponentSets, selectComponentSet } from './api/componentSets';
export type { ComponentSetOption, ComponentSetSelection, ComponentSetsCatalog } from './api/componentSets';
export type { DefaultCommitResult, DefaultPromptStatus, PromptFlow, PromptHistorySurfaces, PromptModule, PromptModuleType, PromptPreset, PromptPreview, PromptPreviewMessage, PromptRecalledSurfaces, PromptRegexRow, PromptRole, PromptSurfaces, PromptVariableSpec } from './api/prompts';
export { ASSISTANT_ENDPOINTS, consultAssistant, createAssistant, deleteAssistant, getAssistantPanel, getSessionAssistant, listAssistantReplies, listAssistants, setSessionAssistant, updateAssistant } from './api/assistants';
export type { AssistantCatalog, AssistantDefinition, AssistantReply, AssistantReplyRecord, AssistantWrite } from './api/assistants';
export { PROFILE_ENDPOINTS, envelopeAssistantRoster, exportProfile, importProfile, importProfilePng } from './api/profiles';
export type { ImportResult, ImportRow, ProfileEnvelope, ProfileExportParams, ProfileExportResult, ProfileWarning, ProfileWorldBundle } from './api/profiles';
export { importWorldInfo, listAttachments, uploadAttachment } from './api/attachments';
export type { AttachmentItem, AttachmentResult } from './api/attachments';
export { exportCard, postRevision, searchSession } from './api/revisions';
export type { SearchHit } from './api/revisions';
export { MEMORY_CONFIG_DEFAULTS, MEMORY_CONFIG_ENDPOINTS, getMemoryConfig, getRecap, getSpeakerColors, listRevisions, patchMemoryConfig } from './api/hud';
export type { MemoryConfig, MemoryConfigPatch, RecapEntry, RevisionRow, SpeakerColor } from './api/hud';
export { checkReconcileNow, foldBtsWorldState, getAgentCalls, getAttributions, getEntityRegistry, latestWorldState, parseAttributionText, parseBtsNotation, parseBtsThreads, parseRelationshipMetrics, writeEntityOp, writeNestedState } from './api/bts';
export type { AgentCallRow, AttributedQuote, AttributionRow, BtsMetric, BtsOffScreen, BtsState, BtsThread, BtsVenue, BtsWorldState, EntityOpResult, EntityRegistry, ReconcileResult, RegistryAttribute, RegistryItem, RegistryLegacy, RegistryLocation, RegistryRelationship, RegistryThread, WorldStateEventRow } from './api/bts';
export { SESSION_OVERRIDE_ENDPOINTS, getOpening, getSessionModules, normalizeSessionModule, putSessionModule, selectGreeting } from './api/opening';
export type { OpeningInfo, SessionModule } from './api/opening';
export { CONNECTION_ENDPOINTS, createConnection, deleteConnection, discoverModels, listConnections, normalizeConnectionEntry, setAgentConnection, updateConnection } from './api/connections';
export type { ConnectionEntry, ConnectionWrite, ConnectionsCatalog, DiscoverModelsOptions, ModelDiscovery } from './api/connections';
export { MEMORY_ENDPOINTS, getMemoryCollections, getReembedStatus, resetMemory, startReembed } from './api/memory';
export type { MemoryResetResult, ReembedStart, ReembedState, ReembedStatus } from './api/memory';
export { FLOW_ENDPOINTS, MOCK_FLOW_MANIFEST, getFlowManifest, normalizeFlowManifest } from './api/flow';
export type { ActivationMap, ActivationState, FlowEdge, FlowGraph, FlowManifest, FlowNode, FlowRegion, FlowSubGraph } from './flow/types';
export type { ActivationStep } from './flow/activation';
