<#
.SYNOPSIS
    Exports CIS configuration policies from Intune to a CSV for review,
    using a manifest as the source of truth.

.DESCRIPTION
    Reads the manifest (default ./output/manifest.json), extracts the canonical
    policy name from each referenced JSON file, then queries Graph for all
    configurationPolicies and emits id,name for any whose name matches the
    manifest set.

    Review the CSV, remove any rows you don't want deleted, then pass it
    to Delete-CISPolicies.ps1 -CsvFile.

    Reports:
      - manifest entries with no matching policy in the tenant
      - matched policies whose roleScopeTagIds doesn't include the expected tag

.PARAMETER Tenant
    Target tenant: QA or Prod.

.PARAMETER ManifestFile
    Path to manifest.json. Defaults to ./output/manifest.json. Policy file paths
    in the manifest are resolved relative to the manifest's own directory.

.PARAMETER ExpectedScopeTag
    Scope tag displayName the matched policies are expected to carry. Defaults
    to "001". Used for a non-fatal warning only — not as a filter — so that
    duplicate/mistagged copies still appear in the export and can be cleaned up.

.PARAMETER OutputFile
    Path to write the CSV. Defaults to ./cis-policies-<tenant>.csv.

.EXAMPLE
    pwsh Export-CISPolicies.ps1 -Tenant Prod
    pwsh Export-CISPolicies.ps1 -Tenant QA -ManifestFile ./output/manifest.json
#>
param(
    [Parameter(Mandatory)]
    [ValidateSet('QA', 'Prod')]
    [string]$Tenant,

    [string]$ManifestFile = "./output/manifest.json",

    [string]$ExpectedScopeTag = '001',

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

# ---------------------------------------------------------------------------
# 1. Load manifest and extract expected policy names from each JSON file
# ---------------------------------------------------------------------------
if (-not (Test-Path $ManifestFile)) {
    Write-Error "Manifest not found: $ManifestFile"
    return
}

$manifestRaw = Get-Content $ManifestFile -Encoding UTF8 -Raw
if ($manifestRaw[0] -eq [char]0xFEFF) { $manifestRaw = $manifestRaw.Substring(1) }
$manifest = $manifestRaw | ConvertFrom-Json
$manifestDir = Split-Path (Resolve-Path $ManifestFile) -Parent

$expectedNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$missingFiles = @()
foreach ($entry in $manifest) {
    $policyPath = Join-Path $manifestDir $entry.file
    if (-not (Test-Path $policyPath)) {
        $missingFiles += $entry.file
        continue
    }
    $raw = Get-Content $policyPath -Encoding UTF8 -Raw
    if ($raw[0] -eq [char]0xFEFF) { $raw = $raw.Substring(1) }
    $policy = $raw | ConvertFrom-Json
    if ($policy.name) {
        [void]$expectedNames.Add([string]$policy.name)
    }
}

Write-Host "Manifest: $ManifestFile" -ForegroundColor Gray
Write-Host "  Entries:        $($manifest.Count)" -ForegroundColor Gray
Write-Host "  Expected names: $($expectedNames.Count)" -ForegroundColor Gray
if ($missingFiles.Count -gt 0) {
    Write-Warning "Manifest references $($missingFiles.Count) file(s) that don't exist on disk:"
    $missingFiles | ForEach-Object { Write-Warning "  $_" }
}
if ($expectedNames.Count -eq 0) {
    Write-Host "No expected names parsed from manifest. Nothing to export." -ForegroundColor Yellow
    return
}

# ---------------------------------------------------------------------------
# 2. Connect
# ---------------------------------------------------------------------------
$requiredScopes = "DeviceManagementConfiguration.Read.All", "DeviceManagementRBAC.Read.All"
Disconnect-MgGraph -ErrorAction SilentlyContinue | Out-Null
Write-Host "Connecting to Microsoft Graph ($Tenant`: $tenantDomain)..." -ForegroundColor White
Connect-MgGraph -Scopes $requiredScopes -TenantId $tenantDomain -ContextScope Process -ErrorAction Stop
Write-Host "Connected as $((Get-MgContext).Account) [$Tenant]" -ForegroundColor Green

# ---------------------------------------------------------------------------
# 3. Resolve expected scope tag (best-effort, for warning only)
# ---------------------------------------------------------------------------
$expectedTagId = $null
try {
    $tagsResponse = Invoke-MgGraphRequest -Method GET -Uri "https://graph.microsoft.com/beta/deviceManagement/roleScopeTags?`$select=id,displayName"
    $matchedTag = $tagsResponse.value | Where-Object { $_.displayName -eq $ExpectedScopeTag }
    if ($matchedTag) {
        $expectedTagId = [string]$matchedTag.id
        Write-Host "Expected scope tag '$ExpectedScopeTag' -> id $expectedTagId" -ForegroundColor Gray
    } else {
        Write-Warning "Scope tag '$ExpectedScopeTag' not found in tenant; skipping tag-mismatch check."
    }
} catch {
    Write-Warning "Failed to read roleScopeTags ($($_.Exception.Message)); skipping tag-mismatch check."
}

# ---------------------------------------------------------------------------
# 4. Fetch tenant policies and match against manifest names
# ---------------------------------------------------------------------------
$matched = [System.Collections.ArrayList]::new()
$foundNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$tagMismatches = @()

$uri = "https://graph.microsoft.com/beta/deviceManagement/configurationPolicies?`$select=id,name,roleScopeTagIds&`$top=200"
$totalScanned = 0
while ($uri) {
    $response = Invoke-MgGraphRequest -Method GET -Uri $uri
    if ($response.value) {
        $totalScanned += $response.value.Count
        foreach ($p in $response.value) {
            if ($expectedNames.Contains([string]$p.name)) {
                $tagIds = @($p.roleScopeTagIds)
                [void]$matched.Add([PSCustomObject]@{
                    id              = $p.id
                    name            = $p.name
                    roleScopeTagIds = ($tagIds -join ';')
                })
                [void]$foundNames.Add([string]$p.name)
                if ($expectedTagId -and ($tagIds -notcontains $expectedTagId)) {
                    $tagMismatches += "$($p.name) (id=$($p.id), tags=$($tagIds -join ','))"
                }
            }
        }
    }
    $uri = $response['@odata.nextLink']
}

# ---------------------------------------------------------------------------
# 5. Report and write CSV
# ---------------------------------------------------------------------------
$missingFromTenant = @($expectedNames | Where-Object { -not $foundNames.Contains($_) } | Sort-Object)

Write-Host "`nScanned $totalScanned tenant policies" -ForegroundColor Gray
Write-Host "  Matched manifest names:    $($matched.Count) row(s) across $($foundNames.Count) unique name(s)" -ForegroundColor Gray
Write-Host "  Manifest names not found:  $($missingFromTenant.Count)" -ForegroundColor Gray
if ($missingFromTenant.Count -gt 0) {
    Write-Host "    (first 10):" -ForegroundColor Gray
    $missingFromTenant | Select-Object -First 10 | ForEach-Object { Write-Host "      - $_" -ForegroundColor DarkGray }
}
if ($tagMismatches.Count -gt 0) {
    Write-Warning "$($tagMismatches.Count) matched polic(y/ies) are NOT tagged '$ExpectedScopeTag':"
    $tagMismatches | Select-Object -First 10 | ForEach-Object { Write-Warning "  $_" }
}

if ($matched.Count -eq 0) {
    Write-Host "No matching policies found in tenant." -ForegroundColor Yellow
    return
}

$matched | Export-Csv -Path $OutputFile -NoTypeInformation -Encoding UTF8
Write-Host "`nExported to: $OutputFile" -ForegroundColor Green
Write-Host "Review the file, then run:" -ForegroundColor White
Write-Host "  pwsh Delete-CISPolicies.ps1 -Tenant $Tenant -CsvFile $OutputFile" -ForegroundColor Cyan
