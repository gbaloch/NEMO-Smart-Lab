"""
Read-only browsing of a tool's configuration/settings files (Setup.ini, config.ini, alarm/IO
translation tables, etc.) - a separate concern from recipes.py (process programs) and readers.py
(run data), layered on the same remote_cache primitives both already use.

Where these files actually live varies per tool (confirmed live against real Oak data): some
tools (fiji1/2/3) keep them loose at the tool's own root folder, side by side with Logfile/
Recipes (e.g. "Setup.ini.txt", "Setup.ini - Copy.txt"); others (fiji5, mvd) keep them under their
own dedicated "configuration" subfolder, which can itself nest further subfolders (fiji5's
"configuration/SW versions", "configuration/default"). SmartLabTool.config_subdir picks which: a
real subfolder name (recursed into, same as recipes.py's own list_recipes), or "." to mean the
tool's own root folder itself - browsed shallowly, just that one folder's own files, never a full
recursive walk of the tool's entire remote tree (which would also pull in every run log and
recipe alongside them). Blank disables this feature entirely (opt-in per tool).

Nothing in this module ever writes anywhere - same reasoning as recipes.py.
"""

from django.utils import timezone

from NEMO_smart_lab import remote_cache
from NEMO_smart_lab.readers import FILE_ENCODING
from NEMO_smart_lab.recipes import _category_sort_priority, _is_flat_file, _recipe_id

CONFIG_TREE_TTL = remote_cache.RECIPE_TREE_TTL
CONFIG_CONTENT_TTL = remote_cache.RECIPE_CONTENT_TTL

# Only these are offered as a text preview (get_config_file_detail's "raw_text") - a config folder
# can still hold flat files that aren't plain settings text (e.g. a .csv/.log export sitting next
# to config.ini); those still show up in the listing (recipes._is_flat_file already dropped the
# actual binaries/installers/spreadsheets), just without a raw-text preview.
_TEXT_EXTENSIONS = (".ini", ".txt", ".cfg", ".conf")


def _mtime_aware(mtime):
    return timezone.make_aware(mtime) if timezone.is_naive(mtime) else mtime


def list_config_files(cfg):
    """[{"id", "fetch_path", "name", "category", "mtime", "size"}, ...] for every configuration
    file this tool has, sorted by (category, name). "fetch_path" is the path relative to the
    tool's own remote root - already accounting for whichever of the two layouts described in this
    module's docstring this tool uses - so a caller never needs to know which one produced a given
    entry. [] if config_subdir/remote_tool isn't configured (opt-in - see
    SmartLabTool.config_subdir)."""
    tool = cfg.get("remote_tool")
    config_subdir = (cfg.get("config_subdir") or "").strip()
    if not tool or not config_subdir:
        return []

    if config_subdir == ".":
        entries = remote_cache.list_remote_dir(tool.sync_endpoint, tool.remote_subdir_or_default, ttl=CONFIG_TREE_TTL)
        files = [
            {
                "id": _recipe_id(name),
                "fetch_path": name,
                "name": name,
                "category": "(root)",
                "mtime": _mtime_aware(mtime),
                "size": size,
            }
            for name, mtime, size, is_dir in entries
            if not is_dir and _is_flat_file(name)
        ]
        files.sort(key=lambda f: f["name"].lower())
        return files

    root = f"{tool.remote_subdir_or_default}/{config_subdir}"
    entries = remote_cache.list_remote_tree(tool.sync_endpoint, root, ttl=CONFIG_TREE_TTL)
    files = []
    for relpath, mtime, size, is_dir in entries:
        if is_dir or not _is_flat_file(relpath):
            continue
        category = relpath.split("/", 1)[0] if "/" in relpath else "(root)"
        files.append(
            {
                "id": _recipe_id(f"{config_subdir}/{relpath}"),
                "fetch_path": f"{config_subdir}/{relpath}",
                "name": relpath.rsplit("/", 1)[-1],
                "category": category,
                "mtime": _mtime_aware(mtime),
                "size": size,
            }
        )
    files.sort(key=lambda f: (_category_sort_priority(f["category"]), f["category"].lower(), f["name"].lower()))
    return files


def find_config_file(cfg, file_id):
    return next((f for f in list_config_files(cfg) if f["id"] == file_id), None)


def get_config_file_detail(cfg, file_id):
    """Full detail for one config file: the list_config_files() entry, plus raw_text - the file's
    own text content if its extension looks like a text format (see _TEXT_EXTENSIONS), or None
    (shown as "no preview available" - e.g. a binary file that happens to live in the same folder)
    otherwise. None (the whole thing, not just raw_text) if file_id doesn't match anything
    currently listed (e.g. deleted/renamed on Oak since the listing was last refreshed)."""
    tool = cfg.get("remote_tool")
    entry = find_config_file(cfg, file_id)
    if not tool or entry is None:
        return None

    if not entry["name"].lower().endswith(_TEXT_EXTENSIONS):
        return {**entry, "raw_text": None}

    local_path = remote_cache.ensure_cached(tool, entry["fetch_path"], ttl=CONFIG_CONTENT_TTL)
    with open(local_path, encoding=FILE_ENCODING) as f:
        raw_text = f.read()
    return {**entry, "raw_text": raw_text}
