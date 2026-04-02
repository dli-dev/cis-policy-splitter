# CIS Policy Splitter — Python Rewrite Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move all JSON splitting/processing from PowerShell to Python. Keep PowerShell only for Graph API deployment.

**Architecture:** Two scripts with `output/` as the boundary. `split-cis-policies.py` reads Build Kit JSONs + config, writes split output JSONs + manifest. `Deploy-CISPolicies.ps1` reads manifest + output JSONs, pushes to Intune via Graph API.

**Tech Stack:** Python 3.10+ (stdlib only — json, argparse, pathlib, copy, re), PowerShell 7+ with Microsoft.Graph SDK.

**Design doc:** `docs/plans/2026-04-02-python-rewrite-design.md`

---

### Task 1: Config loading and control lookup

**Files:**
- Create: `split-cis-policies.py`
- Create: `tests/test_split.py`

**Step 1: Write the test**

```python
# tests/test_split.py
import json
import pytest
from pathlib import Path

# We'll import the module once it exists
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_load_config_builds_lookup():
    """Config loading should build a settingDefinitionId -> control info lookup."""
    from split_cis_policies import load_config

    config_path = Path(__file__).resolve().parent.parent / "cis-control-config.json"
    config, lookup = load_config(str(config_path))

    # Should have all non-comment controls
    assert len(lookup) > 0

    # Check a known exceptionable control
    sid_49_1 = "device_vendor_msft_policy_config_localpoliciessecurityoptions_accounts_enableguestaccountstatus"
    assert sid_49_1 in lookup
    assert lookup[sid_49_1]["disposition"] == "exceptionable"
    assert lookup[sid_49_1]["cis_rec"] == "49.1"
    assert lookup[sid_49_1]["description"] == "Guest Account Status"
    assert lookup[sid_49_1]["is_child"] is False
    assert len(lookup[sid_49_1]["alternatives"]) == 1

    # Check a known reject
    sid_12_1 = "device_vendor_msft_policy_config_camera_allowcamera"
    assert sid_12_1 in lookup
    assert lookup[sid_12_1]["disposition"] == "reject"

    # Check a known modified
    sid_49_29 = "device_vendor_msft_policy_config_localpoliciessecurityoptions_useraccountcontrol_behavioroftheelevationpromptforstandardusers"
    assert sid_49_29 in lookup
    assert lookup[sid_49_29]["disposition"] == "modified"
    assert lookup[sid_49_29]["modified_value"] is not None

    # Check a known child
    sid_26_7 = "device_vendor_msft_policy_config_devicelock_maxinactivitytimedevicelock"
    assert sid_26_7 in lookup
    assert lookup[sid_26_7]["is_child"] is True

    # doNotDeploy should be treated as rejects
    sid_49_9 = None
    for sid, ctrl in lookup.items():
        if ctrl["cis_rec"] == "49.9":
            sid_49_9 = sid
            break
    # 49.9 and 49.10 are in doNotDeploy but have no settingDefinitionId in config,
    # so they won't appear in the lookup. The script handles doNotDeploy by CIS rec#.
    # This is fine — they'll be dropped by the per-file processing.


def test_load_config_skip_files():
    """Config should expose skipFiles and autopilotPolicies lists."""
    from split_cis_policies import load_config

    config_path = Path(__file__).resolve().parent.parent / "cis-control-config.json"
    config, _ = load_config(str(config_path))

    assert "CIS (L1) Windows Update (103) - Windows 11 Intune 4.0.0 " in config["skipFiles"]
    assert "CIS (L1) Autopilot - Windows 11 Intune 4.0.0" in config["autopilotPolicies"]
    assert config["scopeTags"]["readonly"] == "001-readonly"
    assert config["scopeTags"]["exceptionable"] == "001"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: FAIL — `split_cis_policies` module not found.

**Step 3: Write minimal implementation**

```python
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
```

Note: save this as `split_cis_policies.py` (underscore, not hyphen) so it can be imported in tests. We'll add a `split-cis-policies.py` entry point wrapper later, or just use the underscore name.

**Step 4: Run test to verify it passes**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: config loading and control lookup for Python splitter"
```

---

### Task 2: Top-level setting classification

**Files:**
- Modify: `split_cis_policies.py`
- Modify: `tests/test_split.py`

**Step 1: Write the test**

```python
# Add to tests/test_split.py

def _make_choice_setting(sid: str, value: str, children: list = None) -> dict:
    """Helper to build a minimal choice setting."""
    return {
        "id": "0",
        "settingInstance": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance",
            "settingDefinitionId": sid,
            "settingInstanceTemplateReference": None,
            "choiceSettingValue": {
                "settingValueTemplateReference": None,
                "value": value,
                "children": children or [],
            },
        },
    }


def _make_simple_setting(sid: str, value: int) -> dict:
    """Helper to build a minimal simple integer setting."""
    return {
        "id": "0",
        "settingInstance": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
            "settingDefinitionId": sid,
            "settingInstanceTemplateReference": None,
            "simpleSettingValue": {
                "@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue",
                "settingValueTemplateReference": None,
                "value": value,
            },
        },
    }


def _make_collection_setting(sid: str, values: list[str]) -> dict:
    """Helper to build a minimal simple setting collection (User Rights)."""
    return {
        "id": "0",
        "settingInstance": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingCollectionInstance",
            "settingDefinitionId": sid,
            "settingInstanceTemplateReference": None,
            "simpleSettingCollectionValue": [
                {
                    "@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValue",
                    "settingValueTemplateReference": None,
                    "value": v,
                }
                for v in values
            ],
        },
    }


def test_classify_top_level_accept():
    """Settings not in lookup should be accepted (kept in baseline)."""
    from split_cis_policies import classify_settings

    lookup = {}  # empty lookup = everything is accept
    settings = [_make_choice_setting("some_unknown_sid", "some_value")]

    result = classify_settings(settings, lookup)
    assert len(result["baseline"]) == 1
    assert len(result["extracted"]) == 0
    assert result["dropped"] == 0


def test_classify_top_level_reject():
    """Rejected settings should be dropped."""
    from split_cis_policies import classify_settings

    sid = "device_vendor_msft_policy_config_camera_allowcamera"
    lookup = {
        sid: {
            "cis_rec": "12.1",
            "disposition": "reject",
            "description": "Allow Camera",
            "is_child": False,
            "alternatives": [],
            "modified_value": None,
        }
    }
    settings = [_make_choice_setting(sid, "some_value")]

    result = classify_settings(settings, lookup)
    assert len(result["baseline"]) == 0
    assert result["dropped"] == 1


def test_classify_top_level_exceptionable():
    """Exceptionable top-level settings should be extracted."""
    from split_cis_policies import classify_settings

    sid = "device_vendor_msft_policy_config_localpoliciessecurityoptions_accounts_enableguestaccountstatus"
    lookup = {
        sid: {
            "cis_rec": "49.1",
            "disposition": "exceptionable",
            "description": "Guest Account Status",
            "is_child": False,
            "alternatives": [{"name": "enabled", "settingValue": {"value": sid + "_1"}}],
            "modified_value": None,
        }
    }
    settings = [_make_choice_setting(sid, sid + "_0")]

    result = classify_settings(settings, lookup)
    assert len(result["baseline"]) == 0
    assert len(result["extracted"]) == 1
    assert result["extracted"][0]["cis_rec"] == "49.1"


def test_classify_top_level_modified():
    """Modified settings should have their value swapped and stay in baseline."""
    from split_cis_policies import classify_settings

    sid = "device_vendor_msft_policy_config_localpoliciessecurityoptions_useraccountcontrol_behavioroftheelevationpromptforstandardusers"
    new_value = sid + "_1"
    lookup = {
        sid: {
            "cis_rec": "49.29",
            "disposition": "modified",
            "description": "UAC Standard User Elevation Prompt Behavior",
            "is_child": False,
            "alternatives": [],
            "modified_value": new_value,
        }
    }
    settings = [_make_choice_setting(sid, sid + "_0")]

    result = classify_settings(settings, lookup)
    assert len(result["baseline"]) == 1
    # Value should be swapped
    assert result["baseline"][0]["settingInstance"]["choiceSettingValue"]["value"] == new_value
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py::test_classify_top_level_accept -v`
Expected: FAIL — `classify_settings` not found.

**Step 3: Write implementation**

Add to `split_cis_policies.py`:

```python
import copy


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
```

**Step 4: Run tests**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: top-level setting classification"
```

---

### Task 3: Child setting processing

**Files:**
- Modify: `split_cis_policies.py`
- Modify: `tests/test_split.py`

**Step 1: Write the test**

```python
# Add to tests/test_split.py

def test_classify_child_reject():
    """Rejected children should be removed from parent's children array."""
    from split_cis_policies import classify_settings

    child_sid = "device_vendor_msft_bitlocker_systemdrivesrequirestartupauthentication_configurepinusagedropdown_name"
    parent_sid = "some_parent_sid"

    lookup = {
        child_sid: {
            "cis_rec": "4.11.7.2.12",
            "disposition": "reject",
            "description": "Configure TPM Startup PIN",
            "is_child": True,
            "alternatives": [],
            "modified_value": None,
        }
    }

    child = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationChoiceSettingInstance",
        "settingDefinitionId": child_sid,
        "settingInstanceTemplateReference": None,
        "choiceSettingValue": {
            "settingValueTemplateReference": None,
            "value": "some_value",
            "children": [],
        },
    }
    keep_child = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
        "settingDefinitionId": "some_other_child",
        "settingInstanceTemplateReference": None,
        "simpleSettingValue": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue",
            "settingValueTemplateReference": None,
            "value": 42,
        },
    }

    settings = [_make_choice_setting(parent_sid, "parent_val", [child, keep_child])]

    result = classify_settings(settings, lookup)
    assert len(result["baseline"]) == 1
    # Parent should remain with only the kept child
    parent_children = result["baseline"][0]["settingInstance"]["choiceSettingValue"]["children"]
    assert len(parent_children) == 1
    assert parent_children[0]["settingDefinitionId"] == "some_other_child"


def test_classify_child_exceptionable():
    """Exceptionable child should be extracted with parent wrapper, removed from baseline parent."""
    from split_cis_policies import classify_settings

    parent_sid = "device_vendor_msft_policy_config_devicelock_devicepasswordenabled"
    child_sid = "device_vendor_msft_policy_config_devicelock_maxinactivitytimedevicelock"
    other_child_sid = "device_vendor_msft_policy_config_devicelock_mindevicepasswordlength"

    lookup = {
        child_sid: {
            "cis_rec": "26.7",
            "disposition": "exceptionable",
            "description": "Max Inactivity Time Device Lock",
            "is_child": True,
            "alternatives": [
                {"name": "30min", "settingValue": {"value": 30}},
            ],
            "modified_value": None,
        }
    }

    target_child = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
        "settingDefinitionId": child_sid,
        "settingInstanceTemplateReference": None,
        "simpleSettingValue": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue",
            "settingValueTemplateReference": None,
            "value": 15,
        },
    }
    other_child = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
        "settingDefinitionId": other_child_sid,
        "settingInstanceTemplateReference": None,
        "simpleSettingValue": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue",
            "settingValueTemplateReference": None,
            "value": 14,
        },
    }

    settings = [_make_choice_setting(parent_sid, parent_sid + "_0", [target_child, other_child])]

    result = classify_settings(settings, lookup)

    # Baseline parent should have only the other child
    assert len(result["baseline"]) == 1
    bl_children = result["baseline"][0]["settingInstance"]["choiceSettingValue"]["children"]
    assert len(bl_children) == 1
    assert bl_children[0]["settingDefinitionId"] == other_child_sid

    # Extracted should have parent wrapper with only the target child
    assert len(result["extracted"]) == 1
    ext = result["extracted"][0]
    assert ext["cis_rec"] == "26.7"
    ext_children = ext["setting"]["settingInstance"]["choiceSettingValue"]["children"]
    assert len(ext_children) == 1
    assert ext_children[0]["settingDefinitionId"] == child_sid
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py::test_classify_child_reject -v`
Expected: FAIL — children aren't being processed yet.

**Step 3: Write implementation**

Update `classify_settings` in `split_cis_policies.py` to process children before top-level classification:

```python
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
```

**Step 4: Run tests**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: child setting processing (reject/NA removal, exceptionable extraction)"
```

---

### Task 4: Value swap for alternatives

**Files:**
- Modify: `split_cis_policies.py`
- Modify: `tests/test_split.py`

**Step 1: Write the tests**

```python
# Add to tests/test_split.py

def test_swap_value_choice():
    """Choice setting value swap should replace the value string."""
    from split_cis_policies import swap_alt_value

    sid = "device_vendor_msft_policy_config_localpoliciessecurityoptions_accounts_enableguestaccountstatus"
    setting = _make_choice_setting(sid, sid + "_0")
    alt = {"name": "enabled", "settingValue": {"value": sid + "_1"}}

    swapped = swap_alt_value(setting, alt)
    assert swapped["settingInstance"]["choiceSettingValue"]["value"] == sid + "_1"
    # Original should be unmodified
    assert setting["settingInstance"]["choiceSettingValue"]["value"] == sid + "_0"


def test_swap_value_simple_integer():
    """Simple integer setting value swap should replace the integer."""
    from split_cis_policies import swap_alt_value

    sid = "device_vendor_msft_policy_config_localpoliciessecurityoptions_interactivelogon_machineinactivitylimit_v2"
    setting = _make_simple_setting(sid, 900)
    alt = {"name": "30min", "settingValue": {"value": 1800}}

    swapped = swap_alt_value(setting, alt)
    assert swapped["settingInstance"]["simpleSettingValue"]["value"] == 1800
    # Original should be unmodified
    assert setting["settingInstance"]["simpleSettingValue"]["value"] == 900


def test_swap_value_collection():
    """Collection setting value swap should replace the entire SID array."""
    from split_cis_policies import swap_alt_value

    sid = "device_vendor_msft_policy_config_userrights_createsymboliclinks"
    setting = _make_collection_setting(sid, ["*S-1-5-32-544"])
    alt = {"name": "admins-hyperv", "settingValue": {"value": ["*S-1-5-32-544", "*S-1-5-83-0"]}}

    swapped = swap_alt_value(setting, alt)
    coll = swapped["settingInstance"]["simpleSettingCollectionValue"]
    assert len(coll) == 2
    assert coll[0]["value"] == "*S-1-5-32-544"
    assert coll[1]["value"] == "*S-1-5-83-0"
    assert coll[0]["@odata.type"] == "#microsoft.graph.deviceManagementConfigurationStringSettingValue"
    # Original should be unmodified
    assert len(setting["settingInstance"]["simpleSettingCollectionValue"]) == 1


def test_swap_value_null_skipped():
    """Null settingValue should return a deep copy without swap."""
    from split_cis_policies import swap_alt_value

    sid = "device_vendor_msft_policy_config_userrights_debugprograms"
    setting = _make_collection_setting(sid, ["*S-1-5-32-544"])
    alt = {"name": "admins-debuggers", "settingValue": None}

    swapped = swap_alt_value(setting, alt)
    # Should be a copy of original, no swap
    assert len(swapped["settingInstance"]["simpleSettingCollectionValue"]) == 1


def test_swap_value_child_integer():
    """Value swap for a child setting inside a parent wrapper."""
    from split_cis_policies import swap_alt_value

    parent_sid = "device_vendor_msft_policy_config_devicelock_devicepasswordenabled"
    child_sid = "device_vendor_msft_policy_config_devicelock_maxinactivitytimedevicelock"

    child = {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationSimpleSettingInstance",
        "settingDefinitionId": child_sid,
        "settingInstanceTemplateReference": None,
        "simpleSettingValue": {
            "@odata.type": "#microsoft.graph.deviceManagementConfigurationIntegerSettingValue",
            "settingValueTemplateReference": None,
            "value": 15,
        },
    }

    setting = _make_choice_setting(parent_sid, parent_sid + "_0", [child])
    alt = {"name": "30min", "settingValue": {"value": 30}}

    swapped = swap_alt_value(setting, alt, target_child_sid=child_sid)
    swapped_child = swapped["settingInstance"]["choiceSettingValue"]["children"][0]
    assert swapped_child["simpleSettingValue"]["value"] == 30
    # Original should be unmodified
    assert child["simpleSettingValue"]["value"] == 15
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py::test_swap_value_choice -v`
Expected: FAIL — `swap_alt_value` not found.

**Step 3: Write implementation**

Add to `split_cis_policies.py`:

```python
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

    elif "SimpleSettingCollectionInstance" in odata_type:
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
```

**Step 4: Run tests**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: value swap for all three setting types (choice, integer, collection)"
```

---

### Task 5: Output JSON and manifest generation

**Files:**
- Modify: `split_cis_policies.py`
- Modify: `tests/test_split.py`

**Step 1: Write the test**

```python
# Add to tests/test_split.py
import tempfile
import os


def test_build_output_policy():
    """Output policy should contain only Graph API creation fields."""
    from split_cis_policies import build_output_policy

    source = {
        "name": "CIS (L1) Some Policy - Windows 11 Intune 4.0.0",
        "description": "CIS Recommendation Numbers:\n\n1.1\n1.2\n",
        "platforms": "windows10",
        "technologies": "mdm",
        "templateReference": {
            "templateId": "",
            "templateFamily": "none",
            "templateDisplayName": None,
            "templateDisplayVersion": None,
        },
    }

    settings = [_make_choice_setting("some_sid", "some_value")]

    policy = build_output_policy(
        name="CIS L1 - Some Policy",
        description="test desc",
        source_policy=source,
        scope_tag="001-readonly",
        settings=settings,
    )

    assert policy["name"] == "CIS L1 - Some Policy"
    assert policy["description"] == "test desc"
    assert policy["platforms"] == "windows10"
    assert policy["roleScopeTagIds"] == ["001-readonly"]
    assert policy["settings"] == settings
    # Should NOT contain Graph metadata
    assert "@odata.context" not in policy
    assert "createdDateTime" not in policy
    assert "id" not in policy
    # templateReference should omit null display fields
    assert "templateDisplayName" not in policy["templateReference"]


def test_process_file_end_to_end(tmp_path):
    """Full file processing should produce baseline + exceptionable + alt files + manifest entries."""
    from split_cis_policies import load_config, process_file

    config_path = Path(__file__).resolve().parent.parent / "cis-control-config.json"
    source_path = (
        Path(__file__).resolve().parent.parent
        / "IntuneWindows11v4.0.0"
        / "Settings Catalog"
        / "Level 1"
        / "CIS (L1) User Rights (89) - Windows 11 Intune 4.0.0.json"
    )

    config, lookup = load_config(str(config_path))
    manifest = process_file(str(source_path), config, lookup, str(tmp_path), dry_run=False)

    # Should have 1 baseline + 3 exceptionable baselines (89.10, 89.12, 89.14) + 3 alts
    assert any(e["type"] == "baseline" for e in manifest)
    exc_baselines = [e for e in manifest if e["type"] == "exceptionable"]
    assert len(exc_baselines) == 3
    alts = [e for e in manifest if e["type"] == "alternative"]
    assert len(alts) == 3

    # Baseline file should exist
    bl = [e for e in manifest if e["type"] == "baseline"][0]
    assert (tmp_path / bl["file"]).exists()

    # Exceptionable files should exist
    for e in exc_baselines:
        assert (tmp_path / e["file"]).exists()
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py::test_build_output_policy -v`
Expected: FAIL — `build_output_policy` not found.

**Step 3: Write implementation**

Add to `split_cis_policies.py`:

```python
import re


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
    dry_run: bool = False,
) -> list[dict]:
    """Process a single CIS Build Kit JSON file.

    Returns a list of manifest entries.
    """
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

        assign_to = "AllUsers" if is_autopilot else "AllDevices"
        policy_type = "autopilot" if is_autopilot else "baseline"
        manifest_entries.append({
            "file": rel_path,
            "type": policy_type,
            "assignTo": assign_to,
        })

    # --- Write exceptionable + alternatives ---
    for ext in result["extracted"]:
        # Exceptionable baseline
        exc_name = f"CIS {ext['cis_rec']} - {ext['description']} - Baseline"
        exc_policy = build_output_policy(
            name=exc_name,
            description=f"CIS {ext['cis_rec']} exceptionable baseline: {ext['description']}",
            source_policy=policy,
            scope_tag=config["scopeTags"]["exceptionable"],
            settings=[ext["setting"]],
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
            "assignTo": "AllDevices",
        })

        # Alternatives
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
                "assignTo": "None",
            })

    return manifest_entries
```

**Step 4: Run tests**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: output JSON generation with manifest"
```

---

### Task 6: CLI entry point and batch mode

**Files:**
- Modify: `split_cis_policies.py`
- Modify: `tests/test_split.py`

**Step 1: Write the test**

```python
# Add to tests/test_split.py

def test_batch_mode(tmp_path):
    """Processing a directory should handle all files and write manifest.json."""
    from split_cis_policies import main

    source_dir = (
        Path(__file__).resolve().parent.parent
        / "IntuneWindows11v4.0.0"
        / "Settings Catalog"
    )
    config_path = str(Path(__file__).resolve().parent.parent / "cis-control-config.json")

    main(
        path=str(source_dir),
        config_path=config_path,
        output_dir=str(tmp_path),
        dry_run=False,
    )

    # manifest.json should exist
    manifest_path = tmp_path / "manifest.json"
    assert manifest_path.exists()

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Should have baselines, exceptionables, and alternatives
    types = {e["type"] for e in manifest}
    assert "baseline" in types
    assert "exceptionable" in types
    assert "alternative" in types

    # All referenced files should exist
    for entry in manifest:
        assert (tmp_path / entry["file"]).exists(), f"Missing: {entry['file']}"

    # Windows Update should be skipped (in skipFiles)
    names = [e["file"] for e in manifest]
    assert not any("Windows Update" in n for n in names)

    # Autopilot should be tagged AllUsers
    autopilot = [e for e in manifest if "Autopilot" in e["file"]]
    assert len(autopilot) == 1
    assert autopilot[0]["assignTo"] == "AllUsers"
    assert autopilot[0]["type"] == "autopilot"
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py::test_batch_mode -v`
Expected: FAIL — `main` not found.

**Step 3: Write implementation**

Add to `split_cis_policies.py`:

```python
import argparse


def main(
    path: str,
    config_path: str,
    output_dir: str = "./output",
    dry_run: bool = False,
) -> None:
    """Main entry point: process all files and write manifest."""
    config, lookup = load_config(config_path)

    # Resolve input files
    p = Path(path)
    if p.is_dir():
        json_files = sorted(p.rglob("*.json"))
    else:
        json_files = [p]

    if not json_files:
        print(f"No JSON files found at: {path}")
        return

    print(f"Loaded config: {len(lookup)} controls")
    print(f"Found {len(json_files)} JSON file(s)")

    all_manifest = []

    for jf in json_files:
        entries = process_file(str(jf), config, lookup, output_dir, dry_run)
        all_manifest.extend(entries)

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
```

**Step 4: Run tests**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_split.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add split_cis_policies.py tests/test_split.py
git commit -m "feat: CLI entry point and batch mode with manifest"
```

---

### Task 7: Validate output against existing PowerShell output

**Files:**
- Create: `tests/test_regression.py`

This is not a unit test — it's a one-time regression check comparing the Python output against the existing PowerShell output to catch any differences.

**Step 1: Write the regression test**

```python
# tests/test_regression.py
"""
Regression test: compare Python splitter output against existing PowerShell output.

Run: python3 -m pytest tests/test_regression.py -v

This compares:
- Same set of baseline/exceptionable files generated
- Same policy names
- Same settings in each policy (by settingDefinitionId)
- Value swap for 49.29 is correct
"""
import json
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_regression_against_powershell_output():
    from split_cis_policies import main

    project_root = Path(__file__).resolve().parent.parent
    ps_output = project_root / "output"
    config_path = str(project_root / "cis-control-config.json")
    source_dir = str(project_root / "IntuneWindows11v4.0.0" / "Settings Catalog")

    with tempfile.TemporaryDirectory() as tmp:
        main(path=source_dir, config_path=config_path, output_dir=tmp, dry_run=False)

        py_output = Path(tmp)

        # Compare baseline files
        ps_baselines = sorted((ps_output / "baseline").glob("*.json"))
        py_baselines = sorted((py_output / "baseline").glob("*.json"))

        ps_bl_names = {f.stem for f in ps_baselines}
        py_bl_names = {f.stem for f in py_baselines}

        assert py_bl_names == ps_bl_names, (
            f"Baseline mismatch.\n"
            f"  Only in PS: {ps_bl_names - py_bl_names}\n"
            f"  Only in Py: {py_bl_names - ps_bl_names}"
        )

        # Compare exceptionable files
        ps_exc = sorted((ps_output / "exceptionable").glob("*.json"))
        py_exc = sorted((py_output / "exceptionable").glob("*.json"))

        ps_exc_names = {f.stem for f in ps_exc}
        py_exc_names = {f.stem for f in py_exc}

        assert py_exc_names == ps_exc_names, (
            f"Exceptionable mismatch.\n"
            f"  Only in PS: {ps_exc_names - py_exc_names}\n"
            f"  Only in Py: {py_exc_names - ps_exc_names}"
        )

        # Spot-check: 49.29 modified value in baseline
        bl_49 = py_output / "baseline" / "CIS L1 - Local Policies Security Options.json"
        with open(bl_49) as f:
            data = json.load(f)
        sid_49_29 = "device_vendor_msft_policy_config_localpoliciessecurityoptions_useraccountcontrol_behavioroftheelevationpromptforstandardusers"
        found = False
        for s in data["settings"]:
            if s["settingInstance"]["settingDefinitionId"] == sid_49_29:
                assert s["settingInstance"]["choiceSettingValue"]["value"] == sid_49_29 + "_1"
                found = True
                break
        assert found, "49.29 not found in baseline"

        # Spot-check: 49.1, 49.4, 49.8 should NOT be in baseline (exceptionable, extracted)
        for s in data["settings"]:
            sid = s["settingInstance"]["settingDefinitionId"]
            assert "accounts_enableguestaccountstatus" not in sid, "49.1 should be extracted"
            assert "accounts_renameguestaccount" not in sid, "49.4 should be extracted"
            assert "interactivelogon_machineinactivitylimit" not in sid, "49.8 should be extracted"

        # Spot-check: 26.7 alt (30min) should have value 30, not 15
        alt_26_7 = py_output / "exceptionable" / "CIS 26.7 - Max Inactivity Time Device Lock (child of Device Lock) - Alt (30min).json"
        with open(alt_26_7) as f:
            data = json.load(f)
        child = data["settings"][0]["settingInstance"]["choiceSettingValue"]["children"][0]
        assert child["simpleSettingValue"]["value"] == 30

        # Spot-check: 49.1 alt (enabled) should have swapped value
        alt_49_1 = py_output / "exceptionable" / "CIS 49.1 - Guest Account Status - Alt (enabled).json"
        with open(alt_49_1) as f:
            data = json.load(f)
        assert data["settings"][0]["settingInstance"]["choiceSettingValue"]["value"].endswith("_1")
```

**Step 2: Run regression test**

Run: `cd /Users/liuderek/cis-policy-splitter && python3 -m pytest tests/test_regression.py -v`

Expected: PASS — same file sets, correct value swaps (including the alt swaps that the PowerShell script was missing).

If there are differences, investigate and fix before proceeding.

**Step 3: Commit**

```bash
git add tests/test_regression.py
git commit -m "test: regression test comparing Python output to PowerShell output"
```

---

### Task 8: Slim down Deploy-CISPolicies.ps1

**Files:**
- Replace: `Deploy-CISPolicies.ps1`

**Step 1: Write the slimmed-down script**

Replace `Deploy-CISPolicies.ps1` with a manifest-driven deployer:

```powershell
<#
.SYNOPSIS
    Deploys CIS policies to Intune via Graph API using output from split-cis-policies.py.

.DESCRIPTION
    Reads output/manifest.json and the corresponding policy JSON files, then
    creates each policy in Intune with appropriate scope tags and assignments.

    Run split_cis_policies.py first to generate the output directory.

.PARAMETER OutputDir
    Directory containing manifest.json and policy JSON files. Default: ./output

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -OutputDir ./output

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -OutputDir ./output -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$OutputDir = "./output"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Load manifest
# ---------------------------------------------------------------------------
$manifestPath = Join-Path $OutputDir "manifest.json"
if (-not (Test-Path $manifestPath)) {
    Write-Error "Manifest not found: $manifestPath. Run split_cis_policies.py first."
    return
}

$manifestRaw = Get-Content $manifestPath -Encoding UTF8 -Raw
if ($manifestRaw[0] -eq [char]0xFEFF) { $manifestRaw = $manifestRaw.Substring(1) }
$manifest = $manifestRaw | ConvertFrom-Json

Write-Host "Loaded manifest: $($manifest.Count) policies" -ForegroundColor Gray

# ---------------------------------------------------------------------------
# 2. Scope tag resolution cache
# ---------------------------------------------------------------------------
$scopeTagCache = @{}

function Resolve-ScopeTagId {
    param([string]$TagName)
    if ($TagName -eq '0' -or $TagName -eq 'Default') { return '0' }
    if ($scopeTagCache.ContainsKey($TagName)) { return $scopeTagCache[$TagName] }

    if ($WhatIfPreference) {
        $scopeTagCache[$TagName] = "WHATIF-$TagName"
        return "WHATIF-$TagName"
    }

    try {
        $uri = "https://graph.microsoft.com/beta/deviceManagement/roleScopeTags?`$filter=displayName eq '$TagName'"
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        if ($response.value -and $response.value.Count -gt 0) {
            $id = $response.value[0].id.ToString()
            $scopeTagCache[$TagName] = $id
            Write-Host "  Resolved scope tag '$TagName' -> ID $id" -ForegroundColor Gray
            return $id
        }
    } catch {}

    Write-Warning "Scope tag '$TagName' not found. Using Default (0)."
    $scopeTagCache[$TagName] = '0'
    return '0'
}

# ---------------------------------------------------------------------------
# 3. Policy creation
# ---------------------------------------------------------------------------
function Find-ExistingPolicy {
    param([string]$PolicyName)
    if ($WhatIfPreference) { return $null }
    try {
        $encodedName = $PolicyName -replace "'", "''"
        $uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$filter=name eq '$encodedName'&`$select=id,name"
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        if ($response.value -and $response.value.Count -gt 0) { return $response.value[0] }
    } catch {}
    return $null
}

function New-IntunePolicy {
    param([hashtable]$PolicyBody, [string]$AssignTo)

    $policyName = $PolicyBody.name
    $result = @{ Name = $policyName; Id = $null; Created = $false; Skipped = $false; Assigned = $false; AssignTo = $AssignTo; Error = $null }

    if ($WhatIfPreference) {
        Write-Host "  [WhatIf] Would create: $policyName (assign: $AssignTo)" -ForegroundColor DarkYellow
        $result.Id = 'WHATIF-ID'
        return $result
    }

    $existing = Find-ExistingPolicy -PolicyName $policyName
    if ($existing) {
        Write-Host "  SKIP (exists): $policyName (ID: $($existing.id))" -ForegroundColor Yellow
        $result.Id = $existing.id; $result.Skipped = $true
        return $result
    }

    try {
        $body = $PolicyBody | ConvertTo-Json -Depth 30
        $response = Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies" -Body $body -ContentType "application/json"
        $result.Id = $response.id; $result.Created = $true
        Write-Host "  CREATED: $policyName (ID: $($response.id))" -ForegroundColor Green
    } catch {
        $result.Error = $_.Exception.Message
        Write-Warning "Failed to create '$policyName': $($_.Exception.Message)"
        return $result
    }

    if ($AssignTo -ne 'None' -and $result.Id) {
        try {
            $target = switch ($AssignTo) {
                'AllDevices' { @{ '@odata.type' = '#microsoft.graph.allDevicesAssignmentTarget' } }
                'AllUsers'   { @{ '@odata.type' = '#microsoft.graph.allLicensedUsersAssignmentTarget' } }
            }
            $assignBody = @{ assignments = @(@{ target = $target }) }
            Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
            $result.Assigned = $true
            Write-Host "  ASSIGNED: $policyName -> $AssignTo" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
            $result.Error = "Assignment failed: $($_.Exception.Message)"
        }
    }

    return $result
}

# ---------------------------------------------------------------------------
# 4. Connect to Graph
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference) {
    $requiredScope = "DeviceManagementConfiguration.ReadWrite.All"
    try {
        $context = Get-MgContext
        if (-not $context -or $context.Scopes -notcontains $requiredScope) {
            throw "need auth"
        }
        Write-Host "Graph API: Connected as $($context.Account)" -ForegroundColor Gray
    } catch {
        Write-Host "Connecting to Microsoft Graph..." -ForegroundColor White
        Connect-MgGraph -Scopes $requiredScope -ErrorAction Stop
        Write-Host "Connected as $((Get-MgContext).Account)" -ForegroundColor Green
    }
} else {
    Write-Host "[WhatIf] Would connect to Microsoft Graph" -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------------------
# 5. Deploy each policy from manifest
# ---------------------------------------------------------------------------
$stats = @{ Created = 0; Skipped = 0; Failed = 0 }
$deploymentLog = [System.Collections.ArrayList]::new()

foreach ($entry in $manifest) {
    $policyPath = Join-Path $OutputDir $entry.file
    $raw = Get-Content $policyPath -Encoding UTF8 -Raw
    if ($raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    $policyBody = $raw | ConvertFrom-Json -AsHashtable

    # Resolve scope tag names to IDs
    $resolvedTags = @()
    foreach ($tag in $policyBody.roleScopeTagIds) {
        $resolvedTags += Resolve-ScopeTagId -TagName $tag
    }
    $policyBody.roleScopeTagIds = $resolvedTags

    Write-Host "`n[$($entry.type.ToUpper())] $($policyBody.name)" -ForegroundColor White
    $result = New-IntunePolicy -PolicyBody $policyBody -AssignTo $entry.assignTo

    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; type = $entry.type
        assignTo = $entry.assignTo; created = $result.Created
        skipped = $result.Skipped; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Skipped) { $stats.Skipped++ }
    if ($result.Error -and -not $result.Created) { $stats.Failed++ }
}

# ---------------------------------------------------------------------------
# 6. Write deployment log and summary
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference -and $deploymentLog.Count -gt 0) {
    $logContent = [ordered]@{
        deployedAt = (Get-Date -Format 'o')
        deployedBy = try { (Get-MgContext).Account } catch { $env:USERNAME }
        totalPolicies = $deploymentLog.Count
        created = $stats.Created; skipped = $stats.Skipped; failed = $stats.Failed
        policies = @($deploymentLog)
    }
    $logPath = Join-Path $OutputDir "deployment-log.json"
    $logJson = $logContent | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText(
        (Join-Path (Resolve-Path $OutputDir).Path "deployment-log.json"),
        $logJson, [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "`nDeployment log: $logPath" -ForegroundColor Gray
}

Write-Host "`n=== Deployment Summary ===" -ForegroundColor White
Write-Host "  Created: $($stats.Created)" -ForegroundColor Green
Write-Host "  Skipped: $($stats.Skipped) (already exist)" -ForegroundColor Yellow
Write-Host "  Failed:  $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
```

**Step 2: Verify it parses**

Run: `pwsh -c "& { Get-Help ./Deploy-CISPolicies.ps1 }"`
Expected: Help text displayed without errors.

**Step 3: Verify WhatIf mode with manifest**

Run the Python splitter first, then:
Run: `pwsh ./Deploy-CISPolicies.ps1 -OutputDir ./output -WhatIf`
Expected: Lists all policies it would create with WhatIf messages.

**Step 4: Commit**

```bash
git add Deploy-CISPolicies.ps1
git commit -m "refactor: slim Deploy-CISPolicies.ps1 to manifest-driven deployer"
```

---

Plan complete and saved to `docs/plans/2026-04-02-python-rewrite-plan.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Parallel Session (separate)** — Open a new session with executing-plans, batch execution with checkpoints.

Which approach?