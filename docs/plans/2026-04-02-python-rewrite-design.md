# CIS Policy Splitter — Python Rewrite Design

**Date:** 2026-04-02
**Goal:** Move all JSON processing from PowerShell to Python. Keep PowerShell only for Graph API deployment.

---

## Architecture

Two scripts with a clear boundary:

- **`split-cis-policies.py`** — Reads CIS Build Kit JSONs + config, classifies settings, splits into output JSONs, writes a manifest.
- **`Deploy-CISPolicies.ps1`** — Reads manifest + output JSONs, pushes to Intune via Graph API.

The boundary is the `output/` directory. Python writes it, PowerShell reads it.

### Unchanged files

- `build-setting-id-map.py` — One-time helper, no changes needed.
- `cis-control-config.json` — No structural changes. `doNotDeploy` entries treated as rejects by the Python script.

---

## `split-cis-policies.py`

### CLI

```
python3 split-cis-policies.py \
  --path ./IntuneWindows11v4.0.0/Settings\ Catalog \
  --config ./cis-control-config.json \
  --output ./output \
  [--dry-run]
```

- `--path` — Single JSON file or directory (recursed for *.json)
- `--config` — Path to cis-control-config.json
- `--output` — Output directory (default: `./output`)
- `--dry-run` — Print what would be written without writing files

### Processing Logic (per source JSON)

1. **Skip check** — If policy name matches `skipFiles`, skip entirely.
2. **Autopilot flag** — If policy name matches `autopilotPolicies`, tag for All Users assignment.
3. **Walk settings array** — For each top-level setting:
   - **Walk children recursively** — Check each child's `settingDefinitionId`:
     - `reject`/`na`/`doNotDeploy` child -> remove from parent's `children[]`
     - `exceptionable` child -> remove from parent, save (parent clone + only this child) for extraction
     - `accept`/unknown child -> keep
   - **Check top-level disposition**:
     - `accept`/unknown -> keep in baseline (with filtered children)
     - `reject`/`na`/`doNotDeploy` -> drop
     - `exceptionable` -> extract as standalone policy
     - `modified` -> swap value in-place, keep in baseline
4. **Write baseline JSON** — Remaining settings.
5. **Write exceptionable JSONs** — One per extracted setting.
6. **Write alternative JSONs** — One per alternative, with value swapped.

### Value Swap Logic for Alternatives

Detect setting type from `settingInstance["@odata.type"]` and swap accordingly:

**Choice settings** (most controls — e.g., 49.1 Guest Account Status):
```python
setting["settingInstance"]["choiceSettingValue"]["value"] = alt["settingValue"]["value"]
```

**Simple integer** (49.8 inactivity limit):
```python
setting["settingInstance"]["simpleSettingValue"]["value"] = alt["settingValue"]["value"]
```

**Simple collection / User Rights** (89.x — array of SID strings):
```python
setting["settingInstance"]["simpleSettingCollectionValue"] = [
    {
        "@odata.type": "#microsoft.graph.deviceManagementConfigurationStringSettingValue",
        "settingValueTemplateReference": None,
        "value": sid
    }
    for sid in alt["settingValue"]["value"]
]
```

If the alt's `settingValue` is `null` (e.g., 89.12 where SID is org-specific), skip the swap and log a warning.

For **child extraction alts** (26.7): same logic but target the child inside the cloned parent's `children[]`.

### Child Setting Handling

Seven `isChild: true` entries in the config:
- **1 exceptionable** (26.7) — Clone parent, keep only this child for extraction. Remove from parent's children in baseline.
- **6 reject/NA** (BitLocker: 4.11.7.2.12, 4.11.7.2.13, 4.11.7.1.5, 4.11.7.2.5, 4.11.7.2.6, 4.11.7.2.8) — Remove from parent's children in baseline.

### Output Format

Each output JSON contains only Graph API policy creation fields:

```json
{
    "name": "...",
    "description": "...",
    "platforms": "windows10",
    "technologies": "mdm",
    "roleScopeTagIds": ["001-readonly"],
    "templateReference": {
        "templateId": "...",
        "templateFamily": "..."
    },
    "settings": [...]
}
```

### Manifest File

`output/manifest.json` tells the deployer what to do with each file:

```json
[
    {
        "file": "baseline/CIS L1 - Local Policies Security Options.json",
        "type": "baseline",
        "assignTo": "AllDevices"
    },
    {
        "file": "exceptionable/CIS 49.1 - Guest Account Status - Baseline.json",
        "type": "exceptionable",
        "assignTo": "AllDevices"
    },
    {
        "file": "exceptionable/CIS 49.1 - Guest Account Status - Alt (enabled).json",
        "type": "alternative",
        "assignTo": "None"
    },
    {
        "file": "baseline/CIS L1 - Autopilot.json",
        "type": "autopilot",
        "assignTo": "AllUsers"
    }
]
```

---

## `Deploy-CISPolicies.ps1` (slimmed down)

### CLI

```
pwsh Deploy-CISPolicies.ps1 -OutputDir ./output [-WhatIf]
```

Single parameter. No more `-Path` or `-ConfigPath`.

### Logic

1. Read `output/manifest.json`
2. Connect to Graph API (`Connect-MgGraph`)
3. For each manifest entry:
   - Read the policy JSON from disk
   - Resolve scope tag names to numeric IDs (from `roleScopeTagIds` in the JSON)
   - Check for existing policy with same name (skip if duplicate)
   - POST to `beta/deviceManagement/configurationPolicies`
   - Assign based on `assignTo` from manifest
4. Write `deployment-log.json`

~150 lines total, down from ~987.

---

## Naming Convention (unchanged)

```
Bundled baselines:   CIS {level} - {section name}
Exceptionable:       CIS {rec#} - {setting name} - Baseline
Alternatives:        CIS {rec#} - {setting name} - Alt ({value})
Autopilot:           CIS L1 - Autopilot
```
