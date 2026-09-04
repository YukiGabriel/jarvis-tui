#!/usr/bin/env -S uv run --python 3.14 --with textual python3
"""Jarvis-TUI — interface própria do mordomo, conectada ao agente jarvis via opencode."""
from __future__ import annotations
import argparse, asyncio, json, os, re, subprocess, sys, threading, time
from collections import deque
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Center
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, RichLog, Static, ProgressBar
from textual import work
from rich.markdown import Markdown
from rich.markup import escape

CONFIG = Path.home() / ".config/opencode/opencode.json"
SHARE = Path.home() / ".local/share/jarvis-tui"
SESSION_FILE = SHARE / "session_id"
DEBUG_LOG = SHARE / "debug.log"
HIST_FILE = SHARE / "chat.jsonl"
HIST_MAX = 200   # linhas guardadas em disco
HIST_SHOW = 30   # mensagens restauradas ao abrir


def hist_append(who: str, text: str) -> None:
    """Guarda uma mensagem no diário; apara o rabo para não crescer sem fim."""
    try:
        SHARE.mkdir(parents=True, exist_ok=True)
        with open(HIST_FILE, "a") as f:
            f.write(json.dumps({"ts": datetime.now().strftime("%H:%M"),
                                "who": who, "text": text},
                               ensure_ascii=False) + "\n")
        with open(HIST_FILE) as f:
            lines = f.readlines()
        if len(lines) > HIST_MAX:
            with open(HIST_FILE, "w") as f:
                f.writelines(lines[-HIST_MAX:])
    except Exception:
        pass


def hist_load() -> list[dict]:
    """Últimas mensagens do diário (para restaurar a tela)."""
    try:
        out = []
        with open(HIST_FILE) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out[-HIST_SHOW:]
    except OSError:
        return []


def dbg(msg: str) -> None:
    try:
        SHARE.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{datetime.now():%H:%M:%S} {msg}\n")
    except Exception:
        pass

# Microfone liga/desliga: tecla configurável; JARVIS_MIC_MAX é o teto
# de segurança (auto-para) em segundos. Ex.: JARVIS_MIC_KEY="f2" jarvis-tui
MIC_KEY = os.environ.get("JARVIS_MIC_KEY", "ctrl+m")
try:
    MIC_MAX = int(os.environ.get("JARVIS_MIC_MAX", os.environ.get("JARVIS_MIC_SECS", "120")))
except ValueError:
    MIC_MAX = 120
# Voz das respostas: tecla configurável; JARVIS_VOZ=0 nasce muda.
SPK_KEY = os.environ.get("JARVIS_SPK_KEY", "ctrl+s")
VOZ_ON = os.environ.get("JARVIS_VOZ", "1") != "0"
# Ouvido interno ("hey jarvis" só dentro da TUI): tecla e limiares via ambiente.
# Ex.: JARVIS_OUV_KEY="ctrl+w" JARVIS_WAKE=0 jarvis-tui
OUV_KEY = os.environ.get("JARVIS_OUV_KEY", "ctrl+w")
WAKE_ON = os.environ.get("JARVIS_WAKE", "1") != "0"
WAKE_LIMIAR = float(os.environ.get("JARVIS_WAKE_LIMIAR", "0.4"))
WAKE_CONSEC = int(os.environ.get("JARVIS_WAKE_CONSEC", "3"))  # frames seguidos (~0.25s de frase)
# Motor do ouvido: openwakeword ("hey jarvis", livre) ou porcupine ("jarvis" nativo,
# exige chave). Padrão: openwakeword.
WAKE_MOTOR = os.environ.get("JARVIS_WAKE_MOTOR", "openwakeword")
PPN_SENS = float(os.environ.get("JARVIS_PPN_SENS", "0.65"))
PPN_KEY_FILE = SHARE / "picovoice.key"
_ppn = None


def ppn_key() -> str:
    key = os.environ.get("PICOVOICE_KEY", "").strip()
    if not key:
        try:
            key = PPN_KEY_FILE.read_text().strip()
        except OSError:
            key = ""
    return key


def wake_motor() -> str:
    if WAKE_MOTOR == "auto":
        return "porcupine" if ppn_key() else "openwakeword"
    return WAKE_MOTOR


def ppn():
    """Porcupine com keyword jarvis nativa; None sem chave."""
    global _ppn
    if _ppn is None:
        key = ppn_key()
        if not key:
            return None
        if VOZ_VENV not in sys.path:
            sys.path.insert(0, VOZ_VENV)
        import pvporcupine
        _ppn = pvporcupine.create(access_key=key, keywords=["jarvis"],
                                  sensitivities=[PPN_SENS])
    return _ppn
SIL_RMS = float(os.environ.get("JARVIS_SILENCIO_RMS", "8000"))
SIL_SECS = float(os.environ.get("JARVIS_SILENCIO_SECS", "3"))
# Parada por voz ("pode parar", "chega"…): checagem parcial durante a gravação.
STOP_VOZ = os.environ.get("JARVIS_STOP_VOZ", "1") != "0"
STOP_CADA = float(os.environ.get("JARVIS_STOP_CADA", "4"))
STOP_WORDS = ("pode parar", "para de gravar", "chega", "envia", "pronto",
              "acabou", "cabou", "é isso", "pode enviar")
VOZ_VENV = str(Path.home() / ".local/share/jarvis-voice/lib/python3.14/site-packages")
# Comandos auxiliares: mesma pasta do TUI, $JARVIS_BIN ou ~/.local/bin.
# Ex.: JARVIS_BIN=/opt/jarvis/bin JARVIS_TRANSCREVER=/usr/local/bin/stt jarvis-tui
BIN_DIR = Path(os.environ.get("JARVIS_BIN", Path.home() / ".local/bin"))
TRANSCREVER = os.environ.get("JARVIS_TRANSCREVER", str(BIN_DIR / "jarvis-transcrever"))
CHUNK = 2560  # 80ms a 16kHz/16-bit
BUF_MAX = int(180 / 0.08)  # ~3 min de anel

_wake_model = None
_wake_erro = ""


def wake_model():
    """Carrega o hey_jarvis uma vez; devolve None se indisponível."""
    global _wake_model, _wake_erro
    if _wake_model is None and not _wake_erro:
        try:
            if VOZ_VENV not in sys.path:
                sys.path.insert(0, VOZ_VENV)
            from openwakeword.model import Model
            _wake_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        except Exception as e:
            _wake_erro = str(e)[:120]
            dbg(f"wake-model: {_wake_erro}")
    return _wake_model

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")

# ── Reator: frames do núcleo (pulso) ──────────────────────────────
REACTOR = [
    "[#00263d]  ╭────────╮  [/]\n[#00263d] ╭┤[#00D4FF] ◜  ◝ [/]├╮ [/]\n[#00263d] ││[#5A7FA6]  ◉  [/]││ [/]\n[#00263d] ╰┤[#00D4FF] ◟  ◞ [/]├╯ [/]\n[#00263d]  ╰────────╯  [/]",
    "[#00314d]  ╭────────╮  [/]\n[#00314d] ╭┤[#00D4FF] ◜  ◝ [/]├╮ [/]\n[#00314d] ││[#7DF9FF]  ◉  [/]││ [/]\n[#00314d] ╰┤[#00D4FF] ◟  ◞ [/]├╯ [/]\n[#00314d]  ╰────────╯  [/]",
    "[#005577]  ╭────────╮  [/]\n[#005577] ╭┤[#7DF9FF]  ◝ ◞ [/]├╮ [/]\n[#005577] ││[#EAF6FF]  ◉  [/]││ [/]\n[#005577] ╰┤[#7DF9FF]  ◟ ◜ [/]├╯ [/]\n[#005577]  ╰────────╯  [/]",
    "[#00D4FF]  ╭────────╮  [/]\n[#00D4FF] ╭┤[#EAF6FF] ◞  ◟ ├╮ [/]\n[#00D4FF] ││[#FFFFFF]  ◉  [/]││ [/]\n[#00D4FF] ╰┤[#EAF6FF] ◝  ◜ ├╯ [/]\n[#00D4FF]  ╰────────╯  [/]",
    "[#7DF9FF]  ╭────────╮  [/]\n[#7DF9FF] ╭┤[#FFFFFF] ◟  ◞ ├╮ [/]\n[#7DF9FF] ││[#FFFFFF]  ◉  [/]││ [/]\n[#7DF9FF] ╰┤[#FFFFFF] ◜  ◝ ├╯ [/]\n[#7DF9FF]  ╰────────╯  [/]",
]
SPIN = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
BOOT_LINES = [
    "> núcleo arc ............ [OK]",
    "> uplink opencode ........ [OK]",
    "> MCPs ................... [14]",
    "> voz / ouvido ........... [OK]",
    "> calibrando HUD ......... [OK]",
    "> sincronizando sessão ... [OK]",
    "> Ao seu dispor, senhor.",
]

JARVIS_CSS = """
Screen { background: #02070F; color: #D6F6FF; }
#hud { background: #0A1E33; color: #00D4FF; border-bottom: heavy #00D4FF; padding: 0 1; height: 3; text-style: bold; }
#hud .dim { color: #5A7FA6; }
#chatwrap { background: #081324; border: round #1E4A7A; width: 70%; height: 1fr; padding: 0 1; }
#chatwrap > .title { background: #0E223D; color: #7DF9FF; padding: 0 1; text-style: bold; }
#chatwrap:focus-within { border: round #00D4FF; }
#chat { background: #081324; height: 1fr; scrollbar-color: #00D4FF; scrollbar-background: #0A1E33; }
#side { background: #071120; width: 30%; height: 1fr; padding: 0 1 0 0; }
.panel { background: #0D2038; border: round #1E4A7A; margin: 0 1 1 1; padding: 1 1; height: auto; }
.panel-title { color: #00D4FF; text-style: bold; }
.panel:focus { border: round #00D4FF; }
#prompt { background: #10294A; border: round #1E4A7A; width: 1fr; padding: 0 1; }
#promptbar { background: #02070F; color: #5A7FA6; padding: 0 1; }
#inputrow { height: auto; margin: 0 0 1 0; }
#mic { width: auto; min-width: 10; background: #14315A; color: #7DF9FF; border: round #1E4A7A; padding: 0 1; }
#mic:hover { background: #1E4A7A; color: #FFFFFF; border: round #00D4FF; }
#mic.rec { background: #3A0A12; color: #FF5C6C; border: round #FF2D40; text-style: bold; }
#spk { width: auto; min-width: 5; background: #0E223D; color: #7DF9FF; border: round #1E4A7A; }
#spk:hover { border: round #00D4FF; color: #FFFFFF; }
Input { background: #10294A; color: #EAF6FF; }
Input:focus { border: round #00D4FF; background: #14315A; }
Button:focus { border: round #00D4FF; text-style: bold; }
Footer { background: #0A1E33; color: #5A7FA6; }
/* splash */
#splash { align: center middle; background: #02070F; }
#reactor { text-align: center; margin-bottom: 1; }
#stitle { text-align: center; color: #00D4FF; text-style: bold; }
#ssub { text-align: center; color: #5A7FA6; }
#bootlog { background: #081324; border: round #1E4A7A; width: 62; height: 9; margin: 1 0; padding: 0 1; color: #7DF9FF; }
#bar { width: 62; margin: 0 0 1 0; color: #00D4FF; background: #0A1E33; }
"""

def load_mcps() -> list[tuple[str, bool]]:
    try:
        data = json.loads(CONFIG.read_text())
        mcps = data.get("mcp", {})
        return sorted([(n, bool(c.get("enabled", True))) for n, c in mcps.items()])
    except Exception:
        return []

# Status real (connected/failed) via `opencode mcp list`, com cache de 10 min.
# O painel antigo mostrava só `enabled` — mentia com gmail/github caídos.
# Anti-tempestade: uma checagem por vez; se falhar, silêncio de 5 min.
# JARVIS_MCP_LIVE=0 desliga a checagem ao vivo (só `enabled`).
_MCP_LIVE: dict[str, str] = {}
_MCP_LIVE_TS = 0.0
_MCP_LIVE_REFRESH = 600.0
_MCP_LIVE_BACKOFF = 300.0
_MCP_LIVE_LOCK = threading.Lock()
_MCP_LIVE_BUSY = False
_MCP_LIVE_NEXT = 0.0
_MCP_LIVE_ON = os.environ.get("JARVIS_MCP_LIVE", "1") != "0"


def _parse_mcp_list(out: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for m in re.finditer(r"[●○]\s+[✓✗○●]\s+(\S+)\s+(connected|failed|disabled)", out):
        found[m.group(1)] = m.group(2)
    for m in re.finditer(r"([✓✗○])\s+(\S+)\s+(connected|failed|disabled)", out):
        found.setdefault(m.group(2), m.group(3))
    return found


def _mcp_live_refresh() -> None:
    global _MCP_LIVE, _MCP_LIVE_TS, _MCP_LIVE_BUSY, _MCP_LIVE_NEXT
    try:
        p = subprocess.run(["opencode", "mcp", "list"],
                           capture_output=True, text=True, timeout=25)
        out = (p.stdout or "") + (p.stderr or "")
        parsed = _parse_mcp_list(out)
        with _MCP_LIVE_LOCK:
            if parsed:
                _MCP_LIVE = parsed
                _MCP_LIVE_TS = time.time()
                _MCP_LIVE_NEXT = _MCP_LIVE_TS + _MCP_LIVE_REFRESH
            else:
                _MCP_LIVE_NEXT = time.time() + _MCP_LIVE_BACKOFF
    except Exception as e:
        dbg(f"mcp-live: {e}")
        with _MCP_LIVE_LOCK:
            _MCP_LIVE_NEXT = time.time() + _MCP_LIVE_BACKOFF
    finally:
        _MCP_LIVE_BUSY = False


def mcp_status() -> dict[str, str]:
    global _MCP_LIVE_BUSY, _MCP_LIVE_NEXT
    if not _MCP_LIVE_ON:
        return {}
    now = time.time()
    with _MCP_LIVE_LOCK:
        snapshot = dict(_MCP_LIVE)
        fresh = (now - _MCP_LIVE_TS) < _MCP_LIVE_REFRESH
        wait = now < _MCP_LIVE_NEXT
        busy = _MCP_LIVE_BUSY
        if fresh or busy or wait:
            return snapshot
        _MCP_LIVE_BUSY = True
        _MCP_LIVE_NEXT = now + 30.0
    threading.Thread(target=_mcp_live_refresh, daemon=True).start()
    return snapshot

def read_session() -> str | None:
    try:
        s = SESSION_FILE.read_text().strip()
        return s or None
    except FileNotFoundError:
        return None

def write_session(sid: str) -> None:
    SHARE.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(sid)


class Splash(Screen):
    """Splashscreen animada — sequência de boot do Jarvis."""

    def __init__(self, skip: bool = False):
        super().__init__()
        self.skip = skip
        self.frame = 0
        self.step = 0
        self.progress = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="splash"):
            with Center():
                yield Static(REACTOR[0], id="reactor")
            with Center():
                yield Static("[bold #00D4FF]J.A.R.V.I.S.[/]\n[#5A7FA6]Just A Rather Very Intelligent System[/#5A7FA6]", id="stitle")
            with Center():
                yield Static("", id="bootlog")
            with Center():
                yield ProgressBar(total=100, show_eta=False, id="bar")
            with Center():
                yield Static("[dim]qualquer tecla acelera, senhor…[/]", id="ssub")

    async def on_mount(self) -> None:
        if self.skip:
            await asyncio.sleep(0.4)
            self.dismiss_all()
            return
        self.set_interval(0.14, self._tick_reactor)
        self.set_interval(0.42, self._tick_boot)

    def _tick_reactor(self) -> None:
        try:
            self.frame = (self.frame + 1) % len(REACTOR)
            self.query_one("#reactor", Static).update(REACTOR[self.frame])
        except Exception:
            pass

    def _tick_boot(self) -> None:
        try:
            log = self.query_one("#bootlog", Static)
            bar = self.query_one("#bar", ProgressBar)
            if self.step < len(BOOT_LINES):
                cur = log.renderable if hasattr(log, "renderable") and log.renderable else ""
                log.update(f"{cur}\n[#00D4FF]{BOOT_LINES[self.step]}[/]".strip())
                self.step += 1
                self.progress = int(self.step / len(BOOT_LINES) * 100)
                bar.update(progress=self.progress)
            else:
                self.dismiss_all()
        except Exception:
            pass

    def dismiss_all(self) -> None:
        try:
            self.app.pop_screen()
        except Exception:
            pass

    async def on_key(self, event) -> None:  # qualquer tecla pula
        self.dismiss_all()


class SearchScreen(Screen):
    """Mini-prompt de busca no chat (ctrl+f). Enter busca, Esc fecha."""

    BINDINGS = [("escape", "cancel", "Fechar")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="Buscar no chat…", id="q")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class JarvisApp(App):
    CSS = JARVIS_CSS
    TITLE = "J.A.R.V.I.S."
    BINDINGS = [
        ("ctrl+q", "quit", "Sair"),
        ("ctrl+l", "clear_chat", "Limpar"),
        ("ctrl+f", "search_chat", "Buscar"),
        (MIC_KEY, "toggle_mic", "🎙 Microfone"),
        (SPK_KEY, "toggle_voz", "🔊 Voz"),
        (OUV_KEY, "toggle_ouvido", "👂 Ouvido"),
    ]

    def __init__(self, initial_prompt: str | None = None, no_splash: bool = False):
        super().__init__()
        self.initial_prompt = initial_prompt
        self.no_splash = no_splash
        self.session_id: str | None = read_session()
        self.busy = False
        self.recording = False
        self.voz = VOZ_ON
        self.ouvido = WAKE_ON
        self._spk_proc = None
        # Stream contínuo do microfone (anel) — um dono só, sem disputa.
        self._stream_proc = None
        self._stream_thread = None
        self._buf: deque = deque(maxlen=BUF_MAX)
        self._buflock = threading.Lock()
        self._n_chunks = 0
        self._rec_mark = 0
        self._rec_t0 = 0.0
        self._rec_quiet = 0.0
        self._stop_last = 0.0
        self._stop_checking = False
        self._wake_seguidos = 0
        self._wake_nivel = 0.0  # medidor ao vivo p/ treinar a pronúncia
        self._mic_elapsed = 0
        self._mic_timer = None
        self._spin = 0
        self._sent_init = False
        self._msgs: list[tuple[str, str, str]] = []  # (hora, autor, texto) p/ busca

    def compose(self) -> ComposeResult:
        yield Static(f"  ◈ J.A.R.V.I.S.  │  [dim]uplink: opencode/agent jarvis[/]  │  [dim]{MIC_KEY} 🎙 · {SPK_KEY} 🔊 · {OUV_KEY} 👂[/]", id="hud")
        with Horizontal():
            with Vertical(id="chatwrap"):
                yield Static("◈ UPLINK // CHAT", classes="title")
                yield RichLog(id="chat", wrap=True, markup=True, auto_scroll=True)
            with Vertical(id="side"):
                yield Static("", id="core", classes="panel")
                yield Static("", id="mcps", classes="panel")
                yield Static("", id="sess", classes="panel")
        yield Static(f"SENHOR ❯ digite, /ajuda, dite 🎙 ou diga hey jarvis 👂 · pare com 'pode parar'", id="promptbar")
        with Horizontal(id="inputrow"):
            yield Input(placeholder="Diga ao Jarvis…", id="prompt")
            yield Button("🎙 Falar", id="mic")
            yield Button("🔊", id="spk")
        yield Footer()

    async def on_mount(self) -> None:
        self.refresh_panels()
        self.set_interval(1.0, self._tick_hud)
        self._stream_start()
        chat = self.query_one("#chat", RichLog)
        sid = (self.session_id or "nova")[:13]
        for m in hist_load():
            ts, who, txt = m.get("ts", "--:--"), m.get("who", "?"), m.get("text", "")
            self._msgs.append((ts, who, txt))
            if who == "senhor":
                self._render_user(chat, ts, txt)
            else:
                self._render_jarvis(chat, ts, txt)
        if self._msgs:
            chat.write("[dim]─ histórico restaurado ─[/]")
        chat.write("[#1E4A7A]────────────────────────────────────────────[/]")
        chat.write(f"[bold #00D4FF]◈ Jarvis online.[/] Sessão [dim]{sid}[/] — MCPs ao lado, senhor.")
        if not self.voz:
            try:
                self.query_one("#spk", Button).label = "🔇"
            except Exception:
                pass
        if not self.no_splash:
            self.push_screen(Splash())
            # envia o prompt inicial após o boot
            self.set_timer(3.2, self._maybe_init)
        elif self.initial_prompt:
            self.set_timer(0.4, self._maybe_init)

    def _maybe_init(self) -> None:
        if not self._sent_init and self.initial_prompt:
            self._sent_init = True
            self.send_to_jarvis(self.initial_prompt)

    def _tick_hud(self) -> None:
        self._spin = (self._spin + 1) % len(SPIN)
        self._wake_nivel *= 0.5  # medidor decai
        self.refresh_panels()

    def refresh_panels(self) -> None:
        import random
        try:
            pwr = 96 + random.randint(-4, 3)
            bars = "█" * (pwr // 8) + "░" * (12 - pwr // 8)
            state = "PROCESSANDO" if self.busy else "PRONTO"
            dot = SPIN[self._spin] if self.busy else "●"
            self.query_one("#core", Static).update(
                f"[#00D4FF]◈ NÚCLEO[/]\n  {dot} {state}\n  [#00D4FF]potência[/] [{bars}] {pwr}%"
            )
        except Exception:
            pass
        try:
            mcps = load_mcps()
            live = mcp_status()
            if live:
                on = sum(1 for n, e in mcps if e and live.get(n, "connected") == "connected")
                lines = [f"[#00D4FF]◈ SISTEMAS // {on}/{len(mcps)}[/]"]
            else:
                on = sum(1 for _, e in mcps if e)
                lines = [f"[#00D4FF]◈ SISTEMAS // {on}/{len(mcps)} …[/]"]
            for name, en in mcps[:12]:
                st = live.get(name)
                if not en or st == "disabled":
                    d = "[dim]○[/]"
                elif st == "connected":
                    d = "[#00FFC8]●[/]"
                elif st == "failed":
                    d = "[#FF2D40]✗[/]"
                else:
                    d = "[#00FFC8]●[/]" if en else "[dim]○[/]"
                lines.append(f"  {d} {name}")
            if len(mcps) > 12:
                lines.append(f"  [dim]+{len(mcps)-12} mais[/]")
            self.query_one("#mcps", Static).update("\n".join(lines))
        except Exception:
            pass
        try:
            sid = (self.session_id or "nova")[:18]
            now = datetime.now().strftime("%H:%M")
            stream_ok = self._stream_proc is not None and self._stream_proc.poll() is None
            if self.ouvido and stream_ok:
                ouvido_txt = "[#00FFC8]● ouvido[/]"
            elif self.ouvido:
                ouvido_txt = "[#FFB300]○ ouvido[/]"
            else:
                ouvido_txt = "[dim]○ ouvido[/]"
            nv = self._wake_nivel
            blocos = "█" * int(nv * 5) + "░" * (5 - int(nv * 5))
            frase = "jarvis" if wake_motor() == "porcupine" else "hey jarvis"
            self.query_one("#sess", Static).update(
                f"[#00D4FF]◈ SESSÃO[/]\n  id {sid}\n  hora {now}\n"
                f"  {ouvido_txt} {frase}\n  [dim]nível[/] [{blocos}] {nv:.2f}"
            )
        except Exception:
            pass

    def action_clear_chat(self) -> None:
        try:
            self.query_one("#chat", RichLog).clear()
        except Exception:
            pass

    @staticmethod
    def _render_user(chat: RichLog, ts: str, text: str) -> None:
        chat.write(f"\n[dim]{ts}[/] [bold #FFB300]◈ SENHOR[/]\n  {escape(text)}")

    @staticmethod
    def _render_jarvis(chat: RichLog, ts: str, reply: str) -> None:
        chat.write(f"\n[dim]{ts}[/] [bold #00D4FF]◈ JARVIS[/]")
        if reply:
            chat.write(Markdown(reply))
        else:
            chat.write("[dim](sem resposta)[/]")

    def handle_slash(self, text: str) -> None:
        """Comandos locais /ajuda /voz /ouvido /limpar /nova /briefing /mcp /buscar /sair."""
        chat = self.query_one("#chat", RichLog)
        parts = text[1:].split(None, 1)
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd in ("ajuda", "help", "comandos"):
            chat.write("[#00D4FF]◈ Comandos[/]\n"
                       "  [bold]/briefing[/] resumo do dia\n"
                       "  [bold]/voz[/] liga/desliga a fala · [bold]/ouvido[/] liga/desliga o hey jarvis\n"
                       "  [bold]/nova[/] nova conversa · [bold]/limpar[/] limpa a tela\n"
                       "  [bold]/buscar TERMO[/] procura no chat · [bold]/mcp[/] estado dos sistemas\n"
                       "  [bold]/sair[/] fecha a TUI")
        elif cmd == "voz":
            self.action_toggle_voz()
        elif cmd == "ouvido":
            self.action_toggle_ouvido()
        elif cmd in ("limpar", "clear"):
            self.action_clear_chat()
        elif cmd in ("nova", "novo", "new"):
            self.session_id = None
            try:
                SESSION_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            self.action_clear_chat()
            chat.write("[dim]◈ Nova conversa, senhor. O passado ficou no arquivo.[/]")
        elif cmd == "briefing":
            self.send_to_jarvis("me dê o briefing do dia")
        elif cmd == "mcp":
            self.refresh_panels()
            chat.write("[dim]◈ Painel de sistemas atualizado ao lado, senhor.[/]")
        elif cmd in ("buscar", "busca", "search", "find"):
            if arg:
                self._do_search(arg)
            else:
                self.action_search_chat()
        elif cmd in ("sair", "quit", "exit"):
            self.exit()
        else:
            chat.write(f"[dim]◈ Comando desconhecido: {escape(cmd)}. Tente /ajuda, senhor.[/]")

    def action_search_chat(self) -> None:
        self.push_screen(SearchScreen(), self._do_search)

    def _do_search(self, term: str | None) -> None:
        if not term:
            return
        chat = self.query_one("#chat", RichLog)
        t = term.lower()
        hits = [(ts, who, txt) for ts, who, txt in self._msgs if t in txt.lower()]
        if not hits:
            chat.write(f"[dim]◈ Nada para '{escape(term)}', senhor.[/]")
            return
        lines = [f"[#00D4FF]◈ {len(hits)} ocorrência(s) de '{escape(term)}'[/]"]
        for ts, who, txt in hits[-10:]:
            tag = "SENHOR" if who == "senhor" else "JARVIS"
            trecho = txt.replace("\n", " ⏎ ")
            lines.append(f"  [dim]{ts}[/] [bold]{tag}[/] {escape(trecho[:140])}")
        chat.write("\n".join(lines))

    def action_toggle_mic(self) -> None:
        self._mic_pressed()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "mic":
            self._mic_pressed()
        elif event.button.id == "spk":
            self.action_toggle_voz()

    def action_toggle_voz(self) -> None:
        self.voz = not self.voz
        self._kill_speech()
        try:
            spk = self.query_one("#spk", Button)
            spk.label = "🔊" if self.voz else "🔇"
        except Exception:
            pass
        state = "com voz" if self.voz else "em silêncio"
        try:
            self.query_one("#chat", RichLog).write(f"[dim]◈ Jarvis agora {state}, senhor.[/]")
        except Exception:
            pass

    def action_toggle_ouvido(self) -> None:
        self.ouvido = not self.ouvido
        self._wake_seguidos = 0
        state = "atento ao hey jarvis" if self.ouvido else "com o ouvido tapado"
        try:
            self.query_one("#chat", RichLog).write(f"[dim]👂 Jarvis {state}, senhor.[/]")
        except Exception:
            pass
        self.refresh_panels()

    # ── Ouvido interno: um stream, três usos (wake, gravação, silêncio) ──
    def _stream_start(self) -> bool:
        if self._stream_proc is not None and self._stream_proc.poll() is None:
            return True
        try:
            self._stream_proc = subprocess.Popen(
                ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-t", "raw"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=CHUNK * 4,
            )
        except Exception as e:
            try:
                self.query_one("#chat", RichLog).write(
                    f"[bold red]◈ Microfone falhou:[/] {escape(str(e)[:150])}")
            except Exception:
                pass
            dbg(f"stream: {e}")
            return False
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()
        return True

    @staticmethod
    def _rms(raw: bytes) -> float:
        import struct
        n = len(raw) // 2
        if not n:
            return 0.0
        s = struct.unpack('<' + 'h' * n, raw)
        return (sum(x * x for x in s) / n) ** 0.5

    def _stream_loop(self) -> None:
        proc = self._stream_proc
        out = proc.stdout if proc else None
        while proc is not None and proc.poll() is None and out is not None:
            try:
                raw = out.read(CHUNK)
            except Exception:
                break
            if len(raw) < CHUNK:
                time.sleep(0.05)
                continue
            with self._buflock:
                self._buf.append(raw)
                self._n_chunks += 1
            if self.recording:
                self._rec_watch(raw)
            elif self.ouvido and not self.busy:
                # Inferência ONNX a cada 80ms cravava a CPU; a cada 3º
                # chunk (~240ms) basta para o "hey jarvis".
                if self._n_chunks % 3 == 0:
                    self._wake_watch(raw)

    def _wake_watch(self, raw: bytes) -> None:
        if wake_motor() == "porcupine":
            self._wake_ppn(raw)
        else:
            self._wake_oww(raw)

    def _wake_ppn(self, raw: bytes) -> None:
        try:
            import numpy as np
            engine = ppn()
            if engine is None:
                return
            pcm = np.frombuffer(raw, dtype=np.int16)
            for i in range(0, len(pcm) - 511, 512):
                if engine.process(pcm[i:i + 512]) >= 0:
                    self._wake_nivel = 1.0
                    try:
                        self.call_from_thread(self._wake_fired)
                    except Exception:
                        pass
                    return
        except Exception as e:
            dbg(f"wake-ppn: {str(e)[:100]}")

    def _wake_oww(self, raw: bytes) -> None:
        try:
            import numpy as np
            model = wake_model()
            if model is None:
                return
            score = float(model.predict(np.frombuffer(raw, dtype=np.int16)).get("hey_jarvis", 0.0))
        except Exception:
            return
        if score > self._wake_nivel:
            self._wake_nivel = score
        if score >= WAKE_LIMIAR:
            self._wake_seguidos += 1
        else:
            self._wake_seguidos = 0
        if self._wake_seguidos >= WAKE_CONSEC:
            self._wake_seguidos = 0
            try:
                self.call_from_thread(self._wake_fired)
            except Exception:
                pass

    def _wake_fired(self) -> None:
        if self.recording or self.busy:
            return
        dbg(f"wake: detectado via {wake_motor()}")
        try:
            self.query_one("#chat", RichLog).write("[dim]👂 pois não, senhor — gravando…[/]")
        except Exception:
            pass
        self._mic_start(auto=True)

    def _rec_watch(self, raw: bytes) -> None:
        if self._rms(raw) < SIL_RMS:
            self._rec_quiet += 0.08
        else:
            self._rec_quiet = 0.0
        elapsed = time.time() - self._rec_t0
        if STOP_VOZ and elapsed > 2.0 and not self._stop_checking:
            if elapsed - self._stop_last >= STOP_CADA:
                self._stop_last = elapsed
                self._stop_checking = True
                threading.Thread(target=self._stop_probe, daemon=True).start()
        if (elapsed > 1.5 and self._rec_quiet >= SIL_SECS) or elapsed >= MIC_MAX:
            try:
                self.call_from_thread(self.mic_stop_and_send,
                                      "silêncio" if elapsed < MIC_MAX else "limite")
            except Exception:
                pass

    @staticmethod
    def _contem_parada(texto: str) -> bool:
        t = texto.lower()
        return any(w in t for w in STOP_WORDS)

    @staticmethod
    def _tira_parada(texto: str) -> str:
        """Remove a frase de parada ('…pode parar') do comando final."""
        import re as _re
        partes = _re.split(r"(?<=[.!?…])\s+", texto.strip())
        while partes and JarvisApp._contem_parada(partes[-1]):
            partes.pop()
        limpo = " ".join(partes).strip()
        if not limpo:  # só havia a ordem de parada
            for w in STOP_WORDS:
                texto = _re.sub(w, "", texto, flags=_re.I)
            limpo = _re.sub(r"\s+", " ", texto).strip(" ,.")
        return limpo

    def _stop_probe(self) -> None:
        """Transcreve parcial; achou ordem de parada → encerra e envia."""
        try:
            import tempfile, wave
            with self._buflock:
                frames = list(self._buf)
                mark, total = self._rec_mark, self._n_chunks
            if not self.recording:
                return
            start = max(0, len(frames) - (total - mark))
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav", prefix="jarvis-stop-")
            tmp.close()
            try:
                with wave.open(tmp.name, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(16000)
                    w.writeframes(b"".join(frames[start:]))
                p = subprocess.run(
                    [TRANSCREVER, tmp.name],
                    capture_output=True, text=True, timeout=120)
                parcial = (p.stdout or "").strip()
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
            if parcial and self._contem_parada(parcial):
                try:
                    self.call_from_thread(self.mic_stop_and_send, "voz")
                except Exception:
                    pass
        finally:
            self._stop_checking = False

    def _mic_pressed(self) -> None:
        """Liga/desliga: 1º toque grava, 2º para, transcreve e envia direto."""
        if self.recording:
            self.mic_stop_and_send("toque")
        elif not self.busy:
            self._mic_start(auto=False)

    def _mic_start(self, auto: bool) -> None:
        if not self._stream_start():
            return
        with self._buflock:
            self._rec_mark = max(0, self._n_chunks - 4)  # pré-rol de ~0.3s
        self._rec_t0 = time.time()
        self._rec_quiet = 0.0
        self._mic_elapsed = 0
        self.recording = True
        try:
            mic = self.query_one("#mic", Button)
            mic.label = "■ Parar e enviar"
            mic.add_class("rec")
        except Exception:
            pass
        if not auto:
            try:
                self.query_one("#chat", RichLog).write(
                    "[dim]🎙 gravando… toque de novo para parar e enviar, senhor.[/]")
            except Exception:
                pass
        if self._mic_timer is None:
            self._mic_timer = self.set_interval(1.0, self._mic_tick)

    def _mic_tick(self) -> None:
        if not self.recording:
            return
        self._mic_elapsed = int(time.time() - self._rec_t0)
        try:
            mic = self.query_one("#mic", Button)
            mic.label = f"■ {self._mic_elapsed}s — parar e enviar"
        except Exception:
            pass

    @work(exclusive=True)
    async def mic_stop_and_send(self, motivo: str = "toque") -> None:
        """Fatia o anel, transcreve e envia sem Enter, senhor."""
        if not self.recording and motivo == "toque":
            return
        self.recording = False
        try:
            mic = self.query_one("#mic", Button)
            mic.label = "🎙 Falar"
            mic.remove_class("rec")
        except Exception:
            pass
        chat = self.query_one("#chat", RichLog)
        import tempfile, wave
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav", prefix="jarvis-mic-")
        tmp.close()
        with self._buflock:
            frames = list(self._buf)
            mark, total = self._rec_mark, self._n_chunks
        start = max(0, len(frames) - (total - mark))
        try:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"".join(frames[start:]))
        except Exception as e:
            chat.write(f"[bold red]◈ Áudio falhou:[/] {escape(str(e)[:150])}")
            return
        if motivo == "silêncio":
            chat.write("[dim]🎙 silêncio — encerrei, senhor.[/]")
        if motivo == "voz":
            chat.write("[dim]◈ às ordens — parando.[/]")
        chat.write("[dim]🎙 transcrevendo…[/]")
        try:
            text = await asyncio.to_thread(self._run_transcrever, tmp.name)
        except Exception as e:
            chat.write(f"[bold red]◈ Transcrição falhou:[/] {escape(str(e)[:200])}")
            return
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
        if text:
            if motivo == "voz":
                text = self._tira_parada(text)
            if text:
                self.send_to_jarvis(text)  # envio direto, sem Enter
            else:
                chat.write("[dim]🎙 só ouvi a ordem de parada, senhor.[/]")
        else:
            chat.write("[dim]🎙 nada captado — tente de novo, senhor.[/]")

    def _run_transcrever(self, wav: str) -> str:
        p = subprocess.run(
            [TRANSCREVER, wav],
            capture_output=True, text=True, timeout=180,
        )
        return (p.stdout or "").strip()

    def on_unmount(self) -> None:
        try:
            if self._stream_proc is not None:
                self._stream_proc.terminate()
        except Exception:
            pass
        try:
            global _ppn
            if _ppn is not None:
                _ppn.delete()
                _ppn = None
        except Exception:
            pass
        self._kill_speech()

    @staticmethod
    def _plain(text: str) -> str:
        """Markdown → fraseado para fala: sem código, sem símbolos."""
        t = re.sub(r"```.*?```", " [trecho de código omitido] ", text, flags=re.S)
        t = re.sub(r"`([^`]*)`", r"\1", t)
        t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
        t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
        t = re.sub(r"#(\w)", r"\1", t)
        t = re.sub(r"[*_~>|]\s*", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 1000:  # respostas longas: fala o essencial
            cut = t[:1000].rsplit(".", 1)[0]
            t = (cut or t[:1000]) + ". Continua no texto, senhor."
        return t

    def _kill_speech(self) -> None:
        proc, self._spk_proc = self._spk_proc, None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass

    @work(exclusive=True)
    async def speak(self, text: str) -> None:
        """Fala a resposta em segundo plano, sem travar o chat."""
        self._kill_speech()
        plain = self._plain(text)
        if not plain:
            return
        try:
            await asyncio.to_thread(self._run_falar, plain)
        except Exception:
            pass
        finally:
            self._spk_proc = None

    def _run_falar(self, plain: str) -> None:
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav", prefix="jarvis-fala-")
        tmp.close()
        try:
            subprocess.run(
                ["jarvis-falar", plain, "--wav", tmp.name],
                capture_output=True, timeout=180, check=True,
            )
            for player in (["paplay", tmp.name], ["pw-play", tmp.name], ["aplay", "-q", tmp.name]):
                try:
                    self._spk_proc = subprocess.Popen(
                        player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    self._spk_proc.wait(timeout=180)
                    break
                except FileNotFoundError:
                    continue
                except Exception:
                    break
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self.handle_slash(text)
            return
        self.send_to_jarvis(text)

    @work(exclusive=True)
    async def send_to_jarvis(self, text: str) -> None:
        if self.busy:
            return
        self.busy = True
        chat = self.query_one("#chat", RichLog)
        ts = datetime.now().strftime("%H:%M")
        self._render_user(chat, ts, text)
        self._msgs.append((ts, "senhor", text))
        hist_append("senhor", text)
        chat.write(f"[dim]{SPIN[0]} Jarvis consulta o oráculo…[/]")
        try:
            reply, tools = await asyncio.to_thread(self._run_opencode, text)
            ts2 = datetime.now().strftime("%H:%M")
            self._render_jarvis(chat, ts2, reply)
            self._msgs.append((ts2, "jarvis", reply or ""))
            hist_append("jarvis", reply or "")
            for t in tools[:6]:
                chat.write(f"    [dim]⚙ {t}[/]")
            chat.write("[#1E4A7A]────────────────────────────────────────────[/]")
            if self.voz and reply:
                self.speak(reply)
        except Exception as e:
            chat.write(f"\n[bold red]◈ Falha:[/] {str(e)[:300]}")
        finally:
            self.busy = False
            self.refresh_panels()

    def _run_opencode(self, text: str) -> tuple[str, list[str]]:
        # Sessão TUI é reutilizada entre dias: sem hora explícita o modelo
        # repete a saudação de ontem (ex.: "boa noite" de manhã). Injeta agora.
        try:
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
        text = f"[contexto atual: {now}, America/Sao_Paulo] {text}"
        cmd = ["opencode", "run", "--agent", "jarvis", "--format", "json"]
        if self.session_id:
            cmd += ["--session", self.session_id]
        cmd += [text]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        out = p.stdout or ""
        texts: list[str] = []
        tools: list[str] = []
        sid = self.session_id
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            sid = ev.get("sessionID") or sid
            part = ev.get("part", {}) if isinstance(ev.get("part"), dict) else {}
            if ev.get("type") == "text" and part.get("text"):
                texts.append(ANSI.sub("", part["text"]))
            if isinstance(part.get("tool"), str):
                tools.append(part["tool"])
        if sid:
            self.session_id = sid
            try:
                write_session(sid)
            except Exception:
                pass
        return "\n".join(texts).strip(), tools


def main() -> None:
    ap = argparse.ArgumentParser(description="Jarvis-TUI")
    ap.add_argument("--prompt", default=None, help="Mensagem inicial (usado pela notificação)")
    ap.add_argument("--no-splash", action="store_true", help="Pula a splashscreen")
    args = ap.parse_args()
    SHARE.mkdir(parents=True, exist_ok=True)
    JarvisApp(initial_prompt=args.prompt, no_splash=args.no_splash).run()

if __name__ == "__main__":
    main()
