"""Prueba EN VIVO: traduce SOLO el Prólogo de Crossan B1 ("Del Cristo a Jesús")
con el perfil crossan_jesus, imprimiendo cada bloque EN->ES conforme llega.

Uso (desde la carpeta del proyecto, con el venv activo):
    python3 -u probe_prologo.py

Es aislado: no toca ./work ni ./progress.json (usa un dir temporal)."""
import asyncio
import importlib.util
import re
import tempfile
from pathlib import Path

from bs4 import BeautifulSoup

PROJ = Path(__file__).parent.parent  # raiz del proyecto (este script vive en scripts/)
EPUB = PROJ / "crossan_jesus.epub"

spec = importlib.util.spec_from_file_location("te", str(PROJ / "translate_epub.py"))
te = importlib.util.module_from_spec(spec)
spec.loader.exec_module(te)

# Aislar para no contaminar la corrida real
tmp = Path(tempfile.mkdtemp(prefix="crossan_probe_"))
te.WORK_DIR = tmp / "work"
te.PROGRESS_FILE = tmp / "progress.json"

# Batches chicos: feedback cada ~40-60s + traducción más confiable
# (los batches grandes causaron bloques faltantes con el libro de Dewey).
te.BATCH_SIZE = 5


def strip(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


async def main() -> None:
    print("⏳ Extrayendo EPUB…", flush=True)
    te.extract_epub(EPUB, te.WORK_DIR)
    opf = te.find_opf_path(te.WORK_DIR)
    files = te.find_xhtml_files(opf)
    target = next(p for p in files if p.name.endswith("B1_Front_Matter_2.xhtml"))

    soup = BeautifulSoup(target.read_text(encoding="utf-8"), "lxml-xml")
    blocks = [t for _, t in te.extract_translatable_blocks(soup)]
    print(f"📖 Prólogo: {len(blocks)} bloques traducibles\n", flush=True)

    profile = te.PROFILES["crossan_jesus"]
    sem = asyncio.Semaphore(te.CONCURRENCY)

    import time
    total_batches = (len(blocks) + te.BATCH_SIZE - 1) // te.BATCH_SIZE
    n = 0
    for i in range(0, len(blocks), te.BATCH_SIZE):
        batch = blocks[i:i + te.BATCH_SIZE]
        bn = i // te.BATCH_SIZE + 1
        print(f"🔄 Batch {bn}/{total_batches} ({len(batch)} bloques) — traduciendo…", flush=True)
        t0 = time.time()
        translations = await te.translate_batch(batch, "", sem, profile)
        print(f"   ⏱  {time.time() - t0:.0f}s\n", flush=True)
        for en, es in zip(batch, translations):
            en_t, es_t = strip(en), strip(es)
            if len(en_t) < 30:
                continue
            n += 1
            print(f"── [{n}] ────────────────────────────────", flush=True)
            print(f"EN: {en_t[:380]}", flush=True)
            print(f"ES: {es_t[:380]}\n", flush=True)

    print("✅ PRUEBA TERMINADA — revisa terminología, citas bíblicas y tono.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
