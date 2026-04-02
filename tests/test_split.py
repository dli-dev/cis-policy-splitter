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
