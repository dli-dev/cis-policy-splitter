<#
.SYNOPSIS
    Deploys CIS policies to Intune via Graph API using output from split-cis-policies.py.

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

    Run split_cis_policies.py first to generate the output directory.

.PARAMETER Tenant
    Target tenant: QA or Prod. Resolves to utorontoqa.onmicrosoft.com or utoronto.onmicrosoft.com.

.PARAMETER ManifestFile
    Path to manifest.json. Deploys all policies listed in the manifest.

.PARAMETER PolicyFile
    One or more paths to individual policy JSON files to deploy.

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -Tenant Prod -PolicyFile ./output/baseline/CIS_L1_Firewall.json

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -Tenant QA -PolicyFile file1.json, file2.json -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [Parameter(ParameterSetName = 'Manifest')]
    [string]$ManifestFile = "./output/manifest.json",

    [Parameter(ParameterSetName = 'Files', Mandatory)]
    [string[]]$PolicyFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Resolve policy file list
# ---------------------------------------------------------------------------
if ($PSCmdlet.ParameterSetName -eq 'Files') {
    $deployItems = @()
    foreach ($f in $PolicyFile) {
        if (-not (Test-Path $f)) {
            Write-Error "Policy file not found: $f"
            return
        }
        $deployItems += @{ File = (Resolve-Path $f).Path; AssignTo = $null }
    }
    Write-Host "Deploying $($deployItems.Count) policy file(s)" -ForegroundColor Gray
} else {
    if (-not (Test-Path $ManifestFile)) {
        Write-Error "Manifest not found: $ManifestFile. Run split_cis_policies.py first."
        return
    }
    $manifestRaw = Get-Content $ManifestFile -Encoding UTF8 -Raw
    if ($manifestRaw[0] -eq [char]0xFEFF) { $manifestRaw = $manifestRaw.Substring(1) }
    $manifest = $manifestRaw | ConvertFrom-Json

    $manifestDir = Split-Path $ManifestFile -Parent
    $deployItems = @()
    foreach ($entry in $manifest) {
        $deployItems += @{
            File          = (Join-Path $manifestDir $entry.file)
            AssignTo      = if ($entry.PSObject.Properties['assignTo']) { $entry.assignTo } else { $null }
            ExcludeGroups = if ($entry.PSObject.Properties['excludeGroups']) { @($entry.excludeGroups) } else { @() }
        }
    }
    Write-Host "Loaded manifest: $($deployItems.Count) policies" -ForegroundColor Gray
}

# ---------------------------------------------------------------------------
# 2. Scope tag resolution cache
# ---------------------------------------------------------------------------
$scopeTagCache = @{}

function Resolve-ScopeTagId {
    param([string]$TagName)
    if ($TagName -eq '0' -or $TagName -eq 'Default') { return '0' }
    if ($scopeTagCache.ContainsKey($TagName)) { return $scopeTagCache[$TagName] }

    if ($WhatIfPreference) {
        $scopeTagCache[$TagName] = "WHATIF-$TagName"
        return "WHATIF-$TagName"
    }

    try {
        $uri = "https://graph.microsoft.com/beta/deviceManagement/roleScopeTags?`$filter=displayName eq '$TagName'"
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        if ($response.value -and $response.value.Count -gt 0) {
            $id = $response.value[0].id.ToString()
            $scopeTagCache[$TagName] = $id
            Write-Host "  Resolved scope tag '$TagName' -> ID $id" -ForegroundColor Gray
            return $id
        }
    } catch {}

    Write-Warning "Scope tag '$TagName' not found. Using Default (0)."
    $scopeTagCache[$TagName] = '0'
    return '0'
}

# ---------------------------------------------------------------------------
# 3. Group resolution cache
# ---------------------------------------------------------------------------
$groupCache = @{}

function Resolve-GroupId {
    param([string]$GroupName)
    if ($groupCache.ContainsKey($GroupName)) { return $groupCache[$GroupName] }

    if ($WhatIfPreference) {
        $groupCache[$GroupName] = "WHATIF-$GroupName"
        return "WHATIF-$GroupName"
    }

    try {
        $encodedName = $GroupName -replace "'", "''"
        $uri = "https://graph.microsoft.com/v1.0/groups?`$filter=displayName eq '$encodedName'&`$select=id,displayName"
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        if ($response.value -and $response.value.Count -gt 0) {
            $id = $response.value[0].id.ToString()
            $groupCache[$GroupName] = $id
            Write-Host "  Resolved group '$GroupName' -> ID $id" -ForegroundColor Gray
            return $id
        }
    } catch {}

    Write-Warning "Group '$GroupName' not found. Skipping assignment."
    return $null
}

# ---------------------------------------------------------------------------
# 4. Policy creation and assignment
# ---------------------------------------------------------------------------
# Cache of existing configurationPolicies keyed by exact name (one Graph round
# trip up front instead of N OData $filter calls — the previous per-policy
# filter silently failed on names containing spaces / parens and produced
# duplicates). Used by the upsert path to decide CREATE vs UPDATE.
$existingPoliciesByName = @{}

function Initialize-ExistingPolicyCache {
    Write-Host "Fetching existing configuration policies..." -ForegroundColor Gray
    $uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$select=id,name&`$top=999"
    $count = 0
    while ($uri) {
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        foreach ($p in $response.value) {
            $key = $p.name
            if (-not $existingPoliciesByName.ContainsKey($key)) {
                $existingPoliciesByName[$key] = @()
            }
            $existingPoliciesByName[$key] += $p.id
            $count++
        }
        # Hashtable index access returns $null for missing keys even under
        # StrictMode v2, unlike dot notation which throws.
        $uri = $response['@odata.nextLink']
    }
    $duplicateNames = @($existingPoliciesByName.GetEnumerator() | Where-Object { $_.Value.Count -gt 1 })
    Write-Host "  Cached $count existing policies ($($existingPoliciesByName.Count) unique names)" -ForegroundColor Gray
    if ($duplicateNames.Count -gt 0) {
        Write-Warning "Tenant already contains $($duplicateNames.Count) duplicate policy name(s):"
        foreach ($d in $duplicateNames) {
            Write-Warning "  '$($d.Key)' -> $($d.Value.Count) copies: $($d.Value -join ', ')"
        }
    }
}

function Find-ExistingPolicy {
    param([string]$PolicyName)
    if ($existingPoliciesByName.ContainsKey($PolicyName)) {
        return @{ id = $existingPoliciesByName[$PolicyName][0]; name = $PolicyName }
    }
    return $null
}

function New-IntunePolicy {
    param([hashtable]$PolicyBody, [string]$AssignTo, $ExcludeGroups = @())

    # Normalize: PS param binding can collapse empty arrays to $null and breaks .Count later.
    $ExcludeGroups = @($ExcludeGroups | Where-Object { $_ })

    $policyName = $PolicyBody.name
    $result = @{ Name = $policyName; Id = $null; Created = $false; Updated = $false; Assigned = $false; Error = $null }

    $existing = Find-ExistingPolicy -PolicyName $policyName

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

    return $result
}

# ---------------------------------------------------------------------------
# 5. Connect to Graph
# ---------------------------------------------------------------------------
$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

if (-not $WhatIfPreference) {
    $requiredScopes = "DeviceManagementConfiguration.ReadWrite.All", "Group.Read.All"
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Connecting to Microsoft Graph ($Tenant`: $tenantDomain)..." -ForegroundColor White
    Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
    Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]" -ForegroundColor Green
} else {
    Write-Host "[WhatIf] Connecting read-only to Microsoft Graph ($Tenant`: $tenantDomain) to enumerate existing policies..." -ForegroundColor DarkYellow
    $requiredScopes = "DeviceManagementConfiguration.Read.All", "Group.Read.All"
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
}

Initialize-ExistingPolicyCache

# ---------------------------------------------------------------------------
# 6. Confirmation
# ---------------------------------------------------------------------------
$assignGroups = @($deployItems | Where-Object { $_.AssignTo } | ForEach-Object { $_.AssignTo } | Select-Object -Unique)
Write-Host "`n=== Deployment Plan ===" -ForegroundColor White
Write-Host "  Tenant:   $Tenant ($tenantDomain)" -ForegroundColor White
Write-Host "  Policies: $($deployItems.Count)" -ForegroundColor White
if ($assignGroups.Count -gt 0) {
    Write-Host "  Assign to: $($assignGroups -join ', ')" -ForegroundColor Cyan
} else {
    Write-Host "  Assign to: (none)" -ForegroundColor Gray
}

if (-not $WhatIfPreference) {
    $confirm = Read-Host "`nProceed? (y/N)"
    if ($confirm -ne 'y') {
        Write-Host "Aborted." -ForegroundColor Yellow
        return
    }
}

# ---------------------------------------------------------------------------
# 7. Deploy each policy
# ---------------------------------------------------------------------------
$stats = @{ Created = 0; Updated = 0; Failed = 0 }
$deploymentLog = [System.Collections.ArrayList]::new()

foreach ($item in $deployItems) {
    $raw = Get-Content $item.File -Encoding UTF8 -Raw
    if ($raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    $policyBody = $raw | ConvertFrom-Json -AsHashtable

    # Resolve scope tag names to IDs
    $resolvedTags = @()
    foreach ($tag in $policyBody.roleScopeTagIds) {
        $resolvedTags += Resolve-ScopeTagId -TagName $tag
    }
    $policyBody.roleScopeTagIds = $resolvedTags

    Write-Host "`n$($policyBody.name)" -ForegroundColor White
    $result = New-IntunePolicy -PolicyBody $policyBody -AssignTo $item.AssignTo -ExcludeGroups $item.ExcludeGroups

    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; file = $item.File
        assignTo = $item.AssignTo; excludeGroups = @($item.ExcludeGroups); created = $result.Created
        updated = $result.Updated; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Updated) { $stats.Updated++ }
    if ($result.Error -and -not ($result.Created -or $result.Updated)) { $stats.Failed++ }
}

# ---------------------------------------------------------------------------
# 8. Write deployment log and summary
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference -and $deploymentLog.Count -gt 0) {
    $deployedBy = try { (Get-MgContext).Account } catch { $env:USERNAME }
    $logContent = [ordered]@{
        deployedAt = (Get-Date -Format 'o')
        deployedBy = $deployedBy
        totalPolicies = $deploymentLog.Count
        created = $stats.Created; updated = $stats.Updated; failed = $stats.Failed
        policies = @($deploymentLog)
    }
    # Write log next to manifest or in current directory
    if ($PSCmdlet.ParameterSetName -eq 'Manifest') {
        $logDir = Split-Path $ManifestFile -Parent
    } else {
        $logDir = "."
    }
    $logPath = Join-Path $logDir "deployment-log.json"
    $logJson = $logContent | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText(
        (Resolve-Path $logDir | Join-Path -ChildPath "deployment-log.json"),
        $logJson, [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "`nDeployment log: $logPath" -ForegroundColor Gray
}

Write-Host "`n=== Deployment Summary ===" -ForegroundColor White
Write-Host "  Created:    $($stats.Created)" -ForegroundColor Green
Write-Host "  Updated:    $($stats.Updated)" -ForegroundColor Cyan
Write-Host "  Failed:     $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
$assigned = @($deploymentLog | Where-Object { $_.assigned }).Count
Write-Host "  Assigned:   $assigned" -ForegroundColor $(if ($assigned -gt 0) { 'Cyan' } else { 'Gray' })
