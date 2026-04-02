<#
.SYNOPSIS
    Retrieves an Intune configuration policy by name and dumps its full JSON.

.PARAMETER Tenant
    Target tenant: QA or Prod.

.PARAMETER PolicyName
    Display name of the policy to retrieve.

.PARAMETER OutputFile
    Optional path to write the JSON output. If omitted, prints to console.

.EXAMPLE
    pwsh Get-IntunePolicy.ps1 -Tenant Prod -PolicyName "CIS 89.14 - Deny Local Log On - Alt (none)"

.EXAMPLE
    pwsh Get-IntunePolicy.ps1 -Tenant Prod -PolicyName "CIS BL - BitLocker" -OutputFile debug-bitlocker.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [Parameter(Mandatory)]
    [string]$PolicyName,

    [string]$OutputFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

$requiredScopes = "DeviceManagementConfiguration.Read.All"
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
Write-Host "Connecting to Microsoft Graph ($Tenant: $tenantDomain)..." -ForegroundColor White
Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -NoWelcome -ErrorAction Stop
Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]`n" -ForegroundColor Green

# Find the policy
$encodedName = $PolicyName -replace "'", "''"
$uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$filter=name eq '$encodedName'&`$select=id,name"
$response = Invoke-MgGraphRequest -Method GET -Uri $uri

if (-not $response.value -or $response.value.Count -eq 0) {
    Write-Error "Policy not found: '$PolicyName'"
    return
}

$policy = $response.value[0]
Write-Host "Found: $($policy.name) (ID: $($policy.id))" -ForegroundColor Gray

# Get full policy details
$fullPolicy = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies('$($policy.id)')?`$expand=settings"

$json = $fullPolicy | ConvertTo-Json -Depth 30

if ($OutputFile) {
    [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location).Path $OutputFile),
        $json, [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "`nWritten to: $OutputFile" -ForegroundColor Green
} else {
    Write-Host "`n$json"
}
