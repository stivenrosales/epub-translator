#!/usr/bin/env python3
"""fix_partials.py — Re-traduce los capítulos que quedaron 'partial' (con bloques
en inglés porque el SDK devolvió respuestas incompletas en batches grandes).

Estrategia: reusa el pipeline probado de translate_epub (validación + reintentos)
pero con BATCH_SIZE chico para que el SDK no se salte bloques. Traduce desde el
INGLÉS original solo los 3 archivos afectados y los intercambia en el _es.epub.
"""
import asyncio
import shutil
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import translate_epub as T

T.BATCH_SIZE = 8  # batches chicos → el SDK no omite bloques

PROJECT = Path(__file__).parent.parent  # raiz del proyecto (este script vive en scripts/)
SRC_EN = PROJECT / "Art as Experience - John Dewey.epub"
ES = PROJECT / "Art as Experience - John Dewey_es.epub"
TARGETS = {"19_chapter012.xhtml", "20_chapter013.xhtml", "21_chapter014.xhtml"}


async def run():
    # 1) Extraer el INGLÉS original a la WORK_DIR del traductor
    T.extract_epub(SRC_EN, T.WORK_DIR)
    opf = T.find_opf_path(T.WORK_DIR)
    xhtmls = T.find_xhtml_files(opf)
    profile = T.PROFILES["dewey_art_experience"]
    targets = [p for p in xhtmls if p.name in TARGETS]
    print(f"Re-traduciendo {len(targets)} archivos (BATCH_SIZE={T.BATCH_SIZE}):",
          [p.name for p in targets])

    progress: dict = {}
    sem = asyncio.Semaphore(4)
    await asyncio.gather(*[
        T.translate_xhtml_file(p, sem, progress, profile, None) for p in targets
    ])
    print("Estado tras re-traducir:", progress)

    # 2) Intercambiar los 3 archivos en una copia del _es.epub y reempaquetar
    fixdir = PROJECT / "work_fix_es"
    T.extract_epub(ES, fixdir)
    for p in targets:
        rel = p.relative_to(T.WORK_DIR)
        dest = fixdir / rel
        shutil.copy(p, dest)
        print(f"  swap → {rel}")

    backup = ES.with_name(ES.stem + "_pre_fix.epub")
    if not backup.exists():
        shutil.copy(ES, backup)
        print(f"Backup: {backup.name}")

    T.sanitize_for_kindle(fixdir, T.find_opf_path(fixdir))
    T.repack_epub(fixdir, ES)
    shutil.rmtree(fixdir, ignore_errors=True)
    print(f"✅ _es.epub corregido: {ES.name}")


if __name__ == "__main__":
    asyncio.run(run())
