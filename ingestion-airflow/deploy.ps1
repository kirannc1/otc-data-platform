$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath 'platform.env' -PathType Leaf)) {
        throw 'Copy platform.env.example to platform.env and fill in the source and Bronze credentials.'
    }
    docker compose --env-file platform.env up --build --detach --wait --wait-timeout 300
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the platform services.' }
    docker compose --env-file platform.env exec -T airflow python /opt/otc/dags/bronze_ingest.py
    if ($LASTEXITCODE -ne 0) { throw 'Ingestion preflight failed. Check the source gateway and the reported endpoint.' }
} finally {
    Pop-Location
}
