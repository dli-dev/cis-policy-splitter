<#
.SYNOPSIS
    Deletes CIS policies from Intune using a reviewed CSV or deployment log.

.DESCRIPTION
    Deletes configuration policies by their exact IDs from a CSV file
    (exported by Export-CISPolicies.ps1) or a deployment log.

    This script does NOT query Intune for policies — it only deletes
    the specific IDs provided. Review the input file before running.

.PARAMETER Tenant
    Target tenant: QA or Prod. Resolves to utorontoqa.onmicrosoft.com or utoronto.onmicrosoft.com.

.PARAMETER CsvFile
    Path to a CSV with id and name columns (from Export-CISPolicies.ps1).

.PARAMETER DeploymentLog
    Path to deployment-log.json (from Deploy-CISPolicies.ps1).

.PARAMETER TestMode
    Deletes only the first entry. Useful for verifying connectivity and permissions.

.EXAMPLE
    pwsh Export-CISPolicies.ps1 -Tenant Prod
    # Review cis-policies-prod.csv, remove rows you want to keep
    pwsh Delete-CISPolicies.ps1 -Tenant Prod -CsvFile ./cis-policies-prod.csv

.EXAMPLE
    pwsh Delete-CISPolicies.ps1 -Tenant QA -DeploymentLog ./output/deployment-log.json

.EXAMPLE
    pwsh Delete-CISPolicies.ps1 -Tenant Prod -CsvFile ./cis-policies-prod.csv -TestMode
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [Parameter(ParameterSetName = 'Csv', Mandatory)]
    [string]$CsvFile,

    [Parameter(ParameterSetName = 'Log', Mandatory)]
    [string]$DeploymentLog,

    [switch]$TestMode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 1. Load delete list (id + name)
# ---------------------------------------------------------------------------
# Each entry: @{ Id = '...'; Name = '...' }
$deleteTargets = @()

if ($PSCmdlet.ParameterSetName -eq 'Csv') {
    if (-not (Test-Path $CsvFile)) {
        Write-Error "CSV file not found: $CsvFile"
        return
    }
    $rows = Import-Csv $CsvFile -Encoding UTF8
    foreach ($row in $rows) {
        if (-not $row.id) {
            Write-Error "CSV is missing 'id' column. Expected columns: id, name"
            return
        }
        $deleteTargets += @{ Id = $row.id; Name = $row.name }
    }
    Write-Host "Loaded CSV: $($deleteTargets.Count) policies" -ForegroundColor Gray

} else {
    if (-not (Test-Path $DeploymentLog)) {
        Write-Error "Deployment log not found: $DeploymentLog"
        return
    }
    $logRaw = Get-Content $DeploymentLog -Encoding UTF8 -Raw
    if ($logRaw[0] -eq [char]0xFEFF) { $logRaw = $logRaw.Substring(1) }
    $log = $logRaw | ConvertFrom-Json

    foreach ($entry in $log.policies) {
        if ($entry.id -and $entry.id -ne 'WHATIF-ID') {
            $deleteTargets += @{ Id = $entry.id; Name = $entry.name }
        }
    }
    Write-Host "Loaded deployment log: $($deleteTargets.Count) policies" -ForegroundColor Gray
}

if ($deleteTargets.Count -eq 0) {
    Write-Host "No policies to delete." -ForegroundColor Yellow
    return
}

if ($TestMode) {
    Write-Host "[TestMode] Limiting to first entry only: $($deleteTargets[0].Name)" -ForegroundColor Magenta
    $deleteTargets = @($deleteTargets[0])
}

# ---------------------------------------------------------------------------
# 2. Connect to Graph
# ---------------------------------------------------------------------------
$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

if (-not $WhatIfPreference) {
    $requiredScopes = "DeviceManagementConfiguration.ReadWrite.All"
    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    Write-Host "Connecting to Microsoft Graph ($Tenant`: $tenantDomain)..." -ForegroundColor White
    Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
    Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]" -ForegroundColor Green
} else {
    Write-Host "[WhatIf] Would connect to Microsoft Graph ($Tenant`: $tenantDomain)" -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------------------
# 3. Confirmation
# ---------------------------------------------------------------------------
Write-Host "`n=== Deletion Plan ===" -ForegroundColor White
Write-Host "  Tenant:   $Tenant ($tenantDomain)" -ForegroundColor White
Write-Host "  Policies: $($deleteTargets.Count)" -ForegroundColor White

if ($WhatIfPreference) {
    foreach ($t in $deleteTargets) {
        Write-Host "  [WhatIf] Would delete: $($t.Name) (ID: $($t.Id))" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "`nPolicies to delete:" -ForegroundColor Red
    foreach ($t in $deleteTargets) {
        Write-Host "  - $($t.Name) (ID: $($t.Id))" -ForegroundColor Red
    }
    $confirm = Read-Host "`nType DELETE to confirm deletion of $($deleteTargets.Count) policies"
    if ($confirm -ne 'DELETE') {
        Write-Host "Aborted." -ForegroundColor Yellow
        return
    }
}

# ---------------------------------------------------------------------------
# 4. Delete each policy by ID
# ---------------------------------------------------------------------------
$stats = @{ Deleted = 0; NotFound = 0; Failed = 0 }

foreach ($target in $deleteTargets) {
    Write-Host "`n$($target.Name)" -ForegroundColor White

    if ($WhatIfPreference) { continue }

    try {
        Invoke-MgGraphRequest -Method DELETE -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies(%27$($target.Id)%27)"
        Write-Host "  DELETED: $($target.Name) (ID: $($target.Id))" -ForegroundColor Green
        $stats.Deleted++
    } catch {
        $msg = $_.Exception.Message
        if ($msg -match '404|NotFound') {
            Write-Host "  NOT FOUND: $($target.Name) (ID: $($target.Id)) — already deleted?" -ForegroundColor Yellow
            $stats.NotFound++
        } else {
            Write-Warning "Failed to delete '$($target.Name)' (ID: $($target.Id)): $msg"
            $stats.Failed++
        }
    }
}

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
Write-Host "`n=== Deletion Summary ===" -ForegroundColor White
Write-Host "  Deleted:    $($stats.Deleted)" -ForegroundColor Green
Write-Host "  Not found:  $($stats.NotFound)" -ForegroundColor Yellow
Write-Host "  Failed:     $($stats.Failed)" -ForegroundColor $(if ($stats.Failed -gt 0) { 'Red' } else { 'Gray' })
