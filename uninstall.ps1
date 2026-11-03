param([int]$WaitPid=0,[switch]$Preview,[string]$ConfirmId='')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$root=[IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$appId='LocalPaperReader_7ec94129'
function Assert-SafePath([string]$candidate) {
 $full=[IO.Path]::GetFullPath($candidate)
 if (-not $full.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Delete path outside project' }
 $node=$full
 while ($node -and $node.Length -ge $root.Length) {
  if (Test-Path -LiteralPath $node) {
   if ((Get-Item -Force -LiteralPath $node).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked path rejected' }
  }
  $node=[IO.Path]::GetDirectoryName($node)
 }
 return $full
}
$marker=Get-Content -LiteralPath (Assert-SafePath (Join-Path $root 'config\project.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
if ($marker.app_id -ne $appId -or $marker.root -ne $root) { throw 'Project identity mismatch' }
$manifest=Get-Content -LiteralPath (Assert-SafePath (Join-Path $root 'config\manifest.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
$paths=@()
foreach ($relative in $manifest.files) {
 if ([IO.Path]::IsPathRooted($relative) -or $relative -match '(^|[\\/])\.\.([\\/]|$)') { throw 'Invalid manifest entry' }
 $paths+=Assert-SafePath (Join-Path $root $relative)
}
$dynamic=@('data\papers.db','data\export.json','data\export.json.tmp','data\papers.db-journal','data\papers.db-wal','data\papers.db-shm','data\fetch.lock','data\ai.lock','data\fetch-progress.json','data\fetch-progress.json.tmp','data\ai-progress.json','data\ai-progress.json.tmp','data\fetch.stop.signal','data\ai.stop.signal','data\worker.lock','data\progress.json','data\progress.json.tmp','data\stop.signal','data\ui.json','data\ui.lock','config\settings.json','config\settings.json.tmp','config\appearance.json','config\appearance.json.tmp','config\key.dat','config\key.tmp','config\project.json','config\project.json.tmp','config\uninstalling','logs\app.log','logs\app.log.1','logs\app.log.2')
foreach ($relative in $dynamic) { $paths+=Assert-SafePath (Join-Path $root $relative) }
$paths=@($paths | Sort-Object -Unique)
if ($Preview) {
 [PSCustomObject]@{root=$root;task=$appId;files=@($paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })} | ConvertTo-Json -Depth 3
 exit
}
if ($ConfirmId -ne $appId) {
 Write-Host 'This removes only registered Paper Reader files. Other files are preserved.'
 if ((Read-Host 'Type DELETE to continue') -ne 'DELETE') { exit }
}
& (Join-Path $root 'schedule.ps1') -Action Remove | Out-Null
$stop=Assert-SafePath (Join-Path $root 'data\stop.signal')
[IO.File]::WriteAllText($stop,'stop')
[IO.File]::WriteAllText((Join-Path $root 'config\uninstalling'),'stop')
if ($WaitPid -gt 0) {
 try { Wait-Process -Id $WaitPid -Timeout 30 -ErrorAction Stop } catch {
  if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) { throw 'Application still running; files preserved' }
 }
}
# Wait for all project Python processes to release files; never kill another Python.
$deadline=[DateTime]::UtcNow.AddSeconds(50)
do {
 $processes=@(Get-CimInstance Win32_Process | Where-Object {
  $_.ExecutablePath -in @((Join-Path $root 'runtime\python.exe'),(Join-Path $root 'runtime\pythonw.exe'))
 })
 if ($processes.Count -eq 0) { break }
 Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)
if ($processes.Count -gt 0) { throw 'A project process is still running. Retry after closing the app.' }
# Release this process's working-directory handle before removing an empty root.
$parentDirectory=[IO.Path]::GetDirectoryName($root)
Set-Location -LiteralPath $parentDirectory
[Environment]::CurrentDirectory=$parentDirectory
# Validate every target before any deletion, then delete literal files only.
foreach ($path in $paths) { Assert-SafePath $path | Out-Null }
foreach ($path in $paths) {
 if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
}
$remaining=@($paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf })
if ($remaining.Count -gt 0) { throw ('Some registered files remain: '+($remaining -join ', ')) }
# Remove only empty known directories. Unrelated files prevent directory deletion.
$dirs=@($manifest.dirs | Sort-Object { $_.Length } -Descending)
foreach ($relative in $dirs) {
 $path=Assert-SafePath (Join-Path $root $relative)
 if ((Test-Path -LiteralPath $path -PathType Container) -and @(Get-ChildItem -Force -LiteralPath $path).Count -eq 0) { Remove-Item -LiteralPath $path }
}
if (@(Get-ChildItem -Force -LiteralPath $root).Count -eq 0) { Remove-Item -LiteralPath $root }
Write-Host 'Project removed. Unrelated files, if any, were preserved.'
