#!/usr/bin/env bash
# Shared helper for pull-instrument-data/upload-instrument-data/passthrough-instrument-data:
# fetches the tool-name -> Oak-directory mapping from the NEMO Smart Lab plugin's own
# SmartLabTool table (via views.tool_sync_map), instead of each script hardcoding - and having to
# keep in sync by hand - its own copy of that list. See staging/README.md.
#
# Requires: curl, jq.
#
# Usage: the sourcing script must first set (non-empty):
#   nemo_sync_map_url  - e.g. https://nemo.example.edu/smart_lab/api/sync-map.json
#   nemo_api_key       - shared secret matching settings.SMART_LAB_STAGING_API_KEY on that NEMO
#                         instance
# then source this file and call fetch_nemo_tool_map, which populates the associative array
# NEMO_TOOL_MAP (declared here) with {name: remote_directory} and returns 0, or prints an error to
# stderr and returns 1 without touching NEMO_TOOL_MAP.

declare -A NEMO_TOOL_MAP

fetch_nemo_tool_map() {
  if [[ -z "${nemo_sync_map_url:-}" || -z "${nemo_api_key:-}" ]]; then
    echo "ERROR: nemo_sync_map_url/nemo_api_key are not configured - edit the top of this script." >&2
    return 1
  fi

  local map_json
  if ! map_json=$(curl -fsS --max-time 30 -H "Authorization: Token $nemo_api_key" "$nemo_sync_map_url"); then
    echo "ERROR: Could not fetch the tool list from NEMO ($nemo_sync_map_url)." >&2
    return 1
  fi

  local name remote_directory
  while IFS=$'\t' read -r name remote_directory; do
    [[ -n "$name" ]] && NEMO_TOOL_MAP["$name"]="$remote_directory"
  done < <(jq -r 'to_entries[] | [.key, .value] | @tsv' <<<"$map_json")

  if [[ ${#NEMO_TOOL_MAP[@]} -eq 0 ]]; then
    echo "ERROR: NEMO returned an empty (or unparseable) tool list from $nemo_sync_map_url." >&2
    return 1
  fi

  return 0
}
