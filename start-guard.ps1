$ErrorActionPreference = 'Stop'
$guardConfig = Join-Path $PSScriptRoot 'guard-settings.json'
if (-not (Test-Path -LiteralPath $guardConfig -PathType Leaf)) {
    throw '请先运行 install.cmd。'
}
$guardSettings = Get-Content -LiteralPath $guardConfig -Raw -Encoding UTF8 | ConvertFrom-Json
$guardLogRoot = Join-Path $guardSettings.dataRoot 'process-runtime'
New-Item -ItemType Directory -Force -Path $guardLogRoot | Out-Null
$guardStamp = Get-Date -Format 'yyyyMMdd-HHmmss-ffff'
$guardScript = Join-Path $PSScriptRoot 'process_runtime.py'
$guardProcess = Start-Process -FilePath $guardSettings.pythonExe -ArgumentList ('-B -u "' + $guardScript + '" supervise') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $guardLogRoot ('supervisor-' + $guardStamp + '.out.log')) -RedirectStandardError (Join-Path $guardLogRoot ('supervisor-' + $guardStamp + '.error.log')) -PassThru
Write-Host ('已提交后台启动，PID ' + $guardProcess.Id + '。是否正常运行请看守卫弹窗心跳。')
Write-Host ('运行数据：' + $guardSettings.dataRoot)
