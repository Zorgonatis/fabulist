"""Experience profiles (D33): portable, definitions-only experience bundles.

PLAN.md **D33** — *Experience profile envelope*: a bundle `{world + cast defs +
books + preset + assistants}` that drops an entire experience into any Fabulist
log. Export reads **only** the anchor sessions (``__worlds__`` / ``__personas__``
/ ``__books__`` / ``__presets__`` / ``__assistants__``), so it is structurally
incapable of touching a chat session. Import compares each entity against the
local fold by **canonical signature** and appends a supersede only when they
differ (last-import-wins, no merge).

Five modules, one concern each:

* :mod:`fabulist.profiles.schema` — envelope shape + canonical signatures;
* :mod:`fabulist.profiles.exporter` — anchor folds -> envelope;
* :mod:`fabulist.profiles.importer` — envelope -> signature-checked appends;
* :mod:`fabulist.profiles.image` — the envelope's assigned (top-layer) image;
* :mod:`fabulist.profiles.png` — the profile PNG carrier (image + JSON chunk,
  item G).

The API surface is :mod:`fabulist.api.profiles_api`.
"""

from .exporter import export_profile
from .importer import DEFAULT_CONNECTIONS_PATH, import_profile
from .schema import (
    FORMAT,
    FORMAT_VERSION,
    KNOWN_ENVELOPE_KEYS,
    canonical_json,
    entity_signature,
    is_profile,
    new_envelope,
    now_iso,
    unknown_envelope_keys,
)

__all__ = [
    "DEFAULT_CONNECTIONS_PATH",
    "FORMAT",
    "FORMAT_VERSION",
    "KNOWN_ENVELOPE_KEYS",
    "canonical_json",
    "entity_signature",
    "export_profile",
    "import_profile",
    "is_profile",
    "new_envelope",
    "now_iso",
    "unknown_envelope_keys",
]
