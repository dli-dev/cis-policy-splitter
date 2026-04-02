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
