#!/usr/bin/env python3
"""
music_import_watcher.py

Watches /downloads for new music (files or whole
album folders dropped by a download client) and hands each item to
`beet import` immediately so it gets MusicBrainz-tagged and moved into
/music using your beets config.

Behavior: anything already sitting in the watch dir at startup, and anything
that shows up while running, is imported once it stops changing --
SETTLE_STABLE_POLLS consecutive polls (default: 30s) with no growth in
total size or newest-file-mtime. This settle-check exists because download
clients here write directly into the watch dir progressively (not an
atomic move-in-when-finished), so grabbing an item immediately on first
sight risks importing a partial download -- e.g. an album folder that only
has 2 of 13 tracks so far, which would then get its early tracks split off
as isolated singles before the rest arrive. The watch dir is otherwise
assumed to only contain completed (or completing) downloads.

Import strategy for single dropped files (v2):
  A lone audio file dropped by a download client might still be a genuine
  track off a real album (MusicBrainz can often find the recording's real
  release even from a single file) or it might be a true one-off single.
  So for single files we now try a REAL (non-singleton) import first, which
  lets beets search for and assign true album metadata. Only if that leaves
  the file untouched (beets couldn't confidently place it in any album --
  quiet_fallback: skip means it's left in place, not force-filed) do we
  retry as a singleton (-s), which files it under Non-Album with just a
  track title. This is what actually fixes "everything ends up under
  Non-Album" -- forcing -s unconditionally (the old behavior) never even
  gave beets the chance to find a real album.

Why this avoids "Artist A, Artist B" duplicate-artist folders, and how
Navidrome/Jellyfin still show every credited artist correctly:
- beets paths are built from $albumartist (wrapped in the custom %normjoin{}
  path function -- see artistnorm.py -- which collapses the inconsistent
  "/", "_", "&", " and " join phrases MusicBrainz produces into one
  consistent ", " for folder-naming purposes only), never the raw combined
  artist string used verbatim, so multi-artist tracks land under one
  consistently-named folder per release/artist-combo instead of splintering
  across near-duplicate folder names.
- Recent beets/mediafile write ARTISTS/ALBUMARTISTS as true multi-valued
  ID3v2.4 tags (not a concatenated string), so Navidrome/Jellyfin can still
  split and cross-link every credited artist in their own library view,
  regardless of what the folder itself is named.
- This requires `id3v23: no` in your beets config (keep default ID3v2.4).
  install_music_watcher.sh prints a reminder of this and the other required
  config keys -- don't remove that check.
- Items beets can't confidently match against MusicBrainz are NOT left
  unresolved. Every item gets one real-match attempt first (so genuine
  album/recording matches still happen and get full metadata + lyrics), but
  if that fails, it's accepted using its EXISTING tags -- singles land under
  `<PrimaryArtist>/Non-Album/`, so e.g. many unrelated K.J. Yesudas singles
  all end up together under one 'K.J. Yesudas/Non-Album/' folder rather than
  each getting stuck waiting on a MusicBrainz match that regional/
  independent content often never gets. Nothing is left behind in the watch
  folder by design -- `watcher-needs-review.txt` is now only for genuine
  import ERRORS (corrupt files, permission issues), not low-confidence
  matches.

Only top-level entries (files or directories) directly inside WATCH_DIR are
tracked as units of work, matching how a download client drops one folder
or file per completed download.

Non-audio downloads inside WATCH_DIR are ignored entirely (left untouched)
so this can safely share a folder with other download types.
"""

import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

# ---- Config ---------------------------------------------------------------

WATCH_DIR = Path(os.environ.get("WATCH_DIR", "/downloads"))
LIBRARY_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))  # informational; beets config owns this
BEET = os.environ.get("BEET_BIN") or shutil.which("beet") or "beet"

# How often the watch loop re-lists WATCH_DIR for new items. Items are
# imported as soon as they're seen -- this is just the detection latency,
# not a settle delay.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", 5))

# How many consecutive polls an item's total size + newest-file-mtime must
# stay UNCHANGED before we treat it as a finished download and import it.
SETTLE_STABLE_POLLS = int(os.environ.get("SETTLE_STABLE_POLLS", 6))

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".alac", ".ogg", ".opus", ".wav", ".aac", ".wma"}

LOG_DIR = Path(os.environ.get("LOG_DIR", "/config/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
IMPORT_LOG = LOG_DIR / "watcher-import.log"          # beets' own -l skip/summary log
DAEMON_LOG = LOG_DIR / "watcher-daemon.log"           # this script's own log
NEEDS_REVIEW = LOG_DIR / "watcher-needs-review.txt"   # anything left behind after import

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(DAEMON_LOG), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("music-watcher")

# ---- State ------------------------------------------------------------

_lock = threading.Lock()
_processing: set[str] = set()
_dup_flag: dict[str, bool] = {}  # path -> beets reported it as already in the library
_beet_lock = threading.Lock()  # serialize actual `beet import` calls - beets'
                                # SQLite library.db isn't safe for concurrent writers


def has_audio(path: Path) -> bool:
    if path.is_file():
        return path.suffix.lower() in AUDIO_EXTS
    return any(p.suffix.lower() in AUDIO_EXTS for p in path.rglob("*") if p.is_file())


def _signature(path: Path):
    """(total size, newest mtime) across a file or, recursively, a whole
    directory. Used to detect whether an item is still being written to.
    Note: this only catches ACTIVELY growing/changing downloads -- a
    download that stalls and stops writing entirely (rather than finishing)
    will look "stable" after SETTLE_STABLE_POLLS and still get imported
    prematurely. There's no reliable way to distinguish "finished" from
    "permanently stuck" from the filesystem alone; occasional truncated
    files are still possible and worth an eye on watcher-needs-review.txt."""
    try:
        if path.is_file():
            st = path.stat()
            return (st.st_size, st.st_mtime)
        total_size = 0
        newest_mtime = 0.0
        for p in path.rglob("*"):
            if p.is_file():
                st = p.stat()
                total_size += st.st_size
                newest_mtime = max(newest_mtime, st.st_mtime)
        return (total_size, newest_mtime)
    except (FileNotFoundError, OSError):
        return None


def wait_until_settled(path: Path) -> bool:
    """Block (polling every POLL_INTERVAL, from within this item's own
    worker thread -- doesn't hold up the rest of the scan loop) until
    `path` stops growing/changing across SETTLE_STABLE_POLLS consecutive
    checks. Returns False if the path vanished before settling."""
    stable_count = 0
    last_sig = None
    while True:
        if not path.exists():
            return False
        sig = _signature(path)
        if sig is not None and sig == last_sig:
            stable_count += 1
        else:
            stable_count = 1
        last_sig = sig
        if stable_count >= SETTLE_STABLE_POLLS:
            return True
        time.sleep(POLL_INTERVAL)


def _beet_import_once(target: Path, singleton: bool, asis_fallback: bool = False) -> "subprocess.CompletedProcess":
    # asis_fallback=True is used ONLY on the final singleton attempt (see
    # run_beet_import below) -- after a real album-context match already
    # failed AND a singleton MusicBrainz recording match also failed. At
    # that point the blast radius is exactly one already-reasonably-tagged
    # track (not a whole misgrouped album), so it's safe to keep the
    # existing tags as-is rather than leave the file stuck in the watch
    # folder forever. This is the same scoped, deliberate use of "asis" we
    # did by hand for content like DELKOCHI/regional film songs that
    # MusicBrainz simply doesn't index -- just automated here so the backlog
    # doesn't grow unbounded. The whole-album attempt below never uses this.
    # ionice -c3 (idle class) + nice -n19: this runs constantly in the
    # background, so it should yield disk/CPU priority to anything else on
    # the box (Jellyfin, SSH, actual playback) rather than compete with it
    # on a spinning disk.
    cmd = ["ionice", "-c3", "nice", "-n19", BEET, "import", "-q", "-m", "-l", str(IMPORT_LOG)]
    if asis_fallback:
        cmd += ["--quiet-fallback", "asis"]
    if singleton:
        cmd.append("-s")
    cmd.append(str(target))
    with _beet_lock:
        log.info("Running: %s", " ".join(cmd))
        result = subprocess_run(cmd)
    if result.stdout.strip():
        log.info("beet stdout:\n%s", result.stdout.strip())
    if result.stderr.strip():
        log.warning("beet stderr:\n%s", result.stderr.strip())
    if "already in the library" in result.stderr:
        _dup_flag[str(target)] = True
    return result


def run_beet_import(target: Path, is_single_file: bool) -> bool:
    """Returns True if the item ended up imported (moved out of target).
    For single files: try a real album-context match first (so genuine
    album tracks don't get needlessly filed as Non-Album singles). If that
    fails, file it as a singleton using its EXISTING tags as-is -- no
    skipping. This guarantees every single file leaves the watch folder and
    lands under Non-Album/ inside its primary artist's folder (e.g. many
    unrelated K.J. Yesudas singles all end up together under
    'K.J. Yesudas/Non-Album/'), rather than sitting in the queue forever
    waiting for a MusicBrainz match that regional/independent content often
    never gets."""
    if is_single_file:
        result = _beet_import_once(target, singleton=False)
        if not target.exists():
            log.info("Matched with real album context: %s", target)
            return True
        if result.returncode != 0:
            log.warning("Non-singleton import errored (not just skipped) for %s", target)
            return False

        log.info(
            "No confident album match for %s, filing as singleton "
            "(Non-Album) using existing tags",
            target,
        )
        result = _beet_import_once(target, singleton=True, asis_fallback=True)
        return result.returncode == 0
    else:
        result = _beet_import_once(target, singleton=False)
        leftover = [p for p in target.rglob("*") if p.is_file()] if target.exists() else []
        if not leftover:
            log.info("Matched as album: %s", target)
            return True
        if result.returncode != 0:
            log.warning("Album import errored (not just skipped) for %s", target)
            return False

        log.info(
            "No confident album match for %s, accepting existing tags as-is "
            "so it doesn't stay stuck in the watch folder",
            target,
        )
        result = _beet_import_once(target, singleton=False, asis_fallback=True)
        return result.returncode == 0


def subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True)


def cleanup_leftovers(path: Path):
    """After a move-import, remove now-empty dirs; flag anything left behind
    (this is also what catches items that beets skipped via quiet_fallback:
    skip -- they'll still be sitting here, so they get flagged for review
    instead of silently vanishing)."""
    if path.is_file():
        if path.exists():
            # Check if this file is already in the beets library (duplicate download)
            try:
                dup = _dup_flag.pop(str(path), False)
                chk = subprocess_run([BEET, "ls", f"path:{path.name}"])
                if not chk.stdout.strip():
                    parts = path.stem.split(" - ")
                    title = (parts[-1] if len(parts) > 1 else path.stem).strip().lstrip(":")  # ':' = regex query
                    if title:  # an empty title would match the whole library
                        chk = subprocess_run([BEET, "ls", f"title:{title}"])
                if dup or chk.stdout.strip():
                    dup_dir = WATCH_DIR / ".duplicates"
                    dup_dir.mkdir(exist_ok=True)
                    dup_dest = dup_dir / path.name
                    if dup_dest.exists():
                        dup_dest.unlink()
                    path.rename(dup_dest)
                    log.info("Moved duplicate to %s (already in library: %s)", dup_dest,
                             (chk.stdout.strip().splitlines() or ["reported by beets import"])[0])
                    return
            except Exception as e:
                log.warning("Duplicate check failed for %s: %s", path, e)

            with open(NEEDS_REVIEW, "a") as f:
                f.write(f"{path}\n")
            log.warning("File left behind after import, needs review: %s", path)
        return

    leftover_files = [p for p in path.rglob("*") if p.is_file()]
    if leftover_files:
        with open(NEEDS_REVIEW, "a") as f:
            for p in leftover_files:
                f.write(f"{p}\n")
        log.warning("%d file(s) left in %s after import, needs review", len(leftover_files), path)
    else:
        # Remove now-empty directory tree
        for d in sorted(path.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        try:
            path.rmdir()
        except OSError:
            pass


def process_item(path: Path):
    key = str(path)
    with _lock:
        if key in _processing:
            return
        _processing.add(key)
    try:
        if not path.exists():
            return
        log.info("New item detected, waiting for it to settle (finish downloading): %s", path)
        if not wait_until_settled(path):
            log.info("Item disappeared before settling, skipping: %s", path)
            return
        log.info("Item settled, importing: %s", path)
        if not has_audio(path):
            log.info("No audio files found, ignoring: %s", path)
            return

        is_single_file = path.is_file()
        log.info("Importing %s (%s)", path, "single file" if is_single_file else "album/dir")
        ok = run_beet_import(path, is_single_file)
        if not ok:
            log.error("beet import failed for %s, leaving in place for manual review", path)
            with open(NEEDS_REVIEW, "a") as f:
                f.write(f"{path}  # beet import returned non-zero\n")
            return
        cleanup_leftovers(path)
        log.info("Done: %s", path)
    except Exception:
        log.exception("Unexpected error processing %s", path)
    finally:
        with _lock:
            _processing.discard(key)


def startup_sweep():
    """On daemon start (including restarts), sort anything already sitting
    in WATCH_DIR right away. This is what makes a service restart double as
    a manual 'go sort the backlog now' trigger."""
    try:
        entries = [e for e in WATCH_DIR.iterdir() if not e.name.startswith(".")]
    except FileNotFoundError:
        log.error("Watch dir missing at startup: %s", WATCH_DIR)
        return
    if not entries:
        log.info("Startup sweep: nothing in %s", WATCH_DIR)
        return
    log.info("Startup sweep: found %d existing item(s) in %s", len(entries), WATCH_DIR)
    threads = [threading.Thread(target=process_item, args=(e,), daemon=True) for e in entries]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log.info("Startup sweep complete")


def scan_loop():
    """Polling loop: list top-level entries in WATCH_DIR, spawn a worker
    thread for any not already seen/being processed. Polling (rather than
    inotify) keeps this robust across network/mounted filesystems where
    inotify events can be unreliable. Every new item is imported right away
    -- POLL_INTERVAL is detection latency only, not a settle delay."""
    seen_at: dict[str, float] = {}  # path -> mtime when last queued
    while True:
        try:
            entries = [p for p in WATCH_DIR.iterdir()]
        except FileNotFoundError:
            log.error("Watch dir missing: %s (retrying)", WATCH_DIR)
            time.sleep(POLL_INTERVAL)
            continue

        for entry in entries:
            if entry.name.startswith("."):
                continue  # .partial downloads still in flight, .duplicates
            key = str(entry)
            with _lock:
                already = key in _processing
            if already:
                continue
            try:
                mtime = entry.stat().st_mtime
            except FileNotFoundError:
                continue
            if seen_at.get(key) != mtime:
                seen_at[key] = mtime
                threading.Thread(target=process_item, args=(entry,), daemon=True).start()

        # forget entries that are gone so they can be re-queued if reused
        for key in list(seen_at):
            if not Path(key).exists():
                del seen_at[key]

        time.sleep(POLL_INTERVAL)


def main():
    if not WATCH_DIR.exists():
        log.error("Watch dir does not exist: %s", WATCH_DIR)
        sys.exit(1)
    if not Path(BEET).exists():
        log.error("beet binary not found at %s - run install_music_watcher.sh first", BEET)
        sys.exit(1)
    log.info("Starting music import watcher on %s", WATCH_DIR)

    once = "--once" in sys.argv
    startup_sweep()
    if once:
        log.info("--once flag given, exiting after startup sweep")
        return
    scan_loop()


if __name__ == "__main__":
    main()