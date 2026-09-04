#!/bin/sh
# Jarvis-TUI — instala links em ~/.local/bin e o prompt do agente no opencode.
# Uso: ./install.sh [--no-agent]
set -e
AQUI="$(cd "$(dirname "$0")" && pwd)"
DEST="${JARVIS_BIN:-$HOME/.local/bin}"
mkdir -p "$DEST"

for f in "$AQUI"/bin/*; do
  nome="$(basename "$f")"
  chmod +x "$f"
  ln -sf "$f" "$DEST/$nome"
  echo "link: $DEST/$nome"
done
# biblioteca comum (dotfile: o glob acima não pega, link explícito)
if [ -f "$AQUI/bin/.jarvis-comum" ]; then
  chmod +x "$AQUI/bin/.jarvis-comum"
  ln -sf "$AQUI/bin/.jarvis-comum" "$DEST/.jarvis-comum"
  echo "link: $DEST/.jarvis-comum"
fi

if [ "${1:-}" != "--no-agent" ]; then
  AG="$HOME/.config/opencode/agent"
  mkdir -p "$AG"
  if [ -f "$AG/jarvis.md" ]; then
    cp "$AG/jarvis.md" "$AG/jarvis.md.bak-$(date +%Y%m%d)"
    echo "backup do jarvis.md atual em $AG"
  fi
  cp "$AQUI/agent/jarvis.md" "$AG/jarvis.md"
  echo "agente: $AG/jarvis.md"
fi

echo "ok, senhor. Rode: jarvis-tui"
