<#
.SYNOPSIS
    Lists all Intune configuration policies and security baselines assigned to a security group.

.PARAMETER Tenant
    Target tenant: QA or Prod.

.PARAMETER GroupName
    Display name of the Entra ID security group.

.EXAMPLE
    pwsh Get-GroupPolicyAssignments.ps1 -Tenant QA -GroupName "CIS-Pilot-Devices"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [Parameter(Mandatory)]
    [string]$GroupName
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Connect to Graph
# ---------------------------------------------------------------------------
$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

$requiredScopes = "DeviceManagementConfiguration.Read.All", "Group.Read.All"
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
Write-Host "Connecting to Microsoft Graph ($Tenant`: $tenantDomain)..." -ForegroundColor White
Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]`n" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 2. Resolve group name to ID
# ---------------------------------------------------------------------------
$encodedName = $GroupName -replace "'", "''"
$groupResponse = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/v1.0/groups?`$filter=displayName eq '$encodedName'&`$select=id,displayName"

if (-not $groupResponse.value -or $groupResponse.value.Count -eq 0) {
    Write-Error "Group not found: '$GroupName'"
    return
}

$group = $groupResponse.value[0]
$groupId = $group.id
Write-Host "Group: $($group.displayName) ($groupId)" -ForegroundColor Gray

# ---------------------------------------------------------------------------
# 3. Helper: paginated Graph GET
# ---------------------------------------------------------------------------
function Get-AllPages {
    param([string]$Uri)
    $results = @()
    $nextUri = $Uri
    while ($nextUri) {
        $response = Invoke-MgGraphRequest -Method GET -Uri $nextUri
        if ($response.value) { $results += $response.value }
        $nextUri = if ($response.ContainsKey('@odata.nextLink')) { $response.'@odata.nextLink' } else { $null }
    }
    return $results
}

# ---------------------------------------------------------------------------
# 4. Helper: check if a policy's assignments target our group
# ---------------------------------------------------------------------------
function Get-GroupAssignmentType {
    param([array]$Assignments, [string]$GroupId)
    foreach ($a in $Assignments) {
        $target = $a.target
        if (-not $target) { continue }
        $odata = $target.'@odata.type'
        if ($odata -eq '#microsoft.graph.groupAssignmentTarget' -and $target.groupId -eq $GroupId) {
            return 'Include'
        }
        if ($odata -eq '#microsoft.graph.exclusionGroupAssignmentTarget' -and $target.groupId -eq $GroupId) {
            return 'Exclude'
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 5. Query configuration policies (Settings Catalog)
# ---------------------------------------------------------------------------
Write-Host "`nQuerying configuration policies..." -ForegroundColor Gray
$configPolicies = Get-AllPages -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$select=id,name&`$top=100"
Write-Host "  Found $($configPolicies.Count) configuration policies total" -ForegroundColor Gray

$matchedConfig = @()
foreach ($policy in $configPolicies) {
    $assignments = Get-AllPages -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($policy.id)')/assignments"
    $assignType = Get-GroupAssignmentType -Assignments $assignments -GroupId $groupId
    if ($assignType) {
        $matchedConfig += [PSCustomObject]@{
            Name       = $policy.name
            Type       = 'Configuration Policy'
            Assignment = $assignType
            Id         = $policy.id
        }
    }
}

# ---------------------------------------------------------------------------
# 6. Query security baselines (intents)
# ---------------------------------------------------------------------------
Write-Host "Querying security baselines..." -ForegroundColor Gray
$intents = Get-AllPages -Uri "https://graph.microsoft.com/beta/deviceManagement/intents?`$select=id,displayName,templateId&`$top=100"
Write-Host "  Found $($intents.Count) security baselines total" -ForegroundColor Gray

$matchedBaselines = @()
foreach ($intent in $intents) {
    $assignments = Get-AllPages -Uri "https://graph.microsoft.com/beta/deviceManagement/intents('$($intent.id)')/assignments"
    $assignType = Get-GroupAssignmentType -Assignments $assignments -GroupId $groupId
    if ($assignType) {
        $matchedBaselines += [PSCustomObject]@{
            Name       = $intent.displayName
            Type       = 'Security Baseline'
            Assignment = $assignType
            Id         = $intent.id
        }
    }
}

# ---------------------------------------------------------------------------
# 7. Output results
# ---------------------------------------------------------------------------
$allMatched = @($matchedConfig) + @($matchedBaselines)

Write-Host "`n=== Policies assigned to '$GroupName' ===" -ForegroundColor White

if ($allMatched.Count -eq 0) {
    Write-Host "  No policies found." -ForegroundColor Yellow
} else {
    $allMatched | Format-Table -Property Name, Type, Assignment -AutoSize
    Write-Host "Total: $($allMatched.Count) policies" -ForegroundColor Gray
}
