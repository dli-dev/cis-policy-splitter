#!/usr/bin/env python3
"""
split-cis-policies.py

Reads CIS Build Kit Settings Catalog JSON files and a control decisions config,
then splits them into baseline bundles, exceptionable policies, and alternatives.
Writes output JSONs and a manifest for the PowerShell deployer.
"""

import copy
import json
from pathlib import Path


def load_config(config_path: str) -> tuple[dict, dict]:
    """Load the control decisions config and build a settingDefinitionId lookup.

    Returns:
        (config, lookup) where:
        - config is the raw config dict
        - lookup maps settingDefinitionId -> control info dict
    """
    with open(config_path, encoding="utf-8-sig") as f:
        config = json.load(f)

    # Build doNotDeploy set for treating as rejects
    do_not_deploy = set(config.get("doNotDeploy", []))

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
        }

    return config, lookup


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
