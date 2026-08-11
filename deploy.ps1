<#
Simple deploy helper (Windows PowerShell).
Requisitos locales:
- `gh` (GitHub CLI) instalado y autenticado (`gh auth login`).
- `git` instalado.

Uso:
.
  ./deploy.ps1 -RepoName "mi-repo-boletines"

El script crea el repo en GitHub, establece `origin` y hace push de la rama `main`.
#>

param(
    [string]$RepoName = "boletines-monitor",
    [string]$Visibility = "public"
)

Write-Host "Comprobando git..."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git no está instalado o no está en PATH. Instálalo y vuelve a correr el script."
    exit 1
}

Write-Host "Comprobando gh (GitHub CLI)..."
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Error "GitHub CLI (gh) no está instalado. Instálalo desde https://cli.github.com/ y autenticate con 'gh auth login'."
    exit 1
}

if (-not (Test-Path .git)) {
    Write-Host "Inicializando repo git local..."
    git init
}

# Ensure .gitignore exists
$gitignore = @"venv
__pycache__
*.pyc
seen.json
state.json
new_items.json
last_run.json
server.log
*.env
"@
Set-Content -Path .gitignore -Value $gitignore -Encoding UTF8

git add --all
if ((git status --porcelain) -ne '') {
    git commit -m "Initial commit: boletines monitor"
} else {
    Write-Host "No hay cambios para commitear."
}

Write-Host "Creando repo en GitHub: $RepoName ($Visibility)..."
try {
    gh repo create $RepoName --$Visibility --source=. --remote=origin --push
    Write-Host "Repo creado y push realizado."
} catch {
    Write-Error "Fallo creando el repo con gh: $_"
    Write-Host "Si el repo ya existe en GitHub, agrega el remoto manualmente y haz 'git push origin main'."
    exit 1
}

Write-Host "Listo. Repo publicado en GitHub."
Write-Host "Siguiente paso: desde Render (https://render.com) crea un nuevo Web Service y conecta este repo. Usa Start Command: 'gunicorn app:app --bind 0.0.0.0:$PORT'"
Write-Host "Si querés, pega aquí la URL del repo y te doy los pasos exactos para conectar y configurar variables (todo lo que yo pueda hacer sin credenciales)."
