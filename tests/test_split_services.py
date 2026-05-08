# tests/test_split_services.py
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_parse_service_script():
    """Should parse PS1 service hashtable into {display_name: registry_key}."""
    from split_cis_services import parse_service_script

    script_path = (
        Path(__file__).resolve().parent.parent
        / "IntuneWindows11v4.0.0"
        / "System Service Scripts"
        / "Level 1"
        / "CIS Windows 11 v4.0.0 Services L1.ps1"
    )
    services = parse_service_script(str(script_path))

    assert len(services) == 20
    assert services["OpenSSH SSH Server"] == r"HKLM:\SYSTEM\CurrentControlSet\Services\sshd"
    assert services["Routing and Remote Access"] == r"HKLM:\SYSTEM\CurrentControlSet\Services\RemoteAccess"


def test_parse_service_script_l2():
    """Should parse L2 script."""
    from split_cis_services import parse_service_script

    script_path = (
        Path(__file__).resolve().parent.parent
        / "IntuneWindows11v4.0.0"
        / "System Service Scripts"
        / "Level 2"
        / "CIS Windows 11 v4.0.0 Services L2.ps1"
    )
    services = parse_service_script(str(script_path))

    assert len(services) == 22
    assert services["Bluetooth Audio Gateway Service"] == r"HKLM:\SYSTEM\CurrentControlSet\Services\BTAGService"


def test_classify_services():
    """Should split services into baseline, exceptionable, and rejected."""
    from split_cis_services import classify_services

    services = {
        "OpenSSH SSH Server": r"HKLM:\SYSTEM\CurrentControlSet\Services\sshd",
        "Computer Browser": r"HKLM:\SYSTEM\CurrentControlSet\Services\Browser",
        "Bluetooth Audio Gateway Service": r"HKLM:\SYSTEM\CurrentControlSet\Services\BTAGService",
    }
    svc_controls = {
        "sshd": {"cis_rec": "81.14", "disposition": "exceptionable", "description": "OpenSSH SSH Server"},
        "BTAGService": {"cis_rec": "81.1", "disposition": "reject", "description": "Bluetooth Audio Gateway"},
    }

    result = classify_services(services, svc_controls)

    assert len(result["baseline"]) == 1
    assert "Computer Browser" in result["baseline"]
    assert len(result["exceptionable"]) == 1
    assert result["exceptionable"][0]["display_name"] == "OpenSSH SSH Server"
    assert result["rejected"] == 1


def test_generate_detect_script():
    """Detection script should check compliance and exit 0/1."""
    from split_cis_services import generate_detect_script

    services = {
        "Computer Browser": r"HKLM:\SYSTEM\CurrentControlSet\Services\Browser",
        "IIS Admin Service": r"HKLM:\SYSTEM\CurrentControlSet\Services\IISADMIN",
    }

    script = generate_detect_script(services, "CIS L1 - Services Baseline")
    assert "$Services = @{" in script
    assert "'Computer Browser'" in script
    assert "'IIS Admin Service'" in script
    assert "exit 0" in script
    assert "exit 1" in script
    assert "$NonCompliant" in script


def test_generate_remediate_script():
    """Remediation script should disable services via Set-ItemProperty."""
    from split_cis_services import generate_remediate_script

    services = {
        "Computer Browser": r"HKLM:\SYSTEM\CurrentControlSet\Services\Browser",
        "IIS Admin Service": r"HKLM:\SYSTEM\CurrentControlSet\Services\IISADMIN",
    }

    script = generate_remediate_script(services, "CIS L1 - Services Baseline")
    assert "$Services = @{" in script
    assert "'Computer Browser'" in script
    assert "'IIS Admin Service'" in script
    assert "#Requires -RunAsAdministrator" in script
    assert "Set-ItemProperty" in script
    assert "'Start'" in script
    assert "-Value 4" in script


def test_end_to_end(tmp_path):
    """Full run should produce baseline + exceptionable scripts."""
    from split_cis_services import main

    config_path = str(Path(__file__).resolve().parent.parent / "cis-control-config.json")
    source_dir = str(
        Path(__file__).resolve().parent.parent
        / "IntuneWindows11v4.0.0"
        / "System Service Scripts"
    )

    main(source_dir=source_dir, config_path=config_path, output_dir=str(tmp_path))

    # Should have baseline detect/remediate pairs
    assert (tmp_path / "baseline" / "CIS L1 - Services - Detect.ps1").exists()
    assert (tmp_path / "baseline" / "CIS L1 - Services - Remediate.ps1").exists()
    assert (tmp_path / "baseline" / "CIS L2 - Services - Detect.ps1").exists()
    assert (tmp_path / "baseline" / "CIS L2 - Services - Remediate.ps1").exists()

    # L1 baseline detect should NOT contain sshd or RemoteAccess (exceptionable)
    l1_detect = (tmp_path / "baseline" / "CIS L1 - Services - Detect.ps1").read_text()
    assert "sshd" not in l1_detect
    assert "RemoteAccess" not in l1_detect
    assert "Browser" in l1_detect
    assert "exit 0" in l1_detect
    assert "exit 1" in l1_detect

    # L1 baseline remediate should have Set-ItemProperty
    l1_remediate = (tmp_path / "baseline" / "CIS L1 - Services - Remediate.ps1").read_text()
    assert "Set-ItemProperty" in l1_remediate
    assert "Browser" in l1_remediate

    # L2 baseline should NOT contain rejects or exceptionables
    l2_detect = (tmp_path / "baseline" / "CIS L2 - Services - Detect.ps1").read_text()
    assert "BTAGService" not in l2_detect  # reject
    assert "bthserv" not in l2_detect      # reject
    assert "lfsvc" not in l2_detect        # reject
    assert "MSiSCSI" not in l2_detect      # exceptionable
    assert "SessionEnv" not in l2_detect   # exceptionable
    assert "TermService" not in l2_detect  # exceptionable
    assert "MapsBroker" in l2_detect

    # Should have exceptionable detect/remediate pairs
    assert (tmp_path / "exceptionable" / "CIS 81.14 - OpenSSH SSH Server - Detect.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.14 - OpenSSH SSH Server - Remediate.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.23 - Routing and Remote Access - Detect.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.23 - Routing and Remote Access - Remediate.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.13 - Microsoft iSCSI Initiator Service - Detect.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.13 - Microsoft iSCSI Initiator Service - Remediate.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.17 - Remote Desktop Configuration - Detect.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.17 - Remote Desktop Configuration - Remediate.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.18 - Remote Desktop Services - Detect.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.18 - Remote Desktop Services - Remediate.ps1").exists()

    # Exceptionable detect should check just that one service
    sshd_detect = (tmp_path / "exceptionable" / "CIS 81.14 - OpenSSH SSH Server - Detect.ps1").read_text()
    assert "sshd" in sshd_detect
    assert "Browser" not in sshd_detect

    # manifest.json should exist with new format
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    types = {e["type"] for e in manifest}
    assert "service-baseline" in types
    assert "service-exceptionable" in types

    # Manifest entries should use detectScript/remediateScript keys
    for entry in manifest:
        assert "detectScript" in entry
        assert "remediateScript" in entry
        assert "file" not in entry

    # assignTo should use config value, not "AllDevices"
    for entry in manifest:
        assert entry["assignTo"] == "001i-test-security-baseline"
