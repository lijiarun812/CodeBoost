$ErrorActionPreference = 'Stop'
$config = @{}
Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\.env') -Encoding utf8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $config[$parts[0].Trim()] = $parts[1].Trim().Trim('"').Trim("'") }
    }
}
if (-not $config['DASHSCOPE_API_KEY']) { throw 'DASHSCOPE_API_KEY is missing' }
$base = $config['DASHSCOPE_BASE_URL'].TrimEnd('/')
$model = $config['DASHSCOPE_MODEL']
$body = @{
    model = $model
    messages = @(@{ role = 'user'; content = 'Reply with exactly OK.' })
    max_tokens = 32
    temperature = 0.7
    enable_thinking = $false
} | ConvertTo-Json -Depth 5 -Compress
$headers = @{ Authorization = 'Bearer ' + $config['DASHSCOPE_API_KEY'] }
$response = Invoke-RestMethod -Uri ($base + '/chat/completions') -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 30
Write-Output ('model=' + $response.model)
Write-Output ('reply=' + $response.choices[0].message.content)
