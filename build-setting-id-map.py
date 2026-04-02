#!/usr/bin/env python3
"""
build-setting-id-map.py

Walks all CIS Build Kit JSON files in IntuneWindows11v4.0.0/Settings Catalog/,
flattens parent+children settings, and maps CIS rec numbers to their
settingDefinitionId values.

The CIS Build Kit encodes parent/child settings as nested children[] arrays.
The CIS rec numbers in the description correspond to a FLATTENED walk of
parent + children (in order), NOT just the top-level settings array.

Output format:
  CIS_REC  TOP/CHILD  SETTING_DEFINITION_ID  SOURCE_FILE

Nesting types handled:
  - choiceSettingValue.children[]
  - groupSettingCollectionValue[].children[]
  - simpleSettingCollectionValue (leaf, no children)
  - simpleSettingValue (leaf, no children)
"""

import json
import os
import re
import sys
from pathlib import Path

# Target controls for special focus
SPECIAL_FOCUS = {
    "exceptionable": ["26.7", "49.8", "49.1", "49.4", "4.4.2", "4.6.9.1",
                       "4.10.9.1.3", "4.10.9.2", "4.10.26.2", "4.11.36.4.2.1",
                       "68.2", "89.10", "89.12", "89.14"],
    "reject":        ["55.5", "76.1.2", "80.5", "12.1"],
    "na":            ["4.11.7.1.5", "4.11.7.2.5", "4.11.7.2.6", "4.11.7.2.8"],
    "modified":      ["49.29"],
    "do_not_deploy": ["49.9", "49.10"],
}

# Flatten special focus to a lookup set
ALL_SPECIAL = {}
for category, recs in SPECIAL_FOCUS.items():
    for rec in recs:
        ALL_SPECIAL[rec] = category


def parse_cis_recs(description: str) -> list[str]:
    """Extract CIS recommendation numbers from the description field.

    The description contains text like:
      "CIS Recommendation Numbers:\n\n1.1\n4.1.3.1\n..."
    with possible typos in "Recommendation" (Recommendaiton, Recommendatio).
    """
    # Find everything after the "CIS Recommend..." line
    # Variants seen: "CIS Recommendation Numbers:", "CIS Recommendaiton Numbers:",
    #                "CIS Recommendatio Numbers:", "CIS Recommendations"
    match = re.search(r'CIS\s+Recommend\w*(?:\s+Numbers?)?\s*:?\s*\n\n?(.*)', description, re.DOTALL | re.IGNORECASE)
    if not match:
        return []

    raw = match.group(1)
    recs = []
    for line in raw.strip().split('\n'):
        line = line.strip()
        if line and re.match(r'^[\d.]+$', line):
            recs.append(line)
    return recs


def flatten_setting_instance(setting_instance: dict) -> list[dict]:
    """Recursively flatten a settingInstance and its children.

    Returns a list of dicts: [{"settingDefinitionId": ..., "type": "top"|"child"}, ...]

    The first entry is the parent (the settingInstance itself).
    Subsequent entries are children, recursively flattened in document order.
    """
    results = []

    def _extract(node: dict, is_top: bool = True):
        """Extract settingDefinitionId from a node, then recurse into children."""
        sid = node.get("settingDefinitionId")
        if sid:
            results.append({
                "settingDefinitionId": sid,
                "type": "top" if is_top else "child",
            })

        # Check all possible child containers
        # 1. choiceSettingValue.children[]
        csv = node.get("choiceSettingValue")
        if csv and isinstance(csv, dict):
            for child in csv.get("children", []):
                _extract(child, is_top=False)

        # 2. groupSettingCollectionValue[].children[]
        gscv = node.get("groupSettingCollectionValue")
        if gscv and isinstance(gscv, list):
            for group_item in gscv:
                for child in group_item.get("children", []):
                    _extract(child, is_top=False)

        # 3. simpleSettingCollectionValue - leaf, no children to recurse
        # 4. simpleSettingValue - leaf, no children to recurse

    _extract(setting_instance)
    return results


def process_json_file(filepath: str) -> tuple[list[str], list[dict], str]:
    """Process a single JSON file.

    Returns (cis_recs, flattened_settings, short_filename).
    """
    with open(filepath, encoding='utf-8-sig') as f:
        data = json.load(f)

    short_name = os.path.basename(filepath)
    description = data.get("description", "")
    cis_recs = parse_cis_recs(description)

    flattened = []
    for setting in data.get("settings", []):
        si = setting.get("settingInstance", {})
        flattened.extend(flatten_setting_instance(si))

    return cis_recs, flattened, short_name


def main():
    base_dir = Path(__file__).resolve().parent / "IntuneWindows11v4.0.0" / "Settings Catalog"

    if not base_dir.exists():
        print(f"ERROR: Settings Catalog directory not found: {base_dir}", file=sys.stderr)
        sys.exit(1)

    json_files = sorted(base_dir.rglob("*.json"))

    if not json_files:
        print(f"ERROR: No JSON files found in {base_dir}", file=sys.stderr)
        sys.exit(1)

    # Collect all mappings
    all_mappings = []       # list of (cis_rec, type, setting_def_id, source_file)
    mismatches = []         # files where rec count != flattened count
    special_found = {}      # track which special focus controls were found
    total_recs = 0
    total_settings = 0

    print("=" * 120)
    print(f"CIS Build Kit Setting ID Mapping")
    print(f"Source: {base_dir}")
    print(f"Files found: {len(json_files)}")
    print("=" * 120)
    print()

    for jf in json_files:
        cis_recs, flattened, short_name = process_json_file(str(jf))

        # Determine subfolder (Level 1, Level 2, Bitlocker)
        rel = jf.relative_to(base_dir)
        subfolder = str(rel.parent) if str(rel.parent) != "." else ""

        label = f"[{subfolder}] {short_name}" if subfolder else short_name
        total_recs += len(cis_recs)
        total_settings += len(flattened)

        print(f"--- {label} ---")
        print(f"    CIS recs: {len(cis_recs)}  |  Flattened settings: {len(flattened)}  |  Top-level settings: {sum(1 for s in flattened if s['type'] == 'top')}")

        if len(cis_recs) != len(flattened):
            mismatch_msg = f"  *** MISMATCH: {len(cis_recs)} recs vs {len(flattened)} settings in {label}"
            print(mismatch_msg)
            mismatches.append(mismatch_msg)

        # Zip and output
        max_entries = max(len(cis_recs), len(flattened))
        for i in range(max_entries):
            rec = cis_recs[i] if i < len(cis_recs) else "???"
            if i < len(flattened):
                stype = flattened[i]["type"].upper()
                sid = flattened[i]["settingDefinitionId"]
            else:
                stype = "???"
                sid = "*** NO MATCHING SETTING ***"

            # Check if this is a special focus control
            marker = ""
            if rec in ALL_SPECIAL:
                marker = f"  <-- [{ALL_SPECIAL[rec].upper()}]"
                special_found[rec] = {
                    "category": ALL_SPECIAL[rec],
                    "settingDefinitionId": sid if sid != "*** NO MATCHING SETTING ***" else None,
                    "type": stype,
                    "source": short_name,
                }

            all_mappings.append((rec, stype, sid, short_name))
            print(f"    {rec:<25s} {stype:<6s} {sid}  ({short_name}){marker}")

        print()

    # Summary
    print("=" * 120)
    print("SUMMARY")
    print("=" * 120)
    print(f"Total JSON files processed: {len(json_files)}")
    print(f"Total CIS recs:            {total_recs}")
    print(f"Total flattened settings:  {total_settings}")
    print()

    if mismatches:
        print("MISMATCHES (rec count != flattened setting count):")
        for m in mismatches:
            print(m)
        print()
    else:
        print("No mismatches found -- all rec counts match flattened setting counts.")
        print()

    # Special focus summary
    print("=" * 120)
    print("SPECIAL FOCUS CONTROLS")
    print("=" * 120)
    for category, recs in SPECIAL_FOCUS.items():
        print(f"\n  [{category.upper()}]")
        for rec in recs:
            if rec in special_found:
                info = special_found[rec]
                print(f"    {rec:<25s} {info['type']:<6s} {info['settingDefinitionId'] or 'NOT FOUND'}  ({info['source']})")
            else:
                print(f"    {rec:<25s} *** NOT FOUND IN ANY JSON FILE ***")

    # Controls not found in any file
    print()
    all_recs_in_files = {m[0] for m in all_mappings}
    missing_special = [r for r in ALL_SPECIAL if r not in all_recs_in_files]
    if missing_special:
        print("WARNING: These special focus controls were NOT found in any JSON file:")
        for r in missing_special:
            print(f"    {r} [{ALL_SPECIAL[r].upper()}]")
    print()


if __name__ == "__main__":
    main()
