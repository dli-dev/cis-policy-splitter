#!/usr/bin/env python3
"""
split-cis-policies.py

Reads CIS Build Kit Settings Catalog JSON files and a control decisions config,
then splits them into baseline bundles, exceptionable policies, and alternatives.
Writes output JSONs and a manifest for the PowerShell deployer.
"""

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
