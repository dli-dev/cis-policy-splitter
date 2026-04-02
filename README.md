# CIS Policy Deployment Script — Design & Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split CIS Build Kit JSON files into baseline bundles, exceptionable policies, and alternatives, then optionally deploy to Intune via Graph API.

**Architecture:** Config-driven PowerShell script. Reads CIS Build Kit JSONs + a control decisions config. Identifies settings by `settingDefinitionId` (not position). Outputs split JSON files to disk, optionally deploys via Graph API with scope tags and assignments.

**Tech Stack:** PowerShell 7+, Microsoft.Graph PowerShell SDK, Intune Graph API beta endpoint.

---

## Background

UofT is deploying the CIS Microsoft Intune for Windows 11 Benchmark v4.0.0 across all
managed devices. CIS provides a Build Kit containing pre-built Intune Settings Catalog
policies as JSON files, organized by level (L1, L2, BL) and section.

Each JSON file bundles multiple CIS controls into a single policy. However, our committee
has reviewed every control and classified them into dispositions:

- **Accept** — deploy as-is (majority of controls)
- **Exceptionable** — deploy as baseline, but departments may need different values
  for COSU/kiosk/Hyper-V/RDP devices. These get extracted into individual policies
  with pre-approved alternatives.
- **Reject** — do not deploy (breaks operational requirements)
- **Not applicable** — does not apply to our environment (e.g., AD DS settings)
- **Modified** — deploy with a different value than CIS recommends (49.29 only)

The full decision log is in `cis-control-decisions.md`.

## Delegated Exception Model

Exceptionable policies use Intune RBAC to allow departmental self-service:

- **Non-exceptionable** policies get scope tag `001-readonly` — dept admins can see
  but not modify assignments.
- **Exceptionable** policies get scope tag `001` — dept admins can add their own
  groups as include/exclude on assignments, scoped to only their own groups.
- Departments create SCCM collections (`{deptCode}ic-{description}`) that auto-sync
  to Entra ID groups. They then exclude their group from the exceptionable baseline
  and include it on the alternative policy.

The full model is documented in `delegated-cis-exception-model.md`.

## Critical Design Constraint: Parent/Child Settings

CIS Build Kit JSON files have a nested structure. Some settings are top-level entries
in the `settings[]` array, others are `children[]` nested inside a parent's value.
The CIS rec numbers in the description correspond to a flattened walk of parent +
children, NOT to the top-level settings array.

```
Example: CIS (L1) Device Lock & WHFB
  12 CIS rec numbers, 6 top-level settings, ~11 flattened (parent + children)

  Setting 0: devicelock_devicepasswordenabled (TOP-LEVEL)
    Child: alphanumericdevicepasswordrequired     ← CIS 26.2
    Child: devicepasswordexpiration               ← CIS 26.3
    Child: maxdevicepasswordfailedattempts         ← CIS 26.4
    Child: maxinactivitytimedevicelock             ← CIS 26.7 (EXCEPTIONABLE)
    Child: mindevicepasswordlength                 ← CIS 26.6
```

Therefore the config identifies settings by `settingDefinitionId`, not position.

For exceptionable child settings:
- **Baseline bundle:** Remove the child from parent's children array (parent + other children stay)
- **Exceptionable policy:** Clone the parent, keep ONLY the target child

For rejected/NA child settings:
- Remove the child from parent's children array
- If parent has no remaining children AND parent itself is rejected, remove the parent

---

## Script

```
Deploy-CISPolicies.ps1
  -Path          Single JSON file or directory (recurse for *.json)
  -ConfigPath    Path to cis-control-config.json
  -OutputDir     Where to write split JSONs (default: ./output)
  -Deploy        Switch: push to Intune via Graph API
  -WhatIf        Switch: show what would be created, don't write anything
```

## Config File Structure

```json
{
  "skipFiles": [
    "CIS (L1) Windows Update (103) - Windows 11 Intune 4.0.0"
  ],
  "autopilotPolicies": [
    "CIS (L1) Autopilot - Windows 11 Intune 4.0.0"
  ],
  "scopeTags": {
    "readonly": "001-readonly",
    "exceptionable": "001"
  },
  "doNotDeploy": [
    "49.9", "49.10"
  ],
  "controls": {
    "26.7": {
      "disposition": "exceptionable",
      "description": "Max Inactivity Time Device Lock",
      "settingDefinitionId": "devicelock_maxinactivitytimedevicelock",
      "alternatives": [
        {
          "name": "30min",
          "description": "30 minutes",
          "settingValue": { "value": "..." }
        },
        {
          "name": "disabled",
          "description": "Disabled",
          "settingValue": { "value": "..." }
        }
      ]
    },
    "49.29": {
      "disposition": "modified",
      "description": "UAC standard user elevation prompt",
      "settingDefinitionId": "localpoliciessecurityoptions_useraccountcontrol_behavioroftheelevationpromptforstandardusers",
      "modifiedValue": { "value": "..." }
    },
    "4.11.7.2.6": {
      "disposition": "na",
      "settingDefinitionId": "..."
    },
    "12.1": {
      "disposition": "reject",
      "settingDefinitionId": "..."
    }
  }
}
```

Any setting whose `settingDefinitionId` is not in the config defaults to "accept".

## Processing (per JSON file)

```
1. Skip if filename matches skipFiles
2. Flag if filename matches autopilotPolicies (assign to All Users)
3. Build lookup: settingDefinitionId → disposition (from config)
4. Walk settings array:
   For each top-level setting:
     a. Check disposition of the setting itself
     b. Walk children, check disposition of each child
     c. Apply:
        - reject/na child → remove from children array
        - exceptionable child → remove from children, save for extraction
        - modified child → swap value in-place
        - accept child → keep
     d. After processing children:
        - reject/na top-level (and no remaining children) → remove from settings
        - exceptionable top-level → remove from settings, save for extraction
        - modified top-level → swap value in-place
        - accept top-level → keep
5. Generate baseline bundle JSON (remaining settings)
6. Generate exceptionable baseline JSONs (extracted settings, one per control)
7. Generate alternative JSONs (from config alternative values)
```

## Output Naming Convention

```
Bundled baselines:   CIS {level} - {section name}
Exceptionable:       CIS {rec#} - {setting name} - Baseline ({value})
Alternatives:        CIS {rec#} - {setting name} - Alt ({value})
Autopilot:           CIS L1 - Autopilot
```

## Deployment (if -Deploy)

```
Authentication:
  Connect-MgGraph -Scopes "DeviceManagementConfiguration.ReadWrite.All"
  Interactive/delegated sign-in.

For each output JSON:
  POST /beta/deviceManagement/configurationPolicies
  Set scope tag: 001-readonly (bundled) or 001 (exceptionable + alts)
  Assign:
    Bundled baselines  → Include: All Devices
    Autopilot policies → Include: All Users
    Exceptionable      → Include: All Devices
    Alternatives       → no assignment (depts add includes later)
```

## Key Files

```
cis-control-decisions.md              Full decision log with rationale
delegated-cis-exception-model.md      RBAC delegation model and validation proof
cis-control-config.json               Machine-readable config (to be created)
scripts/Deploy-CISPolicies.ps1        The script (to be created)
IntuneWindows11v4.0.0/                CIS Build Kit source files
```

---

## Implementation Tasks

### Task 1: Build settingDefinitionId mapping for all controls in config

**Files:**
- Create: `scripts/build-setting-id-map.py`
- Output: printed mapping of CIS rec# → settingDefinitionId → top/child

This is a one-time helper that walks all JSON files, flattens parent+children,
and maps CIS rec numbers to settingDefinitionIds. Output is used to populate
the config file.

**Step 1: Write the helper script**

```python
import json, os

base = 'IntuneWindows11v4.0.0/Settings Catalog'
prefix = 'device_vendor_msft_policy_config_'

# Controls we need to map
targets = {
    # Exceptionable
    '26.7', '49.8', '49.1', '49.4', '4.4.2', '4.6.9.1',
    '4.10.9.1.3', '4.10.9.2', '4.10.26.2', '4.11.36.4.2.1',
    '68.2', '89.10', '89.12', '89.14',
    # Reject
    '55.5', '76.1.2', '80.5', '81.6', '12.1', '81.1', '81.2',
    '4.11.7.2.9', '4.11.7.2.12', '4.11.7.2.13',
    # NA
    '4.11.7.1.5', '4.11.7.2.5', '4.11.7.2.6', '4.11.7.2.8',
    # Modified
    '49.29',
    # Do not deploy
    '49.9', '49.10',
}

def walk_children(children, depth=1):
    results = []
    for child in (children or []):
        sid = child.get('settingDefinitionId', '')
        short = sid.replace(prefix, '') if sid.startswith(prefix) else sid
        results.append(('child', short, depth))
        # Recurse into nested children
        for vtype in ['choiceSettingValue', 'simpleSettingValue']:
            val = child.get(vtype, {})
            if isinstance(val, dict):
                results.extend(walk_children(val.get('children', []), depth+1))
    return results

for root, dirs, files in sorted(os.walk(base)):
    for f in sorted(files):
        if not f.endswith('.json'):
            continue
        path = os.path.join(root, f)
        with open(path, encoding='utf-8-sig') as fh:
            data = json.load(fh)

        recs = [r.strip() for r in data['description'].strip().split('\n')
                if r.strip() and r.strip()[0].isdigit()]

        flat = []
        for s in data['settings']:
            inst = s['settingInstance']
            sid = inst['settingDefinitionId']
            short = sid.replace(prefix, '') if sid.startswith(prefix) else sid
            flat.append(('top', short))
            for vtype in ['choiceSettingValue', 'simpleSettingValue',
                          'groupSettingCollectionValue']:
                val = inst.get(vtype, {})
                if isinstance(val, dict):
                    for item in walk_children(val.get('children', [])):
                        flat.append((item[0], item[1]))

        for i, rec in enumerate(recs):
            if rec in targets and i < len(flat):
                level_type, sid = flat[i]
                print(f'{rec:20s} {level_type:6s} {sid}')
```

**Step 2: Run it**

Run: `python3 scripts/build-setting-id-map.py`

**Step 3: Use output to populate config**

Take the printed mappings and fill in the `settingDefinitionId` fields in
`cis-control-config.json`.

**Step 4: Commit**

```bash
git add scripts/build-setting-id-map.py
git commit -m "feat: add helper to map CIS rec numbers to settingDefinitionIds"
```

---

### Task 2: Create the control decisions config file

**Files:**
- Create: `scripts/cis-control-config.json`

Using the mappings from Task 1, create the full config with all controls,
dispositions, settingDefinitionIds, and alternative value placeholders.

Reference `cis-control-decisions.md` for the complete list of dispositions.

Alternative `settingValue` fields can be left as `null` initially — they'll
be populated by testing each setting in the Intune portal and capturing the
correct Graph API enum values.

**Step 1: Create config file with all control entries**

See config structure above. Include all entries from `cis-control-decisions.md`.

**Step 2: Validate config against decisions doc**

Run a quick check: every CIS rec# in `cis-control-decisions.md` should have
a corresponding entry in the config (or be in skipFiles/doNotDeploy).

**Step 3: Commit**

```bash
git add scripts/cis-control-config.json
git commit -m "feat: add CIS control decisions config"
```

---

### Task 3: Core script — JSON splitting (top-level settings only)

**Files:**
- Create: `scripts/Deploy-CISPolicies.ps1`

Start with the simpler case: files where all settings are top-level (no children).
Handle children in Task 4.

**Step 1: Write script skeleton**

```powershell
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$Path,
    [Parameter(Mandatory)][string]$ConfigPath,
    [string]$OutputDir = "./output",
    [switch]$Deploy
)

# Load config
$config = Get-Content $ConfigPath -Encoding UTF8 | ConvertFrom-Json

# Build settingDefinitionId → control lookup
$controlLookup = @{}
foreach ($prop in $config.controls.PSObject.Properties) {
    $ctrl = $prop.Value
    if ($ctrl.settingDefinitionId) {
        $controlLookup[$ctrl.settingDefinitionId] = @{
            CisRec = $prop.Name
            Disposition = $ctrl.disposition
            Description = $ctrl.description
            Alternatives = $ctrl.alternatives
            ModifiedValue = $ctrl.modifiedValue
        }
    }
}

# Resolve input files
$jsonFiles = if (Test-Path $Path -PathType Container) {
    Get-ChildItem $Path -Recurse -Filter '*.json'
} else {
    Get-Item $Path
}

# Create output dirs
New-Item -Path "$OutputDir/baseline" -ItemType Directory -Force | Out-Null
New-Item -Path "$OutputDir/exceptionable" -ItemType Directory -Force | Out-Null

foreach ($file in $jsonFiles) {
    $raw = Get-Content $file.FullName -Encoding UTF8 -Raw
    # Handle BOM
    if ($raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    $policy = $raw | ConvertFrom-Json

    # Check skipFiles
    if ($config.skipFiles -contains $policy.name) {
        Write-Host "SKIP: $($policy.name)" -ForegroundColor Yellow
        continue
    }

    $isAutopilot = $config.autopilotPolicies -contains $policy.name

    # Parse level from policy name
    $level = if ($policy.name -match '\(L1\)') { 'L1' }
             elseif ($policy.name -match '\(L2\)') { 'L2' }
             elseif ($policy.name -match '\(BL\)') { 'BL' }
             else { 'L1' }

    # Process settings
    $baselineSettings = @()
    $extractedSettings = @()

    foreach ($setting in $policy.settings) {
        $sid = $setting.settingInstance.settingDefinitionId
        $shortSid = $sid -replace '^device_vendor_msft_policy_config_', ''

        $ctrl = $controlLookup[$shortSid]

        if (-not $ctrl) {
            # Not in config → accept → keep in baseline
            $baselineSettings += $setting
            continue
        }

        switch ($ctrl.Disposition) {
            'accept' { $baselineSettings += $setting }
            'reject' { Write-Host "  DROP: $($ctrl.CisRec) $($ctrl.Description)" }
            'na'     { Write-Host "  DROP (N/A): $($ctrl.CisRec) $($ctrl.Description)" }
            'modified' {
                # TODO: hardcoded 49.29 value swap
                $baselineSettings += $setting
            }
            'exceptionable' {
                $extractedSettings += @{
                    CisRec = $ctrl.CisRec
                    Description = $ctrl.Description
                    Setting = $setting
                    Alternatives = $ctrl.Alternatives
                }
                Write-Host "  EXTRACT: $($ctrl.CisRec) $($ctrl.Description)"
            }
        }
    }

    # Write baseline bundle
    if ($baselineSettings.Count -gt 0) {
        $sectionName = $policy.name -replace 'CIS \(L[12]\) ', '' -replace 'CIS \(BL\) ', '' -replace ' - Windows 11 Intune 4\.0\.0\s*', ''
        $baselineName = "CIS $level - $sectionName"

        $baselinePolicy = @{
            name = $baselineName
            description = $policy.description
            platforms = $policy.platforms
            technologies = $policy.technologies
            roleScopeTagIds = @($config.scopeTags.readonly)
            templateReference = $policy.templateReference
            settings = $baselineSettings
        }

        $outPath = "$OutputDir/baseline/$baselineName.json"
        $baselinePolicy | ConvertTo-Json -Depth 20 | Set-Content $outPath -Encoding UTF8
        Write-Host "WROTE: $outPath ($($baselineSettings.Count) settings)" -ForegroundColor Green
    }

    # Write exceptionable policies
    foreach ($ext in $extractedSettings) {
        # Baseline exceptionable policy
        $excName = "CIS $($ext.CisRec) - $($ext.Description) - Baseline"
        $excPolicy = @{
            name = $excName
            description = "CIS $($ext.CisRec) exceptionable baseline"
            platforms = $policy.platforms
            technologies = $policy.technologies
            roleScopeTagIds = @($config.scopeTags.exceptionable)
            templateReference = $policy.templateReference
            settings = @($ext.Setting)
        }

        $outPath = "$OutputDir/exceptionable/$excName.json"
        $excPolicy | ConvertTo-Json -Depth 20 | Set-Content $outPath -Encoding UTF8
        Write-Host "WROTE: $outPath" -ForegroundColor Cyan

        # Alternative policies
        foreach ($alt in $ext.Alternatives) {
            if (-not $alt) { continue }
            $altName = "CIS $($ext.CisRec) - $($ext.Description) - Alt ($($alt.name))"
            $altSetting = $ext.Setting | ConvertTo-Json -Depth 20 | ConvertFrom-Json
            # TODO: swap value from $alt.settingValue

            $altPolicy = @{
                name = $altName
                description = "CIS $($ext.CisRec) alternative: $($alt.description)"
                platforms = $policy.platforms
                technologies = $policy.technologies
                roleScopeTagIds = @($config.scopeTags.exceptionable)
                templateReference = $policy.templateReference
                settings = @($altSetting)
            }

            $outPath = "$OutputDir/exceptionable/$altName.json"
            $altPolicy | ConvertTo-Json -Depth 20 | Set-Content $outPath -Encoding UTF8
            Write-Host "WROTE: $outPath" -ForegroundColor Cyan
        }
    }
}
```

**Step 2: Test with a simple 1:1 file**

Run: `pwsh scripts/Deploy-CISPolicies.ps1 -Path "IntuneWindows11v4.0.0/Settings Catalog/Level 1/CIS (L1) User Rights (89) - Windows 11 Intune 4.0.0.json" -ConfigPath scripts/cis-control-config.json -WhatIf`

Expected: baseline JSON with 89.10, 89.12, 89.14 extracted, rest in baseline bundle.

**Step 3: Commit**

```bash
git add scripts/Deploy-CISPolicies.ps1
git commit -m "feat: core CIS policy splitter for top-level settings"
```

---

### Task 4: Handle parent/child settings

**Files:**
- Modify: `scripts/Deploy-CISPolicies.ps1`

Add logic to:
1. Walk children of each top-level setting
2. Check each child's `settingDefinitionId` against the control lookup
3. For rejected/NA children: remove from parent's children array
4. For exceptionable children: clone parent with only the target child, extract
5. For modified children: swap value in-place

Key function: `Process-SettingChildren` that recursively walks and filters
the children array.

**Step 1: Add child-walking logic**

Add a function that deep-clones a setting, walks its children, and applies
dispositions. Test with the Device Lock & WHFB file (CIS 26.7 is a child).

**Step 2: Test with Device Lock file**

Run against `CIS (L1) Device Lock & WHFB - Windows 11 Intune 4.0.0 .json`.
Verify 26.7 is extracted as a standalone policy with its parent wrapper intact.

**Step 3: Test with BitLocker file**

Run against `CIS (BL) BitLocker - Windows 11 Intune 4.0.0.json`.
Verify NA settings (4.11.7.2.6, etc.) are stripped from children.

**Step 4: Commit**

```bash
git add scripts/Deploy-CISPolicies.ps1
git commit -m "feat: handle parent/child settings in CIS policy splitter"
```

---

### Task 5: Handle 49.29 modified value

**Files:**
- Modify: `scripts/Deploy-CISPolicies.ps1`

Hardcode the value swap for 49.29 (UAC standard user elevation prompt).
CIS value: auto-deny. Our value: prompt for credentials on secure desktop.

**Step 1: Find the exact settingDefinitionId and enum value**

Look up 49.29 in the Local Policies Security Options JSON.
Identify the current CIS value enum and the replacement enum.

**Step 2: Add the value swap in the 'modified' case**

In the switch block, when disposition is 'modified' and CisRec is '49.29',
replace the `choiceSettingValue.value` with the correct enum.

**Step 3: Test**

Run against Local Policies Security Options JSON. Verify 49.29 is in the
baseline bundle with the swapped value.

**Step 4: Commit**

```bash
git add scripts/Deploy-CISPolicies.ps1
git commit -m "feat: handle 49.29 modified value swap"
```

---

### Task 6: Graph API deployment

**Files:**
- Modify: `scripts/Deploy-CISPolicies.ps1`

Add the `-Deploy` path: authenticate to Graph, create policies, set scope tags,
create assignments.

**Step 1: Add Graph authentication**

```powershell
if ($Deploy) {
    Connect-MgGraph -Scopes "DeviceManagementConfiguration.ReadWrite.All"
}
```

**Step 2: Add policy creation function**

```powershell
function New-IntunePolicy {
    param($PolicyJson, $ScopeTagId, $AssignTo)

    # Create policy
    $body = $PolicyJson | ConvertTo-Json -Depth 20
    $result = Invoke-MgGraphRequest -Method POST `
        -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies" `
        -Body $body -ContentType "application/json"

    # Set scope tag via roleScopeTagIds (already in the JSON body)

    # Assign if specified
    if ($AssignTo -eq 'AllDevices') {
        $assignment = @{
            assignments = @(@{
                target = @{
                    '@odata.type' = '#microsoft.graph.allDevicesAssignmentTarget'
                }
            })
        }
        Invoke-MgGraphRequest -Method POST `
            -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.id)')/assign" `
            -Body ($assignment | ConvertTo-Json -Depth 10) -ContentType "application/json"
    }
    elseif ($AssignTo -eq 'AllUsers') {
        $assignment = @{
            assignments = @(@{
                target = @{
                    '@odata.type' = '#microsoft.graph.allLicensedUsersAssignmentTarget'
                }
            })
        }
        Invoke-MgGraphRequest -Method POST `
            -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.id)')/assign" `
            -Body ($assignment | ConvertTo-Json -Depth 10) -ContentType "application/json"
    }

    return $result
}
```

**Step 3: Wire deployment into main loop**

After writing output JSONs, if `-Deploy`, read each output JSON and call
`New-IntunePolicy` with appropriate scope tag and assignment target.

**Step 4: Test with -WhatIf first, then deploy a single test policy**

**Step 5: Commit**

```bash
git add scripts/Deploy-CISPolicies.ps1
git commit -m "feat: add Graph API deployment to CIS policy splitter"
```

---

### Task 7: Batch mode (directory processing)

**Files:**
- Modify: `scripts/Deploy-CISPolicies.ps1`

Already handled in Task 3 skeleton (resolves Path as file or directory).
This task is just end-to-end testing.

**Step 1: Run against entire Settings Catalog directory**

Run: `pwsh scripts/Deploy-CISPolicies.ps1 -Path "IntuneWindows11v4.0.0/Settings Catalog" -ConfigPath scripts/cis-control-config.json -OutputDir ./output`

**Step 2: Verify output**

- Count baseline JSONs (should match number of non-skipped source files)
- Count exceptionable JSONs (should be 13 baselines + ~15 alternatives)
- Spot-check a few files for correct settings

**Step 3: Final commit**

```bash
git add scripts/Deploy-CISPolicies.ps1
git commit -m "feat: verified batch mode for CIS policy deployment"
```
