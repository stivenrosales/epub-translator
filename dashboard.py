#!/usr/bin/env python3
"""
Dashboard en vivo para translate_epub.py.

Lee progress.json y el log más reciente (translate*.log) y muestra el avance
con rich, en colores, con barras y eventos. Es READ-ONLY: no lanza ni mata
nada. Lo puedes abrir y cerrar cuando quieras (Ctrl+C lo cierra sin afectar
la traducción en curso).

Uso:
    python3 dashboard.py                  # detecta el log más reciente
    python3 dashboard.py mi_log.log       # log específico
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text

PROJECT_DIR = Path(__file__).resolve().parent
PROGRESS_FILE = PROJECT_DIR / "progress.json"

# ─── Patrones del log producidos por translate_epub.py ─────────────────────

# Línea típica de tqdm:
#   Batches:  45%|████▍     | 112/249 [1:23:45<2:30:00, 60.5s/batch, ch04.html]
TQDM_LINE_RE = re.compile(
    r"Batches:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)\s*"
    r"\[(\d+:\d+:\d+|\d+:\d+)<[^\],]+,\s*[\d.]+s/batch(?:,\s*([^\]]+?))?\]"
)
WARN_RE = re.compile(r"\[!\]\s+(.*)")
HEADER_FUENTE_RE = re.compile(r"\bFuente\s*:\s*(.+?)\s*$")
HEADER_PERFIL_RE = re.compile(r"\bPerfil\s*:\s*(.+?)\s*$")
HEADER_MODELO_RE = re.compile(r"\bModelo\s*:\s*(.+?)\s*$")
HEADER_GLOSARIO_RE = re.compile(r"\bGlosario\s*:\s*(\d+)\s+términos")
HEADER_PEND_RE = re.compile(r"\bPendientes:\s*(\d+)\s+archivos,\s*~(\d+)\s+batches")
HEADER_XHTMLS_RE = re.compile(r"\bXHTMLs\s*:\s*(\d+)\s+archivos")
DONE_RE = re.compile(r"✓\s+Listo:\s+(.+?)\s*$")
END_PHASE_TOC = "Traduciendo toc.ncx"
END_PHASE_OPF = "Actualizando content.opf"
END_PHASE_KINDLE = "Saneando para Kindle"
END_PHASE_PACK = "Empaquetando"


# ─── Estado en memoria ──────────────────────────────────────────────────────

@dataclass
class Event:
    ts: datetime
    kind: str  # "ok" | "warn" | "err" | "info"
    msg: str


@dataclass
class State:
    log_path: Path | None = None
    fuente: str = "—"
    perfil: str = "—"
    modelo: str = "—"
    glosario: int = 0
    xhtml_total: int = 0
    pending_files: int = 0
    total_batches: int = 0
    cur_batch: int = 0
    cur_pct: int = 0
    elapsed: str = "0:00:00"
    cur_file: str = ""
    phase: str = "esperando"  # esperando / traduciendo / cerrando / hecho
    output: str = ""
    events: deque[Event] = field(default_factory=lambda: deque(maxlen=50))
    last_log_size: int = 0
    history_batch_times: deque = field(default_factory=lambda: deque(maxlen=20))


# ─── Utilidades ─────────────────────────────────────────────────────────────

def find_latest_log() -> Path | None:
    candidates = sorted(
        [*PROJECT_DIR.glob("translate*.log"), *(PROJECT_DIR / "logs").glob("*.log")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def parse_log(state: State) -> None:
    """Lee incrementalmente el log y actualiza state."""
    if not state.log_path or not state.log_path.exists():
        return
    size = state.log_path.stat().st_size
    if size < state.last_log_size:
        # Log truncado / reiniciado
        state.last_log_size = 0
        state.events.clear()
    if size == state.last_log_size:
        return

    with state.log_path.open("rb") as f:
        f.seek(state.last_log_size)
        chunk = f.read().decode("utf-8", errors="ignore")
    state.last_log_size = size

    # tqdm escribe sin newlines y usa \r — partimos también por \r
    parts = re.split(r"[\r\n]+", chunk)
    now = datetime.now()

    for line in parts:
        line = line.strip()
        if not line:
            continue

        # Cabeceras de inicio
        m = HEADER_FUENTE_RE.search(line)
        if m:
            state.fuente = m.group(1).strip()
            continue
        m = HEADER_PERFIL_RE.search(line)
        if m:
            state.perfil = m.group(1).strip()
            continue
        m = HEADER_MODELO_RE.search(line)
        if m:
            state.modelo = m.group(1).strip()
            continue
        m = HEADER_GLOSARIO_RE.search(line)
        if m:
            state.glosario = int(m.group(1))
            continue
        m = HEADER_XHTMLS_RE.search(line)
        if m:
            state.xhtml_total = int(m.group(1))
            continue
        m = HEADER_PEND_RE.search(line)
        if m:
            state.pending_files = int(m.group(1))
            state.total_batches = int(m.group(2))
            state.phase = "traduciendo"
            continue

        # Fases de cierre
        if END_PHASE_TOC in line:
            state.phase = "TOC"
            state.events.append(Event(now, "info", "traduciendo TOC (índice)"))
            continue
        if END_PHASE_OPF in line:
            state.phase = "OPF"
            state.events.append(Event(now, "info", "actualizando metadata"))
            continue
        if END_PHASE_KINDLE in line:
            state.phase = "kindle"
            state.events.append(Event(now, "info", "saneando para Kindle"))
            continue
        if END_PHASE_PACK in line:
            state.phase = "empaquetando"
            state.events.append(Event(now, "info", "empaquetando epub"))
            continue
        m = DONE_RE.search(line)
        if m:
            state.output = m.group(1).strip()
            state.phase = "hecho"
            state.events.append(Event(now, "ok", f"epub final listo: {state.output}"))
            continue

        # Avisos [!]
        m = WARN_RE.match(line)
        if m:
            msg = m.group(1).strip()
            kind = "err" if "Error" in msg else "warn"
            state.events.append(Event(now, kind, msg))
            continue

        # tqdm — barra de progreso (puede haber varias por línea)
        for tm in TQDM_LINE_RE.finditer(line):
            pct, cur, total, elapsed, cur_file = tm.groups()
            state.cur_pct = int(pct)
            new_batch = int(cur)
            state.total_batches = int(total)
            state.elapsed = elapsed
            if cur_file:
                state.cur_file = cur_file.strip()
            if new_batch != state.cur_batch:
                state.history_batch_times.append(time.time())
                state.cur_batch = new_batch


def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def calc_eta(state: State) -> str:
    """ETA basado en throughput observado de la barra (cache deslizante)."""
    if state.cur_batch >= state.total_batches and state.total_batches > 0:
        return "—"
    h = list(state.history_batch_times)
    if len(h) < 2:
        return "calculando…"
    span = h[-1] - h[0]
    if span <= 0:
        return "calculando…"
    rate = (len(h) - 1) / span  # batches / segundo
    remaining = max(state.total_batches - state.cur_batch, 0)
    if rate <= 0:
        return "—"
    secs = int(remaining / rate)
    h_, rem = divmod(secs, 3600)
    m_, s_ = divmod(rem, 60)
    if h_:
        return f"{h_}h {m_:02d}m"
    if m_:
        return f"{m_}m {s_:02d}s"
    return f"{s_}s"


# ─── Renderizado ────────────────────────────────────────────────────────────

def render_header(state: State) -> Panel:
    title = state.fuente if state.fuente != "—" else "esperando log…"
    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(justify="left", ratio=1)
    table.add_column(justify="left", ratio=1)
    table.add_column(justify="right", ratio=1)
    table.add_row(
        Text.assemble(("Perfil  ", "dim"), (state.perfil, "bold cyan")),
        Text.assemble(("Modelo  ", "dim"), (state.modelo, "bold magenta")),
        Text.assemble(("Glosario  ", "dim"), (f"{state.glosario} términos", "yellow")),
    )
    return Panel(
        table,
        title=Text.assemble(("📖 ", ""), (title, "bold white")),
        border_style="cyan",
        padding=(0, 1),
    )


def render_progress(state: State) -> Panel:
    pct = state.cur_pct
    cur, total = state.cur_batch, state.total_batches
    elapsed = state.elapsed
    eta = calc_eta(state)

    bar_color = "green" if pct >= 75 else ("yellow" if pct >= 30 else "magenta")
    progress = Progress(
        TextColumn("[bold]{task.percentage:>3.0f}%[/bold]"),
        BarColumn(bar_width=None, complete_style=bar_color, finished_style="bold green"),
        TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
        expand=True,
    )
    if total > 0:
        progress.add_task("global", total=total, completed=cur)
    else:
        progress.add_task("global", total=1, completed=0)

    info = Table.grid(expand=True, padding=(0, 2))
    info.add_column(ratio=1)
    info.add_column(ratio=1)
    info.add_column(ratio=1)
    info.add_row(
        Text.assemble(("⏱  Transcurrido  ", "dim"), (elapsed, "bold magenta")),
        Text.assemble(("⏳ ETA  ", "dim"), (eta, "bold magenta")),
        Text.assemble(("Fase  ", "dim"), (state.phase, "bold cyan")),
    )

    cur_file_line = Text.assemble(
        ("Archivo activo  ", "dim"),
        (state.cur_file or "—", "bold white"),
    )

    body = Group(progress, Text(""), info, Text(""), cur_file_line)
    return Panel(
        body,
        title=Text("Progreso global", style="bold cyan"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_files(state: State) -> Panel:
    progress = load_progress()
    counts = {"done": 0, "partial": 0, "skipped": 0, "other": 0}
    file_rows = []
    for path, status in progress.items():
        # Saltar el toc.ncx (no es un xhtml de prosa)
        is_ncx = path.startswith("ncx:")
        clean_path = path.split(":", 1)[-1] if is_ncx else path
        short = clean_path.rsplit("/", 1)[-1]
        if status not in counts:
            counts["other"] += 1
        else:
            counts[status] += 1
        file_rows.append((short, status, is_ncx))

    processed = sum(1 for _, _, is_ncx in file_rows if not is_ncx)
    pending = max(state.xhtml_total - processed, 0)

    counter = Table.grid(expand=True, padding=(0, 2))
    counter.add_column(justify="center", ratio=1)
    counter.add_column(justify="center", ratio=1)
    counter.add_column(justify="center", ratio=1)
    counter.add_column(justify="center", ratio=1)
    counter.add_row(
        Text.assemble(("✓ done  ", ""), (str(counts["done"]), "bold green")),
        Text.assemble(("⚠ partial  ", ""), (str(counts["partial"]), "bold yellow")),
        Text.assemble(("⏳ pending  ", ""), (str(pending), "bold blue")),
        Text.assemble(("⊘ skipped  ", ""), (str(counts["skipped"]), "dim")),
    )

    # Tabla compacta con los archivos (últimos 12 procesados)
    tbl = Table.grid(expand=True, padding=(0, 1))
    tbl.add_column(ratio=4)
    tbl.add_column(ratio=1, justify="right")
    style_for = {
        "done": ("bold green", "✓"),
        "partial": ("bold yellow", "⚠"),
        "skipped": ("dim", "⊘"),
        "other": ("dim white", "·"),
    }
    # Solo mostrar archivos no-ncx, últimos 12
    show_rows = [r for r in file_rows if not r[2]][-12:]
    for short, status, _ in show_rows:
        color, glyph = style_for.get(status, style_for["other"])
        tbl.add_row(Text(short, style=color), Text(f"{glyph} {status}", style=color))
    if not show_rows:
        tbl.add_row(Text("(esperando primeros archivos…)", style="dim italic"), Text(""))

    body = Group(counter, Text(""), tbl)
    return Panel(
        body,
        title=Text("Archivos", style="bold cyan"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_events(state: State) -> Panel:
    if not state.events:
        body = Text("(sin eventos aún)", style="dim italic")
    else:
        tbl = Table.grid(expand=True, padding=(0, 1))
        tbl.add_column(ratio=0, no_wrap=True)
        tbl.add_column(ratio=0, no_wrap=True)
        tbl.add_column(ratio=1)
        glyph_style = {
            "ok":   ("✓", "bold green"),
            "warn": ("⚠", "bold yellow"),
            "err":  ("✗", "bold red"),
            "info": ("•", "bold cyan"),
        }
        for ev in list(state.events)[-12:]:
            glyph, gstyle = glyph_style.get(ev.kind, ("·", "white"))
            tbl.add_row(
                Text(ev.ts.strftime("%H:%M:%S"), style="dim"),
                Text(glyph, style=gstyle),
                Text(ev.msg, style="white"),
            )
        body = tbl
    return Panel(
        body,
        title=Text("Eventos recientes", style="bold cyan"),
        border_style="cyan",
        padding=(1, 2),
    )


def render_footer(state: State) -> Panel:
    log_name = state.log_path.name if state.log_path else "(no encontrado)"
    progress_name = "progress.json" if PROGRESS_FILE.exists() else "(sin progress.json)"

    if state.phase == "hecho":
        status_text = Text.assemble(
            ("● ", "bold green"), ("traducción completada", "bold green"),
        )
    elif state.phase == "esperando":
        status_text = Text.assemble(
            ("● ", "bold yellow"), ("esperando que arranque la traducción…", "yellow"),
        )
    else:
        status_text = Text.assemble(
            ("● ", "bold cyan"), ("traducción en curso", "bold cyan"),
        )

    line = Table.grid(expand=True, padding=(0, 2))
    line.add_column(ratio=1)
    line.add_column(ratio=1, justify="center")
    line.add_column(ratio=1, justify="right")
    line.add_row(
        Text.assemble(("📜 log: ", "dim"), (log_name, "white")),
        status_text,
        Text.assemble(("📒 ", "dim"), (progress_name, "dim white")),
    )
    return Panel(line, border_style="dim cyan", padding=(0, 1))


def build_layout(state: State) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="progress", size=11),
        Layout(name="middle"),
        Layout(name="footer", size=3),
    )
    layout["middle"].split_row(
        Layout(name="files", ratio=1),
        Layout(name="events", ratio=1),
    )
    layout["header"].update(render_header(state))
    layout["progress"].update(render_progress(state))
    layout["files"].update(render_files(state))
    layout["events"].update(render_events(state))
    layout["footer"].update(render_footer(state))
    return layout


# ─── Bucle principal ────────────────────────────────────────────────────────

def main() -> None:
    state = State()
    if len(sys.argv) > 1:
        state.log_path = Path(sys.argv[1]).expanduser().resolve()
        if not state.log_path.exists():
            print(f"⚠ Log no encontrado: {state.log_path}")
            sys.exit(1)
    else:
        state.log_path = find_latest_log()

    console = Console()
    refresh_hz = 4

    try:
        with Live(
            build_layout(state),
            console=console,
            refresh_per_second=refresh_hz,
            screen=True,
        ) as live:
            while True:
                # Re-detectar el log si cambió (otra traducción arrancó)
                latest = find_latest_log()
                if latest and (not state.log_path or latest != state.log_path):
                    if not state.log_path or latest.stat().st_mtime > state.log_path.stat().st_mtime:
                        state.log_path = latest
                        state.last_log_size = 0
                        state.events.clear()
                parse_log(state)
                live.update(build_layout(state))
                time.sleep(1 / refresh_hz)
    except KeyboardInterrupt:
        console.print(
            "\n[dim]Dashboard cerrado. La traducción sigue corriendo si estaba activa.[/dim]"
        )


if __name__ == "__main__":
    main()
