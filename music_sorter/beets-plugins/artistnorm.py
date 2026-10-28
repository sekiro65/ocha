"""artistnorm.py — beets plugin that keeps collab credits from turning into
separate artists in Jellyfin/Navidrome.

What it does:
  * Template functions for paths:
      %normjoin{$albumartist} -> all credited artists joined with ", "
      %primary{$albumartist}  -> just the first/main credited artist
  * On import (both matched and as-is/singleton imports) it rewrites tags:
      albumartist / albumartists -> primary artist only, so a feature lands
                                    under the main artist instead of creating
                                    a new "A feat. B" artist
      artists (ARTISTS tag)      -> every credited artist as separate values,
                                    so each collaborator is still linked
      artist (ARTIST tag)        -> untouched display credit, e.g. "A feat. B"
    Jellyfin must have "Prefer non-standard artists tag" enabled on the music
    library to read the multi-value ARTISTS tag.
  * `beet normartists [query]` applies the same rewrite to existing items
    (follow with `beet move` to relocate files to the new paths).

Splitting on "&" / " and " / "with" is ambiguous ("Simon and Garfunkel").
Exact-match exceptions go in artist-exceptions.txt next to this file (or its
parent dir), one per line, case-insensitive; those are never split.

Install: put this file in a dir listed under `pluginpath:` and add
`artistnorm` to `plugins:`.
"""
import re
from pathlib import Path

from beets import ui
from beets.plugins import BeetsPlugin

# Separators that split a multi-artist credit string.
_SEP_RE = re.compile(
    r"\s*\((?:feat\.?|ft\.?|featuring|with)\s+"   # "A (feat. B)"
    r"|\s+(?:feat\.?|ft\.?|featuring|with)\s+"     # "A feat. B"
    r"|\s+(?:&|and|x)\s+"                          # "A & B", "A x B"
    r"|\s*/\s*|_\s+|;\s*|,\s*",
    re.IGNORECASE,
)

_SKIP_ALBUMARTISTS = {"various artists", "various", "va"}


def _load_exceptions():
    here = Path(__file__).parent
    for p in (here / "artist-exceptions.txt", here.parent / "artist-exceptions.txt"):
        try:
            with open(p) as f:
                return {
                    line.strip().lower()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                }
        except FileNotFoundError:
            continue
    return set()


class ArtistNormPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self._exceptions = _load_exceptions()
        self.template_funcs["normjoin"] = self._normjoin
        self.template_funcs["primary"] = self._primary
        # import_task_apply only fires for matched imports; import_task_choice
        # also covers as-is/singleton fallbacks. Normalizing is idempotent.
        self.register_listener("import_task_choice", self._on_import)
        self.register_listener("import_task_apply", self._on_import)

    def _split(self, text):
        text = (text or "").strip()
        if not text or text.lower() in self._exceptions:
            return [text] if text else []
        parts = [p.strip(" )") for p in _SEP_RE.split(text)]
        seen, out = set(), []
        for p in parts:
            if p and p.lower() not in seen:
                seen.add(p.lower())
                out.append(p)
        return out or [text]

    def _primary(self, text):
        if not text:
            return text
        return self._split(text)[0]

    def _normjoin(self, text):
        if not text:
            return text
        return ", ".join(self._split(text))

    def normalize(self, item):
        """Rewrite artist fields in place. Returns True if anything changed."""
        if item.get("comp") or (item.albumartist or "").strip().lower() in _SKIP_ALBUMARTISTS:
            return False
        changed = False
        credited = self._split(item.artist)
        if len(credited) > 1 and len(item.get("artists") or []) < len(credited):
            item.artists = credited
            changed = True
        primary = self._primary(item.albumartist or item.artist)
        if primary and item.albumartist != primary:
            item.albumartist = primary
            changed = True
        if primary and list(item.get("albumartists") or []) != [primary]:
            item.albumartists = [primary]
            changed = True
        return changed

    def _on_import(self, session, task):
        if getattr(task, "skip", False):
            return
        for item in task.imported_items():
            self.normalize(item)

    def commands(self):
        cmd = ui.Subcommand("normartists", help="split collab credits / set primary album artist")
        cmd.parser.add_option("-n", "--pretend", action="store_true", help="show changes only")

        def func(lib, opts, args):
            n = 0
            for item in lib.items(args):
                before = (item.albumartist, list(item.get("artists") or []))
                if not self.normalize(item):
                    continue
                n += 1
                ui.print_(f"{before[0]!r} -> {item.albumartist!r}  artists={item.artists}  | {item.title}")
                if opts.pretend:
                    continue
                item.store()
                item.try_write()
                album = item.get_album()
                if album and album.albumartist != item.albumartist:
                    album.albumartist = item.albumartist
                    album.albumartists = [item.albumartist]
                    album.store(inherit=False)
            ui.print_(f"{n} item(s) {'would change' if opts.pretend else 'changed'}")

        cmd.func = func
        return [cmd]
