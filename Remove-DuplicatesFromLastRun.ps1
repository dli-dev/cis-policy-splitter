<#
.SYNOPSIS
    Deletes the configuration policies created by the most recent
    Deploy-CISPolicies.ps1 run, recovering from a duplicate-creation incident.

.DESCRIPTION
    Reads output/deployment-log.json, picks every entry where created=true,
    and DELETEs each id from configurationPolicies. The original (older)
    policies with the same names are left untouched.

    Run with -WhatIf first to preview. Requires
    DeviceManagementConfiguration.ReadWrite.All.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [string]$LogFile = "./output/deployment-log.json"
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $LogFile)) {
    Write-Error "Deployment log not found: $LogFile"
    return
}

$log = Get-Content $LogFile -Raw -Encoding UTF8 | ConvertFrom-Json
$created = @($log.policies | Where-Object { $_.created -and $_.id })

if ($created.Count -eq 0) {
    Write-Host "No policies marked created=true in $LogFile. Nothing to delete." -ForegroundColor Yellow
    return
}

$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

Write-Host "Tenant:        $Tenant ($tenantDomain)"
Write-Host "Log file:      $LogFile"
Write-Host "Run timestamp: $($log.deployedAt)"
Write-Host "Policies to delete: $($created.Count)"
Write-Host ""

$created | ForEach-Object { Write-Host "  $($_.id)  $($_.name)" }
Write-Host ""

if (-not $WhatIfPreference) {
    $confirm = Read-Host "Proceed with deletion? (y/N)"
    if ($confirm -ne 'y') {
        Write-Host "Aborted." -ForegroundColor Yellow
        return
    }

    Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
    Connect-MgGraph -Scopes "DeviceManagementConfiguration.ReadWrite.All" `
        -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
    Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]" -ForegroundColor Green
}

$deleted = 0
$failed  = 0

foreach ($p in $created) {
    $uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($p.id)')"
    if ($PSCmdlet.ShouldProcess($p.name, "DELETE $($p.id)")) {
        try {
            Invoke-MgGraphRequest -Method DELETE -Uri $uri | Out-Null
            Write-Host "  DELETED: $($p.name) ($($p.id))" -ForegroundColor Green
            $deleted++
        } catch {
            Write-Warning "  Failed to delete $($p.id): $($_.Exception.Message)"
            $failed++
        }
    }
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor White
Write-Host "  Deleted: $deleted" -ForegroundColor Green
Write-Host "  Failed:  $failed" -ForegroundColor $(if ($failed -gt 0) { 'Red' } else { 'Gray' })
