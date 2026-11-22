[CmdletBinding(DefaultParameterSetName = 'Preflight')]
param(
    [Parameter(Mandatory)][string]$Config,
    [Parameter(ParameterSetName = 'Preview')][switch]$Preview,
    [Parameter(ParameterSetName = 'Apply')][switch]$Apply,
    [string]$Context = 'kind-otc',
    [ValidateRange(1, 65535)][int]$PostgresPort = 15432,
    [ValidateRange(1, 65535)][int]$CommercePort = 8000,
    [ValidatePattern('^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$')][string]$ExportJob
)

$ErrorActionPreference = 'Stop'
$Config = (Resolve-Path -LiteralPath $Config).Path
$Repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Seed = Join-Path $PSScriptRoot 'seed_volume.py'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Expected $Python. Extract the ZIP directly into the repository root that contains the seed virtual environment."
}

$PlanJson = & $Python $Seed --config $Config --preview
if ($LASTEXITCODE -ne 0) { throw 'Invalid seed configuration. See the error above.' }
$PlanText = $PlanJson -join [Environment]::NewLine
$PlanText
if ($Preview) { return }
$Plan = $PlanText | ConvertFrom-Json
$NeedsDealer = ($Plan.dealer.bookings -gt 0 -or $Plan.dealer.pos_receipts -gt 0)
$KubectlArgs = @('--context', $Context, '-n', 'otc-sources')

if ($NeedsDealer) {
    & kubectl @KubectlArgs get cronjob dealer-export -o name
    if ($LASTEXITCODE -ne 0) { throw 'The dealer-export CronJob must exist.' }
}
if ($Plan.parent_kafka_events -gt 0) {
    & kubectl @KubectlArgs rollout status deployment/outbox --timeout=45s
    if ($LASTEXITCODE -ne 0) { throw 'Kafka outbox deployment is unavailable.' }
}

$Original = @{}
foreach ($Key in @('DEALER_SEED_DSN', 'COMMERCE_URL', 'OTC_SEED_CONTEXT')) {
    $Original[$Key] = [Environment]::GetEnvironmentVariable($Key, 'Process')
}
try {
    if ($NeedsDealer) {
        $Encoded = & kubectl @KubectlArgs get secret source-credentials -o 'jsonpath={.data.POSTGRES_PASSWORD}'
        if ($LASTEXITCODE -ne 0 -or -not $Encoded) { throw 'Could not read the PostgreSQL Secret.' }
        $Password = [Uri]::EscapeDataString([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded)))
        $env:DEALER_SEED_DSN = "postgresql://postgres:${Password}@127.0.0.1:${PostgresPort}/dealer_legacy"
        Remove-Variable Encoded, Password
    }
    $env:COMMERCE_URL = "http://127.0.0.1:${CommercePort}"
    $env:OTC_SEED_CONTEXT = $Context
    $Arguments = @($Seed, '--config', $Config, '--expect-config-sha256', $Plan.config_sha256)
    if ($Apply) { $Arguments += '--apply' }
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Seed exited with code $LASTEXITCODE. Review the source error above." }
} finally {
    foreach ($Key in $Original.Keys) {
        [Environment]::SetEnvironmentVariable($Key, $Original[$Key], 'Process')
    }
}
if (-not $Apply) { return }

if ($Plan.parent_kafka_events -gt 0) {
    $Prefix = "P-$($Plan.batch_id)-"
    $Expected = [string]$Plan.parent_kafka_events
@'
import sys
from time import monotonic, sleep
from main import connect

prefix, expected = sys.argv[1], int(sys.argv[2])
deadline = monotonic() + 120
while True:
    with connect() as db:
        total, published = db.execute(
            "SELECT count(*), count(*) FILTER (WHERE published) "
            "FROM outbox WHERE left(event_id, length(%s)) = %s",
            (prefix, prefix),
        ).fetchone()
    if total == expected and published == expected:
        print(f"Kafka verified {published} events for {prefix}")
        break
    if total > expected or monotonic() >= deadline:
        raise SystemExit(f"Expected {expected} Kafka events, found {total} queued and {published} published")
    sleep(2)
'@ | & kubectl @KubectlArgs exec -i deploy/outbox -- python - $Prefix $Expected
    if ($LASTEXITCODE -ne 0) { throw 'Kafka verification failed. Inspect the outbox and rerun the same batch.' }
}

if ($NeedsDealer) {
    if (-not $ExportJob) { $ExportJob = "dealer-export-$($Plan.batch_id.ToLowerInvariant())" }
    $Existing = & kubectl @KubectlArgs get job $ExportJob --ignore-not-found -o name
    if ($LASTEXITCODE -ne 0) { throw "Could not check Job $ExportJob." }
    if (-not $Existing) {
        & kubectl @KubectlArgs create job --from=cronjob/dealer-export $ExportJob
        if ($LASTEXITCODE -ne 0) { throw "Could not create Job $ExportJob." }
    }
    & kubectl @KubectlArgs wait --for=condition=complete "job/$ExportJob" --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw "Job $ExportJob did not finish. Check its logs." }
    $Logs = & kubectl @KubectlArgs logs "job/$ExportJob"
    if ($LASTEXITCODE -ne 0 -or ($Logs -notcontains "Published dealer/exports/$ExportJob/manifest.json")) {
        throw "Could not confirm the dealer export manifest for $ExportJob."
    }
    $Logs
}
