# Build with XeLaTeX twice to resolve slide numbers and cover positions.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$latexCommand = Get-Command xelatex -ErrorAction SilentlyContinue
if ($latexCommand) { $latexExe = $latexCommand.Source }
elseif (Test-Path -LiteralPath 'D:/texlive/2025/bin/windows/xelatex.exe') { $latexExe = 'D:/texlive/2025/bin/windows/xelatex.exe' }
else { throw 'XeLaTeX is required. Install TeX Live/MiKTeX or use Overleaf with XeLaTeX.' }
New-Item -ItemType Directory -Force build | Out-Null
for ($pass = 0; $pass -lt 2; $pass++) {
    & $latexExe '-interaction=nonstopmode' '-halt-on-error' '-output-directory=build' 'MotionMuse.tex'
    if ($LASTEXITCODE -ne 0) { throw 'Compilation failed. See build/MotionMuse.log.' }
}
Copy-Item -LiteralPath 'build/MotionMuse.pdf' -Destination 'MotionMuse.pdf' -Force
Write-Output 'Created MotionMuse.pdf'
