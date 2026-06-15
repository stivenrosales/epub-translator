#!/usr/bin/env python3
"""enrich_epub.py — Enriquece un EPUB ya traducido (de translate_epub.py) con:

  • una GUÍA DE LECTURA al inicio de cada capítulo (intro que conecta con lo
    previo + términos clave definidos en simple con analogía), y
  • un MAPA CONCEPTUAL HTML/CSS al comienzo de cada sub-sección (marcada por el
    ornamento «* * *», clase "ornament").

Diseño (Aprobación B del brainstorming):
  - Script SEPARADO que reusa la plomería de translate_epub.py (extract/repack/
    sanitize) y su llamada al Agent SDK (call_claude). No retraduce nada.
  - Claude NO escribe HTML: devuelve DATOS ESTRUCTURADOS (JSON) y Python los
    renderiza con una plantilla determinista → XHTML siempre válido para Kindle.
  - Paralelo: los 14 capítulos son independientes y se procesan con asyncio
    (semáforo). Para evitar dependencia secuencial, cada capítulo recibe la
    LISTA completa de títulos traducidos + su posición, no el output del previo.
  - Resumible: enrich_progress.json cachea los datos por capítulo; re-render sin
    re-llamar a Claude. Usa --force para regenerar.

Uso:
    python3 enrich_epub.py [ruta_al_epub_es.epub] [--force] [--only N]
Si no se pasa ruta, toma el *_es.epub más reciente del directorio.
Salida: <stem sin _es>_es_enriquecido.epub
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# Reutilizamos la plomería del traductor (importar es seguro: tiene guard __main__)
from translate_epub import (
    call_claude,
    extract_epub,
    find_opf_path,
    find_xhtml_files,
    repack_epub,
    sanitize_for_kindle,
)

PROJECT_DIR = Path(__file__).parent
WORK_DIR = PROJECT_DIR / "work_enrich"
PROGRESS_PATH = PROJECT_DIR / "enrich_progress.json"
CSS_SRC = PROJECT_DIR / "enrichment.css"
CSS_NAME = "enrichment.css"
ENRICH_CONCURRENCY = 4

CHAPTER_RE = re.compile(r"chapter\d+\.xhtml$", re.IGNORECASE)  # excluye *_fnNN.xhtml

# ─── Prompt de enriquecimiento ──────────────────────────────────────────────

ENRICH_SYSTEM_PROMPT = """Eres un filósofo y pedagogo experto en estética, en la obra de John Dewey y en "El arte como experiencia". Tu tarea NO es traducir: es leer un capítulo YA TRADUCIDO al español y destilar su estructura conceptual para ayudar a un lector culto pero no especialista a entenderlo antes de leerlo.

Recibes: el título del capítulo, la lista ordenada de los 14 capítulos del libro (para que sitúes este en el arco), y el texto del capítulo dividido en SECCIONES numeradas (separadas por el ornamento «* * *» del propio Dewey).

Devuelves EXCLUSIVAMENTE un objeto JSON válido (sin markdown, sin ```), con esta forma EXACTA:

{
  "resumen_capitulo": "2-3 frases que sintetizan la tesis del capítulo (uso interno).",
  "guia": {
    "intro": ["1 o 2 párrafos que presentan de qué trata el capítulo y lo CONECTAN con lo visto en los capítulos anteriores (usa la lista de títulos). Claro, al grano, motivador."],
    "terminos": [
      {"palabra": "término tal como aparece", "definicion": "definición en lenguaje SIMPLE y directo, sin jerga", "analogia": "una analogía cotidiana (deja \"\" si no aplica)"}
    ]
  },
  "mapas": [
    {
      "central": "CONCEPTO CENTRAL de la sección (mayúsculas, breve)",
      "ramas": [
        {"idea": "idea clave", "relacion": "relación con el centro u otra rama, p.ej. \"se opone a\", \"ritmo con\" (deja \"\" si no aplica)", "subnodos": ["detalle 1", "detalle 2"]}
      ]
    }
  ]
}

REGLAS:
- El array "mapas" DEBE tener EXACTAMENTE tantos elementos como SECCIONES recibas, en el MISMO orden. Un mapa por sección.
- Los mapas deben ser RICOS: 3 a 5 ramas por mapa, con subnodos y relaciones rotuladas cuando aporten. Captura el ARGUMENTO de la sección, no solo palabras sueltas.
- "terminos": 3 a 6 términos por capítulo, los que el lector se va a topar. Definición simple + analogía cuando ayude.
- Respeta la terminología de la traducción de Claramonte que ves en el texto (p.ej. "criatura viviente", "hacer y padecer", "una experiencia", "consumación", "lo estético", "cualidad").
- Español neutro latinoamericano, registro culto pero claro.
- SOLO el JSON. Nada antes ni después.
"""


def build_enrich_prompt(title: str, all_titles: list[str], idx: int, sections: list[str]) -> str:
    titles_block = "\n".join(
        f"  {i + 1}. {t}{'   ← ESTE' if i == idx else ''}" for i, t in enumerate(all_titles)
    )
    sec_block = "\n\n".join(
        f"───── SECCIÓN {i + 1} ─────\n{txt}" for i, txt in enumerate(sections)
    )
    return f"""CAPÍTULO ACTUAL: {idx + 1}. {title}

LOS 14 CAPÍTULOS DEL LIBRO (para situar el arco):
{titles_block}

Este capítulo tiene {len(sections)} sección(es), así que el array "mapas" debe tener EXACTAMENTE {len(sections)} mapa(s), en orden.

TEXTO DEL CAPÍTULO (ya traducido):
{sec_block}
"""


# ─── Parseo robusto del JSON ────────────────────────────────────────────────

def parse_enrich_json(text: str) -> dict:
    """Extrae el objeto JSON aunque venga con fences o texto alrededor."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    # Recorta del primer { al último }
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No se encontró JSON. Primeros 200 chars: {text[:200]!r}")
    return json.loads(s[start : end + 1])


# ─── División del capítulo en secciones por «* * *» ─────────────────────────

def split_chapter(soup: BeautifulSoup):
    """Devuelve (h1, sections) donde sections es una lista de listas de tags de
    bloque. El corte se hace en cada <p class="ornament">. El h1 queda fuera."""
    blocks = soup.body.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6"]) if soup.body else []
    h1 = next((b for b in blocks if b.name == "h1"), None)

    def is_ornament(t):
        cls = t.get("class") or []
        if "ornament" in cls:
            return True
        # fallback: contenido tipo "* * *"
        return bool(re.fullmatch(r"[\s*·•∗❋✳…]+", t.get_text(strip=True) or "")) and t.name == "p" and len(t.get_text(strip=True)) <= 8

    sections: list[list] = []
    current: list = []
    for b in blocks:
        if b is h1:
            continue
        if is_ornament(b):
            sections.append(current)
            current = []
        else:
            current.append(b)
    sections.append(current)
    # No descartamos secciones vacías para mantener el alineamiento con los mapas,
    # pero filtramos las que no tienen ningún bloque con texto real.
    sections = [s for s in sections if any(b.get_text(strip=True) for b in s)]
    return h1, sections


def section_plain_text(section: list) -> str:
    return "\n".join(b.get_text(" ", strip=True) for b in section if b.get_text(strip=True))


# ─── Renderizado determinista → XHTML ───────────────────────────────────────

def _el(soup, name, cls=None, text=None):
    t = soup.new_tag(name)
    if cls:
        t["class"] = cls
    if text is not None:
        t.string = text
    return t


def render_guide(soup, guide: dict, chap_num: int, title: str):
    box = _el(soup, "div", "guia-lectura")
    box.append(_el(soup, "p", "guia-titulo", f"Guía de lectura — Cap. {chap_num}: {title}"))
    for para in guide.get("intro", []):
        if para and para.strip():
            box.append(_el(soup, "p", "guia-intro", para.strip()))
    terms = [t for t in guide.get("terminos", []) if t.get("palabra")]
    if terms:
        box.append(_el(soup, "p", "guia-subtitulo", "Términos que verás"))
        ul = _el(soup, "ul", "guia-terminos")
        for t in terms:
            li = soup.new_tag("li")
            li.append(_el(soup, "span", "termino-palabra", f"{t['palabra'].strip()}. "))
            defin = (t.get("definicion") or "").strip()
            if defin:
                li.append(_el(soup, "span", "termino-def", defin + " "))
            ana = (t.get("analogia") or "").strip()
            if ana:
                if not ana.lower().startswith(("como ", "es como")):
                    ana = "Como " + ana
                li.append(_el(soup, "span", "termino-analogia", ana))
            ul.append(li)
        box.append(ul)
    return box


def render_map(soup, mapa: dict, sec_num: int):
    box = _el(soup, "div", "mapa-concepto")
    box.append(_el(soup, "p", "mapa-titulo", f"◆ Mapa conceptual — Sección {sec_num} ◆"))
    box.append(_el(soup, "div", "mapa-central", (mapa.get("central") or "—").strip()))
    ramas = mapa.get("ramas", [])
    if ramas:
        ul = _el(soup, "ul", "mapa-ramas")
        for r in ramas:
            li = _el(soup, "li", "rama")
            li.append(_el(soup, "span", "rama-idea", (r.get("idea") or "").strip()))
            rel = (r.get("relacion") or "").strip()
            if rel:
                li.append(soup.new_string(" "))
                li.append(_el(soup, "span", "rama-rel", f"({rel})"))
            subs = [s for s in r.get("subnodos", []) if s and s.strip()]
            if subs:
                sub_ul = _el(soup, "ul", "subnodos")
                for s in subs:
                    sub_ul.append(_el(soup, "li", None, s.strip()))
                li.append(sub_ul)
            ul.append(li)
        box.append(ul)
    return box


def inject(soup, h1, sections, data: dict, chap_num: int, title: str):
    """Inserta la guía tras el h1 y un mapa al inicio de cada sección."""
    guide = render_guide(soup, data.get("guia", {}), chap_num, title)
    if h1 is not None:
        h1.insert_after(guide)
    elif sections and sections[0]:
        sections[0][0].insert_before(guide)

    mapas = data.get("mapas", [])
    n = len(sections)
    if len(mapas) != n:
        print(f"    ⚠ cap {chap_num}: {len(mapas)} mapas para {n} secciones (ajustando)")
    for i, section in enumerate(sections):
        if not section:
            continue
        mapa = mapas[i] if i < len(mapas) else {"central": title.upper(), "ramas": []}
        section[0].insert_before(render_map(soup, mapa, i + 1))


# ─── Enriquecimiento de un capítulo ─────────────────────────────────────────

async def enrich_chapter(path: Path, idx: int, all_titles: list[str], sem, progress: dict, force: bool):
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml-xml")
    h1, sections = split_chapter(soup)
    title = (h1.get_text(" ", strip=True) if h1 else all_titles[idx]) or all_titles[idx]
    # normaliza el título quitando el número inicial "3  Cómo se tiene..."
    title_clean = re.sub(r"^\s*\d+[\.\s]+", "", title).strip()

    if not sections:
        print(f"  · Cap {idx + 1} ({path.name}): sin secciones traducibles, omitido")
        return

    key = path.name
    cached = progress.get(key)
    if cached and not force:
        data = cached
        print(f"  ↺ Cap {idx + 1} «{title_clean}»: usando caché ({len(sections)} secciones)")
    else:
        sec_texts = [section_plain_text(s) for s in sections]
        prompt = build_enrich_prompt(title_clean, all_titles, idx, sec_texts)
        async with sem:
            print(f"  → Cap {idx + 1} «{title_clean}»: generando ({len(sections)} secciones)…")
            raw = await call_claude(prompt, ENRICH_SYSTEM_PROMPT)
        data = parse_enrich_json(raw)
        progress[key] = data
        PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ Cap {idx + 1} «{title_clean}»: {len(data.get('mapas', []))} mapas + guía")

    inject(soup, h1, sections, data, idx + 1, title_clean)
    path.write_text(str(soup), encoding="utf-8")


# ─── Enlazar CSS ────────────────────────────────────────────────────────────

def link_css_in_chapter(path: Path):
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "lxml-xml")
    head = soup.find("head")
    if head is None:
        return
    if not head.find("link", attrs={"href": CSS_NAME}):
        link = soup.new_tag("link", rel="stylesheet", type="text/css", href=CSS_NAME)
        head.append(link)
        path.write_text(str(soup), encoding="utf-8")


def add_css_to_manifest(opf_path: Path, css_dest: Path):
    soup = BeautifulSoup(opf_path.read_text(encoding="utf-8"), "lxml-xml")
    manifest = soup.find("manifest")
    if manifest is None:
        return
    href = css_dest.relative_to(opf_path.parent).as_posix()
    if not manifest.find("item", attrs={"href": href}):
        item = soup.new_tag("item")
        item["id"] = "enrichment-css"
        item["href"] = href
        item["media-type"] = "text/css"
        manifest.append(item)
        opf_path.write_text(str(soup), encoding="utf-8")


# ─── Selección del EPUB fuente ──────────────────────────────────────────────

def find_source_es_epub(argv_path: str | None) -> Path:
    if argv_path:
        p = Path(argv_path)
        if not p.exists():
            sys.exit(f"✗ No existe: {p}")
        return p
    candidates = sorted(
        PROJECT_DIR.glob("*_es.epub"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Prioriza Dewey si está
    dewey = [c for c in candidates if "Art as Experience" in c.name or "Dewey" in c.name]
    if dewey:
        return dewey[0]
    if not candidates:
        sys.exit("✗ No se encontró ningún *_es.epub. Corre primero translate_epub.py.")
    return candidates[0]


# ─── Main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    ap = argparse.ArgumentParser(description="Enriquece un EPUB traducido con guías y mapas conceptuales.")
    ap.add_argument("epub", nargs="?", help="Ruta al *_es.epub (por defecto: el más reciente / Dewey)")
    ap.add_argument("--force", action="store_true", help="Regenera aunque haya caché")
    ap.add_argument("--only", type=int, help="Procesa solo el capítulo N (1-based, para pruebas)")
    args = ap.parse_args()

    src = find_source_es_epub(args.epub)
    stem = src.stem[:-3] if src.stem.endswith("_es") else src.stem
    output = PROJECT_DIR / f"{stem}_es_enriquecido.epub"

    print(f"Fuente   : {src.name}")
    print(f"Destino  : {output.name}")
    print(f"Concurrencia: {ENRICH_CONCURRENCY}")

    extract_epub(src, WORK_DIR)
    opf_path = find_opf_path(WORK_DIR)
    xhtml_files = find_xhtml_files(opf_path)
    chapters = [p for p in xhtml_files if CHAPTER_RE.search(p.name)]
    print(f"Capítulos: {len(chapters)} detectados")
    if not chapters:
        sys.exit("✗ No se detectaron archivos de capítulo (patrón chapterNNN.xhtml).")

    # Títulos traducidos (del h1 de cada capítulo) para el arco narrativo
    all_titles = []
    for p in chapters:
        s = BeautifulSoup(p.read_text(encoding="utf-8"), "lxml-xml")
        h1 = s.find("h1")
        t = h1.get_text(" ", strip=True) if h1 else p.stem
        all_titles.append(re.sub(r"^\s*\d+[\.\s]+", "", t).strip())

    progress = {}
    if PROGRESS_PATH.exists() and not args.force:
        progress = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))

    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    tasks = []
    for idx, p in enumerate(chapters):
        if args.only and (idx + 1) != args.only:
            continue
        tasks.append(enrich_chapter(p, idx, all_titles, sem, progress, args.force))
    await asyncio.gather(*tasks)

    # CSS: copiar junto a los capítulos, enlazar en cada uno y registrar en el OPF
    css_dest = chapters[0].parent / CSS_NAME
    css_dest.write_text(CSS_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    for p in chapters:
        link_css_in_chapter(p)
    add_css_to_manifest(opf_path, css_dest)

    print("Saneando para Kindle…")
    sanitize_for_kindle(WORK_DIR, opf_path)

    print("Empaquetando…")
    repack_epub(WORK_DIR, output)
    print(f"\n✅ Listo: {output.name}")


if __name__ == "__main__":
    asyncio.run(main())
