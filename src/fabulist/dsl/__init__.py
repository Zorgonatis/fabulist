"""Spec-driven openui DSL helpers (W2.4; hot-reloaded in W9.27).

The shared home for the DSL logic that used to be smeared across
``graph/nodes.py`` (segmentation), ``graph/validation.py`` (the gate) and
``prompts/compiler.py`` (``strip_dsl``): one authoritative component table, one
statement recognizer (positional **and** named-argument forms), and one strip
helper.

* :mod:`.store` — the hot-reloadable component-set store (the generated
  ``library.spec.json`` / ``library.doc.txt`` / ``set.json`` are data, re-read
  while the engine runs, cached per set DIRECTORY — content, never selection).
* :mod:`.spec` — the component spec table (name, prop order, arity), read
  through the store.
* :mod:`.statements` — recognition, arg splitting, kwargs→positional repair,
  dangling-child pruning.
* :mod:`.strip` — the DSL-strip helper for history projection.
* :mod:`.sets` — the component-SET registry: the renderer's union (every
  shipped set's components) vs the model's ACTIVE set, and the
  "set ⊆ union = loadable" machine-check (issue #15).

Pure, model-free, and independent of ``fabulist.graph`` / ``fabulist.prompts``
so any layer can import it.
"""

from __future__ import annotations

from . import sets, spec, statements, store, strip
from .sets import (
    ComponentSetEntry,
    SetLoadability,
    active_set_id,
    active_set_id_for_session,
    check_set_loadable,
    component_sets,
    default_set_id,
    loadable_set_ids,
    session_set_id,
    session_snapshot,
    swap_notice_text,
    union_component_names,
)
from .spec import (
    CORE_COMPONENT_NAMES,
    ComponentSchema,
    PropInfo,
    component_names,
    component_names_longest_first,
    component_schema,
    component_signatures,
    flavors_path,
    reset_spec_cache,
    spec_path,
    spec_table,
)
from .statements import (
    Statement,
    component_stmt_re,
    is_named_arg,
    is_statement,
    parse_statement,
    positionalize,
    prune_child_refs,
    split_args,
    split_statements,
    statement_like_re,
    statement_re,
)
from .store import (
    ARTIFACT_DIR_ENV_VAR,
    FLAVORS_ENV_VAR,
    SPEC_ENV_VAR,
    ComponentLibraryStore,
    ComponentSetError,
    ComponentSetSnapshot,
    ComponentSetSwap,
    library_store,
    resolve_artifact_dir,
    resolve_flavors_path,
    resolve_spec_path,
)
from .strip import OPEN_FENCE_RE, strip_dsl

__all__ = [
    "ARTIFACT_DIR_ENV_VAR",
    "CORE_COMPONENT_NAMES",
    "ComponentLibraryStore",
    "ComponentSchema",
    "ComponentSetEntry",
    "SetLoadability",
    "ComponentSetError",
    "ComponentSetSnapshot",
    "ComponentSetSwap",
    "FLAVORS_ENV_VAR",
    "OPEN_FENCE_RE",
    "PropInfo",
    "SPEC_ENV_VAR",
    "Statement",
    "active_set_id",
    "active_set_id_for_session",
    "check_set_loadable",
    "component_names",
    "component_names_longest_first",
    "component_schema",
    "component_sets",
    "component_signatures",
    "component_stmt_re",
    "default_set_id",
    "flavors_path",
    "is_named_arg",
    "is_statement",
    "library_store",
    "loadable_set_ids",
    "parse_statement",
    "positionalize",
    "prune_child_refs",
    "reset_spec_cache",
    "resolve_artifact_dir",
    "resolve_flavors_path",
    "resolve_spec_path",
    "session_set_id",
    "session_snapshot",
    "swap_notice_text",
    "sets",
    "spec",
    "spec_path",
    "spec_table",
    "split_args",
    "split_statements",
    "statement_like_re",
    "statement_re",
    "statements",
    "store",
    "strip",
    "strip_dsl",
    "union_component_names",
]
