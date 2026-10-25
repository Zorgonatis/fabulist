"""The appearance store: theme preset, one pasted-CSS override, one wallpaper.

A reader's furniture — which preset they read on, the one stylesheet they pasted,
and the image they put behind the prose — is **user state, not a session fact**.
It is deliberately NOT an event in the log (ADR-001 owns *narrative* state;
ADR-027 carves this out): a theme is not replayable history, it must not fork with
a branch, and rewriting it on every slider move would spray thousands of system
events into the transcript's audit trail. The appearance instead rides the
repo's existing user-state convention — a small JSON document under ``data/``
(the same gitignored, per-install directory the event log itself lives in),
written atomically, read on every request.

Document shape (::

    {
      "preset": "dark",              # one of PRESETS (light | dark | oled | savilen)
      "custom_css": "…",             # the GUARDED stylesheet the browser gets
      "wallpaper": "wallpaper.jpg",  # a file in data/appearance/, or null
      "fit": "cover",                # how the image meets the viewport
      "align": "center",             # reading-column alignment (issue #41)
      "version": 1
    }

Issue #41's ``align`` (``left`` / ``center`` / ``right``) rides the same
document for the same reason the wallpaper does: it is a reader's preference for
the whole install, not a session fact, and one document means "which preset" and
"where the column sits" can never disagree. ``center`` is the shipped default —
the measure is centred today (:issue:`22`), so an absent key renders exactly the
current look.

Issue #13 is the reason the wallpaper is a FIELD here and not a second store:
the image bytes live beside the document (``data/appearance/wallpaper.<ext>``,
:mod:`fabulist.theme.wallpaper`), and the fact that one is set lives in this
document. One reader, one location, one write path — so "clear the wallpaper"
and "change the preset" can never disagree about which install they belong to.

**Unknown keys are preserved verbatim** and carried through every write. That is
the extension point that made this field cheap, and a build that does not know a
key still rewrites the file without dropping it. :func:`AppearanceStore.patch`
only ever touches the keys it is handed; the wallpaper has its own two methods
(:meth:`AppearanceStore.save_wallpaper` / :meth:`AppearanceStore.remove_wallpaper`)
because its write also has to move bytes.

The precedence this store's data rides (owner ruling D-D, pinned by
``tests/test_appearance_api.py``)::

    base (app.css :root) -> preset (data-theme block) -> user CSS -> session tone

The tone step is inline custom properties on ``<html>``, so it outranks the user
CSS unless that CSS says ``!important``. This module never tries to win that
fight: it stores what the guard allowed and the frontend injects it last in
``<head>``, which is the whole contract.

Path resolution follows the repo's config/env convention: ``FABULIST_APPEARANCE_FILE``
wins when set (a container pinning the data volume), else ``data/appearance.json``
relative to the working directory — the same rule ``scripts/serve.py`` applies to
``FABULIST_ENV_FILE`` and to the log's ``data/fabulist.db``. A missing file is a
FIRST RUN, not an error: the defaults (``preset="dark"``, no user CSS) are the
shipped look, and the first ``PATCH`` creates the file and its directory.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fabulist.theme.custom_css import DroppedDeclaration, GuardedCss, guard
from fabulist.theme.wallpaper import (
    DEFAULT_FIT,
    FITS,
    WallpaperError,
    WallpaperInfo,
    describe_file,
    validate_upload,
)

__all__ = [
    "ALIGNMENTS",
    "ALIGN_LABELS",
    "APPEARANCE_FILE_ENV",
    "DEFAULT_ALIGN",
    "DEFAULT_APPEARANCE_PATH",
    "DEFAULT_FIT",
    "DEFAULT_PRESET",
    "FITS",
    "MAX_CUSTOM_CSS_CHARS",
    "PRESET_LABELS",
    "PRESET_META",
    "PRESETS",
    "preset_sort_key",
    "Appearance",
    "AppearanceError",
    "AppearanceStore",
    "appearance_path",
    "dropped_wire",
]

#: Env var pinning the document's path (a container mounting ``data/``).
APPEARANCE_FILE_ENV = "FABULIST_APPEARANCE_FILE"

#: Default document path, relative to the working directory (``data/`` is the
#: gitignored per-install user-state directory — the log lives there too).
DEFAULT_APPEARANCE_PATH = Path("data") / "appearance.json"

#: The shipped default: dark. `:root` in app.css IS dark, so an unknown or absent
#: ``data-theme`` renders exactly this. (Removing ``mid`` in issue #37 did not
#: move the default: dark is still the one the stylesheet's base block IS.)
DEFAULT_PRESET = "dark"

#: Where the reading column sits in the window (issue #41): the same measure,
#: moved to the start, the middle or the end. ``center`` is the shipped default —
#: it is what the issue #22 reading column already does — so an absent key
#: renders the current look and no migration is needed.
ALIGNMENTS: tuple[str, ...] = ("left", "center", "right")
DEFAULT_ALIGN = "center"

#: Human labels for the segmented control (the frontend renders these verbatim).
ALIGN_LABELS: dict[str, str] = {"left": "Left", "center": "Center", "right": "Right"}

#: One row per first-party preset (issue #37). ``greyscale`` + ``lightness`` is
#: the page's own lightness (the sort key's first half, white -> black);
#: ``hue`` is the primary accent hue for a coloured theme (the sort key's second
#: half, RGB order). Adding a preset is a row here plus its ``app.css`` block —
#: the presentation order is DERIVED below, never hand-maintained.
PRESET_META: dict[str, dict[str, Any]] = {
    "light": {
        "label": "Light",
        "scheme": "light",
        "greyscale": True,
        "lightness": 96.1,
        "hue": 0.0,
    },
    "dark": {
        "label": "Dark",
        "scheme": "dark",
        "greyscale": True,
        "lightness": 7.0,
        "hue": 0.0,
    },
    "oled": {
        "label": "OLED",
        "scheme": "dark",
        "greyscale": True,
        "lightness": 0.0,
        "hue": 0.0,
    },
    "savilen": {
        "label": "Savilen",
        "scheme": "dark",
        "greyscale": False,
        "lightness": 0.0,
        "hue": 175.2,
    },
}


def preset_sort_key(preset: str) -> tuple[int, float]:
    """The presentation sort key (issue #37 owner ruling).

    Greyscale themes come first, ordered white -> black by descending page
    lightness; coloured themes follow in RGB order by ascending primary hue
    (red 0° -> green 120° -> blue 240°). A known preset's key is total, so the
    catalog order is deterministic without any hand-maintained list.
    """
    meta = PRESET_META[preset]
    if meta["greyscale"]:
        return (0, -float(meta["lightness"]))
    return (1, float(meta["hue"]))


#: Preset ids in presentation order: light, dark, oled, savilen.
PRESETS: tuple[str, ...] = tuple(sorted(PRESET_META, key=preset_sort_key))

#: Human labels for the selector (the frontend renders these verbatim).
PRESET_LABELS: dict[str, str] = {
    preset: str(meta["label"]) for preset, meta in PRESET_META.items()
}

#: A paste box is for a stylesheet, not for a design system. 20 KB is roughly 400
#: hand-written rules — far past any real override, and small enough that the
#: document stays a document.
MAX_CUSTOM_CSS_CHARS = 20_000

#: Document format version (bumped only on an incompatible shape change).
DOCUMENT_VERSION = 1


class AppearanceError(ValueError):
    """An appearance write was rejected (unknown preset, oversized CSS, bad fit)."""


@dataclass(frozen=True)
class Appearance:
    """The current appearance: preset, guarded user stylesheet, and wallpaper.

    ``wallpaper`` is the FILE NAME inside :meth:`AppearanceStore.wallpaper_dir`
    (``"wallpaper.jpg"``) or ``None``; ``fit`` is how it meets the viewport
    (:data:`fabulist.theme.wallpaper.FITS` — ``cover`` is the only shipped mode).
    The dataclass carries names, not paths: resolving them is the store's job,
    and a hand-edited name that escapes the directory must not resolve.
    """

    preset: str = DEFAULT_PRESET
    custom_css: str = ""
    wallpaper: str | None = None
    fit: str = DEFAULT_FIT
    align: str = DEFAULT_ALIGN


def appearance_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the appearance document's path (see the module docstring).

    ``explicit`` (a caller/test override) wins, then ``FABULIST_APPEARANCE_FILE``,
    then :data:`DEFAULT_APPEARANCE_PATH` under the working directory.
    """
    if explicit is not None:
        return Path(explicit)
    pinned = os.environ.get(APPEARANCE_FILE_ENV)
    if pinned:
        return Path(pinned)
    return DEFAULT_APPEARANCE_PATH


class AppearanceStore:
    """Read/patch the appearance document (thread-safe, atomic writes).

    Duck-typed as a small object rather than a settings *service*: the only
    operations are :meth:`read` (a fold of one JSON document, defaults applied),
    :meth:`patch` (validate, write, return the new state plus the guard's
    report) and the two wallpaper writes that also move bytes. Unknown document
    keys are preserved so a build that does not know issue #13's ``wallpaper`` /
    ``fit`` fields still rewrites the file without dropping them.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = appearance_path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # -- reads ----------------------------------------------------------

    def _read_document(self) -> dict[str, Any]:
        """Parse the document, or return an empty one on a first-run checkout.

        A malformed document is NOT fatal: the appearance is furniture, and
        refusing to boot the UI over a stray comma in a theme file would be a
        worse failure than falling back to the shipped default. The unparseable
        text is left on disk (the next write replaces it) and the read reports
        defaults.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return document if isinstance(document, dict) else {}

    def read(self) -> Appearance:
        """The current appearance (defaults for anything absent or invalid)."""
        with self._lock:
            document = self._read_document()
        preset = document.get("preset")
        if not isinstance(preset, str) or preset not in PRESETS:
            preset = DEFAULT_PRESET
        custom_css = document.get("custom_css")
        if not isinstance(custom_css, str):
            custom_css = ""
        wallpaper = document.get("wallpaper")
        if not isinstance(wallpaper, str) or not wallpaper:
            wallpaper = None
        fit = document.get("fit")
        if not isinstance(fit, str) or fit not in FITS:
            fit = DEFAULT_FIT
        align = document.get("align")
        if not isinstance(align, str) or align not in ALIGNMENTS:
            align = DEFAULT_ALIGN
        return Appearance(
            preset=preset, custom_css=custom_css, wallpaper=wallpaper, fit=fit, align=align
        )

    # -- the wallpaper (issue #13) --------------------------------------

    @property
    def wallpaper_dir(self) -> Path:
        """Where the image bytes live: the document's stem, beside the document.

        ``data/appearance.json`` -> ``data/appearance/``. Derived rather than
        configurable, so a container that pins ``FABULIST_APPEARANCE_FILE`` to a
        volume gets the image on that volume for free — one setting, one place.
        """
        return self._path.with_suffix("")

    def wallpaper_file(self) -> Path | None:
        """The stored wallpaper's path, or ``None`` when there is no readable one.

        A name that is not a plain file inside :attr:`wallpaper_dir` never
        resolves (a hand-edited ``"../secrets"`` is a name, not a path), and a
        recorded name whose file has gone missing reads as "no wallpaper" — the
        document is furniture, and a dangling reference must not 500 the theme.
        """
        name = self.read().wallpaper
        if not name:
            return None
        directory = self.wallpaper_dir
        candidate = directory / name
        if candidate.parent != directory or not candidate.is_file():
            return None
        return candidate

    def wallpaper_info(self) -> WallpaperInfo | None:
        """The stored image's format/dimensions/size, or ``None`` when absent.

        Dimensions are re-read from the container header on every call instead of
        being cached in the document: the header is the fact, and a document that
        disagrees with its own image would be a second source of truth for one
        number.
        """
        path = self.wallpaper_file()
        if path is None:
            return None
        try:
            return describe_file(path)
        except (WallpaperError, OSError):
            return None

    def save_wallpaper(self, data: bytes) -> Appearance:
        """Validate and store the reader's image, replacing any previous one.

        Validation happens BEFORE anything on disk changes
        (:func:`fabulist.theme.wallpaper.validate_upload`): a refused upload
        leaves both the file and the document exactly as they were. The bytes are
        written first and the document second, so a crash between the two leaves
        the previous name pointing at the previous file — never a document that
        claims an image the disk does not have.

        Raises:
            WallpaperError: unsupported format, unmeasurable header, over 8 MB or
                over 4096px on an edge — with the reason the drawer shows.
        """
        info = validate_upload(data)
        name = f"wallpaper.{info.format.ext}"
        with self._lock:
            document = self._read_document()
            directory = self.wallpaper_dir
            directory.mkdir(parents=True, exist_ok=True)
            # One wallpaper at a time: a re-upload in another format must not
            # leave the previous file behind (nor a stale wallpaper.* the glob
            # below would have to guess about).
            for stale in directory.glob("wallpaper.*"):
                if stale.is_file():
                    stale.unlink(missing_ok=True)
            target = directory / name
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            document["wallpaper"] = name
            if document.get("fit") not in FITS:
                document["fit"] = DEFAULT_FIT
            document.setdefault("preset", DEFAULT_PRESET)
            document.setdefault("custom_css", "")
            document.setdefault("align", DEFAULT_ALIGN)
            document["version"] = DOCUMENT_VERSION
            self._write_document(document)
        return self.read()

    def remove_wallpaper(self) -> Appearance:
        """Delete the stored image and clear the field (the default is "none").

        The file is removed before the document is rewritten, so an interrupted
        removal leaves a document naming a file that is gone — which reads as "no
        wallpaper" (:meth:`wallpaper_file`) rather than as a broken install.
        Removing when nothing is set is a no-op that still lands the document in
        its default state.
        """
        with self._lock:
            document = self._read_document()
            directory = self.wallpaper_dir
            if directory.is_dir():
                for stale in directory.glob("wallpaper.*"):
                    if stale.is_file():
                        stale.unlink(missing_ok=True)
            document["wallpaper"] = None
            if document.get("fit") not in FITS:
                document["fit"] = DEFAULT_FIT
            document.setdefault("preset", DEFAULT_PRESET)
            document.setdefault("custom_css", "")
            document.setdefault("align", DEFAULT_ALIGN)
            document["version"] = DOCUMENT_VERSION
            self._write_document(document)
        return self.read()

    # -- writes ---------------------------------------------------------

    def patch(
        self,
        *,
        preset: str | None = None,
        custom_css: str | None = None,
        fit: str | None = None,
        align: str | None = None,
    ) -> tuple[Appearance, GuardedCss | None]:
        """Apply the named fields, write the document, return the new state.

        Only the keys passed (``preset`` / ``custom_css`` / ``fit`` / ``align``)
        are touched; everything else in the document — including keys this build
        does not know — is written back unchanged. ``custom_css`` is run through
        :func:`fabulist.theme.custom_css.guard` and the GUARDED text is what is
        stored, so the file always holds exactly what the browser will be handed;
        the returned :class:`GuardedCss` carries the dropped declarations so the
        drawer can say what was refused and why.

        ``fit`` is the wallpaper's one knob (issue #13): the drawer sends it when
        the reader picks a mode, and the value is validated against
        :data:`fabulist.theme.wallpaper.FITS` here rather than in the router, so a
        second caller cannot write a mode the stylesheet has no rule for.
        ``align`` is issue #41's reading-column position, validated against
        :data:`ALIGNMENTS` for the same reason.

        Raises:
            AppearanceError: an unknown preset id, CSS past the size cap, an
                unknown fit, or an unknown alignment. All are refused
                ATOMICALLY — nothing is written.
        """
        if preset is not None and preset not in PRESETS:
            known = ", ".join(PRESETS)
            raise AppearanceError(f"unknown preset {preset!r}; known presets: {known}")
        if custom_css is not None and len(custom_css) > MAX_CUSTOM_CSS_CHARS:
            raise AppearanceError(
                f"custom_css is {len(custom_css)} characters; the limit is "
                f"{MAX_CUSTOM_CSS_CHARS}"
            )
        if fit is not None and fit not in FITS:
            known = ", ".join(FITS)
            raise AppearanceError(f"unknown fit {fit!r}; known fits: {known}")
        if align is not None and align not in ALIGNMENTS:
            known = ", ".join(ALIGNMENTS)
            raise AppearanceError(
                f"unknown align {align!r}; known alignments: {known}"
            )

        with self._lock:
            document = self._read_document()
            if preset is not None:
                document["preset"] = preset
            if fit is not None:
                document["fit"] = fit
            if align is not None:
                document["align"] = align
            guarded: GuardedCss | None = None
            if custom_css is not None:
                guarded = guard(custom_css)
                document["custom_css"] = guarded.css
            document.setdefault("preset", DEFAULT_PRESET)
            document.setdefault("custom_css", "")
            document.setdefault("wallpaper", None)
            if document.get("fit") not in FITS:
                document["fit"] = DEFAULT_FIT
            if document.get("align") not in ALIGNMENTS:
                document["align"] = DEFAULT_ALIGN
            document["version"] = DOCUMENT_VERSION
            self._write_document(document)

        return self.read(), guarded

    def _write_document(self, document: dict[str, Any]) -> None:
        """Atomic write (sibling tmp + ``os.replace``), creating ``data/`` if needed."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)


def dropped_wire(dropped: list[DroppedDeclaration]) -> list[dict[str, str]]:
    """Serialise dropped declarations for the PATCH response / the drawer."""
    return [
        {"selector": item.selector, "declaration": item.declaration, "reason": item.reason}
        for item in dropped
    ]
