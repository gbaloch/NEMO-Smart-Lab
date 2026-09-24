"""
Read-only browsing of a tool's actual recipe files on its RemoteSyncEndpoint (e.g. Oak) - a
separate concern from NEMO_smart_lab.readers (which only ever reads *run* data - what already
happened), layered on the same NEMO_smart_lab.remote_cache primitives readers.py uses.

Recipe folders are laid out inconsistently per tool (a mix of a "standard recipes" folder under
whatever name a given tool happens to use, and per-user folders, nested arbitrarily deep) - rather
than guess at a "standard vs personal" split, this exposes the whole tree, grouped by top-level
folder name, and lets the UI decide what to do with that.

Recipe files themselves are small, tab-delimited, CRLF-terminated step programs (confirmed live
against real Oak data for all six tools), e.g.:
    flow        0       20

    heater      17      150     

    goto        11      100     cycles

Not every line has all four fields (some are blank/ragged) - _parse_steps tolerates that. Text
encoding is Windows Latin-1 (a "°C" unit is byte 0xB0, not valid UTF-8) - same reason
readers.FILE_ENCODING already exists; reused here rather than re-guessing an encoding.

Nothing in this module ever writes anywhere - see this module's and remote_cache's docstrings for
why (recipes drive physical hardware; editing is a deliberately deferred future phase, not this
one).

This package used to be one module (recipes.py); it is now split by concern - see the submodules. Everything that
module exposed is re-exported here, so `from NEMO_smart_lab.recipes import ...` keeps working.
"""

# flake8: noqa: F401
from NEMO_smart_lab.recipes.base_pressure import (
    base_pressure_recipe_targets,
    get_base_pressure_recipe_links,
    suggest_base_pressure_recipes,
)
from NEMO_smart_lab.recipes.cycles import (
    _MAX_DISTINCT_RECIPES_PER_REQUEST,
    total_cycles_run,
)
from NEMO_smart_lab.recipes.duplicates import (
    _recipe_content_fingerprint,
    find_duplicate_recipes,
)
from NEMO_smart_lab.recipes.listing import (
    RECIPE_CONTENT_TTL,
    RECIPE_TREE_TTL,
    _NON_FLAT_FILE_EXTENSIONS,
    _WORDS_RE,
    _category_sort_priority,
    _is_flat_file,
    _recipe_id,
    _strip_txt_suffixes,
    find_recipe,
    find_recipe_by_name,
    get_recently_updated_recipes,
    list_recipes,
)
from NEMO_smart_lab.recipes.parsing import (
    _heater_channel_label,
    _parse_steps,
    _prewarm_recipe_files,
    _recipe_channel_labels,
    _summarize_steps,
    get_recipe_detail,
)
