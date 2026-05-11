# Deploy-CISPolicies Upsert Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

> **DESIGN REVISION 2026-05-11 (after Task 1 commit eb41ccb):** Task 1 originally made `/assign` *declarative* (wholesale-replace, including clearing to empty). That was wrong for this workflow — departments and units across UofT will add their own portal-side include/exclude assignments to deployed policies, and they don't have manifest access. Wholesale-replace would wipe their targeting on every re-deploy. Pivoting to **additive merge**: deploy can only ADD assignments, never REMOVE. Task 6 below implements the pivot; Tasks 2-5 test scenarios are inverted to verify preservation instead of replacement. The body-PATCH change from Task 1 is correct and stays.

**Goal:** Convert `Deploy-CISPolicies.ps1` from "create-if-missing, skip-if-exists" into an upsert: PATCH the body when a policy exists, POST when it doesn't, and ADDITIVELY MERGE assignments — preserving any existing portal-side targeting while ensuring the manifest's declared `assignTo` / `excludeGroups` are present.

**Architecture:** The script already maintains a name → ID cache of existing policies via `Initialize-ExistingPolicyCache` (one paginated GET at startup). Today the "found in cache" branch returns early with `Skipped = $true`. After this change, that branch instead issues `PATCH /configurationPolicies('{id}')` with the full body from disk. For assignments, the script GETs the policy's current `/assignments`, computes the union of (existing targets, manifest's declared targets) deduplicated by `(@odata.type, groupId)`, and POSTs `/assign` only if the manifest contributed something new. Department-added portal targeting is never overwritten.

**Tech Stack:** PowerShell 7 (`pwsh`), Microsoft Graph beta endpoint (`/deviceManagement/configurationPolicies`), `Microsoft.Graph.Authentication` module (`Invoke-MgGraphRequest`, `Connect-MgGraph`).

**Pre-flight callouts (revised):**
- The current manifest at `output/manifest.json` has 49 entries; 15 are missing `assignTo`. Under the **additive merge** flow, those 15 entries leave existing portal assignments alone — no migration required.
- `platforms` and `technologies` are immutable on existing policies. If a manifest entry's `platforms`/`technologies` ever drifts from what's in the tenant, Graph will reject the PATCH with a 400. We intentionally do NOT auto-recover (delete+recreate) — we log the failure and move on. See [/Users/liuderek/cis-policy-splitter/Deploy-CISPolicies.ps1](../../Deploy-CISPolicies.ps1) for the existing error-logging pattern we mirror.
- **The manifest cannot REMOVE assignments — only add.** If the manifest's `assignTo` changes from group A to group B, the policy ends up assigned to BOTH; group A must be removed manually via the portal. This is the deliberate tradeoff for letting departments manage their own portal targeting.
- No diff-before-write on the body in this iteration. Every run PATCHes every existing policy (bumps `lastModifiedDateTime`). Acceptable noise for v1; revisit if audit-log churn becomes a complaint.

---

## Task 1: Implement upsert flow in `Deploy-CISPolicies.ps1`

All edits are to a single file: `/Users/liuderek/cis-policy-splitter/Deploy-CISPolicies.ps1`. The task ends in one commit so the script is never left in a half-converted state.

**Files:**
- Modify: `Deploy-CISPolicies.ps1` (multiple regions — see steps)

---

**Step 1: Update the `$result` initialization to track `Updated` instead of `Skipped`**

Edit `Deploy-CISPolicies.ps1` line 196.

Find:
```powershell
$result = @{ Name = $policyName; Id = $null; Created = $false; Skipped = $false; Assigned = $false; Error = $null }
```

Replace with:
```powershell
$result = @{ Name = $policyName; Id = $null; Created = $false; Updated = $false; Assigned = $false; Error = $null }
```

---

**Step 2: Make the existing-policy cache populate even in `-WhatIf`**

The cache fetch is a read-only GET — safe in WhatIf mode. Populating it lets WhatIf accurately report "Would update" vs "Would create" instead of always saying "Would create".

Edit `Deploy-CISPolicies.ps1` line 152 (inside `Initialize-ExistingPolicyCache`).

Find:
```powershell
function Initialize-ExistingPolicyCache {
    if ($WhatIfPreference) { return }
    Write-Host "Fetching existing configuration policies..." -ForegroundColor Gray
```

Replace with:
```powershell
function Initialize-ExistingPolicyCache {
    Write-Host "Fetching existing configuration policies..." -ForegroundColor Gray
```

(Delete the `if ($WhatIfPreference) { return }` line entirely.)

Then edit line 182 (inside `Find-ExistingPolicy`).

Find:
```powershell
function Find-ExistingPolicy {
    param([string]$PolicyName)
    if ($WhatIfPreference) { return $null }
    if ($existingPoliciesByName.ContainsKey($PolicyName)) {
```

Replace with:
```powershell
function Find-ExistingPolicy {
    param([string]$PolicyName)
    if ($existingPoliciesByName.ContainsKey($PolicyName)) {
```

(Delete the `if ($WhatIfPreference) { return $null }` line entirely.)

**Caveat:** `Initialize-ExistingPolicyCache` is called at line 288 *after* the Graph-connect block, which is itself guarded by `if (-not $WhatIfPreference)`. We need to make sure the GET in WhatIf mode still has a Graph context. Look at lines 278-286 — in WhatIf mode, no `Connect-MgGraph` runs, so `Invoke-MgGraphRequest` will fail.

Add a connect call inside the WhatIf branch at line 285 so the cache GET works. Edit lines 284-286.

Find:
```powershell
} else {
    Write-Host "[WhatIf] Would connect to Microsoft Graph ($Tenant`: $tenantDomain)" -ForegroundColor DarkYellow
}
```

Replace with:
```powershell
} else {
    Write-Host "[WhatIf] Connecting read-only to Microsoft Graph ($Tenant`: $tenantDomain) to enumerate existing policies..." -ForegroundColor DarkYellow
    $requiredScopes = "DeviceManagementConfiguration.Read.All", "Group.Read.All"
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
}
```

(WhatIf mode now performs a real read-only connect so the cache populates accurately; nothing is written.)

---

**Step 3: Replace the WhatIf early-return in `New-IntunePolicy` to distinguish create vs update**

Edit `Deploy-CISPolicies.ps1` lines 198-204.

Find:
```powershell
    if ($WhatIfPreference) {
        $assignMsg = if ($AssignTo) { " (include: $AssignTo)" } else { "" }
        if ($ExcludeGroups.Count -gt 0) { $assignMsg += " (exclude: $($ExcludeGroups -join ', '))" }
        Write-Host "  [WhatIf] Would create: $policyName$assignMsg" -ForegroundColor DarkYellow
        $result.Id = 'WHATIF-ID'
        return $result
    }

    $existing = Find-ExistingPolicy -PolicyName $policyName
    if ($existing) {
        Write-Host "  SKIP (exists): $policyName (ID: $($existing.id))" -ForegroundColor Yellow
        $result.Id = $existing.id; $result.Skipped = $true
        return $result
    }
```

Replace with:
```powershell
    $existing = Find-ExistingPolicy -PolicyName $policyName

    if ($WhatIfPreference) {
        $action = if ($existing) { "Would UPDATE" } else { "Would CREATE" }
        $assignMsg = if ($AssignTo) { " (include: $AssignTo)" } else { "" }
        if ($ExcludeGroups.Count -gt 0) { $assignMsg += " (exclude: $($ExcludeGroups -join ', '))" }
        if (-not $AssignTo -and $ExcludeGroups.Count -eq 0) { $assignMsg = " (assignments: cleared)" }
        Write-Host "  [WhatIf] $action`: $policyName$assignMsg" -ForegroundColor DarkYellow
        $result.Id = if ($existing) { $existing.id } else { 'WHATIF-ID' }
        if ($existing) { $result.Updated = $true } else { $result.Created = $true }
        return $result
    }
```

(The `SKIP (exists)` block is gone entirely. The WhatIf block now uses `$existing` to pick the right verb, and explicitly notes "assignments: cleared" when the manifest declares none — that's the surprising case operators need to see.)

---

**Step 4: Add the PATCH branch for existing policies and keep POST for new policies**

Edit `Deploy-CISPolicies.ps1` lines 213-222.

Find:
```powershell
    try {
        $body = $PolicyBody | ConvertTo-Json -Depth 30
        $response = Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies" -Body $body -ContentType "application/json"
        $result.Id = $response.id; $result.Created = $true
        Write-Host "  CREATED: $policyName (ID: $($response.id))" -ForegroundColor Green
    } catch {
        $result.Error = $_.Exception.Message
        Write-Warning "Failed to create '$policyName': $($_.Exception.Message)"
        return $result
    }
```

Replace with:
```powershell
    $body = $PolicyBody | ConvertTo-Json -Depth 30

    if ($existing) {
        try {
            Invoke-MgGraphRequest -Method PATCH -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($existing.id)')" -Body $body -ContentType "application/json" | Out-Null
            $result.Id = $existing.id; $result.Updated = $true
            Write-Host "  UPDATED: $policyName (ID: $($existing.id))" -ForegroundColor Cyan
        } catch {
            $result.Error = $_.Exception.Message
            Write-Warning "Failed to update '$policyName' (ID: $($existing.id)): $($_.Exception.Message)"
            return $result
        }
    } else {
        try {
            $response = Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies" -Body $body -ContentType "application/json"
            $result.Id = $response.id; $result.Created = $true
            Write-Host "  CREATED: $policyName (ID: $($response.id))" -ForegroundColor Green
        } catch {
            $result.Error = $_.Exception.Message
            Write-Warning "Failed to create '$policyName': $($_.Exception.Message)"
            return $result
        }
    }
```

(Note the PATCH URL uses the OData function-style `('{id}')` form to match the existing `/assign` URL on line 255. Both work, but consistency is nice.)

---

**Step 5: Make `/assign` declarative — always invoke, even with empty targets**

Edit `Deploy-CISPolicies.ps1` line 252.

Find:
```powershell
    if ($result.Id -and $assignmentTargets.Count -gt 0) {
        try {
            $assignBody = @{ assignments = $assignmentTargets }
            Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
            $result.Assigned = $true
            $assignSummary = @()
            if ($AssignTo) { $assignSummary += "include: $AssignTo" }
            if ($ExcludeGroups.Count -gt 0) { $assignSummary += "exclude: $($ExcludeGroups -join ', ')" }
            Write-Host "  ASSIGNED: $policyName -> $($assignSummary -join '; ')" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
            $result.Error = "Assignment failed: $($_.Exception.Message)"
        }
    }
```

Replace with:
```powershell
    if ($result.Id) {
        try {
            $assignBody = @{ assignments = $assignmentTargets }
            Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
            $result.Assigned = $true
            $assignSummary = @()
            if ($AssignTo) { $assignSummary += "include: $AssignTo" }
            if ($ExcludeGroups.Count -gt 0) { $assignSummary += "exclude: $($ExcludeGroups -join ', ')" }
            if ($assignmentTargets.Count -eq 0) { $assignSummary += "cleared" }
            Write-Host "  ASSIGNED: $policyName -> $($assignSummary -join '; ')" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
            $result.Error = "Assignment failed: $($_.Exception.Message)"
        }
    }
```

(Only difference: dropped the `-and $assignmentTargets.Count -gt 0` guard, and added a "cleared" label in the summary when targets are empty so operators see explicitly that this run wiped the assignment list.)

---

**Step 6: Update `$stats` initialization (Skipped → Updated)**

Edit `Deploy-CISPolicies.ps1` line 314.

Find:
```powershell
$stats = @{ Created = 0; Skipped = 0; Failed = 0 }
```

Replace with:
```powershell
$stats = @{ Created = 0; Updated = 0; Failed = 0 }
```

---

**Step 7: Update the per-policy stats counters and the deployment-log entry**

Edit `Deploy-CISPolicies.ps1` lines 332-340.

Find:
```powershell
    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; file = $item.File
        assignTo = $item.AssignTo; excludeGroups = @($item.ExcludeGroups); created = $result.Created
        skipped = $result.Skipped; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Skipped) { $stats.Skipped++ }
    if ($result.Error -and -not $result.Created) { $stats.Failed++ }
```

Replace with:
```powershell
    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; file = $item.File
        assignTo = $item.AssignTo; excludeGroups = @($item.ExcludeGroups); created = $result.Created
        updated = $result.Updated; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Updated) { $stats.Updated++ }
    if ($result.Error -and -not ($result.Created -or $result.Updated)) { $stats.Failed++ }
```

(The `Failed` counter now treats either Created or Updated as a successful body operation — assignment-only failures keep their existing "logged but not counted as Failed" behavior, which is unchanged from before.)

---

**Step 8: Update the deployment-log top-level summary fields**

Edit `Deploy-CISPolicies.ps1` line 352.

Find:
```powershell
        created = $stats.Created; skipped = $stats.Skipped; failed = $stats.Failed
```

Replace with:
```powershell
        created = $stats.Created; updated = $stats.Updated; failed = $stats.Failed
```

---

**Step 9: Update the on-screen summary print**

Edit `Deploy-CISPolicies.ps1` lines 371-373.

Find:
```powershell
Write-Host "  Created:    $($stats.Created)" -ForegroundColor Green
Write-Host "  Skipped:    $($stats.Skipped) (already exist)" -ForegroundColor Yellow
Write-Host "  Failed:     $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
```

Replace with:
```powershell
Write-Host "  Created:    $($stats.Created)" -ForegroundColor Green
Write-Host "  Updated:    $($stats.Updated)" -ForegroundColor Cyan
Write-Host "  Failed:     $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
```

---

**Step 10: Update the script `.DESCRIPTION` and the inline comment above the existing-policy cache**

Edit `Deploy-CISPolicies.ps1` lines 4-7.

Find:
```powershell
.DESCRIPTION
    Creates configuration policies in Intune from policy JSON files. Policies are
    created without assignments — use a separate assignment workflow to target them.

    Accepts either a manifest file (deploy all) or individual policy JSON paths.
```

Replace with:
```powershell
.DESCRIPTION
    Upserts configuration policies in Intune from policy JSON files. The manifest
    is the source of truth for both policy body and assignments:

      * If a policy with the same name does not exist, it is CREATED (POST).
      * If it does exist, the body is wholesale-replaced via PATCH (preserves the
        GUID, bumps lastModifiedDateTime on every run).
      * Assignments are ALWAYS reconciled to the manifest's declared set via the
        /assign action — including clearing to empty when the manifest declares
        no targets. Portal-only assignments WILL be removed on the next run.

    Accepts either a manifest file (deploy all) or individual policy JSON paths.

    Caveat: platforms and technologies are immutable on existing policies. If the
    manifest's platforms/technologies drift from the tenant copy, PATCH fails with
    400 and the deploy logs the error and moves on — manual delete-and-rename is
    the recovery path.
```

Edit `Deploy-CISPolicies.ps1` lines 145-148.

Find:
```powershell
# Cache of existing configurationPolicies keyed by exact name (one Graph round
# trip up front instead of N OData $filter calls — the previous per-policy
# filter silently failed on names containing spaces / parens and produced
# duplicates).
```

Replace with:
```powershell
# Cache of existing configurationPolicies keyed by exact name (one Graph round
# trip up front instead of N OData $filter calls — the previous per-policy
# filter silently failed on names containing spaces / parens and produced
# duplicates). Used by the upsert path to decide CREATE vs UPDATE.
```

---

**Step 11: Run PowerShell syntax check**

Run:
```bash
pwsh -NoProfile -Command "& { try { \$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw ./Deploy-CISPolicies.ps1), [ref]\$null); 'OK' } catch { 'PARSE ERROR: ' + \$_.Exception.Message; exit 1 } }"
```

Expected: prints `OK`.

If you get `PARSE ERROR: ...`, fix the syntax before moving on. Common pitfall: an unbalanced brace from a partial edit.

---

**Step 12: Commit**

```bash
git add Deploy-CISPolicies.ps1
git commit -m "$(cat <<'EOF'
feat(deploy): convert Deploy-CISPolicies to declarative upsert

PATCH existing policies in place (preserves GUID) instead of skipping.
Always reconcile assignments via /assign with the manifest's full target
set, including clearing to empty when no targets are declared.

Manifest is now the source of truth for both body and assignment.
Portal-only assignments will be removed on the next run.
EOF
)"
```

---

## Task 2: WhatIf smoke test

Run a dry-run against QA to confirm the upsert logic predicts the right action for each manifest entry.

**Step 1: Run with -WhatIf**

```bash
cd /Users/liuderek/cis-policy-splitter
pwsh ./Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json -WhatIf
```

This will trigger a real interactive `Connect-MgGraph` (read-only scopes), enumerate existing policies, and then for each manifest entry print either:
- `[WhatIf] Would CREATE: <name> (include: <group>)`
- `[WhatIf] Would UPDATE: <name> (include: <group>)`
- `[WhatIf] Would UPDATE: <name> (assignments: cleared)` — for entries without `assignTo` that already exist in tenant

**Step 2: Verify the predicted action mix matches expectations**

The 15 manifest entries without `assignTo` should appear with `(assignments: cleared)` — that's the surprising case. If any policy you expected to stay untouched is in that list, **stop here** and update the manifest to declare its current portal assignment before proceeding to Task 3.

Expected output shape:
```
Loaded manifest: 49 policies
[WhatIf] Connecting read-only to Microsoft Graph (QA: utorontoqa.onmicrosoft.com)...
Fetching existing configuration policies...
  Cached N existing policies (N unique names)

=== Deployment Plan ===
  Tenant:   QA (utorontoqa.onmicrosoft.com)
  Policies: 49
  ...

<policy name>
  [WhatIf] Would UPDATE: <policy name> (include: <group>)
...
```

No `git commit` for this task — verification only.

---

## Task 3: QA integration test — verify CREATE and UPDATE paths

Confirm that both branches of the upsert work against a real tenant.

**Step 1: Pick a target policy and confirm its current state**

Pick one policy from the manifest — say `CIS BL - BitLocker.json`. In the [Intune portal](https://intune.microsoft.com) → Devices → Configuration → look up its current `lastModifiedDateTime` and assignments. Note them.

**Step 2: Run a full deploy against QA**

```bash
cd /Users/liuderek/cis-policy-splitter
pwsh ./Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json
```

Answer `y` at the `Proceed?` prompt.

Expected console output for an existing policy:
```
CIS BL - BitLocker
  UPDATED: CIS BL - BitLocker (ID: <guid>)
  ASSIGNED: CIS BL - BitLocker -> include: 001i-test-security-baseline
```

Expected console output for a not-yet-existing policy (if any):
```
<policy name>
  CREATED: <policy name> (ID: <new-guid>)
  ASSIGNED: <policy name> -> include: <group>
```

**Step 3: Verify in the portal**

Open the same policy in the Intune portal. Confirm:
- The GUID is unchanged (it's the same record).
- `lastModifiedDateTime` is now bumped to the deploy time.
- Settings are intact (spot-check 2-3 settings against the JSON in `output/baseline/CIS BL - BitLocker.json`).
- The Assignments tab shows exactly `001i-test-security-baseline` (or whatever the manifest declared).

**Step 4: Verify the deployment log**

```bash
cat /Users/liuderek/cis-policy-splitter/output/deployment-log.json | python3 -m json.tool | head -50
```

Expected: top-level `created`, `updated`, `failed` counters present; per-policy entries have `updated: true` / `created: true` (not `skipped`).

No `git commit` — verification only.

---

## Task 4: QA integration test — verify assignment replacement (declarative)

Confirm that a portal-side assignment edit gets reverted by the next deploy. This is the load-bearing behavior change vs the old script.

**Step 1: Manually add a stray assignment in the portal**

Pick a policy (e.g. `CIS BL - BitLocker`). In the Intune portal → Assignments → Edit → add an extra include group (any group will do — pick one that's clearly not in the manifest, e.g. a personal test group). Save.

Confirm the policy now has two include groups: the manifest's group + your manual addition.

**Step 2: Re-run the deploy**

```bash
cd /Users/liuderek/cis-policy-splitter
pwsh ./Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json
```

Expected: same `UPDATED` + `ASSIGNED` line as Task 3.

**Step 3: Verify in the portal**

Reload the policy's Assignments tab. The manual group is gone. Only the manifest's declared targets remain. ✅ declarative semantics confirmed.

If the manual group is *still* there: the `/assign` call is not actually wholesale-replacing. Re-read step 5 of Task 1 and confirm the `.Count -gt 0` guard was actually removed.

No `git commit` — verification only.

---

## Task 5: QA integration test — verify assignment clearing (empty set)

This is the riskier behavior: a manifest entry with no `assignTo` will clear all assignments on the existing policy. Make sure that path actually works as designed.

**Step 1: Pick a manifest entry without `assignTo`**

```bash
python3 -c "
import json
m = json.load(open('/Users/liuderek/cis-policy-splitter/output/manifest.json'))
for e in m:
    if not e.get('assignTo'):
        print(e['file'])
" | head -3
```

Pick one of the printed entries. (If none exist, create the scenario: pick an entry, **temporarily** remove its `assignTo` for this test — restore it before committing anything else.)

**Step 2: Manually assign that policy to a group in the portal**

In the Intune portal, find the policy whose name matches the file you picked. Add an include group via the Assignments tab. Save. Confirm there's at least one assignment listed.

**Step 3: Re-run the deploy**

```bash
cd /Users/liuderek/cis-policy-splitter
pwsh ./Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json
```

Expected console line for that policy:
```
  ASSIGNED: <name> -> cleared
```

**Step 4: Verify in the portal**

Reload the policy's Assignments tab. It should be empty — no include groups, no exclude groups. ✅ empty-set clearing confirmed.

**Step 5: Restore any temporary manifest edits**

If you removed an `assignTo` for the test in Step 1, restore it. `git status` should show no manifest changes before you finish.

```bash
cd /Users/liuderek/cis-policy-splitter
git status
git diff output/manifest.json   # should be empty
```

No `git commit` — verification only.

---

---

## Task 6: Convert `/assign` from declarative replace to additive merge

> **Supersedes Step 5 of Task 1.** Body-PATCH (Task 1, Step 4) stays. `/assign` semantics change: deploy can only ADD assignments, never REMOVE. Builds on commit `eb41ccb`.

**Files:**
- Modify: `Deploy-CISPolicies.ps1` — replace the assignment block and the WhatIf assignment message; update `.DESCRIPTION`.

---

**Step 1: Replace the assignment block in `New-IntunePolicy`**

Edit `Deploy-CISPolicies.ps1`. Find the existing assignment block (lines 245-287 — the `# Build assignment targets ...` comment through the closing brace before `return $result`).

Find:
```powershell
    # Build assignment targets (one include + zero-or-more excludes)
    $assignmentTargets = @()

    if ($AssignTo) {
        $includeGid = Resolve-GroupId -GroupName $AssignTo
        if ($includeGid) {
            $assignmentTargets += @{
                target = @{
                    '@odata.type' = '#microsoft.graph.groupAssignmentTarget'
                    groupId       = $includeGid
                }
            }
        }
    }

    foreach ($exGroup in $ExcludeGroups) {
        if (-not $exGroup) { continue }
        $exGid = Resolve-GroupId -GroupName $exGroup
        if ($exGid) {
            $assignmentTargets += @{
                target = @{
                    '@odata.type' = '#microsoft.graph.exclusionGroupAssignmentTarget'
                    groupId       = $exGid
                }
            }
        }
    }

    if ($result.Id) {
        try {
            $assignBody = @{ assignments = $assignmentTargets }
            Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
            $result.Assigned = $true
            $assignSummary = @()
            if ($AssignTo) { $assignSummary += "include: $AssignTo" }
            if ($ExcludeGroups.Count -gt 0) { $assignSummary += "exclude: $($ExcludeGroups -join ', ')" }
            if ($assignmentTargets.Count -eq 0) { $assignSummary += "cleared" }
            Write-Host "  ASSIGNED: $policyName -> $($assignSummary -join '; ')" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
            $result.Error = "Assignment failed: $($_.Exception.Message)"
        }
    }
```

Replace with:
```powershell
    # Build the manifest's declared assignment targets (one include + zero-or-more excludes).
    $manifestTargets = @()

    if ($AssignTo) {
        $includeGid = Resolve-GroupId -GroupName $AssignTo
        if ($includeGid) {
            $manifestTargets += @{
                target = @{
                    '@odata.type' = '#microsoft.graph.groupAssignmentTarget'
                    groupId       = $includeGid
                }
            }
        }
    }

    foreach ($exGroup in $ExcludeGroups) {
        if (-not $exGroup) { continue }
        $exGid = Resolve-GroupId -GroupName $exGroup
        if ($exGid) {
            $manifestTargets += @{
                target = @{
                    '@odata.type' = '#microsoft.graph.exclusionGroupAssignmentTarget'
                    groupId       = $exGid
                }
            }
        }
    }

    # Additive merge: existing portal assignments are NEVER removed by deploy. We GET the
    # policy's current assignment set, union the manifest's targets (deduped by
    # @odata.type+groupId), and POST /assign only if the manifest contributes something new.
    # Federated assignment management: departments add their own include/exclude groups in
    # the portal and we must preserve those across re-deploys.
    if ($result.Id) {
        $currentAssignments = @()
        try {
            $assignResp = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assignments"
            $currentAssignments = @($assignResp.value)
        } catch {
            Write-Warning "Failed to fetch existing assignments for '$policyName': $($_.Exception.Message). Skipping assignment reconcile."
            return $result
        }

        # Build dedup set keyed by (@odata.type, groupId) from existing portal assignments.
        $existingKeys = @{}
        foreach ($a in $currentAssignments) {
            $type = $a.target['@odata.type']
            $gid  = $a.target['groupId']
            if ($type -and $gid) { $existingKeys["$type|$gid"] = $true }
        }

        # Compute what the manifest contributes that isn't already present.
        $toAdd = @()
        foreach ($mt in $manifestTargets) {
            $key = "$($mt.target['@odata.type'])|$($mt.target['groupId'])"
            if (-not $existingKeys.ContainsKey($key)) { $toAdd += $mt }
        }

        if ($toAdd.Count -gt 0) {
            # Merged = existing (target-only, strip id/source) + new manifest targets.
            $mergedTargets = @()
            foreach ($a in $currentAssignments) {
                $mergedTargets += @{ target = $a.target }
            }
            foreach ($mt in $toAdd) { $mergedTargets += $mt }

            try {
                $assignBody = @{ assignments = $mergedTargets }
                Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
                $result.Assigned = $true
                Write-Host "  ASSIGNED: $policyName -> added $($toAdd.Count) target(s); preserved $($currentAssignments.Count) existing" -ForegroundColor Green
            } catch {
                Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
                $result.Error = "Assignment failed: $($_.Exception.Message)"
            }
        } elseif ($manifestTargets.Count -gt 0) {
            Write-Host "  ASSIGN: $policyName -> all $($manifestTargets.Count) manifest target(s) already present; $($currentAssignments.Count) existing preserved" -ForegroundColor Gray
        } elseif ($currentAssignments.Count -gt 0) {
            Write-Host "  ASSIGN: $policyName -> manifest declares no targets; $($currentAssignments.Count) existing preserved" -ForegroundColor Gray
        }
    }
```

---

**Step 2: Update the WhatIf assignment message to reflect additive semantics**

Edit `Deploy-CISPolicies.ps1`. Find the WhatIf block inside `New-IntunePolicy` (it starts at `if ($WhatIfPreference)` shortly after `$existing = Find-ExistingPolicy ...`).

Find:
```powershell
    if ($WhatIfPreference) {
        $action = if ($existing) { "Would UPDATE" } else { "Would CREATE" }
        $assignMsg = if ($AssignTo) { " (include: $AssignTo)" } else { "" }
        if ($ExcludeGroups.Count -gt 0) { $assignMsg += " (exclude: $($ExcludeGroups -join ', '))" }
        if (-not $AssignTo -and $ExcludeGroups.Count -eq 0) { $assignMsg = " (assignments: cleared)" }
        Write-Host "  [WhatIf] $action`: $policyName$assignMsg" -ForegroundColor DarkYellow
        $result.Id = if ($existing) { $existing.id } else { 'WHATIF-ID' }
        if ($existing) { $result.Updated = $true } else { $result.Created = $true }
        return $result
    }
```

Replace with:
```powershell
    if ($WhatIfPreference) {
        $action = if ($existing) { "Would UPDATE" } else { "Would CREATE" }
        $assignParts = @()
        if ($AssignTo) { $assignParts += "include: $AssignTo" }
        if ($ExcludeGroups.Count -gt 0) { $assignParts += "exclude: $($ExcludeGroups -join ', ')" }
        $assignMsg = if ($assignParts.Count -gt 0) {
            " (would add if missing: $($assignParts -join '; '))"
        } else {
            " (manifest declares no targets; existing preserved)"
        }
        Write-Host "  [WhatIf] $action`: $policyName$assignMsg" -ForegroundColor DarkYellow
        $result.Id = if ($existing) { $existing.id } else { 'WHATIF-ID' }
        if ($existing) { $result.Updated = $true } else { $result.Created = $true }
        return $result
    }
```

(WhatIf doesn't actually GET assignments per-policy to dedup — that'd add N read calls per dry-run. The "would add if missing" wording is honest about the additive semantic without pretending to predict the dedup outcome.)

---

**Step 3: Update `.DESCRIPTION` to reflect additive-on-assignments semantics**

Edit `Deploy-CISPolicies.ps1` lines 4-21 (the `.DESCRIPTION` block introduced in Task 1).

Find:
```powershell
.DESCRIPTION
    Upserts configuration policies in Intune from policy JSON files. The manifest
    is the source of truth for both policy body and assignments:

      * If a policy with the same name does not exist, it is CREATED (POST).
      * If it does exist, the body is wholesale-replaced via PATCH (preserves the
        GUID, bumps lastModifiedDateTime on every run).
      * Assignments are ALWAYS reconciled to the manifest's declared set via the
        /assign action — including clearing to empty when the manifest declares
        no targets. Portal-only assignments WILL be removed on the next run.

    Accepts either a manifest file (deploy all) or individual policy JSON paths.

    Caveat: platforms and technologies are immutable on existing policies. If the
    manifest's platforms/technologies drift from the tenant copy, PATCH fails with
    400 and the deploy logs the error and moves on — manual delete-and-rename is
    the recovery path.
```

Replace with:
```powershell
.DESCRIPTION
    Upserts configuration policies in Intune from policy JSON files. The manifest
    is the source of truth for the policy BODY; assignments are merged additively.

      * If a policy with the same name does not exist, it is CREATED (POST).
      * If it does exist, the body is wholesale-replaced via PATCH (preserves the
        GUID, bumps lastModifiedDateTime on every run).
      * Assignments are ADDITIVELY MERGED. Existing portal-side assignments
        (groups added by departments/units to target their own scopes) are
        ALWAYS PRESERVED. The manifest's declared assignTo / excludeGroups are
        added only if not already present. Deploy NEVER removes an existing
        assignment.

    Accepts either a manifest file (deploy all) or individual policy JSON paths.

    Caveats:
      * platforms and technologies are immutable on existing policies. If the
        manifest's platforms/technologies drift from the tenant copy, PATCH
        fails with 400 and the deploy logs the error and moves on — manual
        delete-and-rename is the recovery path.
      * The manifest cannot REMOVE an assignment. If the manifest's assignTo
        changes from group A to group B, the policy ends up assigned to BOTH;
        group A must be removed manually via the portal.
```

---

**Step 4: Run PowerShell syntax check**

Run:
```bash
cd /Users/liuderek/cis-policy-splitter && pwsh -NoProfile -Command "& { try { \$null = [System.Management.Automation.PSParser]::Tokenize((Get-Content -Raw ./Deploy-CISPolicies.ps1), [ref]\$null); 'OK' } catch { 'PARSE ERROR: ' + \$_.Exception.Message; exit 1 } }"
```

Expected: prints `OK`.

---

**Step 5: Commit**

```bash
cd /Users/liuderek/cis-policy-splitter && git add Deploy-CISPolicies.ps1 docs/plans/2026-05-11-deploy-cis-policies-declarative-upsert.md
git commit -m "$(cat <<'EOF'
fix(deploy): make /assign additive merge instead of declarative replace

Deploy now GETs existing portal assignments and unions them with the
manifest's declared targets before POSTing /assign. Department-added
include/exclude groups are preserved across re-deploys.

Tradeoff: the manifest can only ADD assignments. Changing assignTo
from group A to group B leaves the policy assigned to both — manual
portal cleanup is required to remove A.

Supersedes Step 5 of the original Task 1 (commit eb41ccb).
EOF
)"
```

---

## Revised QA Test Scenarios (replace Tasks 2-5 from original plan)

The original Tasks 2-5 tested *declarative* semantics. Under additive merge those scenarios invert. New scenarios:

### Revised Task 2: `-WhatIf` smoke test

```bash
cd /Users/liuderek/cis-policy-splitter
pwsh ./Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json -WhatIf
```

Expected output shape:
```
[WhatIf] Would UPDATE: <name> (would add if missing: include: <group>)
[WhatIf] Would UPDATE: <name> (manifest declares no targets; existing preserved)
[WhatIf] Would CREATE: <name> (would add if missing: include: <group>)
```

No `(assignments: cleared)` should appear anywhere.

### Revised Task 3: Verify CREATE and UPDATE body paths still work

Same as before — full deploy, verify in portal that an existing policy is PATCHed (GUID unchanged, `lastModifiedDateTime` bumped). Assignment behavior is checked separately in Task 4 and 5.

### Revised Task 4: Verify portal-added assignment is PRESERVED (the key behavior change)

1. Pick a policy in the manifest (e.g. `CIS BL - BitLocker`). Confirm its current assignments include the manifest's `assignTo` group.
2. In the Intune portal, manually ADD an extra include group to its Assignments (any group — pick one clearly not in the manifest, e.g. a personal test group). Save.
3. Re-run the deploy.
4. **Expected:** for that policy, the console line should be either `ASSIGN: ... all N manifest target(s) already present; M existing preserved` (gray) OR a brief `ASSIGNED: ...; preserved M existing` (green) if the manifest's group had been removed manually too. Either way, the portal Assignments tab should still show BOTH the manifest's group AND your manually-added group. ✅ additive preservation confirmed.

If the manual group is gone after re-deploy: the merge logic is wrong — re-read step 1 of Task 6, confirm the GET-then-union flow is intact.

### Revised Task 5: Verify manifest entry without `assignTo` LEAVES existing assignments alone

1. Pick a manifest entry without `assignTo` (use the python one-liner from the original Task 5 to find one).
2. In the Intune portal, find the same-named policy and confirm at least one assignment is configured (add one if necessary).
3. Re-run the deploy.
4. **Expected:** console line `ASSIGN: ... manifest declares no targets; M existing preserved` (gray). Portal Assignments tab unchanged — the assignment you added is still there. ✅ no-op-on-empty confirmed.

---

## Done

After Task 6 + the revised Tasks 2-5, two commits exist on `main`: `eb41ccb` (Task 1 — body PATCH + the now-superseded declarative-/assign) and the Task 6 commit (additive-/assign + docstring revision). QA tenant has all bodies synced to manifest; assignments are the union of (manifest's declared baseline + any departmental portal additions).

**If anything in revised Tasks 3-5 fails:** do not patch over it. Investigate the symptom, revise this plan, and re-implement.
