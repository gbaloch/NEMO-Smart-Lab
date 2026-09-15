"""
On-demand, TTL-gated caching of tool data from a RemoteSyncEndpoint (e.g. Oak) - readers.py calls
into this instead of assuming a SmartLabTool.local_root is a fully pre-populated mirror kept fresh
by a separately-scheduled `sync_remote_data` run. Oak (or any configured endpoint) stays the source
of truth; local_root becomes a cache of exactly what's actually been looked at, refreshed
transparently as needed - no manual re-sync required.

Both entry points below take the SmartLabTool row itself (not just the readers.py cfg dict), since
they need sync_endpoint/remote_subdir, neither of which reaches readers.py otherwise. See
SmartLabTool.as_source_config()'s "remote_tool" key for how readers.py decides whether to use this
module at all - only when a sync_endpoint is configured; a tool reading from a live-mounted share
never touches this file, and behaves exactly as it always has.

Freshness is tracked with Django's cache framework (settings.CACHES) rather than anything
file-based - a cache hit means "assume the local copy is fine without asking the remote host
again"; a miss triggers exactly one `rsync`/`rsync --list-only` call (via NEMO_smart_lab.remote_sync)
before falling through to the same local-file reads readers.py has always done.

Reads/writes a cache aliased "smart_lab" if the host project's settings.CACHES defines one,
falling back to the project's plain default cache otherwise - so a deployment that wants this
freshness tracking to survive a process restart (a multi-worker deployment sharing state across
workers, or just not wanting a routine deploy/reload to force a fresh round of re-verification
against every already-fetched file - confirmed live: on a single-process dev server, an in-memory
cache wiped by an ordinary autoreload once turned "re-verify a few hundred already-local,
already-finished files" into several minutes, before ensure_cached()/ensure_cached_many() also
started trusting local existence directly - see their own docstrings) can opt in with a couple of
lines in settings.py, e.g.:

    CACHES = {
        "default": {...},
        "smart_lab": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": "/path/to/some/writable/dir",
        },
    }

No "smart_lab" alias configured (the common case - nothing to set up) means this behaves exactly
as before: LocMemCache, per-process, fine for a single `runserver`/single-worker deployment either
way now that local-existence is trusted outright regardless of this cache's own state.
"""

import hashlib
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from django.core.cache import InvalidCacheBackendError, caches
from django.utils import timezone

try:
    cache = caches["smart_lab"]
except InvalidCacheBackendError:
    cache = caches["default"]

from NEMO_smart_lab import remote_sync

logger = logging.getLogger(__name__)


def _cache_key(prefix, *parts):
    # Remote paths routinely contain spaces (e.g. "Logfile/Heater Data") and other characters a
    # cache backend may not accept verbatim (memcached rejects whitespace/control characters in
    # keys outright) - hash the variable part instead of concatenating it raw, so this cache key
    # stays valid regardless of which CACHES backend a deployment configures.
    digest = hashlib.sha1("/".join(str(p) for p in parts).encode()).hexdigest()[:24]
    return f"smart_lab:{prefix}:{digest}"

# Deliberately long: re-listing/re-fetching every page load (the original 60s default) made
# routine browsing pay a live SSH round trip per tool/page far more often than data actually
# changes, which is what made pages feel slow. 8 hours trades freshness for speed - a tool whose
# *current* run is still in progress can show up to 8h-stale status/chart data until this window
# rolls over (or until its own cache entry is evicted/expires and a fresh view re-triggers a
# fetch) rather than always reflecting what Oak has right now. Tune down (per-call `ttl=`, or
# these module constants) if that trade isn't right for a given deployment - e.g. a tool whose
# live "is it on right now" status matters more than page speed.
LISTING_TTL = 60 * 60 * 8  # how long a directory listing (which runs/files exist) is trusted
CONTENT_TTL = 60 * 60 * 8  # how long a specific already-fetched file/run is trusted without re-checking
STREAM_TTL = 15  # not currently used - see readers.py's stream telemetry note (still eager-only) -
# kept short since that data is explicitly meant to reflect "right now"

# Recipes (NEMO_smart_lab.recipes) get their own, shorter TTLs than run/log data above: a recipe a
# user just edited on the tool PC should show up on the next page load reasonably soon, and both
# the tree listing and any one recipe file are small/cheap to re-fetch either way.
RECIPE_TREE_TTL = 60 * 30
RECIPE_CONTENT_TTL = 60 * 30

# How long a *failed* fetch is remembered before being retried again. Without this, a path that's
# gone from the remote host for good (renamed, deleted, a stale directory-listing entry left over
# from before a rename) pays a full failed rsync round trip on every single page load that
# references it, forever - measured live against Oak: a handful of permanently-missing "Pressure
# Data" files alone turned fiji1's base-pressure chart into a 70+ second request, every time, since
# nothing ever remembered they'd already failed. Short relative to the success TTLs above (a
# transient network/Oak hiccup should recover reasonably soon) but long enough that routine
# browsing doesn't re-pay the same known-broken path over and over.
FAILURE_TTL = 60 * 5

_LISTING_LINE_RE = re.compile(r"^(\S+)\s+([\d,]+)\s+(\d{4}/\d{2}/\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(.+)$")


def _parse_listing(raw):
    entries = []
    for line in raw.splitlines():
        m = _LISTING_LINE_RE.match(line.strip())
        if not m:
            continue
        perms, size, date, time_, name = m.groups()
        if name == ".":
            continue
        mtime = datetime.strptime(f"{date} {time_}", "%Y/%m/%d %H:%M:%S")
        entries.append((name, mtime, int(size.replace(",", "")), perms.startswith("d")))
    return entries


def list_remote_dir(endpoint, remote_relpath, ttl=LISTING_TTL):
    """Returns [(name, mtime, size, is_dir), ...] for <endpoint.base_path>/<remote_relpath>/,
    freshly listed via `rsync --list-only` at most once per `ttl` seconds (cached per
    endpoint+path) - repeat calls within that window return the same cached list without asking
    the remote host again. Raises remote_sync.RemoteSyncError if the listing itself fails (no
    local fallback here - a listing failure has no "existing copy" concept to fall back to; the
    caller decides what to do, e.g. readers.py callers already raising ToolDataError on
    RemoteSyncError)."""
    cache_key = _cache_key("list", endpoint.pk, remote_relpath)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    entries = _parse_listing(remote_sync.list_remote(endpoint, remote_relpath))
    cache.set(cache_key, entries, ttl)
    return entries


def list_remote_tree(endpoint, remote_relpath, ttl=RECIPE_TREE_TTL):
    """Like list_remote_dir(), but returns the *entire* tree under
    <endpoint.base_path>/<remote_relpath>/ in one round trip (remote_sync.list_remote_recursive) -
    names in the returned entries are paths relative to remote_relpath (may contain "/" for nested
    folders), not bare filenames. Used by NEMO_smart_lab.recipes, where recipe folders can nest
    several levels deep (per-user folders, sub-folders within those) and walking that one directory
    at a time would multiply round trips for no benefit - recursion is a single rsync flag either
    way, not a separate remote command, so this costs the same one round trip as a shallow listing.
    """
    cache_key = _cache_key("tree", endpoint.pk, remote_relpath)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    entries = _parse_listing(remote_sync.list_remote_recursive(endpoint, remote_relpath))
    cache.set(cache_key, entries, ttl)
    return entries


def _record_sync_result(tool, ok, message):
    tool.last_synced = timezone.now()
    tool.last_sync_ok = ok
    tool.last_sync_message = message[:500]
    tool.save(update_fields=["last_synced", "last_sync_ok", "last_sync_message"])


def _fetch(tool, remote_relpath, local_path, is_dir):
    """Raw transfer, no cache bookkeeping or DB writes - shared by ensure_cached (single path,
    records its own outcome immediately) and ensure_cached_many (concurrent batch, which records
    ONE outcome for the whole batch afterward instead of one per file, to avoid many threads
    writing to the same SmartLabTool row at once)."""
    remote_relpath_full = f"{tool.remote_subdir_or_default}/{remote_relpath}"
    if is_dir:
        return remote_sync.sync_tool_from_remote(local_path, tool.sync_endpoint, remote_relpath_full, timeout=30)
    return remote_sync.sync_file_from_remote(local_path, tool.sync_endpoint, remote_relpath_full)


def ensure_cached(tool, remote_relpath, is_dir=False, ttl=CONTENT_TTL):
    """Makes sure tool.local_root/<remote_relpath> exists and is reasonably fresh, fetching it from
    tool.sync_endpoint if needed - at most once per `ttl` seconds per path; repeat calls within
    that window just return the local path without asking the remote host again.

    That per-path freshness memory lives only in this process's in-memory cache (see the module
    docstring), which is wiped by every process restart (a dev-server autoreload, a prod deploy).
    Measured live against Oak: a cold cache re-verifying several hundred already-fully-local,
    already-finished historical run files - each individually a real ~1.5s SSH/rsync round trip,
    only 8 running at once - turned one single page load into minutes, even though every one of
    those files was already sitting on disk, byte-for-byte correct, needing no transfer at all. So
    a local copy already existing is *also* treated as sufficient on its own, skipping the network
    entirely regardless of the in-memory cache's state - not just as the failure-fallback it always
    was below. This is deliberately aggressive: once a path has ever been fetched once (in this
    process or a previous one), it is trusted for as long as its local copy exists, not just for
    `ttl` seconds - correct for the run/log data this is overwhelmingly used for (a finished run's
    file never changes again; NEW runs are what LISTING_TTL's separate directory-listing cache
    controls, not this), but means a file that's still being actively appended to by the instrument
    right now (the very latest, currently in-progress run) won't pick up newer bytes without either
    this process restarting or its local copy being removed first.

    Also remembers a *failed* fetch for FAILURE_TTL (see that constant) so a path that's genuinely
    gone from the remote host doesn't pay a fresh failed round trip on every single call - a stale
    local copy (if any) is served straight from that negative-cache hit, and a path with no local
    copy at all re-raises the same remembered error without re-contacting the remote host.

    Falls back to serving an existing local copy if the fetch itself fails (logs a warning, does
    NOT update last_sync_ok/last_sync_message - a graceful stale-serve isn't "the last sync
    failed", it's "we didn't need to try"). Only raises remote_sync.RemoteSyncError - and records
    the failure on the SmartLabTool row - when there's no local copy to fall back to at all.

    Returns the local path (str), same shape callers already work with when local_root was a full
    eager mirror.
    """
    # remote_relpath always uses "/" (it's a remote/URL-style path built by callers, recipes.py,
    # etc.) - split before joining so the local path gets this platform's real separator instead of
    # a literal embedded "/" on Windows.
    local_path = os.path.join(tool.local_root, *remote_relpath.split("/"))
    local_root = os.path.abspath(tool.local_root)
    if os.path.commonpath([local_root, os.path.abspath(local_path)]) != local_root:
        raise remote_sync.RemoteSyncError(f"Refusing to access path outside local_root: {remote_relpath!r}")
    cache_key = _cache_key("synced", tool.pk, remote_relpath)
    if cache.get(cache_key):
        return local_path

    if os.path.exists(local_path):
        cache.set(cache_key, True, ttl)
        return local_path

    failure_key = _cache_key("failed", tool.pk, remote_relpath)
    cached_error = cache.get(failure_key)
    if cached_error is not None:
        raise remote_sync.RemoteSyncError(cached_error)

    try:
        message = _fetch(tool, remote_relpath, local_path, is_dir)
    except remote_sync.RemoteSyncError as e:
        cache.set(failure_key, str(e), FAILURE_TTL)
        if os.path.exists(local_path):
            logger.warning("Serving stale cached copy of %s for %s: %s", remote_relpath, tool.name, e)
            return local_path
        _record_sync_result(tool, False, str(e))
        raise

    cache.set(cache_key, True, ttl)
    _record_sync_result(tool, True, message)
    return local_path


def ensure_cached_many(tool, remote_relpaths, is_dir=False, ttl=CONTENT_TTL, max_workers=8):
    """Concurrently pre-warms the per-path cache for several paths at once - used by readers.py's
    history functions so a full page of runs doesn't serialize N SSH round trips one after another
    (even with connection multiplexing reusing one authenticated connection, each rsync invocation
    is still its own subprocess + protocol exchange - measured at ~1.5s each against Oak, which
    badly multiplies across even a single 25-run history page). Only actually contacts the remote
    host for paths that aren't already cache-fresh.

    Doesn't return anything or raise - it's purely a best-effort pre-warm. Callers still call
    ensure_cached()/the *_local_path() helpers per path afterward exactly as they would without
    this, which now either hits an already-warm cache entry (fast) or, for anything that also
    failed here, gets the same RemoteSyncError/fallback behavior ensure_cached() always has.

    Skips (no network call at all) any path with a fresh *failed*-fetch memory (see
    ensure_cached()'s FAILURE_TTL) - the same negative caching that keeps a lone ensure_cached()
    call from re-paying a known-broken path over and over applies here too, which matters even more
    for a batch: a handful of permanently-missing paths mixed into an otherwise-legitimate list
    (a stale directory-listing entry left over from a rename, say) used to cost one failed rsync
    round trip *every single call*, multiplied by however many pages/charts pre-warm that same list.

    Also skips (no network call at all) any path whose local copy already exists on disk, even on a
    cold in-memory cache - same "trust it, don't re-verify" reasoning as ensure_cached()'s own local-
    existence check (see its docstring): a many-hundred-run history page whose files are all already
    local used to force a real rsync round trip for every single one of them after any process
    restart, since the in-memory freshness cache had nothing to say about paths it had never seen in
    *this* process - confirmed live against fiji1 (500 already-local historical pressure logs
    turning one page load into minutes after a routine dev-server reload wiped that cache).

    Records ONE last_synced/last_sync_ok/last_sync_message update summarizing the whole batch,
    rather than one per file - many threads each calling tool.save() concurrently on the same row
    would risk racing/lock contention on top of being redundant.
    """
    to_fetch = []
    for p in remote_relpaths:
        if cache.get(_cache_key("synced", tool.pk, p)):
            continue
        if cache.get(_cache_key("failed", tool.pk, p)) is not None:
            continue
        if os.path.exists(os.path.join(tool.local_root, p)):
            cache.set(_cache_key("synced", tool.pk, p), True, ttl)
            continue
        to_fetch.append(p)
    if not to_fetch:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch, tool, p, os.path.join(tool.local_root, p), is_dir): p for p in to_fetch}
        for future in as_completed(futures):
            p = futures[future]
            try:
                results[p] = future.result()
            except remote_sync.RemoteSyncError as e:
                logger.warning("Batch pre-warm failed for %s on %s: %s", p, tool.name, e)
                cache.set(_cache_key("failed", tool.pk, p), str(e), FAILURE_TTL)

    for p in results:
        cache.set(_cache_key("synced", tool.pk, p), True, ttl)
    if to_fetch:
        _record_sync_result(tool, len(results) == len(to_fetch), f"Batch pre-warm: {len(results)}/{len(to_fetch)} succeeded")


# A background warm never runs twice at once for the same tool+key - without this, every request
# that lands while a warm is already in flight would spawn its own duplicate copy of the same work.
_WARMING_KEY_PREFIX = "warming"


def warm_in_background(tool, key, remote_relpaths, is_dir=False, ttl=CONTENT_TTL):
    """Like ensure_cached_many, but fires the fetch off on a daemon thread and returns immediately
    instead of blocking the caller on it - for a synchronous request-handling path that wants "warm
    up whatever isn't cached yet, but don't make *this* page wait on it" (see readers.py's
    get_base_pressure_history: only a bounded number of genuinely-missing files are fetched inline
    per request, with the remainder handed to this to catch up in the background across the next
    few page loads, rather than one request ever blocking on fetching, say, a tool's entire
    multi-year history the first time someone opens its chart).

    `key` identifies this particular warm job (e.g. a tool slug plus a short tag) - while one is
    already running for that key, a repeat call is a no-op, so concurrent requests hitting the same
    cold chart don't each spin up their own redundant copy of the same fetch. remote_relpaths that
    are already cache-fresh or local (see ensure_cached_many) cost nothing extra - safe to pass the
    same list in on every request.
    """
    warming_key = _cache_key(_WARMING_KEY_PREFIX, tool.pk, key)
    if cache.get(warming_key):
        return
    cache.set(warming_key, True, 60 * 10)

    def _run():
        try:
            ensure_cached_many(tool, remote_relpaths, is_dir=is_dir, ttl=ttl)
        finally:
            cache.delete(warming_key)

    threading.Thread(target=_run, daemon=True).start()
