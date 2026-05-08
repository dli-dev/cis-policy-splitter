#!/usr/bin/env python3
"""
split-cis-policies.py

Reads CIS Build Kit Settings Catalog JSON files and a control decisions config,
then splits them into baseline bundles, exceptionable policies, and alternatives.
Writes output JSONs and a manifest for the PowerShell deployer.
"""

import argparse
import copy
import json
import re
from pathlib import Path


def load_config(config_path: str) -> tuple[dict, dict, dict]:
    """Load the control decisions config and build lookups.

    Returns:
        (config, lookup, bundle_lookup) where:
        - config is the raw config dict
        - lookup maps settingDefinitionId -> control info dict
        - bundle_lookup maps cis_rec -> bundle_id (only for bundled controls)
    """
    with open(config_path, encoding="utf-8-sig") as f:
        config = json.load(f)

    lookup = {}
    controls = config.get("controls", {})
    for cis_rec, ctrl in controls.items():
        # Skip JSON comment fields
        if cis_rec.startswith("_"):
            continue

        sid = ctrl.get("settingDefinitionId")
        if not sid:
            continue

        disposition = ctrl.get("disposition", "accept")

        lookup[sid] = {
            "cis_rec": cis_rec,
            "disposition": disposition,
            "description": ctrl.get("description", ""),
            "is_child": ctrl.get("isChild", False),
            "alternatives": ctrl.get("alternatives", []),
            "modified_value": ctrl.get("modifiedValue"),
            "baseline_value": ctrl.get("baselineValue"),
        }

    bundle_lookup = {}
    for bundle_id, bundle in config.get("bundles", {}).items():
        if bundle_id.startswith("_"):
            continue
        for ctrl_rec in bundle.get("controls", []):
            if ctrl_rec in bundle_lookup:
                raise ValueError(
                    f"Control {ctrl_rec} appears in multiple bundles "
                    f"({bundle_lookup[ctrl_rec]} and {bundle_id})"
                )
            bundle_lookup[ctrl_rec] = bundle_id

    return config, lookup, bundle_lookup


def _process_children(children: list[dict], lookup: dict) -> dict:
    """Recursively process a children array.

    Returns:
        {
            "filtered": [children to keep],
            "extracted": [{"cis_rec", "description", "child", "alternatives"}, ...],
            "dropped": int,
            "modified": bool,
        }
    """
    filtered = []
    extracted = []
    dropped = 0
    modified = False

    for child in children:
        child_sid = child.get("settingDefinitionId", "")
        child_ctrl = lookup.get(child_sid)

        # Recurse into nested children first
        child_copy = child
        nested_modified = False

        # choiceSettingValue.children
        csv = child.get("choiceSettingValue")
        if csv and isinstance(csv, dict) and csv.get("children"):
            nested = _process_children(csv["children"], lookup)
            if nested["modified"]:
                child_copy = copy.deepcopy(child)
                child_copy["choiceSettingValue"]["children"] = nested["filtered"]
                nested_modified = True
                dropped += nested["dropped"]
                extracted.extend(nested["extracted"])

        # Now apply this child's disposition
        disposition = None
        if child_ctrl and child_ctrl["is_child"]:
            disposition = child_ctrl["disposition"]

        if disposition in ("reject", "na"):
            dropped += 1
            modified = True
        elif disposition == "exceptionable":
            extracted.append({
                "cis_rec": child_ctrl["cis_rec"],
                "description": child_ctrl["description"],
                "child": child_copy if nested_modified else child,
                "alternatives": child_ctrl["alternatives"],
            })
            modified = True
        elif disposition == "modified":
            # Apply the modifiedValue to this child's value, then keep it.
            base = child_copy if nested_modified else copy.deepcopy(child)
            if child_ctrl["modified_value"]:
                if "choiceSettingValue" in base and isinstance(base["choiceSettingValue"], dict):
                    base["choiceSettingValue"]["value"] = child_ctrl["modified_value"]
                elif "simpleSettingValue" in base and isinstance(base["simpleSettingValue"], dict):
                    base["simpleSettingValue"]["value"] = child_ctrl["modified_value"]
            filtered.append(base)
            modified = True
        else:
            # accept or not in config — keep
            if nested_modified:
                filtered.append(child_copy)
                modified = True
            else:
                filtered.append(child)

    return {
        "filtered": filtered,
        "extracted": extracted,
        "dropped": dropped,
        "modified": modified,
    }


def classify_settings(settings: list[dict], lookup: dict) -> dict:
    """Classify each setting by disposition, handling parent/child nesting.

    Returns:
        {
            "baseline": [settings to keep],
            "extracted": [{"cis_rec", "description", "setting", "alternatives"}, ...],
            "dropped": int,
        }
    """
    baseline = []
    extracted = []
    dropped = 0

    for setting in settings:
        inst = setting["settingInstance"]
        sid = inst["settingDefinitionId"]

        # --- Step 1: Process children ---
        processed_setting = setting
        child_extracted = []

        # choiceSettingValue.children
        csv = inst.get("choiceSettingValue")
        if csv and isinstance(csv, dict) and csv.get("children"):
            result = _process_children(csv["children"], lookup)
            if result["modified"]:
                processed_setting = copy.deepcopy(setting)
                processed_setting["settingInstance"]["choiceSettingValue"]["children"] = result["filtered"]
                dropped += result["dropped"]
            child_extracted.extend(result["extracted"])

        # groupSettingCollectionValue[].children
        gscv = inst.get("groupSettingCollectionValue")
        if gscv and isinstance(gscv, list):
            for gi, group in enumerate(gscv):
                group_children = group.get("children", [])
                if group_children:
                    result = _process_children(group_children, lookup)
                    if result["modified"]:
                        if processed_setting is setting:
                            processed_setting = copy.deepcopy(setting)
                        processed_setting["settingInstance"]["groupSettingCollectionValue"][gi]["children"] = result["filtered"]
                        dropped += result["dropped"]
                    child_extracted.extend(result["extracted"])

        # Build standalone policies for extracted children (parent + only target child)
        for ext_child in child_extracted:
            parent_for_child = copy.deepcopy(setting)
            p_inst = parent_for_child["settingInstance"]
            if "choiceSettingValue" in p_inst and p_inst["choiceSettingValue"]:
                p_inst["choiceSettingValue"]["children"] = [ext_child["child"]]
            elif "groupSettingCollectionValue" in p_inst and p_inst["groupSettingCollectionValue"]:
                for g in p_inst["groupSettingCollectionValue"]:
                    g["children"] = [ext_child["child"]]

            extracted.append({
                "cis_rec": ext_child["cis_rec"],
                "description": ext_child["description"],
                "setting": parent_for_child,
                "alternatives": ext_child["alternatives"],
            })

        # --- Step 2: Top-level disposition ---
        ctrl = lookup.get(sid)

        if not ctrl:
            baseline.append(processed_setting)
            continue

        disposition = ctrl["disposition"]

        if disposition == "accept":
            baseline.append(processed_setting)
        elif disposition in ("reject", "na"):
            dropped += 1
        elif disposition == "modified":
            modified_setting = copy.deepcopy(processed_setting)
            if ctrl["modified_value"]:
                m_inst = modified_setting["settingInstance"]
                if "choiceSettingValue" in m_inst:
                    m_inst["choiceSettingValue"]["value"] = ctrl["modified_value"]
            baseline.append(modified_setting)
        elif disposition == "exceptionable":
            extracted.append({
                "cis_rec": ctrl["cis_rec"],
                "description": ctrl["description"],
                "setting": processed_setting,
                "alternatives": ctrl["alternatives"],
            })

    return {
        "baseline": baseline,
        "extracted": extracted,
        "dropped": dropped,
    }


def _find_child(inst: dict, target_sid: str) -> dict | None:
    """Recursively find a child node by settingDefinitionId."""
    # choiceSettingValue.children
    csv = inst.get("choiceSettingValue")
    if csv and isinstance(csv, dict):
        for child in csv.get("children", []):
            if child.get("settingDefinitionId") == target_sid:
                return child
            found = _find_child(child, target_sid)
            if found:
                return found

    # groupSettingCollectionValue[].children
    gscv = inst.get("groupSettingCollectionValue")
    if gscv and isinstance(gscv, list):
        for group in gscv:
            for child in group.get("children", []):
                if child.get("settingDefinitionId") == target_sid:
                    return child
                found = _find_child(child, target_sid)
                if found:
                    return found

    return None


def swap_alt_value(setting: dict, alt: dict, target_child_sid: str = None) -> dict:
    """Deep-copy a setting and swap its value for an alternative.

    Args:
        setting: The original setting dict.
        alt: The alternative dict with "name" and "settingValue" keys.
        target_child_sid: If set, swap the value on this child inside the setting
                          rather than the top-level setting itself.

    Returns:
        A deep copy of the setting with the value swapped.
    """
    swapped = copy.deepcopy(setting)

    alt_value = alt.get("settingValue")
    if alt_value is None:
        return swapped

    value = alt_value.get("value")
    if value is None:
        return swapped

    # Find the target node to swap
    if target_child_sid:
        # Find the child by settingDefinitionId
        target = _find_child(swapped["settingInstance"], target_child_sid)
        if not target:
            return swapped
    else:
        target = swapped["settingInstance"]

    # Detect setting type and swap
    odata_type = target.get("@odata.type", "")

    if "ChoiceSettingInstance" in odata_type:
        target["choiceSettingValue"]["value"] = value
        # If alt specifies children (including empty list), override them
        if "children" in alt_value:
            target["choiceSettingValue"]["children"] = alt_value["children"]

    elif "SimpleSettingCollectionInstance" in odata_type:
        if not value:
            # Empty collection not allowed by Graph API — skip this alt
            return swapped
        target["simpleSettingCollectionValue"] = [
            {
                "@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValue",
                "settingValueTemplateReference": None,
                "value": v,
            }
            for v in value
        ]

    elif "SimpleSettingInstance" in odata_type:
        target["simpleSettingValue"]["value"] = value

    return swapped


def build_output_policy(
    name: str,
    description: str,
    source_policy: dict,
    scope_tag: str,
    settings: list[dict],
) -> dict:
    """Build a clean output policy dict with only Graph API creation fields."""
    template_ref = {
        "templateId": source_policy["templateReference"]["templateId"],
        "templateFamily": source_policy["templateReference"]["templateFamily"],
    }
    # Only include non-null display fields
    for field in ("templateDisplayName", "templateDisplayVersion"):
        val = source_policy["templateReference"].get(field)
        if val:
            template_ref[field] = val

    return {
        "name": name,
        "description": description,
        "platforms": source_policy["platforms"],
        "technologies": source_policy["technologies"],
        "roleScopeTagIds": [scope_tag],
        "templateReference": template_ref,
        "settings": settings,
    }


def _sanitize_filename(name: str) -> str:
    """Remove characters illegal in file paths."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


def _parse_level(policy_name: str) -> str:
    """Parse CIS level from policy name."""
    if "(L1)" in policy_name:
        return "L1"
    elif "(L2)" in policy_name:
        return "L2"
    elif "(BL)" in policy_name:
        return "BL"
    return "L1"


def _parse_section_name(policy_name: str) -> str:
    """Extract section name from policy name.

    Strips: "CIS (L1) ", " - Windows 11 Intune 4.0.0", " (nn)" suffix.
    """
    name = re.sub(r'^CIS\s+\([^)]+\)\s+', '', policy_name)
    name = re.sub(r'\s*-\s*Windows 11 Intune \d+\.\d+\.\d+\s*$', '', name)
    name = re.sub(r'\s+\(\d+\)\s*$', '', name)
    return name.strip()


def process_file(
    filepath: str,
    config: dict,
    lookup: dict,
    output_dir: str,
    assignment_group: str | None = None,
    autopilot_assignment_group: str | None = None,
    dry_run: bool = False,
    bundle_lookup: dict | None = None,
    bundled_extracts: dict | None = None,
) -> list[dict]:
    """Process a single CIS Build Kit JSON file.

    Returns a list of manifest entries.

    Bundled exceptionable extractions are stashed into ``bundled_extracts``
    (keyed by bundle_id) instead of being written as standalone policies. The
    caller is responsible for emitting the bundled outputs after all files
    have been processed.
    """
    if bundle_lookup is None:
        bundle_lookup = {}
    if bundled_extracts is None:
        bundled_extracts = {}
    with open(filepath, encoding="utf-8-sig") as f:
        policy = json.load(f)

    policy_name = policy["name"].strip()

    # Skip check
    if policy_name in [s.strip() for s in config.get("skipFiles", [])]:
        return []

    # Autopilot check
    is_autopilot = policy_name in [s.strip() for s in config.get("autopilotPolicies", [])]

    level = _parse_level(policy_name)
    section_name = _parse_section_name(policy_name)

    # Classify settings
    result = classify_settings(policy["settings"], lookup)

    manifest_entries = []

    # --- Write baseline ---
    if result["baseline"]:
        baseline_name = f"CIS {level} - {section_name}"
        baseline_policy = build_output_policy(
            name=baseline_name,
            description=policy.get("description", ""),
            source_policy=policy,
            scope_tag=config["scopeTags"]["readonly"],
            settings=result["baseline"],
        )

        safe_name = _sanitize_filename(baseline_name)
        rel_path = f"baseline/{safe_name}.json"
        full_path = Path(output_dir) / rel_path

        if not dry_run:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(baseline_policy, f, indent=2, ensure_ascii=False)

        policy_type = "autopilot" if is_autopilot else "baseline"
        manifest_entries.append({
            "file": rel_path,
            "type": policy_type,
            "assignTo": autopilot_assignment_group if is_autopilot else assignment_group,
        })

    # --- Write exceptionable + alternatives ---
    for ext in result["extracted"]:
        # Determine if this is a child extraction (need target_child_sid for swap)
        target_child_sid = None
        ctrl_for_ext = lookup.get(ext["setting"]["settingInstance"]["settingDefinitionId"])
        if not ctrl_for_ext or ctrl_for_ext.get("cis_rec") != ext["cis_rec"]:
            # The extracted setting's top-level SID doesn't match the control —
            # this is a child extraction with parent wrapper
            for sid, ctrl in lookup.items():
                if ctrl["cis_rec"] == ext["cis_rec"] and ctrl["is_child"]:
                    target_child_sid = sid
                    break

        # If this control belongs to a bundle, defer emission to the bundle
        # writer (called from main after all files are processed).
        bundle_id = bundle_lookup.get(ext["cis_rec"])
        if bundle_id:
            bundled_extracts.setdefault(bundle_id, []).append({
                "cis_rec": ext["cis_rec"],
                "description": ext["description"],
                "setting": ext["setting"],
                "alternatives": ext["alternatives"],
                "target_child_sid": target_child_sid,
                "source_policy": policy,
            })
            continue

        # Apply baselineValue override (if any) before writing the standalone
        # exceptionable baseline. Walk the lookup by cis_rec since the control
        # entry may be keyed by a child SID we didn't carry through.
        ctrl_by_rec = next(
            (c for c in lookup.values() if c["cis_rec"] == ext["cis_rec"]),
            None,
        )
        baseline_setting = ext["setting"]
        if ctrl_by_rec and ctrl_by_rec.get("baseline_value"):
            fake_alt = {
                "name": "_baseline",
                "settingValue": ctrl_by_rec["baseline_value"],
            }
            baseline_setting = swap_alt_value(
                ext["setting"], fake_alt, target_child_sid=target_child_sid
            )

        # Exceptionable baseline
        exc_name = f"CIS {ext['cis_rec']} - {ext['description']} - Baseline"
        exc_policy = build_output_policy(
            name=exc_name,
            description=f"CIS {ext['cis_rec']} exceptionable baseline: {ext['description']}",
            source_policy=policy,
            scope_tag=config["scopeTags"]["exceptionable"],
            settings=[baseline_setting],
        )

        safe_name = _sanitize_filename(exc_name)
        rel_path = f"exceptionable/{safe_name}.json"
        full_path = Path(output_dir) / rel_path

        if not dry_run:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(exc_policy, f, indent=2, ensure_ascii=False)

        manifest_entries.append({
            "file": rel_path,
            "type": "exceptionable",
            "assignTo": assignment_group,
        })

        for alt in ext.get("alternatives", []):
            if not alt:
                continue

            alt_name = f"CIS {ext['cis_rec']} - {ext['description']} - Alt ({alt['name']})"
            alt_setting = swap_alt_value(ext["setting"], alt, target_child_sid=target_child_sid)

            alt_policy = build_output_policy(
                name=alt_name,
                description=f"CIS {ext['cis_rec']} alternative ({alt['name']}): {alt.get('description', '')}",
                source_policy=policy,
                scope_tag=config["scopeTags"]["exceptionable"],
                settings=[alt_setting],
            )

            safe_name = _sanitize_filename(alt_name)
            rel_path = f"exceptionable/{safe_name}.json"
            full_path = Path(output_dir) / rel_path

            if not dry_run:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    json.dump(alt_policy, f, indent=2, ensure_ascii=False)

            manifest_entries.append({
                "file": rel_path,
                "type": "alternative",
                "assignTo": None,
            })

    return manifest_entries


def emit_bundles(
    bundles_config: dict,
    bundled_extracts: dict,
    config: dict,
    lookup: dict,
    output_dir: str,
    assignment_group: str | None,
    dry_run: bool,
) -> list[dict]:
    """Emit one policy per (bundle × tier).

    For each bundle, build a single policy per tier that combines all the
    bundle's controls. Each control is swapped to its tier-specific value:
    the tier's ``alts`` map names the alt to use; controls absent from the
    map use baseline (with optional ``baselineValue`` override).
    """
    manifest_entries = []
    ctrl_by_rec = {ctrl["cis_rec"]: ctrl for ctrl in lookup.values()}
    scope_tag = config["scopeTags"]["exceptionable"]

    for bundle_id, bundle in bundles_config.items():
        if bundle_id.startswith("_"):
            continue

        extracts = bundled_extracts.get(bundle_id, [])
        if not extracts:
            continue

        extracts_by_rec = {e["cis_rec"]: e for e in extracts}
        # Use the first extract's source policy as the templateReference donor.
        # All bundle members must share platforms/technologies; we trust the
        # config author to bundle compatible controls.
        source_policy = extracts[0]["source_policy"]

        for tier_idx, tier in enumerate(bundle.get("tiers", [])):
            tier_name = tier["name"]
            alts_map = tier.get("alts", {})

            settings = []
            for ctrl_rec in bundle["controls"]:
                ext = extracts_by_rec.get(ctrl_rec)
                if not ext:
                    print(
                        f"  WARN: bundle '{bundle_id}' tier '{tier_name}' "
                        f"missing extract for control {ctrl_rec} — skipped"
                    )
                    continue

                target_child_sid = ext["target_child_sid"]
                ctrl = ctrl_by_rec.get(ctrl_rec)

                if ctrl_rec in alts_map:
                    alt_name = alts_map[ctrl_rec]
                    matching_alt = next(
                        (a for a in ext["alternatives"] if a.get("name") == alt_name),
                        None,
                    )
                    if not matching_alt:
                        raise ValueError(
                            f"Bundle '{bundle_id}' tier '{tier_name}' references "
                            f"alt '{alt_name}' for {ctrl_rec}, but no such alt exists"
                        )
                    swapped = swap_alt_value(
                        ext["setting"], matching_alt, target_child_sid=target_child_sid
                    )
                else:
                    # Baseline tier — apply baseline_value override if defined,
                    # else keep the source value as-is.
                    if ctrl and ctrl.get("baseline_value"):
                        fake_alt = {
                            "name": "_baseline",
                            "settingValue": ctrl["baseline_value"],
                        }
                        swapped = swap_alt_value(
                            ext["setting"], fake_alt, target_child_sid=target_child_sid
                        )
                    else:
                        swapped = copy.deepcopy(ext["setting"])

                # Renumber id to position in the bundle's settings array
                swapped = copy.deepcopy(swapped)
                swapped["id"] = str(len(settings))
                settings.append(swapped)

            policy_name = f"{bundle['name']} - {tier_name}"
            policy = build_output_policy(
                name=policy_name,
                description=f"{bundle.get('description', '')} ({tier_name})".strip(),
                source_policy=source_policy,
                scope_tag=scope_tag,
                settings=settings,
            )

            safe_name = _sanitize_filename(policy_name)
            rel_path = f"exceptionable/{safe_name}.json"
            full_path = Path(output_dir) / rel_path

            if not dry_run:
                full_path.parent.mkdir(parents=True, exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    json.dump(policy, f, indent=2, ensure_ascii=False)

            # First tier (Baseline) is the assigned exceptionable policy;
            # subsequent tiers are alternatives with no default assignment.
            entry_type = "exceptionable" if tier_idx == 0 else "alternative"
            manifest_entries.append({
                "file": rel_path,
                "type": entry_type,
                "assignTo": assignment_group if tier_idx == 0 else None,
            })

    return manifest_entries


def main(
    path: str,
    config_path: str,
    output_dir: str = "./output",
    dry_run: bool = False,
) -> None:
    """Main entry point: process all files and write manifest."""
    config, lookup, bundle_lookup = load_config(config_path)

    # Resolve input files
    p = Path(path)
    if p.is_dir():
        json_files = sorted(p.rglob("*.json"))
    else:
        json_files = [p]

    if not json_files:
        print(f"No JSON files found at: {path}")
        return

    assignment_group = config.get("assignmentGroup")
    autopilot_assignment_group = config.get("autopilotAssignmentGroup", assignment_group)

    print(f"Loaded config: {len(lookup)} controls")
    if assignment_group:
        print(f"Assignment group: {assignment_group}")
    if autopilot_assignment_group and autopilot_assignment_group != assignment_group:
        print(f"Autopilot assignment group: {autopilot_assignment_group}")
    print(f"Found {len(json_files)} JSON file(s)")

    all_manifest = []
    bundled_extracts: dict[str, list[dict]] = {}

    for jf in json_files:
        entries = process_file(
            str(jf),
            config,
            lookup,
            output_dir,
            assignment_group,
            autopilot_assignment_group,
            dry_run,
            bundle_lookup=bundle_lookup,
            bundled_extracts=bundled_extracts,
        )
        all_manifest.extend(entries)

    # Emit bundles (one policy per tier, combining all bundle members).
    bundle_entries = emit_bundles(
        config.get("bundles", {}),
        bundled_extracts,
        config,
        lookup,
        output_dir,
        assignment_group,
        dry_run,
    )
    all_manifest.extend(bundle_entries)

    # Write manifest
    if not dry_run:
        manifest_path = Path(output_dir) / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(all_manifest, f, indent=2, ensure_ascii=False)
        print(f"\nManifest written: {manifest_path} ({len(all_manifest)} entries)")

    # Summary
    baselines = sum(1 for e in all_manifest if e["type"] in ("baseline", "autopilot"))
    exceptionables = sum(1 for e in all_manifest if e["type"] == "exceptionable")
    alternatives = sum(1 for e in all_manifest if e["type"] == "alternative")
    print(f"\n=== Summary ===")
    print(f"  Baselines:      {baselines}")
    print(f"  Exceptionables: {exceptionables}")
    print(f"  Alternatives:   {alternatives}")
    print(f"  Total policies: {len(all_manifest)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Split CIS Build Kit JSON files into baseline and exceptionable policies."
    )
    parser.add_argument("--path", required=True, help="Single JSON file or directory (recursed)")
    parser.add_argument("--config", required=True, help="Path to cis-control-config.json")
    parser.add_argument("--output", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files")
    args = parser.parse_args()

    main(
        path=args.path,
        config_path=args.config,
        output_dir=args.output,
        dry_run=args.dry_run,
    )
