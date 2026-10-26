# Изгражда Буквар за Windows: програма + вграден Tesseract + инсталатор.
# Употреба (PowerShell в папката на проекта):
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Нужни: Python 3.10+ (python.org), Tesseract (UB Mannheim), Inno Setup 6.
# Ако Tesseract или Inno Setup липсват, скриптът опитва да ги инсталира с winget.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # иначе изтеглянето в PowerShell 5 е много бавно
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot
$Version = (Select-String -Path "bukvar\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$env:BUKVAR_VERSION = $Version
Write-Host "== Буквар $Version ==" -ForegroundColor Cyan

# Езици, които влизат в инсталатора (модели "fast": бързи и малки)
$Langs = @("bul", "eng", "rus", "ukr", "srp", "mkd", "deu", "fra", "ita", "spa", "tur", "ell", "ron", "osd")

function Find-Tesseract {
    $dirs = @("$env:ProgramFiles\Tesseract-OCR", "${env:ProgramFiles(x86)}\Tesseract-OCR",
              "$env:LOCALAPPDATA\Programs\Tesseract-OCR")
    foreach ($d in $dirs) { if (Test-Path "$d\tesseract.exe") { return $d } }
    return $null
}

function Find-ISCC {
    $paths = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
               "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# 1. Python среда
Write-Host "-- Python среда" -ForegroundColor Cyan
$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd -or $pyCmd.Source -like "*WindowsApps*") {
    throw "Не е намерен Python. Инсталирайте Python 3.12 от https://www.python.org/downloads/ (отметнете 'Add python.exe to PATH') и пуснете отново."
}
if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt pyinstaller --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install не успя" }

# 2. Tesseract
Write-Host "-- Tesseract" -ForegroundColor Cyan
$tess = Find-Tesseract
if (-not $tess) {
    Write-Host "Tesseract не е намерен, инсталирам с winget..."
    winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements --silent
    $tess = Find-Tesseract
    if (-not $tess) { throw "Инсталирайте Tesseract от https://github.com/UB-Mannheim/tesseract/wiki и пуснете отново." }
}
Write-Host "Tesseract: $tess"

# 3. Езикови модели
Write-Host "-- Езикови модели" -ForegroundColor Cyan
$cache = "build\tessdata"
New-Item -ItemType Directory -Force -Path $cache | Out-Null
foreach ($l in $Langs) {
    $f = "$cache\$l.traineddata"
    if (-not (Test-Path $f)) {
        $repo = if ($l -eq "osd") { "tessdata" } else { "tessdata_fast" }
        Write-Host "  изтеглям $l"
        Invoke-WebRequest "https://github.com/tesseract-ocr/$repo/raw/main/$l.traineddata" -OutFile $f
    }
}

# 4. PyInstaller
Write-Host "-- PyInstaller" -ForegroundColor Cyan
& $py -m PyInstaller bukvar.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller не успя" }

# 5. Вграждане на Tesseract (без чуждите езици — само избраните)
Write-Host "-- Вграждане на Tesseract" -ForegroundColor Cyan
$dest = "dist\Bukvar\tesseract"
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Get-ChildItem $tess -File | Where-Object { $_.Extension -in ".exe", ".dll" -and $_.Name -notlike "unins*" } |
    Copy-Item -Destination $dest
New-Item -ItemType Directory -Force -Path "$dest\tessdata" | Out-Null
Copy-Item "$cache\*.traineddata" "$dest\tessdata\"
Copy-Item "$tess\tessdata\pdf.ttf" "$dest\tessdata\"
Copy-Item "LICENSE" "dist\Bukvar\LICENSE.txt"
Copy-Item "THIRD_PARTY.txt" "dist\Bukvar\THIRD_PARTY.txt"

# Бърза проверка, че вградената програма работи
& "dist\Bukvar\bukvar-cli.exe" --list-langs
if ($LASTEXITCODE -ne 0) { throw "Проверката на bukvar-cli не успя" }

# 6. Инсталатор
Write-Host "-- Инсталатор" -ForegroundColor Cyan
$iscc = Find-ISCC
if (-not $iscc) {
    Write-Host "Inno Setup не е намерен, инсталирам с winget..."
    winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements --silent
    $iscc = Find-ISCC
}
if ($iscc) {
    & $iscc installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup не успя" }
    Write-Host "`nГотово: Output\Bukvar-Setup-$Version.exe" -ForegroundColor Green
} else {
    Write-Host "`nПрограмата е в dist\Bukvar (без инсталатор — Inno Setup липсва)." -ForegroundColor Yellow
}
