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


def classify_settings(settings: list[dict], lookup: dict) -> dict:
    """Classify each top-level setting by disposition.

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
        sid = setting["settingInstance"]["settingDefinitionId"]
        ctrl = lookup.get(sid)

        if not ctrl:
            # Not in config -> accept
            baseline.append(setting)
            continue

        disposition = ctrl["disposition"]

        if disposition == "accept":
            baseline.append(setting)

        elif disposition in ("reject", "na"):
            dropped += 1

        elif disposition == "modified":
            modified = copy.deepcopy(setting)
            if ctrl["modified_value"]:
                inst = modified["settingInstance"]
                if "choiceSettingValue" in inst:
                    inst["choiceSettingValue"]["value"] = ctrl["modified_value"]
            baseline.append(modified)

        elif disposition == "exceptionable":
            extracted.append({
                "cis_rec": ctrl["cis_rec"],
                "description": ctrl["description"],
                "setting": setting,
                "alternatives": ctrl["alternatives"],
            })

    return {
        "baseline": baseline,
        "extracted": extracted,
        "dropped": dropped,
    }
