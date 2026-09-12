#!/usr/bin/env python3
"""fix_navdoc.py — Traduce el navDoc.xhtml (índice navegable EPUB3) que quedó
entero en inglés. Determinista: usa los títulos canónicos de Claramonte ya
verificados. No gasta cuota ni deja que el modelo reinvente terminología.
"""
import re
import shutil
from pathlib import Path

from bs4 import BeautifulSoup
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import translate_epub as T

PROJECT = Path(__file__).parent.parent  # raiz del proyecto (este script vive en scripts/)
ES = PROJECT / "Art as Experience - John Dewey_es.epub"
WORK = PROJECT / "work_navfix"

CHAPTER_TITLES = {
    1: "La criatura viviente",
    2: "La criatura viviente y «las cosas etéreas»",
    3: "Cómo se tiene una experiencia",
    4: "El acto de expresión",
    5: "El objeto expresivo",
    6: "Sustancia y forma",
    7: "La historia natural de la forma",
    8: "La organización de las energías",
    9: "La sustancia común de las artes",
    10: "La sustancia variada de las artes",
    11: "La contribución humana",
    12: "El reto a la filosofía",
    13: "Crítica y percepción",
    14: "Arte y civilización",
}
FRONT = {
    "Cover": "Portada",
    "Title Page": "Portadilla",
    "Copyright": "Créditos",
    "Dedication": "Dedicatoria",
    "Preface": "Prefacio",
    "Contents": "Índice",
    "Table of Contents": "Índice",
    "Index": "Índice analítico",
}


def translate_label(text: str) -> str | None:
    t = re.sub(r"\s+", " ", text).strip()
    # Capítulo: "N Título ..." (puede acabar en *)
    m = re.match(r"^(\d+)\s+(.*)$", t)
    if m and int(m.group(1)) in CHAPTER_TITLES:
        n = int(m.group(1))
        star = "*" if t.rstrip().endswith("*") else ""
        return f"{n} {CHAPTER_TITLES[n]}{star}"
    return FRONT.get(t)


def main():
    T.extract_epub(ES, WORK)
    nav = next(WORK.rglob("navDoc.xhtml"), None) or next(WORK.rglob("nav.xhtml"), None)
    if nav is None:
        print("✗ no se encontró navDoc.xhtml"); return
    soup = BeautifulSoup(nav.read_text(encoding="utf-8"), "lxml-xml")
    changed = 0
    for a in soup.find_all("a"):
        es = translate_label(a.get_text(" ", strip=True))
        if es and es != a.get_text(" ", strip=True):
            a.string = es
            changed += 1
    nav.write_text(str(soup), encoding="utf-8")
    print(f"navDoc: {changed} entradas traducidas")

    T.repack_epub(WORK, ES)
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"✅ _es.epub con navDoc en español: {ES.name}")


if __name__ == "__main__":
    main()
