# Instalador do sigaa-ifsc-mcp para Windows.
#
#   irm https://raw.githubusercontent.com/heitorsDev/sigaa-ifsc-mcp/main/install.ps1 | iex
#
# Variáveis opcionais: SIGAA_MCP_REPO, SIGAA_MCP_REF (padrão: main),
# SIGAA_MCP_NAME (nome nos clientes MCP, padrão: sigaa).

& {
    $ErrorActionPreference = 'Stop'

    $repo = if ($env:SIGAA_MCP_REPO) { $env:SIGAA_MCP_REPO.TrimEnd('/') } else { 'https://github.com/heitorsDev/sigaa-ifsc-mcp' }
    $ref = if ($env:SIGAA_MCP_REF) { $env:SIGAA_MCP_REF } else { 'main' }
    $name = if ($env:SIGAA_MCP_NAME) { $env:SIGAA_MCP_NAME } else { 'sigaa' }
    $source = "sigaa-ifsc-mcp @ $repo/archive/$ref.zip"

    function Info($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
    function Warn($msg) { Write-Host "!! $msg" -ForegroundColor Yellow }
    function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

    $env:Path = "$HOME\.local\bin;$env:Path"

    # 1. uv
    if (-not (Have 'uv')) {
        Info 'Instalando uv (astral.sh)'
        powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
        $env:Path = "$HOME\.local\bin;$env:Path"
    }
    if (-not (Have 'uv')) { throw 'uv não ficou disponível no PATH.' }

    # 2. pacote
    Info "Instalando sigaa-ifsc-mcp ($ref)"
    uv tool install --force --reinstall $source
    if ($LASTEXITCODE -ne 0) { throw 'Falha ao instalar o pacote.' }
    $bin = Join-Path (uv tool dir --bin).Trim() 'sigaa-mcp.exe'
    if (-not (Test-Path $bin)) { throw "sigaa-mcp não foi instalado em $bin." }

    # 3. navegador: o login por janela usa o Edge, que já vem no Windows.
    $edge = @(
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($edge) {
        Info "Login por janela vai usar o Edge: $edge"
    } else {
        Info 'Edge não encontrado; baixando o Chromium do Playwright'
        $env:SIGAA_BROWSER_CHANNEL = ''
        & $bin install-browser
        if ($LASTEXITCODE -ne 0) { Warn 'Falha ao baixar o Chromium.' }
    }

    # 4. clientes MCP
    $registered = $false

    if (Have 'claude') {
        Info "Registrando no Claude Code (escopo do usuário) como '$name'"
        try { claude mcp remove --scope user $name *> $null } catch { }
        claude mcp add --scope user $name -- $bin
        if ($LASTEXITCODE -eq 0) { $registered = $true } else { Warn 'Não foi possível registrar no Claude Code.' }
    }

    # Claude Desktop: instalação comum e versão da Microsoft Store.
    $desktopDirs = @("$env:APPDATA\Claude")
    $desktopDirs += Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
        ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' }
    foreach ($dir in $desktopDirs | Where-Object { Test-Path $_ }) {
        $cfgPath = Join-Path $dir 'claude_desktop_config.json'
        Info "Registrando no Claude Desktop ($cfgPath)"
        $cfg = $null
        if (Test-Path $cfgPath) {
            Copy-Item $cfgPath "$cfgPath.bak" -Force
            $text = Get-Content $cfgPath -Raw -Encoding UTF8
            if ($text -and $text.Trim()) { $cfg = $text | ConvertFrom-Json }
        }
        if (-not $cfg) { $cfg = New-Object PSObject }
        if (-not $cfg.PSObject.Properties['mcpServers']) {
            $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue (New-Object PSObject)
        }
        $entry = New-Object PSObject -Property @{ command = $bin; args = @() }
        $cfg.mcpServers | Add-Member -NotePropertyName $name -NotePropertyValue $entry -Force
        $json = $cfg | ConvertTo-Json -Depth 32
        [IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))
        $registered = $true
        Warn 'Reinicie o Claude Desktop para carregar o servidor.'
    }

    # 5. fim
    Write-Host ''
    Info "Pronto! Executável: $bin"
    if (-not $registered) {
        Write-Host 'Nenhum cliente MCP encontrado. Configure o seu com o comando:'
        Write-Host "  $bin"
    }
    Write-Host "Para logar agora (abre o navegador): & '$bin' login"
}
