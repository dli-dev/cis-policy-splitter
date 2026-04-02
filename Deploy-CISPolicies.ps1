<#
.SYNOPSIS
    Deploys CIS policies to Intune via Graph API using output from split-cis-policies.py.

.DESCRIPTION
    Reads output/manifest.json and the corresponding policy JSON files, then
    creates each policy in Intune with appropriate scope tags and assignments.

    Run split_cis_policies.py first to generate the output directory.

.PARAMETER OutputDir
    Directory containing manifest.json and policy JSON files. Default: ./output

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -OutputDir ./output

.EXAMPLE
    pwsh Deploy-CISPolicies.ps1 -OutputDir ./output -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$OutputDir = "./output"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Load manifest
# ---------------------------------------------------------------------------
$manifestPath = Join-Path $OutputDir "manifest.json"
if (-not (Test-Path $manifestPath)) {
    Write-Error "Manifest not found: $manifestPath. Run split_cis_policies.py first."
    return
}

$manifestRaw = Get-Content $manifestPath -Encoding UTF8 -Raw
if ($manifestRaw[0] -eq [char]0xFEFF) { $manifestRaw = $manifestRaw.Substring(1) }
$manifest = $manifestRaw | ConvertFrom-Json

Write-Host "Loaded manifest: $($manifest.Count) policies" -ForegroundColor Gray

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
# 3. Policy creation
# ---------------------------------------------------------------------------
function Find-ExistingPolicy {
    param([string]$PolicyName)
    if ($WhatIfPreference) { return $null }
    try {
        $encodedName = $PolicyName -replace "'", "''"
        $uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$filter=name eq '$encodedName'&`$select=id,name"
        $response = Invoke-MgGraphRequest -Method GET -Uri $uri
        if ($response.value -and $response.value.Count -gt 0) { return $response.value[0] }
    } catch {}
    return $null
}

function New-IntunePolicy {
    param([hashtable]$PolicyBody, [string]$AssignTo)

    $policyName = $PolicyBody.name
    $result = @{ Name = $policyName; Id = $null; Created = $false; Skipped = $false; Assigned = $false; AssignTo = $AssignTo; Error = $null }

    if ($WhatIfPreference) {
        Write-Host "  [WhatIf] Would create: $policyName (assign: $AssignTo)" -ForegroundColor DarkYellow
        $result.Id = 'WHATIF-ID'
        return $result
    }

    $existing = Find-ExistingPolicy -PolicyName $policyName
    if ($existing) {
        Write-Host "  SKIP (exists): $policyName (ID: $($existing.id))" -ForegroundColor Yellow
        $result.Id = $existing.id; $result.Skipped = $true
        return $result
    }

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

    if ($AssignTo -ne 'None' -and $result.Id) {
        try {
            $target = switch ($AssignTo) {
                'AllDevices' { @{ '@odata.type' = '#microsoft.graph.allDevicesAssignmentTarget' } }
                'AllUsers'   { @{ '@odata.type' = '#microsoft.graph.allLicensedUsersAssignmentTarget' } }
            }
            $assignBody = @{ assignments = @(@{ target = $target }) }
            Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
            $result.Assigned = $true
            Write-Host "  ASSIGNED: $policyName -> $AssignTo" -ForegroundColor Green
        } catch {
            Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
            $result.Error = "Assignment failed: $($_.Exception.Message)"
        }
    }

    return $result
}

# ---------------------------------------------------------------------------
# 4. Connect to Graph
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference) {
    $requiredScope = "DeviceManagementConfiguration.ReadWrite.All"
    try {
        $context = Get-MgContext
        if (-not $context -or $context.Scopes -notcontains $requiredScope) {
            throw "need auth"
        }
        Write-Host "Graph API: Connected as $($context.Account)" -ForegroundColor Gray
    } catch {
        Write-Host "Connecting to Microsoft Graph..." -ForegroundColor White
        Connect-MgGraph -Scopes $requiredScope -ErrorAction Stop
        Write-Host "Connected as $((Get-MgContext).Account)" -ForegroundColor Green
    }
} else {
    Write-Host "[WhatIf] Would connect to Microsoft Graph" -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------------------
# 5. Deploy each policy from manifest
# ---------------------------------------------------------------------------
$stats = @{ Created = 0; Skipped = 0; Failed = 0 }
$deploymentLog = [System.Collections.ArrayList]::new()

foreach ($entry in $manifest) {
    $policyPath = Join-Path $OutputDir $entry.file
    $raw = Get-Content $policyPath -Encoding UTF8 -Raw
    if ($raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    $policyBody = $raw | ConvertFrom-Json -AsHashtable

    # Resolve scope tag names to IDs
    $resolvedTags = @()
    foreach ($tag in $policyBody.roleScopeTagIds) {
        $resolvedTags += Resolve-ScopeTagId -TagName $tag
    }
    $policyBody.roleScopeTagIds = $resolvedTags

    Write-Host "`n[$($entry.type.ToUpper())] $($policyBody.name)" -ForegroundColor White
    $result = New-IntunePolicy -PolicyBody $policyBody -AssignTo $entry.assignTo

    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; type = $entry.type
        assignTo = $entry.assignTo; created = $result.Created
        skipped = $result.Skipped; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Skipped) { $stats.Skipped++ }
    if ($result.Error -and -not $result.Created) { $stats.Failed++ }
}

# ---------------------------------------------------------------------------
# 6. Write deployment log and summary
# ---------------------------------------------------------------------------
if (-not $WhatIfPreference -and $deploymentLog.Count -gt 0) {
    $deployedBy = try { (Get-MgContext).Account } catch { $env:USERNAME }
    $logContent = [ordered]@{
        deployedAt = (Get-Date -Format 'o')
        deployedBy = $deployedBy
        totalPolicies = $deploymentLog.Count
        created = $stats.Created; skipped = $stats.Skipped; failed = $stats.Failed
        policies = @($deploymentLog)
    }
    $logPath = Join-Path $OutputDir "deployment-log.json"
    $logJson = $logContent | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText(
        (Join-Path (Resolve-Path $OutputDir).Path "deployment-log.json"),
        $logJson, [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "`nDeployment log: $logPath" -ForegroundColor Gray
}

Write-Host "`n=== Deployment Summary ===" -ForegroundColor White
Write-Host "  Created: $($stats.Created)" -ForegroundColor Green
Write-Host "  Skipped: $($stats.Skipped) (already exist)" -ForegroundColor Yellow
Write-Host "  Failed:  $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
