"""Listing a tool's recipe files from Oak: identity, folder ordering, lookup by id or by name."""

import hashlib
import re

from django.utils import timezone

from NEMO_smart_lab import remote_cache, remote_sync


RECIPE_TREE_TTL = remote_cache.RECIPE_TREE_TTL


RECIPE_CONTENT_TTL = remote_cache.RECIPE_CONTENT_TTL


def _recipe_id(relpath):
    # Recipe relative paths routinely contain spaces, "%", "#", etc. (confirmed live) - hashing
    # sidesteps any URL-encoding edge case entirely, same trick remote_cache._cache_key and
    # reservations._cache_key already use for awkward strings.
    return hashlib.sha1(relpath.encode()).hexdigest()[:16]


_WORDS_RE = re.compile(r"[a-z0-9]+")


# Recipe/config folders on the tool PC routinely sit alongside the instrument software's own
# installation files - installers, drivers, compiled executables, LabVIEW alias files - and
# (confirmed live) the occasional stray spreadsheet. None of these are ever recipes or settings
# themselves, so they're filtered out of the listing entirely, not just hidden from text preview
# (see configs._TEXT_EXTENSIONS, a narrower, preview-only distinction layered on top of this).
_NON_FLAT_FILE_EXTENSIONS = (
    ".exe", ".dll", ".mxx", ".ocx", ".sys", ".drv", ".msi", ".bin", ".so", ".dylib",
    ".aliases", ".lnk",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pdf",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".tiff",
    ".mp4", ".avi", ".mov", ".wav",
    ".db", ".mdb", ".accdb",
)


def _is_flat_file(name):
    return not name.lower().endswith(_NON_FLAT_FILE_EXTENSIONS)


def _category_sort_priority(category, pinned=()):
    """A folder an admin/user has explicitly pinned (SmartLabTool.pinned_recipe_categories,
    toggled from the small pin icon next to each folder heading on the Recipes page) sorts first
    of all - even ahead of "(root)", since pinning is a deliberate "put this at the very top
    regardless" action. Otherwise, "(root)" and the tool's actual shared recipe folders sort
    next, in this order: STANDARD, Maintenance, Production, Process. Only a folder that IS (once
    normalized) exactly one of these bare names qualifies - a *substring* match alone isn't
    enough - confirmed live a tool can have both a real "STANDARD" folder and an unrelated,
    per-project "Digilens Standard" folder, and only the former is the one meant to sort first;
    the latter is exactly as "someone's own folder" as any other, and sorts with the rest."""
    if category in pinned:
        return -1
    if category == "(root)":
        return 0
    normalized = " ".join(_WORDS_RE.findall(category.lower()))
    if normalized in ("standard", "standard recipe", "standard recipes"):
        return 1
    if normalized in ("maintenance",):
        return 2
    if normalized in ("production", "production recipe", "production recipes"):
        return 3
    if normalized in ("process", "process recipe", "process recipes"):
        return 4
    return 5


def list_recipes(cfg):
    """[{"id", "relpath", "name", "category", "mtime", "size"}, ...] for every recipe *file* (not
    folder) under this tool's configured recipe_subdir, sorted by (category, name). Returns []
    if this tool has no recipe_subdir/remote_tool configured (the feature is opt-in per tool -
    see SmartLabTool.recipe_subdir)."""
    tool = cfg.get("remote_tool")
    recipe_subdir = cfg.get("recipe_subdir")
    if not tool or not recipe_subdir:
        return []

    root = f"{tool.remote_subdir_or_default}/{recipe_subdir}"
    entries = remote_cache.list_remote_tree(tool.sync_endpoint, root, ttl=RECIPE_TREE_TTL)
    pinned = cfg.get("pinned_recipe_categories") or []

    recipes = []
    for relpath, mtime, size, is_dir in entries:
        if is_dir or not _is_flat_file(relpath):
            continue
        category = relpath.split("/", 1)[0] if "/" in relpath else "(root)"
        recipes.append(
            {
                "id": _recipe_id(relpath),
                "relpath": relpath,
                "name": relpath.rsplit("/", 1)[-1],
                "category": category,
                "mtime": timezone.make_aware(mtime) if timezone.is_naive(mtime) else mtime,
                "size": size,
            }
        )
    recipes.sort(key=lambda r: (_category_sort_priority(r["category"], pinned), r["category"].lower(), r["name"].lower()))
    return recipes


def find_recipe(cfg, recipe_id):
    return next((r for r in list_recipes(cfg) if r["id"] == recipe_id), None)


def _strip_txt_suffixes(value):
    # Every trailing ".txt" - a recipe file's own name always has at least one (its real
    # extension); the *run's own recorded* recipe name (embedded in its data file, not a filename)
    # usually doesn't, but confirmed live that some genuinely do (a recipe named with ".txt" as
    # part of its own name, e.g. "Plasma Al2O3 STANDARD.txt", picks up a *second* ".txt" from the
    # heater log export - see smart_lab_filters.strip_txt, which this mirrors) - stripping every
    # trailing occurrence from both sides before comparing is what makes either spelling match.
    while value.lower().endswith(".txt"):
        value = value[: -len(".txt")]
    return value


def find_recipe_by_name(cfg, recipe_name):
    """Best-effort match from a run's own recorded recipe name (e.g. "Thermal Al2O3 STANDARD" -
    embedded in its data file, no folder, no extension) to an actual current recipe file with that
    same name, so a run's own detail page can link straight to "the recipe that (as far as we can
    tell) produced this run" without the viewer having to go search the recipe browser by hand.

    Matches case-insensitively, ignoring either side's ".txt" suffix(es) (see _strip_txt_suffixes).
    Returns None (not an error, and not a guess) if there's no recipe_subdir configured or nothing
    matches at all.

    More than one recipe file can share that same name - confirmed live, common even: the exact
    same standard recipe copied into several different per-user folders, not just a rare edge
    case. Picking arbitrarily among those would be a real guess, not a recognized match - but a
    *canonical* shared location (see _category_sort_priority: "(root)", "STANDARD",
    "Maintenance", "Production", "Process", or a folder an admin has pinned) is a much stronger
    signal for "the" recipe than any one person's own copy of it, so that one wins when there's
    exactly one such canonical match among the duplicates. Still None (still not a guess) when
    every match is someone's own folder, or when more than one canonical match exists (e.g. it's
    in both "(root)" and "STANDARD" somehow) - genuinely ambiguous either way."""
    if not recipe_name or recipe_name == "(unknown)":
        return None
    target = _strip_txt_suffixes(recipe_name.strip()).lower()
    if not target:
        return None
    matches = [r for r in list_recipes(cfg) if _strip_txt_suffixes(r["name"]).lower() == target]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    pinned = cfg.get("pinned_recipe_categories") or []
    canonical = [r for r in matches if _category_sort_priority(r["category"], pinned) < 5]
    return canonical[0] if len(canonical) == 1 else None


def get_recently_updated_recipes(cfg, limit=5):
    """The `limit` most recently-modified recipes (see list_recipes' own "mtime") - shown on the
    tool detail overview page, side by side with "Last run", as a quick "what's someone been
    editing on this tool lately" signal a user wouldn't otherwise notice without digging through
    the full recipe browser.

    Returns [] (not an error) if the remote listing itself fails (e.g. Oak briefly unreachable) -
    this is a nice-to-have sidebar on a page whose main content (the tool's own summary/base
    pressure chart) has its own, separate error handling; a transient recipe-listing hiccup
    shouldn't take down the whole overview page over what's ultimately a secondary panel."""
    try:
        recipes = list_recipes(cfg)
    except remote_sync.RemoteSyncError:
        return []
    return sorted(recipes, key=lambda r: r["mtime"], reverse=True)[:limit]
