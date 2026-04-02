<##############################################################################
   
    CIS Microsoft Intune for Windows 11 Benchmark v4.0.0 Build Kit script
    Section #81 - System Services
    Level 2 (L2)

    The purpose of this script is to configure a system using the recommendations 
    provided in the Benchmark, section(s), and profile level listed above to a 
    hardened state consistent with a CIS Benchmark. 
    
    The script can be tailored to the organization's needs such as by creating 
    exceptions or adding additional event logging.

    This script can be deployed through various means, including Intune script 
    manager, running it locally, or through any automation tool.

    Version: 1.10
    Updated: 24.Apr.2025 by jjarose

##############################################################################>

#Requires -RunAsAdministrator

$L2Services = @{
    'Bluetooth Audio Gateway Service'                           = 'HKLM:\SYSTEM\CurrentControlSet\Services\BTAGService'
    'Bluetooth Support Service'                                 = 'HKLM:\SYSTEM\CurrentControlSet\Services\bthserv'
    'Downloaded Maps Manager'                                   = 'HKLM:\SYSTEM\CurrentControlSet\Services\MapsBroker'
    'GameInput Service'                                         = 'HKLM:\SYSTEM\CurrentControlSet\Services\GameInputSvc'
    'Geolocation Service'                                       = 'HKLM:\SYSTEM\CurrentControlSet\Services\lfsvc'
    'Link-Layer Topology Discovery Mapper'                      = 'HKLM:\SYSTEM\CurrentControlSet\Services\lltdsvc'
    'Microsoft iSCSI Initiator Service'                         = 'HKLM:\SYSTEM\CurrentControlSet\Services\MSiSCSI'
    'Print Spooler'                                             = 'HKLM:\SYSTEM\CurrentControlSet\Services\Spooler'
    'Problem Reports and Solutions Control Panel Support'       = 'HKLM:\SYSTEM\CurrentControlSet\Services\wercplsupport'
    'Remote Access Auto Connection Manager'                     = 'HKLM:\SYSTEM\CurrentControlSet\Services\RasAuto'
    'Remote Desktop Configuration'                              = 'HKLM:\SYSTEM\CurrentControlSet\Services\SessionEnv'
    'Remote Desktop LocalServices'                              = 'HKLM:\SYSTEM\CurrentControlSet\Services\TermService'
    'Remote Desktop LocalServices UserMode Port Redirector'     = 'HKLM:\SYSTEM\CurrentControlSet\Services\UmRdpService'
    'Remote Registry'                                           = 'HKLM:\SYSTEM\CurrentControlSet\Services\RemoteRegistry'
    'Server'                                                    = 'HKLM:\SYSTEM\CurrentControlSet\Services\LanmanServer'
    'SNMP Service'                                              = 'HKLM:\SYSTEM\CurrentControlSet\Services\SNMP'
    'Windows Error Reporting Service'                           = 'HKLM:\SYSTEM\CurrentControlSet\Services\WerSvc'
    'Windows Event Collector'                                   = 'HKLM:\SYSTEM\CurrentControlSet\Services\Wecsvc'
    'Windows Push Notifications System Service'                 = 'HKLM:\SYSTEM\CurrentControlSet\Services\WpnService'
    'Windows PushToInstall Service'                             = 'HKLM:\SYSTEM\CurrentControlSet\Services\PushToInstall'
    'Windows Remote Management'                                 = 'HKLM:\SYSTEM\CurrentControlSet\Services\WinRM'
    'WinHTTP Web Proxy Auto-Discovery Service'                  = 'HKLM:\SYSTEM\CurrentControlSet\Services\WinHttpAutoProxySvc'
}

$DisabledCount = 0
$AlreadyDisabledCount = 0
$NotInstalledCount = 0

foreach ($service in $L2Services.GetEnumerator()) {
    $ServiceName = $service.Key
    $ServicePath = $service.Value

    if (Test-Path -LiteralPath $ServicePath) {
        $StartValue = (Get-ItemProperty -LiteralPath $ServicePath).Start

        if ($StartValue -and $StartValue -ne 4) {
            Set-ItemProperty -LiteralPath $ServicePath -Name 'Start' -Value 4
            Write-Host "Disabled service $ServiceName." -ForegroundColor Yellow
            $DisabledCount++
        } elseif ($StartValue -eq 4) {
            Write-Host "Service $ServiceName is already disabled." -ForegroundColor Green
            $AlreadyDisabledCount++
        }
    } else {
        Write-Host "Service $ServiceName is not installed." -ForegroundColor Green
        $NotInstalledCount++
    }
}

Write-Host "`nThis script configured $DisabledCount services as 'Disabled'." -ForegroundColor Cyan
Write-Host "$AlreadyDisabledCount services were already disabled and $NotInstalledCount are not installed." -ForegroundColor Green