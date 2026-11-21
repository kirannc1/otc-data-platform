param(
    [Parameter(Mandatory = $true)]
    [System.Net.IPAddress]$PlatformIp,
    [string]$SourceIp = '192.168.29.250',
    [string]$Context = 'kind-otc'
)

$ErrorActionPreference = 'Stop'
$Namespace = 'otc-sources'
$RuleName = 'OTC-Source-Ingestion'
$Ports = @(28080, 28000, 25432, 29000, 29092)
$Principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this on the source machine in PowerShell as Administrator to scope the Windows firewall rule.'
}
if (-not (Get-NetIPAddress -IPAddress $SourceIp -ErrorAction SilentlyContinue)) {
    throw "Source IP $SourceIp must be assigned to a local interface."
}
foreach ($Port in $Ports) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @($SourceIp, '0.0.0.0', '::') }) {
        throw "Port $Port is already bound on $SourceIp. Stop the other source gateway first."
    }
}

kubectl --context $Context -n $Namespace set env statefulset/kafka 'KAFKA_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093,EXTERNAL://:29092' "KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://kafka:9092,EXTERNAL://${SourceIp}:29092" 'KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT,EXTERNAL:PLAINTEXT'
if ($LASTEXITCODE -ne 0) { throw 'Could not configure the Kafka external listener.' }
kubectl --context $Context -n $Namespace rollout status statefulset/kafka --timeout=180s
if ($LASTEXITCODE -ne 0) { throw 'Kafka did not become ready.' }

$Rule = Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue
if ($Rule) {
    Set-NetFirewallRule -Name $RuleName -Enabled True -RemoteAddress ($PlatformIp.ToString()) -LocalAddress $SourceIp
} else {
    New-NetFirewallRule -Name $RuleName -DisplayName 'OTC ingestion from platform host' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $SourceIp -RemoteAddress ($PlatformIp.ToString()) -LocalPort $Ports | Out-Null
}

$Forwards = @(
    @{ Namespace = 'otc-erp'; Target = 'svc/erpnext'; Ports = '28080:8080' }
    @{ Namespace = $Namespace; Target = 'svc/commerce'; Ports = '28000:8000' }
    @{ Namespace = $Namespace; Target = 'svc/postgres'; Ports = '25432:5432' }
    @{ Namespace = $Namespace; Target = 'svc/minio'; Ports = '29000:9000' }
    @{ Namespace = $Namespace; Target = 'pod/kafka-0'; Ports = '29092:29092' }
)

$Jobs = @()
try {
    foreach ($Forward in $Forwards) {
        $Jobs += Start-Job -Name $Forward.Target -ArgumentList $Context, $SourceIp, $Forward -ScriptBlock {
            param($ClusterContext, $ListenIp, $Forward)
            kubectl --context $ClusterContext -n $Forward.Namespace port-forward --address $ListenIp $Forward.Target $Forward.Ports
            if ($LASTEXITCODE -ne 0) { throw "Port-forward failed for $($Forward.Target)" }
        }
    }
    Write-Host 'Source gateway starting. Keep this window open. Press Ctrl+C to stop.'
    while ($true) {
        foreach ($Job in $Jobs) {
            Receive-Job $Job
            if ($Job.State -in @('Completed', 'Failed', 'Stopped')) {
                throw "Source gateway stopped for $($Job.Name). Restart the gateway after the source is ready."
            }
        }
        Start-Sleep -Seconds 2
    }
} finally {
    $Jobs | Remove-Job -Force
    Disable-NetFirewallRule -Name $RuleName
}
