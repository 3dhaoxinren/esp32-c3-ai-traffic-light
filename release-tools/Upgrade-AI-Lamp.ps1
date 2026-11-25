[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\AI-Lamp'),
    [string[]]$LegacyExe = @(),
    [switch]$NoLaunch,
    [switch]$PlanOnly,
    [switch]$SkipDesktopScan,
    [switch]$NoRegistry
)
$ErrorActionPreference = 'Stop'
$version = '3.1.5'
$components = @('AI-Lamp.exe','_internal','licenses','LICENSE.txt','THIRD-PARTY-NOTICES.txt','使用说明.txt','package-manifest.json')
$legacyHash = 'AD47DFB624B43215EF82C2EBF6F7E3073900735BF3989ABF3B9F758C4F77504A'
$reg = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$regName = 'HLKM30AIIndicator'

function Canonical([string]$path) {
    if ($path -notmatch '^[a-zA-Z]:[\\/]') { throw "Not a local absolute path: $path" }
    return [IO.Path]::GetFullPath($path).TrimEnd('\')
}
function Assert-NotRedirected([string]$path) {
    $p = $path
    while ($p) {
        if (Test-Path -LiteralPath $p) {
            if ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "不升级指向其他位置的链接路径：$p"
            }
        }
        $next = Split-Path -LiteralPath $p
        if (!$next -or $next -eq $p) { break }
        $p = $next
    }
}
function Child([string]$root,[string]$name) {
    if ([IO.Path]::IsPathRooted($name) -or $name -match '(^|[\\/])\.\.($|[\\/])|[:*?]' ) { throw "Invalid package path: $name" }
    $p = Canonical (Join-Path $root $name)
    if (!$p.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw "路径不在指定目录内：$name" }
    Assert-NotRedirected $p
    return $p
}
function Digest([string]$path) {
    $algorithm=[Security.Cryptography.SHA256]::Create()
    $stream=$null
    try {
        $stream=[IO.File]::OpenRead($path)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-','')
    } finally {
        if ($stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}
function Windows-Processes([string]$condition) {
    if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {
        return Get-CimInstance Win32_Process -Filter $condition -ErrorAction Stop
    }
    return Get-WmiObject -Class Win32_Process -Filter $condition -ErrorAction Stop
}
function Is-Unified([string]$path) {
    if ([IO.Path]::GetFileName($path) -ne 'AI-Lamp.exe' -or !(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    Assert-NotRedirected $path
    $v=(Get-Item -LiteralPath $path).VersionInfo
    return $v.ProductName -eq 'AI Lamp Unified' -and $v.ProductVersion -match '^3\.(0|1)\.[0-9]+$'
}
function Is-Legacy([string]$path) {
    if ([IO.Path]::GetFileName($path) -ne 'AI指示灯.exe' -or !(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    Assert-NotRedirected $path
    return (Digest $path) -eq $legacyHash
}
function Is-UnifiedSource([string]$path) {
    if ($path -notmatch '^[A-Za-z]:[\\/].+[\\/]server\.py$' -or !(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
    $root=Canonical (Split-Path -LiteralPath $path)
    Assert-NotRedirected (Canonical $path)
    $html=Child $root 'web\index.html'
    if (!(Test-Path -LiteralPath $html -PathType Leaf)) { return $false }
    $entry=Get-Content -LiteralPath $path -Encoding UTF8 -Raw
    $ui=Get-Content -LiteralPath $html -Encoding UTF8 -Raw
    return $entry -match 'class AppServer\(ThreadingHTTPServer\)' -and $entry -match 'from desktop_tray import DesktopTray, SingleInstance' -and
        $ui -match 'id="tab-audio"' -and $ui -match 'class="version-tag">3\.[01]\.[0-9]+<'
}
function Command-Snapshot($ep) {
    try {
        $script = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri ($ep.url+'/session.js')).Content.Trim()
        if ($script -notmatch '^window\.HLK_SESSION=("[A-Za-z0-9_-]+");$') { throw 'Invalid session' }
        $h = @{'X-Session-Token'=($Matches[1] | ConvertFrom-Json)}
        $status = Invoke-RestMethod -TimeoutSec 3 -Uri ($ep.url+'/api/state') -Headers $h
        return @{headers=$h;state=$status}
    } catch { throw '无法安全关闭旧版。请从旧版托盘退出，再运行升级。' }
}
function Ensure-Idle($snapshot) {
    if ($snapshot.state.busy -or $snapshot.state.connected -or $snapshot.state.lights.busy) {
        throw '旧版正在操作设备，请等操作结束、释放 AT 连接后再升级。'
    }
}
function Quote-Executable([string]$p) { if ($p -match '\s') { return '"'+$p+'"' }; return $p }

$source = Canonical (Join-Path $PSScriptRoot 'Windows')
if (!(Test-Path -LiteralPath $source -PathType Container)) { $source=Canonical $PSScriptRoot }
$manifestPath=Child $source 'package-manifest.json'
$manifest=Get-Content -LiteralPath $manifestPath -Encoding UTF8 -Raw | ConvertFrom-Json
if ($manifest.product -ne 'AI Lamp Unified' -or $manifest.version -ne $version -or !$manifest.files) { throw '更新包清单不符。请重新下载完整 ZIP。' }
$seen=@{}
foreach ($entry in $manifest.files) {
    $rel=[string]$entry.path
    $first=($rel -split '[\\/]')[0]
    if ($first -notin $components -or $first -eq 'package-manifest.json' -or $seen.ContainsKey($rel) -or
        $entry.sha256 -notmatch '^[A-Fa-f0-9]{64}$') { throw '更新包清单包含无效文件。' }
    $seen[$rel]=$true
    $p=Child $source $rel
    if (!(Test-Path -LiteralPath $p -PathType Leaf) -or (Digest $p) -ne $entry.sha256) {
        throw "更新包文件校验失败：$rel"
    }
}
$sourceExe=Child $source 'AI-Lamp.exe'
if (!$seen.ContainsKey('AI-Lamp.exe') -or !$seen.ContainsKey('_internal/web/index.html') -or
    !(Is-Unified $sourceExe) -or (Get-Item -LiteralPath $sourceExe).VersionInfo.ProductVersion -ne $version) {
    throw 'Windows 程序不完整或版本不符。'
}

$dest=Canonical $InstallDir
if ($dest.Length -lt 7 -or $dest -eq $source -or $dest.StartsWith($source+'\',[StringComparison]::OrdinalIgnoreCase) -or
    ($source.StartsWith($dest+'\',[StringComparison]::OrdinalIgnoreCase))) { throw '请选择与解压目录分开的专用安装目录。' }
$profileRoot=Canonical (Join-Path $env:LOCALAPPDATA 'HLK-M30')
if ($dest -eq $profileRoot -or $dest.StartsWith($profileRoot+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw '请勿把程序安装在用户设置和配对数据目录。'
}
Assert-NotRedirected $dest
$destExe=Child $dest 'AI-Lamp.exe'
$haveUnified=Is-Unified $destExe
if ($haveUnified -and [version]((Get-Item -LiteralPath $destExe).VersionInfo.ProductVersion) -gt [version]$version) {
    throw '安装目录已有更新版本，未降级。'
}
if ((Test-Path -LiteralPath $dest) -and !$haveUnified) {
    if (!(Test-Path -LiteralPath $dest -PathType Container) -or @(Get-ChildItem -LiteralPath $dest -Force).Count) {
        throw '目标目录已有别的内容，未改动。请选择空目录或原统一版目录。'
    }
}

$desktop=[Environment]::GetFolderPath('Desktop')
$known=@{}
$shortcuts=@{}
if (!$SkipDesktopScan -and $desktop) {
    $default=Join-Path $desktop 'HLK-M30工具\AI指示灯.exe'
    if (Is-Legacy $default) { $known[(Canonical $default)]=$true }
    $shell=New-Object -ComObject WScript.Shell
    foreach ($lnk in @(Get-ChildItem -LiteralPath $desktop -File -Filter 'AI指示灯*.lnk')) {
        $target=$shell.CreateShortcut($lnk.FullName).TargetPath
        if ($target -and (Is-Legacy $target)) {
            $known[(Canonical $target)]=$true
            $shortcuts[(Canonical $lnk.FullName)]=$true
        }
    }
}
foreach ($candidate in $LegacyExe) {
    $p=Canonical $candidate
    if (!(Is-Legacy $p)) { throw "指定文件不是已验证的 2.7.0 旧版；不会移走：$p" }
    $known[$p]=$true
}

$oldRun=$null
if (!$NoRegistry) { $oldRun=(Get-ItemProperty -LiteralPath $reg -ErrorAction SilentlyContinue).$regName }
$ep=$null;$snap=$null;$oldProc=$null
$epFile=Child $profileRoot 'ai-endpoint.json'
if (Test-Path -LiteralPath $epFile -PathType Leaf) {
    try {
        $obj=Get-Content -LiteralPath $epFile -Encoding UTF8 -Raw | ConvertFrom-Json
        if ($obj.url -match '^http://127\.0\.0\.1:[1-9][0-9]{0,4}$' -and [int]$obj.pid -gt 0) {
            $oldProc=Windows-Processes ("ProcessId = "+[int]$obj.pid)
            if ($oldProc -and $oldProc.ExecutablePath) {
                $pp=Canonical $oldProc.ExecutablePath
                $pName=[IO.Path]::GetFileName($pp)
                $safe=(Is-Legacy $pp) -or (Is-Unified $pp) -or ($pName -match '^pythonw?\.exe$' -and $oldProc.CommandLine -match '(?i)[\\/]server\.py(?:"|\s|$)')
                if ($safe) {
                    $snap=Command-Snapshot $obj
                    if ($snap.state.app_version -match '^(2\.7\.[0-9]+|3\.[01]\.[0-9]+)$' -and $snap.state.desktop.tray) {
                        if ([version]$snap.state.app_version -gt [version]$version) { throw '已有更新版本正在运行，未降级。' }
                        $ep=$obj;Ensure-Idle $snap
                        if (Is-Legacy $pp) { $known[$pp]=$true }
                    }
                }
            }
        }
    } catch { throw }
}
$existing=@(Windows-Processes "Name = 'AI-Lamp.exe' OR Name = 'AI指示灯.exe'")
foreach ($other in $existing) {
    if (!$ep -or [int]$other.ProcessId -ne [int]$ep.pid) { throw '发现还有旧版实例运行。请先从托盘退出所有旧版。' }
}

$migrateRun=$false
if ($oldRun) {
    if ($oldRun -match '^(?:"(?<exe>[^"]+)"|(?<exe>\S+))(?<rest>.*)$') {
        $oldLaunch=$Matches['exe'];$rest=$Matches['rest']
        if ($oldLaunch -match '^[A-Za-z]:[\\/]') {
            $p=Canonical $oldLaunch
            if ((Is-Legacy $p) -and $rest -match '^\s*(--autostart\s+--no-browser)?\s*$') { $known[$p]=$true;$migrateRun=$true }
            if ((Is-Unified $p) -and $rest -match '^\s*(--autostart\s+--no-browser)?\s*$') { $migrateRun=$true }
            if ($p -match '(?i)[\\/]pythonw?\.exe$' -and
                $rest -match '^\s+(?:"(?<file>[^"]+)"|(?<file>\S+))\s+--autostart\s+--no-browser\s*$') {
                $runScript=$Matches['file']
                if (Is-UnifiedSource $runScript) { $migrateRun=$true }
            }
        }
    }
}
Write-Host "新版目录：$dest"
Write-Host "识别到的 2.7.0 旧程序：$($known.Count)；旧版快捷方式：$($shortcuts.Count)"
if ($PlanOnly) { return }
$launchArgs=@('--autostart','--no-browser')
if ($ep) {
    if ($snap.state.app_version -match '^3\.' -and $snap.state.ai.enabled -eq $false) {
        $launchArgs+='--keep-follow-paused'
    }
    $audio=$null
    try { $audio=Invoke-RestMethod -TimeoutSec 3 -Uri ($ep.url+'/api/audio') -Headers $snap.headers } catch {
        if ($snap.state.app_version -notmatch '^2\.7\.') { throw '无法检查旧版音频工作区，请先自行保存并退出旧版。' }
    }
    if ($audio.busy -or $audio.source) { throw '旧版剪辑区还有音频或正在工作。请先保存需要保留的剪辑，再从托盘退出后重试。' }
    [void](Invoke-RestMethod -Method Post -ContentType 'application/json' -Body '{}' -TimeoutSec 4 -Uri ($ep.url+'/api/exit') -Headers $snap.headers)
    $stop=(Get-Date).AddSeconds(12)
    while (Get-Process -Id ([int]$ep.pid) -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $stop) { throw '旧版没有完全退出，未替换任何文件。' }
        Start-Sleep -Milliseconds 180
    }
}

$parent=Canonical (Split-Path -LiteralPath $dest)
$staging=Child $parent ('.AI-Lamp-stage-'+[Guid]::NewGuid().ToString('N'))
$backup=Child $parent ('AI-Lamp-回退备份-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N').Substring(0,6))
New-Item -ItemType Directory -Path $dest,$staging,$backup -Force | Out-Null
$moved=@();$new=@();$didRun=$false
try {
    foreach ($entry in $manifest.files) {
        $from=Child $source $entry.path
        $to=Child $staging $entry.path
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $to) -Force | Out-Null
        Copy-Item -LiteralPath $from -Destination $to
        if ((Digest $to) -ne $entry.sha256) { throw '暂存校验失败。' }
    }
    Copy-Item -LiteralPath $manifestPath -Destination (Child $staging 'package-manifest.json')
    foreach ($name in $components) {
        $from=Child $dest $name;$fromStage=Child $staging $name
        if (Test-Path -LiteralPath $from) {
            if (!$haveUnified) { throw '目标新增了文件，停止替换。' }
            $old=Child $backup ('unified\'+$name)
            New-Item -ItemType Directory -Path (Split-Path -LiteralPath $old) -Force | Out-Null
            Move-Item -LiteralPath $from -Destination $old
            $moved+=@{original=$from;copy=$old}
        }
        if (Test-Path -LiteralPath $fromStage) {
            Move-Item -LiteralPath $fromStage -Destination $from
            $new+=@{dst=$from;stage=$fromStage}
        }
    }
    $i=0
    foreach ($from in @(@($known.Keys)+@($shortcuts.Keys))) {
        Assert-NotRedirected $from
        if (!(Test-Path -LiteralPath $from -PathType Leaf)) { continue }
        if ($from.EndsWith('.exe',[StringComparison]::OrdinalIgnoreCase) -and !(Is-Legacy $from)) { throw '旧版文件已变化。' }
        $old=Child $backup ('legacy-'+(++$i)+'\'+[IO.Path]::GetFileName($from))
        New-Item -ItemType Directory -Path (Split-Path -LiteralPath $old) -Force | Out-Null
        Move-Item -LiteralPath $from -Destination $old
        $moved+=@{original=$from;copy=$old}
    }
    if ($migrateRun) {
        Set-ItemProperty -LiteralPath $reg -Name $regName -Value ((Quote-Executable $destExe)+' --autostart --no-browser')
        $didRun=$true
    }
    @{oldAutostart=$oldRun;autostartMigrated=$didRun;files=$moved} | ConvertTo-Json -Depth 6 |
        Set-Content -LiteralPath (Child $backup '回退记录.json') -Encoding UTF8
    if (!$NoLaunch) { Start-Process -FilePath $destExe -ArgumentList $launchArgs -WindowStyle Hidden }
} catch {
    if ($didRun) { Set-ItemProperty -LiteralPath $reg -Name $regName -Value $oldRun }
    for ($i=$new.Count-1;$i -ge 0;--$i) {
        if (Test-Path -LiteralPath $new[$i].dst) { Move-Item -LiteralPath $new[$i].dst -Destination $new[$i].stage }
    }
    for ($i=$moved.Count-1;$i -ge 0;--$i) {
        if (Test-Path -LiteralPath $moved[$i].copy) { Move-Item -LiteralPath $moved[$i].copy -Destination $moved[$i].original }
    }
    throw
} finally {
    if ((Test-Path -LiteralPath $staging) -and @(Get-ChildItem -LiteralPath $staging -Force).Count -eq 0) {
        Remove-Item -LiteralPath $staging
    }
}
Write-Host "升级完成。连接设置、配对资料与 C3 固件和设备录音未修改。"
if ($oldRun -and !$migrateRun) { Write-Host '存在未识别的旧自启命令，未改动；如需自启，请在新版重新保存。' }
Write-Host "旧版已移到：$backup"
