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

# Прописываем путь к бинарнику в .env
# ВАЖНО: раньше строка добавлялась только если в .env вообще не было LLAMA_SERVER_BIN,
# но в .env (скопированном из .env.example) уже есть значение-заглушка "llama-server",
# поэтому реальный путь никогда не прописывался. Теперь — всегда заменяем/добавляем.
$exe = Get-ChildItem -Recurse -Filter llama-server.exe $Dest | Select-Object -First 1
if (-not $exe) { throw "llama-server.exe не найден после распаковки архива" }

$envFile = Join-Path $ProjectDir ".env"
$content = if (Test-Path $envFile) { Get-Content $envFile -Raw } else { "" }
$line = "LLAMA_SERVER_BIN=$($exe.FullName)"
if ($content -match "(?m)^LLAMA_SERVER_BIN=.*$") {
    $escaped = $line -replace '\$', '$$'   # $ зарезервирован в строке замены regex
    $content = $content -replace "(?m)^LLAMA_SERVER_BIN=.*$", $escaped
    [IO.File]::WriteAllText($envFile, $content)
    Write-Host "В .env обновлен LLAMA_SERVER_BIN=$($exe.FullName)"
} else {
    Add-Content $envFile "`n$line"
    Write-Host "В .env добавлен LLAMA_SERVER_BIN=$($exe.FullName)"
}
Write-Host "Готово: $Dest"
