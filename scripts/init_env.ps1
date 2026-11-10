function New-HexSecret {
    $bytes = [byte[]]::new(32)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return -join ($bytes | ForEach-Object { $_.ToString('x2') })
}
$jwt = New-HexSecret
$pg = New-HexSecret
$redis = New-HexSecret
$minioUser = 'dograh' + (New-HexSecret).Substring(0, 12)
$minioPass = New-HexSecret

$content = @(
    "REGISTRY=ghcr.io/dograh-hq",
    "ENABLE_TELEMETRY=true",
    "OSS_JWT_SECRET=$jwt",
    "POSTGRES_PASSWORD=$pg",
    "REDIS_PASSWORD=$redis",
    "MINIO_ROOT_USER=$minioUser",
    "MINIO_ROOT_PASSWORD=$minioPass"
)
[System.IO.File]::WriteAllLines((Join-Path (Get-Location) '.env'), $content, [System.Text.UTF8Encoding]::new($false))
Write-Host "Generated .env successfully"
