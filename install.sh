#!/usr/bin/env bash
# Instalador do sigaa-ifsc-mcp para Linux (Arch incluído) e macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/heitorsDev/sigaa-ifsc-mcp/main/install.sh | bash
#
# Variáveis opcionais:
#   SIGAA_MCP_REPO   repositório a instalar (padrão: heitorsDev/sigaa-ifsc-mcp)
#   SIGAA_MCP_REF    branch ou tag (padrão: main)
#   SIGAA_MCP_NAME   nome do servidor nos clientes MCP (padrão: sigaa)
#   SIGAA_BROWSER_PATH  navegador do login por janela (padrão: detecta)

set -euo pipefail

# Tudo dentro de main: com `curl | bash`, o bash lê o script inteiro antes de
# executar, e comandos que leem a entrada padrão não engolem o resto do script.
main() {
  local repo="${SIGAA_MCP_REPO:-https://github.com/heitorsDev/sigaa-ifsc-mcp}"
  local ref="${SIGAA_MCP_REF:-main}"
  local name="${SIGAA_MCP_NAME:-sigaa}"
  local source="sigaa-ifsc-mcp @ ${repo%/}/archive/${ref}.zip"

  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

  # 1. uv --------------------------------------------------------------------
  if ! have uv; then
    if have pacman; then
      info "Instalando uv com pacman (pode pedir a senha do sudo)"
      sudo pacman -S --needed --noconfirm uv </dev/null
    else
      have curl || die "curl não encontrado; instale o curl e rode de novo."
      info "Instalando uv (astral.sh)"
      curl -LsSf https://astral.sh/uv/install.sh | sh </dev/null
    fi
  fi
  have uv || die "uv não ficou disponível no PATH."

  # 2. pacote -----------------------------------------------------------------
  info "Instalando sigaa-ifsc-mcp ($ref)"
  uv tool install --force --reinstall "$source" </dev/null
  local bin
  bin="$(uv tool dir --bin)/sigaa-mcp"
  [ -x "$bin" ] || die "sigaa-mcp não foi instalado em $bin."

  # 3. navegador do login por janela ----------------------------------------
  local browser="${SIGAA_BROWSER_PATH:-}"
  if [ -z "$browser" ]; then
    browser="$(find_browser)"
  fi
  if [ -z "$browser" ] && have pacman; then
    info "Instalando o Chromium com pacman (pode pedir a senha do sudo)"
    if sudo pacman -S --needed --noconfirm chromium </dev/null; then
      browser="$(command -v chromium || true)"
    fi
  fi
  if [ -n "$browser" ]; then
    info "Login por janela vai usar: $browser"
  else
    info "Baixando o Chromium do Playwright"
    "$bin" install-browser </dev/null || warn "Falha ao baixar o Chromium; defina SIGAA_BROWSER_PATH e rode de novo."
  fi

  # 4. clientes MCP -------------------------------------------------------
  local registered=0 entry
  entry="$(entry_json "$bin" "$browser")"

  if have claude; then
    info "Registrando no Claude Code (escopo do usuário) como '$name'"
    claude mcp remove --scope user "$name" </dev/null >/dev/null 2>&1 || true
    if claude mcp add-json --scope user "$name" "$entry" </dev/null; then
      registered=1
    else
      warn "Não foi possível registrar no Claude Code."
    fi
  fi

  local desktop_cfg=""
  case "$(uname -s)" in
    Darwin) desktop_cfg="$HOME/Library/Application Support/Claude/claude_desktop_config.json" ;;
    Linux) desktop_cfg="${XDG_CONFIG_HOME:-$HOME/.config}/Claude/claude_desktop_config.json" ;;
  esac
  if [ -n "$desktop_cfg" ] && [ -d "$(dirname "$desktop_cfg")" ]; then
    info "Registrando no Claude Desktop ($desktop_cfg)"
    merge_desktop_config "$desktop_cfg" "$name" "$entry"
    registered=1
    warn "Reinicie o Claude Desktop para carregar o servidor."
  fi

  # 5. fim ------------------------------------------------------------------
  echo
  info "Pronto! Executável: $bin"
  if [ "$registered" = 0 ]; then
    echo "Nenhum cliente MCP encontrado. Configure o seu com:"
    echo "  $entry"
  fi
  case ":$PATH:" in
    *":$(uv tool dir --bin):"*) ;;
    *) echo "Para usar 'sigaa-mcp' no terminal, rode: uv tool update-shell" ;;
  esac
  echo "Para logar agora (abre o navegador): $bin login"
}

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

find_browser() {
  local b
  for b in chromium google-chrome-stable google-chrome chromium-browser microsoft-edge-stable; do
    if have "$b"; then command -v "$b"; return; fi
  done
  for b in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium" \
           "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
    if [ -x "$b" ]; then echo "$b"; return; fi
  done
}

# JSON da entrada do servidor; o Python do uv cuida do escape.
entry_json() {
  uv run --no-project --quiet python -c '
import json, sys
entry = {"type": "stdio", "command": sys.argv[1], "args": []}
if sys.argv[2]:
    entry["env"] = {"SIGAA_BROWSER_PATH": sys.argv[2]}
print(json.dumps(entry))
' "$1" "$2" </dev/null
}

merge_desktop_config() {
  uv run --no-project --quiet python -c '
import json, os, shutil, sys
path, name, entry = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
entry.pop("type", None)
cfg = {}
if os.path.exists(path):
    shutil.copy(path, path + ".bak")
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    cfg = json.loads(text) if text else {}
cfg.setdefault("mcpServers", {})[name] = entry
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
' "$1" "$2" "$3" </dev/null
}

main "$@"
