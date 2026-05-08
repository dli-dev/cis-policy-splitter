# CIS Service Proactive Remediation — Design

**Date:** 2026-04-02 | **Author:** Derek Liu + Claude

## Problem

CIS service controls (81.x) disable Windows services via registry. The current `split_cis_services.py` generates standalone disable scripts, but there's no deployer and no self-healing. Services can get re-enabled by Windows updates.

## Solution

Use Intune Proactive Remediations (detect + remediate script pairs) deployed via Graph API. Detection runs every 8 hours — if a service is re-enabled, remediation fires automatically.

## Architecture

Same two-stage pipeline as Settings Catalog policies:

```
split_cis_services.py  -->  output/  -->  Deploy-CISServices.ps1  -->  Intune
(generates PS1 pairs)      (reviewed)     (uploads as PRs via Graph)
```

## Grouping Pattern

Matches the Settings Catalog policy pattern:
- **Baseline**: All non-exceptionable/non-rejected services bundled into one PR (e.g., "CIS L1 - Services")
- **Exceptionable**: One PR per service (e.g., "CIS 81.14 - OpenSSH SSH Server")
- **Rejected**: Stripped entirely (not generated)

## Script Design

### Detect Script

```powershell
# Loop through service registry paths
# If all are Start=4 (disabled) or not installed -> exit 0 (compliant)
# If any are not Start=4 -> write names to stdout, exit 1 (non-compliant)
```

### Remediate Script

```powershell
# Loop through service registry paths
# Set Start=4 for any that aren't already disabled
# Report what was changed
```

## Manifest Format

Service manifest entries use `detectScript` + `remediateScript` instead of a single `file`:

```json
{
  "detectScript": "baseline/CIS L1 - Services - Detect.ps1",
  "remediateScript": "baseline/CIS L1 - Services - Remediate.ps1",
  "type": "service-baseline",
  "assignTo": "001i-test-security-baseline"
}
```

## Graph API

Proactive Remediations use `deviceHealthScripts`:

```
POST /beta/deviceManagement/deviceHealthScripts
```

Payload:
- `displayName`: PR name
- `description`: Service list
- `detectionScriptContent`: base64-encoded detect PS1
- `remediationScriptContent`: base64-encoded remediate PS1
- `runAsAccount`: "system"
- `runSchedule`: every 8 hours
- `roleScopeTagIds`: resolved from config
- Assignment via `/assign` endpoint (same pattern as configurationPolicies)

## Decisions

- **PR over Platform Scripts**: PRs self-heal on a schedule; platform scripts run once.
- **8-hour interval**: Balances detection speed vs. device load.
- **System context**: Services need admin rights to modify.
- **Scope tags**: Same as Settings Catalog policies (readonly for baseline, exceptionable tag for exceptions).
