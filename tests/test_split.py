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
