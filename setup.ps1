# Instala el entorno del proyecto (Windows / PowerShell)
# Uso:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "No se encontro el lanzador 'py'. Instala Python 3.12 desde python.org."
}

$py312 = & py -3.12 -c "import sys; print(sys.version_info[:2])" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "No se encontro Python 3.12. Instalalo y vuelve a ejecutar este script."
}

if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
    Write-Warning "Java no esta en el PATH. PySpark necesita Java 17 o superior."
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creando entorno virtual .venv con Python 3.12..."
    & py -3.12 -m venv .venv
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "Actualizando pip..."
& $python -m pip install --upgrade pip

Write-Host "Instalando dependencias..."
& $python -m pip install -r (Join-Path $PSScriptRoot "requirements.txt")

Write-Host "Registrando kernel de Jupyter..."
& $python -m ipykernel install --user --name steam-indie `
    --display-name "Python 3.12 (Steam Indie)"

Write-Host ""
Write-Host "Entorno listo. Verificacion:"
& $python -c "import pyspark, pandas; print('  pyspark', pyspark.__version__); print('  pandas', pandas.__version__)"
Write-Host "  ejecutable: $python"
Write-Host ""
Write-Host "Para activarlo:  .\.venv\Scripts\Activate.ps1"
