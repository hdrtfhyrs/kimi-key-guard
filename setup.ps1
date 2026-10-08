param([string]$PythonPath, [string]$DataRoot, [string]$ExtensionId)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw '当前发布包仅支持Windows。' }
$guardRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$guardConfigPath = Join-Path $guardRoot 'guard-settings.json'
$guardPreviousSettings = $null
if (Test-Path -LiteralPath $guardConfigPath -PathType Leaf) {
    $guardPreviousSettings = Get-Content -LiteralPath $guardConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
}
if (-not $DataRoot -and $guardPreviousSettings) { $DataRoot = $guardPreviousSettings.dataRoot }
if (-not $DataRoot) {
    if (Test-Path -LiteralPath 'D:\' -PathType Container) { $DataRoot = 'D:\KimiKeyGuard\data' }
    else { $DataRoot = Join-Path $env:LOCALAPPDATA 'KimiKeyGuard\data' }
}
$guardData = [System.IO.Path]::GetFullPath($DataRoot).TrimEnd([char]92)
if (-not [System.IO.Path]::IsPathRooted($DataRoot)) { throw 'DataRoot必须是绝对目录。' }
if ($guardData.Equals($guardRoot, [StringComparison]::OrdinalIgnoreCase) -or $guardData.StartsWith($guardRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw '数据目录必须在扩展文件夹之外。'
}
$guardCandidates = New-Object 'System.Collections.Generic.List[string]'
if ($PythonPath) { $guardCandidates.Add($PythonPath) }
if (-not $PythonPath -and $guardPreviousSettings) { $guardCandidates.Add($guardPreviousSettings.pythonExe) }
$guardPy = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $PythonPath -and $guardPy) {
    try {
        $guardDetected = & $guardPy.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $guardDetected) { $guardCandidates.Add(([string]$guardDetected).Trim()) }
    } catch {}
}
if (-not $PythonPath) {
    foreach ($guardName in @('python.exe', 'python3.exe')) {
        $guardCommand = Get-Command $guardName -ErrorAction SilentlyContinue
        if ($guardCommand -and $guardCommand.Source -notlike '*\Microsoft\WindowsApps\*') { $guardCandidates.Add($guardCommand.Source) }
    }
}
$guardPython = $null
foreach ($guardCandidate in $guardCandidates) {
    if (-not (Test-Path -LiteralPath $guardCandidate -PathType Leaf)) { continue }
    try {
        $guardVersion = & $guardCandidate -B -c 'import sys; print(sys.executable) if sys.version_info >= (3,10) else sys.exit(3)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $guardVersion) { $guardPython = ([string]$guardVersion).Trim(); break }
    } catch {}
}
if (-not $guardPython) { throw '未找到Python 3.10或更新版。请从 https://www.python.org/downloads/windows/ 安装，或运行 setup.ps1 -PythonPath 你的python.exe绝对路径。' }
if (-not $ExtensionId) {
    $guardNormalized = $guardRoot.Substring(0, 1).ToUpperInvariant() + $guardRoot.Substring(1)
    $guardSha = [System.Security.Cryptography.SHA256]::Create()
    try { $guardDigest = $guardSha.ComputeHash([System.Text.Encoding]::Unicode.GetBytes($guardNormalized)) }
    finally { $guardSha.Dispose() }
    $guardHex = ([System.BitConverter]::ToString($guardDigest)).Replace('-', '').Substring(0, 32).ToLowerInvariant()
    $ExtensionId = -join ($guardHex.ToCharArray() | ForEach-Object { [char](97 + [Convert]::ToInt32([string]$_, 16)) })
}
if ($ExtensionId -notmatch '^[a-p]{32}$') { throw 'ExtensionId应为Chrome扩展卡片上的32位ID。' }
New-Item -ItemType Directory -Force -Path $guardData | Out-Null
$guardUtf8 = New-Object System.Text.UTF8Encoding($false)
$guardBackup = Join-Path $guardData ('install-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss-ffff'))
New-Item -ItemType Directory -Force -Path $guardBackup | Out-Null
foreach ($guardControl in @('guard-settings.json', 'native-host.json', 'evidence-host.cmd')) {
    $guardOld = Join-Path $guardRoot $guardControl
    if (Test-Path -LiteralPath $guardOld -PathType Leaf) { Copy-Item -LiteralPath $guardOld -Destination (Join-Path $guardBackup $guardControl) }
}
$guardRegistryName = 'Software\Google\Chrome\NativeMessagingHosts\com.user.kimi_key_guard_evidence'
$guardRegistryBefore = @()
foreach ($guardView in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    $guardBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, $guardView)
    try {
        $guardOldKey = $guardBase.OpenSubKey($guardRegistryName)
        $guardOldValue = $null
        if ($guardOldKey) { try { $guardOldValue = $guardOldKey.GetValue('') } finally { $guardOldKey.Dispose() } }
        $guardRegistryBefore += @{view=[string]$guardView; previousDefault=$guardOldValue}
    } finally { $guardBase.Dispose() }
}
[System.IO.File]::WriteAllText((Join-Path $guardBackup 'registry-before.json'), ($guardRegistryBefore | ConvertTo-Json -Depth 5), $guardUtf8)
$guardSettings = @{schema=1; pythonExe=$guardPython; dataRoot=$guardData; extensionId=$ExtensionId; installedAt=[DateTime]::UtcNow.ToString('o')}
[System.IO.File]::WriteAllText($guardConfigPath, ($guardSettings | ConvertTo-Json -Depth 5), $guardUtf8)
$guardCmd = '@echo off' + [Environment]::NewLine + 'chcp 65001 >nul' + [Environment]::NewLine + '"' + $guardPython.Replace('%','%%') + '" -B -u "%~dp0evidence-host.py" %*' + [Environment]::NewLine
[System.IO.File]::WriteAllText((Join-Path $guardRoot 'evidence-host.cmd'), $guardCmd, $guardUtf8)
$guardManifest = @{name='com.user.kimi_key_guard_evidence';description='Kimi key guard local processes and authentic screenshot evidence';path=(Join-Path $guardRoot 'evidence-host.cmd');type='stdio';allowed_origins=@('chrome-extension://' + $ExtensionId + '/')}
$guardManifestPath = Join-Path $guardRoot 'native-host.json'
[System.IO.File]::WriteAllText($guardManifestPath, ($guardManifest | ConvertTo-Json -Depth 5), $guardUtf8)
foreach ($guardView in @([Microsoft.Win32.RegistryView]::Registry32, [Microsoft.Win32.RegistryView]::Registry64)) {
    $guardBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::CurrentUser, $guardView)
    try {
        $guardKey = $guardBase.CreateSubKey($guardRegistryName)
        try {
            $guardKey.SetValue('', $guardManifestPath, [Microsoft.Win32.RegistryValueKind]::String)
            if ($guardKey.GetValue('') -ne $guardManifestPath) { throw 'Chrome本机桥注册未保存成功。' }
        } finally { $guardKey.Dispose() }
    } finally { $guardBase.Dispose() }
}
Write-Host '已安装当前用户的Chrome本机桥，不需要管理员权限。'
Write-Host ('扩展目录：' + $guardRoot)
Write-Host ('扩展ID：' + $ExtensionId)
Write-Host ('数据目录：' + $guardData)
Write-Host '下一步：Chrome扩展页打开开发者模式，加载包含manifest.json的本目录；从已登录Kimi控制台打开守卫。'
Write-Host '没有新增Windows开机、登录或计划任务。'
& (Join-Path $guardRoot 'start-guard.ps1')
