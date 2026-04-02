<#
.SYNOPSIS
    Deploys CIS policies to Intune via Graph API using output from split-cis-policies.py.

.DESCRIPTION
    Creates configuration policies in Intune from policy JSON files. Policies are
    created without assignments — use a separate assignment workflow to target them.

    Accepts either a manifest file (deploy all) or individual policy JSON paths.

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
            File     = (Join-Path $manifestDir $entry.file)
            AssignTo = if ($entry.PSObject.Properties['assignTo']) { $entry.assignTo } else { $null }
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
    $result = @{ Name = $policyName; Id = $null; Created = $false; Skipped = $false; Assigned = $false; Error = $null }

    if ($WhatIfPreference) {
        $assignMsg = if ($AssignTo) { " (assign: $AssignTo)" } else { "" }
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

    # Assign to group if specified
    if ($AssignTo -and $result.Id) {
        $gid = Resolve-GroupId -GroupName $AssignTo
        if ($gid) {
            try {
                $assignBody = @{
                    assignments = @(@{
                        target = @{
                            '@odata.type' = '#microsoft.graph.groupAssignmentTarget'
                            groupId       = $gid
                        }
                    })
                }
                Invoke-MgGraphRequest -Method POST -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($result.Id)')/assign" -Body ($assignBody | ConvertTo-Json -Depth 10) -ContentType "application/json" | Out-Null
                $result.Assigned = $true
                Write-Host "  ASSIGNED: $policyName -> $AssignTo" -ForegroundColor Green
            } catch {
                Write-Warning "Failed to assign '$policyName': $($_.Exception.Message)"
                $result.Error = "Assignment failed: $($_.Exception.Message)"
            }
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
    Write-Host "[WhatIf] Would connect to Microsoft Graph ($Tenant`: $tenantDomain)" -ForegroundColor DarkYellow
}

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
$stats = @{ Created = 0; Skipped = 0; Failed = 0 }
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
    $result = New-IntunePolicy -PolicyBody $policyBody -AssignTo $item.AssignTo

    [void]$deploymentLog.Add([ordered]@{
        name = $result.Name; id = $result.Id; file = $item.File
        assignTo = $item.AssignTo; created = $result.Created
        skipped = $result.Skipped; assigned = $result.Assigned; error = $result.Error
    })

    if ($result.Created) { $stats.Created++ }
    if ($result.Skipped) { $stats.Skipped++ }
    if ($result.Error -and -not $result.Created) { $stats.Failed++ }
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
        created = $stats.Created; skipped = $stats.Skipped; failed = $stats.Failed
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
Write-Host "  Skipped:    $($stats.Skipped) (already exist)" -ForegroundColor Yellow
Write-Host "  Failed:     $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
$assigned = @($deploymentLog | Where-Object { $_.assigned }).Count
Write-Host "  Assigned:   $assigned" -ForegroundColor $(if ($assigned -gt 0) { 'Cyan' } else { 'Gray' })
