<div align="center">

```
  ╭────────╮
╭┤ ◜  ◝ ├╮
││  ◉  ││
╰┤ ◟  ◞ ├╯
  ╰────────╯
```

# Jarvis-TUI

**A interface do mordomo — chat, voz e ouvido, conectados ao agente Jarvis via [opencode](https://opencode.ai).**

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?style=flat-square)
![Textual](https://img.shields.io/badge/textual-TUI-00D4FF?style=flat-square)
![Voz local](https://img.shields.io/badge/voz-100%25_local-00FFC8?style=flat-square)

</div>

---

### ✨ O que faz

| Atalho | Ação |
|--------|------|
| digitar + `Enter` | fala com o Jarvis (via `opencode run --agent jarvis`) |
| `ctrl+m` / botão 🎙 | grava do microfone → transcreve → envia direto |
| `ctrl+s` | liga/desliga a voz das respostas |
| `ctrl+w` | liga/desliga o ouvido (`hey jarvis` só dentro da TUI) |
| `ctrl+l` / `ctrl+q` | limpa o chat / sai |
| `/volume 70` · `/brilho +10` · `/foto [area]` | som, tela e captura sem sair do chat |

Painel lateral mostra **núcleo**, **sistemas (MCPs)** com status real (`●` ok, `✗` falho) e **sessão**. Dizer *"pode parar"* durante a gravação encerra e envia. O briefing da manhã traz a **agenda do dia** (requer o MCP `google-calendar` — ver `examples/opencode-mcp.json`).

### 📦 Instalação

```bash
git clone https://github.com/YukiGabriel/jarvis-tui.git
cd jarvis-tui
./install.sh            # links em ~/.local/bin + prompt do agente
uv run --python 3.14 --with textual --with rich src/jarvis_tui.py
# ou, com pyproject:
uv venv && uv pip install -e . && jarvis-tui
```

### 🔊 Voz (opcional)

O chat funciona sem nada disso. Para microfone + respostas faladas, 100% local:

```bash
uv pip install -r requirements-voice.txt
sudo pacman -S alsa-utils ffmpeg          # Arch (Debian/Ubuntu: apt install)
uv tool run --from piper-tts piper ...      # vozes razo/faber (sob demanda)
```

- **STT:** `bin/jarvis-transcrever` (Faster-Whisper, `JARVIS_MODEL=turbo`, `JARVIS_LANG=pt`)
- **TTS rápido:** `bin/jarvis-vozd` (Kokoro-82M aquecido, `JARVIS_VOZD_PORTA=8765`)
- **TTS reserva:** Piper (`--motor piper`) ou `espeak-ng` (`--motor espeak`)

### ⚙️ Variáveis

| Var | Padrão | Efeito |
|-----|--------|--------|
| `JARVIS_BIN` | `~/.local/bin` | onde vivem os auxiliares |
| `JARVIS_TRANSCREVER` / `JARVIS_FALAR` / `JARVIS_TUI` | `$JARVIS_BIN/...` | comandos usados pela TUI |
| `JARVIS_VOZ` | `1` | `0` nasce muda |
| `JARVIS_WAKE` | `1` | `0` desliga o `hey jarvis` |
| `JARVIS_MCP_LIVE` | `1` | `0` desliga o status ao vivo dos MCPs (economiza CPU) |
| `JARVIS_MIC_MAX` | `120` | teto da gravação (s) |
| `JARVIS_APPS` | `0` | `1` abre apps junto no briefing (uso pessoal) |
| `JARVIS_NOTIF_VOZ` | `1` | `0` emudece a voz de briefing, nag, sentinela, lembretes e foco |
| `JARVIS_CPU` / `JARVIS_MEM` / `JARVIS_DISK` | `85` / `90` / `90` | limiares (%) do sentinela |
| `JARVIS_TEMP` / `JARVIS_BAT` | `80` / `20` | febre (°C) e bateria fraca (%) do sentinela |
| `JARVIS_COOLDOWN` | `30` | minutos entre um aviso e outro do mesmo tipo |
| `JARVIS_VOZD_PORTA` | `8765` | porta do servidor de voz local |

Modo leve (CPU fraca): `JARVIS_MCP_LIVE=0 JARVIS_WAKE=0 jarvis-tui`

### 🧱 Estrutura

```
src/jarvis_tui.py      a TUI (Textual)
bin/                   auxiliares: falar, transcrever, escuta, vozd,
                       kokoro, briefing, nag, ouvir, wake-calibra,
                       sentinela, lembrar, foco + .jarvis-comum (biblioteca)
agent/jarvis.md        prompt do agente (vai para ~/.config/opencode/agent/)
examples/              trecho de opencode.json com os MCPs + systemd do sentinela
```

### 🛡 Sentinela

Vigia CPU, RAM, disco, temperatura e bateria; avisa com **voz + notify**, com cooldown de 30 min por tipo:

```bash
jarvis-sentinela --teste    # demonstra agora (limiares mínimos)
jarvis-sentinela --quieto   # só notify, sem voz
# automático a cada 5 min:
cp examples/systemd/jarvis-sentinela.* ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now jarvis-sentinela.timer
```

### ⏰ Lembretes e foco

```bash
jarvis-lembrar em 20min "regar as plantas"
jarvis-lembrar as 18:45 "ligar para a mãe"
jarvis-lembrar lista | cancela ID
jarvis-foco 25 5 4    # pomodoro com voz (trabalho, pausa, ciclos)
jarvis-foco estado | para
```

No chat basta dizer *"me avise em 20 min"* ou *"inicie um foco"* — o agente agenda sozinho.

### 📜 Licença

MIT.
