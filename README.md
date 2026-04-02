# CIS Policy Splitter

Splits CIS Build Kit JSON files and service scripts into deployable baseline bundles, exceptionable policies, and pre-approved alternatives. Deploys to Intune via Graph API.

## Architecture

Two-phase pipeline with `output/` as the boundary:

```
Phase 1: Python (split)                    Phase 2: PowerShell (deploy)
┌──────────────────────────┐               ┌───────────────────────────┐
│ split_cis_policies.py    │               │ Deploy-CISPolicies.ps1    │
│ split_cis_services.py    │──► output/ ──►│ Reads manifest.json       │
│                          │               │ Creates policies via Graph│
│ Reads Build Kit JSONs    │               │ Sets scope tags            │
│ + cis-control-config.json│               │ Creates assignments       │
│ Writes split JSONs +     │               └───────────────────────────┘
│ service scripts +        │
│ manifest.json            │
└──────────────────────────┘
```

**Phase 1** is pure Python (stdlib only — no pip dependencies beyond pytest for testing).
**Phase 2** is PowerShell 7+ with Microsoft.Graph SDK.

## Quick Start

```bash
# Set up Python environment (one time)
python3 -m venv .venv
.venv/bin/pip install pytest

# Phase 1: Split Settings Catalog policies
.venv/bin/python3 split_cis_policies.py \
  --path "IntuneWindows11v4.0.0/Settings Catalog" \
  --config cis-control-config.json \
  --output ./output

# Phase 1b: Split System Service scripts (appends to manifest)
.venv/bin/python3 split_cis_services.py \
  --source "IntuneWindows11v4.0.0/System Service Scripts" \
  --config cis-control-config.json \
  --output ./output

# Phase 2: Deploy to Intune (dry run first)
pwsh Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json -WhatIf

# Phase 2: Deploy for real
pwsh Deploy-CISPolicies.ps1 -Tenant Prod -ManifestFile ./output/manifest.json

# Deploy specific policies only
pwsh Deploy-CISPolicies.ps1 -Tenant QA -PolicyFile 'output/baseline/CIS L1 - Auditing.json'
```

## Background

UofT deploys the CIS Microsoft Intune for Windows 11 Benchmark v4.0.0 across all managed devices. The CIS Build Kit provides pre-built Intune Settings Catalog policies as JSON files, but each file bundles many controls together. Our committee reviewed every control and classified them:

| Disposition | Meaning | Count |
|-------------|---------|-------|
| **Accept** | Deploy as-is | majority |
| **Exceptionable** | Deploy as baseline, but departments may need different values | 13 Settings Catalog + 5 services |
| **Reject** | Do not deploy (breaks operations) | 9 Settings Catalog + 3 services |
| **Not Applicable** | Doesn't apply (e.g., AD DS settings) | 4 |
| **Modified** | Deploy with a different value (49.29 only) | 1 |

The full decision log is in [`cis-control-decisions.md`](cis-control-decisions.md).

## Delegated Exception Model

Exceptionable policies use Intune RBAC for departmental self-service:

- **Baseline** policies → scope tag `001-readonly` — dept admins can see but not modify
- **Exceptionable** policies → scope tag `001` — dept admins can manage assignments for their groups
- Departments exclude their device group from the exceptionable baseline and include it on the alternative

## How Processing Works

### Settings Catalog (`split_cis_policies.py`)

For each source JSON file:

1. Skip if filename matches `skipFiles` config
2. Flag as autopilot if in `autopilotPolicies` (assigns to All Users instead of All Devices)
3. Walk each setting and its nested children:
   - **Accept** → keep in baseline
   - **Reject/NA** → drop (children removed from parent array)
   - **Modified** → swap value, keep in baseline
   - **Exceptionable** → extract into standalone policy with alternatives
4. Write baseline bundle + exceptionable baselines + alternative JSONs

### Parent/Child Settings

Some settings are nested inside a parent's `children[]` array. For exceptionable children, the parent is cloned with only the target child, creating a valid standalone policy. Rejected children are simply removed from the parent's children array.

### System Service Scripts (`split_cis_services.py`)

Parses PowerShell service disable scripts and splits them:
- **Baseline** scripts disable accepted services
- **Exceptionable** scripts disable one service each (can be excluded per-group)
- **Rejected** services are omitted entirely

## Output Structure

```
output/
├── manifest.json                          # Deployment manifest (all entries)
├── baseline/
│   ├── CIS L1 - Admin Templates - System (4.10).json
│   ├── CIS L1 - Services.ps1
│   └── ...
└── exceptionable/
    ├── CIS 49.1 - Guest Account Status - Baseline.json
    ├── CIS 49.1 - Guest Account Status - Alt (enabled).json
    ├── CIS 81.14 - OpenSSH SSH Server.ps1
    └── ...
```

Each manifest entry specifies `file`, `type` (baseline/autopilot/exceptionable/alternative), and `assignTo` (group name or null for alternatives).

## Config File

[`cis-control-config.json`](cis-control-config.json) drives all decisions:

```jsonc
{
  "skipFiles": ["CIS (L1) Windows Update (103) - ..."],  // Skip entire files
  "autopilotPolicies": ["CIS (L1) Autopilot - ..."],     // Assign to All Users
  "scopeTags": {
    "readonly": "001-readonly",    // Baselines
    "exceptionable": "001"         // Exceptionable + alternatives
  },
  "controls": {
    "49.1": {
      "disposition": "exceptionable",
      "description": "Guest Account Status",
      "settingDefinitionId": "device_vendor_msft_...",
      "alternatives": [
        { "name": "enabled", "settingValue": { "value": "..._1" } }
      ]
    },
    "49.29": {
      "disposition": "modified",
      "settingDefinitionId": "device_vendor_msft_...",
      "modifiedValue": "..._1"
    }
    // ... 28 controls total
  },
  "serviceControls": {
    "sshd": { "disposition": "exceptionable", "description": "OpenSSH SSH Server" },
    "BTAGService": { "disposition": "reject", "description": "Bluetooth Audio Gateway" }
    // ... 8 service controls total
  }
}
```

Any `settingDefinitionId` not in the config defaults to "accept".

## Testing

```bash
.venv/bin/python3 -m pytest tests/ -v
```

21 tests covering config loading, setting classification (top-level and nested children), value swap for all setting types, output generation, and end-to-end batch mode.

## Key Files

| File | Purpose |
|------|---------|
| `split_cis_policies.py` | Settings Catalog splitter (Python) |
| `split_cis_services.py` | System Service Scripts splitter (Python) |
| `Deploy-CISPolicies.ps1` | Manifest-driven Intune deployer (PowerShell) |
| `cis-control-config.json` | All control decisions and alternative values |
| `cis-control-decisions.md` | Human-readable decision log with rationale |
| `build-setting-id-map.py` | One-time helper to map CIS rec# → settingDefinitionId |
| `IntuneWindows11v4.0.0/` | CIS Build Kit v4.0.0 source files |
| `Get-GroupPolicyAssignments.ps1` | List policies assigned to a security group |
| `Get-IntunePolicy.ps1` | Retrieve a policy from Intune by name (debug) |
| `docs/plans/` | Design doc and implementation plan |

## Claude Code Skill

The `intune-deploy-fix-loop` skill (in `~/.claude/skills/`) automates the full update pipeline with Claude Code:

1. Give Claude a new `cis-control-decisions.md` and/or CIS Build Kit
2. Claude updates `cis-control-config.json`, querying Graph API setting definitions for valid options
3. Runs the splitter and deploys to QA
4. Iterates on Graph API errors — captures full error bodies, diagnoses root causes, fixes config, retries
5. Hands off to you for portal verification and Prod deployment

Common errors the loop handles: missing required child settings (BitLocker DRA), invalid children on disabled choices, empty collection values.
