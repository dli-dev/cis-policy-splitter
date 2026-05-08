# CIS Service Proactive Remediation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Update `split_cis_services.py` to generate detect/remediate PS1 pairs, and create `Deploy-CISServices.ps1` to upload them as Proactive Remediations via Graph API.

**Design doc:** `docs/plans/2026-04-02-service-proactive-remediation-design.md`

---

### Task 1: Update splitter to generate detect/remediate pairs

**Files:**
- Modify: `split_cis_services.py`
- Modify: `tests/test_split_services.py`

**Step 1: Update `generate_service_script` to produce two scripts**

Replace the single `generate_service_script` function with two functions:

```python
def generate_detect_script(services: dict[str, str], title: str) -> str:
    """Generate a detection script that exits 1 if any service is not disabled."""
```

Detection logic:
- Build the same `$Services` hashtable
- Loop: check `Test-Path` then `(Get-ItemProperty).Start`
- If `Start` != 4 and service exists, add to `$NonCompliant` list
- If `$NonCompliant.Count -gt 0`: write the service names to stdout, `exit 1`
- Else: write "All services compliant", `exit 0`

```python
def generate_remediate_script(services: dict[str, str], title: str) -> str:
    """Generate a remediation script that disables non-compliant services."""
```

Remediation logic: same as current `generate_service_script` — set `Start` to 4 for any service not already disabled. Keep the counters and Write-Host output for logging.

**Step 2: Update output file naming**

Current: `CIS L1 - Services.ps1`
New:
- `CIS L1 - Services - Detect.ps1`
- `CIS L1 - Services - Remediate.ps1`

Same for exceptionable:
- `CIS 81.14 - OpenSSH SSH Server - Detect.ps1`
- `CIS 81.14 - OpenSSH SSH Server - Remediate.ps1`

**Step 3: Update manifest entry format**

Change from:
```json
{"file": "baseline/CIS L1 - Services.ps1", "type": "baseline", "assignTo": "AllDevices"}
```

To:
```json
{
  "detectScript": "baseline/CIS L1 - Services - Detect.ps1",
  "remediateScript": "baseline/CIS L1 - Services - Remediate.ps1",
  "type": "service-baseline",
  "assignTo": "001i-test-security-baseline"
}
```

Use `service-baseline` and `service-exceptionable` types so `Deploy-CISPolicies.ps1` manifest reader (which uses `type`) doesn't confuse them with Settings Catalog entries.

**Step 4: Use `assignmentGroup` from config instead of hardcoded "AllDevices"**

Read `config.get("assignmentGroup")` and use it for the `assignTo` field, same as `split_cis_policies.py`.

**Step 5: Update tests**

Update `tests/test_split_services.py` to verify:
- Detect script contains `exit 0` and `exit 1` paths
- Remediate script contains `Set-ItemProperty` with value 4
- Manifest entries have `detectScript` and `remediateScript` keys
- Manifest entries use `service-baseline` / `service-exceptionable` types
- `assignTo` uses config value, not "AllDevices"

**Verification:** Run `python -m pytest tests/test_split_services.py -v` — all tests pass.

---

### Task 2: Create `Deploy-CISServices.ps1`

**Files:**
- Create: `Deploy-CISServices.ps1`

**Step 1: Script structure**

Mirror `Deploy-CISPolicies.ps1` structure:

```
param(
    [Parameter(Mandatory)] [ValidateSet('QA', 'Prod')] [string]$Tenant,
    [string]$ManifestFile = "./output/manifest.json",
    [switch]$TestMode
)
```

Supports `-WhatIf` via `[CmdletBinding(SupportsShouldProcess)]`.

**Step 2: Load manifest and filter to service entries**

Read `manifest.json`, filter to entries where `type` starts with `service-`. This allows the service manifest to coexist with policy entries in the same manifest file.

If `-TestMode`, limit to first entry only (same pattern as `Delete-CISPolicies.ps1`).

**Step 3: Connect to Graph**

Same tenant resolution and connection as `Deploy-CISPolicies.ps1`:
- Scopes: `DeviceManagementConfiguration.ReadWrite.All`, `Group.Read.All`
- Disconnect/reconnect pattern

**Step 4: Scope tag resolution**

Reuse the same `Resolve-ScopeTagId` function from `Deploy-CISPolicies.ps1`. Service baseline gets `001-readonly`, exceptionable gets `001`.

Read scope tag to use from `cis-control-config.json`:
- `service-baseline` -> `config.scopeTags.readonly`
- `service-exceptionable` -> `config.scopeTags.exceptionable`

Pass `ConfigFile` as a parameter (default `./cis-control-config.json`) or read scope tags from the manifest. Simpler approach: add `scopeTag` to manifest entries in the splitter.

**Step 5: Check for existing PR by name**

```powershell
function Find-ExistingHealthScript {
    param([string]$DisplayName)
    # Note: $filter may not work here either (same as configurationPolicies)
    # If not, fetch all with $select=id,displayName and match client-side
    # Only fetch deviceHealthScripts, scoped to our PRs
}
```

If exists, skip (same "SKIP (exists)" pattern as policy deployer).

**Step 6: Create the Proactive Remediation**

For each manifest entry:

```powershell
$detectContent = Get-Content $item.DetectScript -Encoding UTF8 -Raw
$remediateContent = Get-Content $item.RemediateScript -Encoding UTF8 -Raw

$body = @{
    displayName              = $prName
    description              = "CIS service controls - generated by split_cis_services.py"
    detectionScriptContent   = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($detectContent))
    remediationScriptContent = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($remediateContent))
    runAsAccount             = "system"
    enforceSignatureCheck    = $false
    runAs32Bit               = $false
    roleScopeTagIds          = @($resolvedScopeTagId)
    isGlobalScript           = $false
}

$response = Invoke-MgGraphRequest -Method POST `
    -Uri "https://graph.microsoft.com/beta/deviceManagement/deviceHealthScripts" `
    -Body ($body | ConvertTo-Json -Depth 10) `
    -ContentType "application/json"
```

**Step 7: Set the run schedule**

After creation, PATCH the schedule:

```powershell
$scheduleBody = @{
    runSchedule = @{
        '@odata.type' = '#microsoft.graph.deviceHealthScriptHourlySchedule'
        interval      = 8
    }
}

Invoke-MgGraphRequest -Method PATCH `
    -Uri "https://graph.microsoft.com/beta/deviceManagement/deviceHealthScripts('$($response.id)')" `
    -Body ($scheduleBody | ConvertTo-Json -Depth 10) `
    -ContentType "application/json"
```

Note: Verify the exact schedule schema — it may be set during creation via `runSchedule` in the POST body, or may require a separate assignment call. Test in QA first.

**Step 8: Assign to group**

```powershell
$assignBody = @{
    deviceHealthScriptAssignments = @(@{
        target = @{
            '@odata.type' = '#microsoft.graph.groupAssignmentTarget'
            groupId       = $resolvedGroupId
        }
        runRemediationScript = $true
        runSchedule = @{
            '@odata.type' = '#microsoft.graph.deviceHealthScriptHourlySchedule'
            interval      = 8
        }
    })
}

Invoke-MgGraphRequest -Method POST `
    -Uri "https://graph.microsoft.com/beta/deviceManagement/deviceHealthScripts('$($response.id)')/assign" `
    -Body ($assignBody | ConvertTo-Json -Depth 10) `
    -ContentType "application/json"
```

**Step 9: Deployment log and summary**

Same pattern as `Deploy-CISPolicies.ps1`:
- Write `deployment-log-services.json` with created/skipped/failed counts
- Print summary with color-coded stats

**Verification:** Run with `-WhatIf` first, then deploy to QA with `-TestMode` to create one PR. Verify in Intune portal that the PR appears with correct detect/remediate scripts and 8-hour schedule.

---

### Task 3: Update launch.json

**Files:**
- Modify: `.vscode/launch.json`

Add entries:

```json
{
    "name": "Split CIS Services",
    "type": "debugpy",
    "request": "launch",
    "program": "${workspaceFolder}/split_cis_services.py",
    "args": [
        "--source", "${workspaceFolder}/IntuneWindows11v4.0.0/System Service Scripts",
        "--config", "${workspaceFolder}/cis-control-config.json",
        "--output", "${workspaceFolder}/output"
    ],
    "console": "integratedTerminal"
},
{
    "name": "Deploy Services QA: TestMode + WhatIf",
    "type": "PowerShell",
    "request": "launch",
    "script": "${workspaceFolder}/Deploy-CISServices.ps1",
    "args": [
        "-Tenant", "QA",
        "-ManifestFile", "${workspaceFolder}/output/manifest.json",
        "-TestMode",
        "-WhatIf"
    ]
}
```

**Verification:** Both launch configs load without errors in VS Code.

---

### Task 4: End-to-end test in QA

**No code changes — manual verification.**

1. Run splitter: `python split_cis_services.py --source ... --config ... --output output`
2. Review generated detect/remediate PS1 files in `output/`
3. Deploy to QA with TestMode: `pwsh Deploy-CISServices.ps1 -Tenant QA -ManifestFile ./output/manifest.json -TestMode`
4. Verify in Intune portal:
   - PR appears under Devices > Remediations
   - Detect and remediate scripts are correct
   - Schedule is 8 hours
   - Assignment targets the correct group
   - Scope tag is correct
5. If all good, deploy remaining services without TestMode
