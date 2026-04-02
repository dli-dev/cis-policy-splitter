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


def test_generate_service_script():
    """Generated script should contain correct hashtable entries."""
    from split_cis_services import generate_service_script

    services = {
        "Computer Browser": r"HKLM:\SYSTEM\CurrentControlSet\Services\Browser",
        "IIS Admin Service": r"HKLM:\SYSTEM\CurrentControlSet\Services\IISADMIN",
    }

    script = generate_service_script(services, "L1", "CIS L1 - Services Baseline")
    assert "$Services = @{" in script
    assert "'Computer Browser'" in script
    assert "'IIS Admin Service'" in script
    assert "#Requires -RunAsAdministrator" in script


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

    # Should have baseline scripts
    assert (tmp_path / "baseline" / "CIS L1 - Services.ps1").exists()
    assert (tmp_path / "baseline" / "CIS L2 - Services.ps1").exists()

    # L1 baseline should NOT contain sshd or RemoteAccess (exceptionable)
    l1_text = (tmp_path / "baseline" / "CIS L1 - Services.ps1").read_text()
    assert "sshd" not in l1_text
    assert "RemoteAccess" not in l1_text
    # But should contain other services
    assert "Browser" in l1_text

    # L2 baseline should NOT contain rejects or exceptionables
    l2_text = (tmp_path / "baseline" / "CIS L2 - Services.ps1").read_text()
    assert "BTAGService" not in l2_text  # reject
    assert "bthserv" not in l2_text      # reject
    assert "lfsvc" not in l2_text        # reject
    assert "MSiSCSI" not in l2_text      # exceptionable
    assert "SessionEnv" not in l2_text   # exceptionable
    assert "TermService" not in l2_text  # exceptionable
    # But should contain accepted services
    assert "MapsBroker" in l2_text

    # Should have exceptionable scripts
    assert (tmp_path / "exceptionable" / "CIS 81.14 - OpenSSH SSH Server.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.23 - Routing and Remote Access.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.13 - Microsoft iSCSI Initiator Service.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.17 - Remote Desktop Configuration.ps1").exists()
    assert (tmp_path / "exceptionable" / "CIS 81.18 - Remote Desktop Services.ps1").exists()

    # Exceptionable script should disable just that one service
    sshd_text = (tmp_path / "exceptionable" / "CIS 81.14 - OpenSSH SSH Server.ps1").read_text()
    assert "sshd" in sshd_text
    assert "Browser" not in sshd_text  # should only have sshd

    # manifest.json should exist
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    types = {e["type"] for e in manifest}
    assert "baseline" in types
    assert "exceptionable" in types
