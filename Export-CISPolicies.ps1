<#
.SYNOPSIS
    Exports CIS configuration policies from Intune to a CSV for review.

.DESCRIPTION
    Connects to Graph, retrieves all configurationPolicies, filters to those
    whose name starts with "CIS", and writes id,name to a CSV file.

    Review the CSV, remove any rows you don't want deleted, then pass it
    to Delete-CISPolicies.ps1 -CsvFile.

.PARAMETER Tenant
    Target tenant: QA or Prod.

.PARAMETER OutputFile
    Path to write the CSV. Defaults to ./cis-policies-<tenant>.csv.

.EXAMPLE
    pwsh Export-CISPolicies.ps1 -Tenant Prod
    pwsh Export-CISPolicies.ps1 -Tenant QA -OutputFile ./my-export.csv
#>
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [string]$OutputFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$tenantDomain = switch ($Tenant) {
    'QA'   { 'utorontoqa.onmicrosoft.com' }
    'Prod' { 'utoronto.onmicrosoft.com' }
}

if (-not $OutputFile) {
    $OutputFile = "./cis-policies-$($Tenant.ToLower()).csv"
}

# Connect
$requiredScopes = "DeviceManagementConfiguration.Read.All"
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
Write-Host "Connecting to Microsoft Graph ($Tenant`: $tenantDomain)..." -ForegroundColor White
Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]" -ForegroundColor Green

# Fetch all policies, keep only CIS
$cisPolicies = [System.Collections.ArrayList]::new()
$uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$select=id,name&`$top=200"
$totalScanned = 0
while ($uri) {
    $response = Invoke-MgGraphRequest -Method GET -Uri $uri
    if ($response.value) {
        $totalScanned += $response.value.Count
        foreach ($p in $response.value) {
            if ($p.name -like 'CIS *') {
                [void]$cisPolicies.Add([PSCustomObject]@{
                    id   = $p.id
                    name = $p.name
                })
            }
        }
    }
    $uri = $response['@odata.nextLink']
}

Write-Host "Scanned $totalScanned policies, found $($cisPolicies.Count) starting with 'CIS'" -ForegroundColor Gray

if ($cisPolicies.Count -eq 0) {
    Write-Host "No CIS policies found." -ForegroundColor Yellow
    return
}

$cisPolicies | Export-Csv -Path $OutputFile -NoTypeInformation -Encoding UTF8
Write-Host "`nExported to: $OutputFile" -ForegroundColor Green
Write-Host "Review the file, then run:" -ForegroundColor White
Write-Host "  pwsh Delete-CISPolicies.ps1 -Tenant $Tenant -CsvFile $OutputFile" -ForegroundColor Cyan
