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
before falling through to the same local-file reads readers.py has always done. LocMemCache (the
default) is per-process - fine for a single `runserver`/single-worker deployment; a multi-worker
one should switch to a shared CACHES backend so workers share freshness state instead of each
independently re-checking the remote host.
"""

import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from django.core.cache import cache
from django.utils import timezone

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

    Falls back to serving an existing local copy if the fetch itself fails (logs a warning, does
    NOT update last_sync_ok/last_sync_message - a graceful stale-serve isn't "the last sync
    failed", it's "we didn't need to try"). Only raises remote_sync.RemoteSyncError - and records
    the failure on the SmartLabTool row - when there's no local copy to fall back to at all.

    Returns the local path (str), same shape callers already work with when local_root was a full
    eager mirror.
    """
    local_path = os.path.join(tool.local_root, remote_relpath)
    cache_key = _cache_key("synced", tool.pk, remote_relpath)
    if cache.get(cache_key):
        return local_path

    try:
        message = _fetch(tool, remote_relpath, local_path, is_dir)
    except remote_sync.RemoteSyncError as e:
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

    Records ONE last_synced/last_sync_ok/last_sync_message update summarizing the whole batch,
    rather than one per file - many threads each calling tool.save() concurrently on the same row
    would risk racing/lock contention on top of being redundant.
    """
    to_fetch = [p for p in remote_relpaths if not cache.get(_cache_key("synced", tool.pk, p))]
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

    for p in results:
        cache.set(_cache_key("synced", tool.pk, p), True, ttl)
    if to_fetch:
        _record_sync_result(tool, len(results) == len(to_fetch), f"Batch pre-warm: {len(results)}/{len(to_fetch)} succeeded")
