param(
  [Parameter(Mandatory=$true)][string]$OutputDirectory,
  [Parameter(Mandatory=$true)][string]$WorkDirectory
)
$ErrorActionPreference='Stop'
$src=$PSScriptRoot
$out=[IO.Path]::GetFullPath($OutputDirectory)
$work=[IO.Path]::GetFullPath($WorkDirectory)
if(Test-Path -LiteralPath (Join-Path $out 'AI-Lamp')){throw 'Choose a fresh output directory (existing release files will not be overwritten).'}
New-Item -ItemType Directory -Path $out,$work -Force | Out-Null
Push-Location -LiteralPath $src
try{
  python -m PyInstaller --noconfirm --onedir --windowed --noupx --name AI-Lamp --icon "$src/icons/green.ico" --version-file "$src/windows-version.txt" --add-data "${src}/web:web" --add-data "${src}/web-local:web-local" --add-data "${src}/icons:icons" --distpath $out --workpath $work --specpath $work "$src/server.py"
  if($LASTEXITCODE){exit $LASTEXITCODE}
} finally {Pop-Location}
