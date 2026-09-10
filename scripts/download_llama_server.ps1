# Скачивает последнюю сборку llama-server (Vulkan x64) с GitHub Releases
# в папку llama-server\ рядом с проектом. Vulkan работает на Intel Arc без доп. рантаймов.
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Dest = Join-Path $ProjectDir "llama-server"
New-Item -ItemType Directory -Force $Dest | Out-Null

Write-Host "Ищу последний релиз llama.cpp..."
$rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
$asset = $rel.assets | Where-Object { $_.name -like "*bin-win-vulkan-x64.zip" } | Select-Object -First 1
if (-not $asset) { throw "Сборка win-vulkan-x64 не найдена в последнем релизе" }

$zip = Join-Path $Dest "llama.zip"
Write-Host "Скачивание $($asset.name)..."
Invoke-WebRequest $asset.browser_download_url -OutFile $zip
Expand-Archive $zip -DestinationPath $Dest -Force
Remove-Item $zip

# Прописываем путь к бинарнику в .env, если там еще нет LLAMA_SERVER_BIN
$envFile = Join-Path $ProjectDir ".env"
if (Test-Path $envFile) {
    $content = Get-Content $envFile -Raw
    if ($content -notmatch "LLAMA_SERVER_BIN=") {
        $exe = Get-ChildItem -Recurse -Filter llama-server.exe $Dest | Select-Object -First 1
        if ($exe) {
            Add-Content $envFile "`nLLAMA_SERVER_BIN=$($exe.FullName)"
            Write-Host "В .env добавлен LLAMA_SERVER_BIN=$($exe.FullName)"
        }
    }
}
Write-Host "Готово: $Dest"
