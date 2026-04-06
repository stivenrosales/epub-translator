#!/usr/bin/env python3
"""
translate_epub.py — Traductor de epub EN → ES LATAM neutro con Claude Sonnet.

Usa el Claude Agent SDK, que se autentica con tu login de Claude Code (Max).
NO consume créditos de API — usa tu cuota de suscripción Max.

Uso:
    1. pip install claude-agent-sdk beautifulsoup4 lxml tenacity tqdm
    2. Asegurate de estar logueado en Claude Code (ya lo estás si leés esto)
    3. python translate_epub.py

Qué hace:
    - Detecta el .epub fuente (excluye los *_es.epub ya traducidos)
    - Detecta el perfil automáticamente: "generic" o "ai_engineering"
       (basado en densidad de <pre>/programlisting/<math>)
    - Descomprime en ./work/
    - Localiza los XHTMLs vía META-INF/container.xml → content.opf (manifest)
    - Para cada XHTML extrae bloques hoja, EXCLUYENDO subtrees de código/math/svg
    - Tokeniza <code>, <math>, <svg> inline como ⟦OPAQUE_N⟧ antes de traducir
      (garantiza que el modelo no toca contenido técnico)
    - Traduce en batches con contexto rolling y glosario del perfil
    - Restaura los tokens opacos intactos en cada bloque traducido
    - Valida estructura de tags (reintenta si difiere)
    - Actualiza toc.ncx y content.opf (dc:language → es-419)
    - Reempaqueta como <original>_es.epub con mimetype primero sin comprimir
    - Checkpoint en progress.json: reanudable

Costo: $0 incremental (auth Max, no API).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm.asyncio import tqdm as atqdm

# ─── Config global ──────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
WORK_DIR = PROJECT_DIR / "work"
PROGRESS_FILE = PROJECT_DIR / "progress.json"

MODEL = "claude-sonnet-4-6"   # Último Sonnet. Si da problemas: "claude-sonnet-4-5"
BATCH_SIZE = 15
CONCURRENCY = 2

# Forzar perfil manualmente: "generic", "ai_engineering", "grokking_algorithms", o None (auto-detect)
FORCE_PROFILE: str | None = None

# ─── Perfiles de libro ──────────────────────────────────────────────────────

# Perfil "generic" — para Godin y similares (no-ficción literaria/negocios)
GLOSSARY_GENERIC: dict[str, str] = {
    "the practice": "la práctica",
    "ship": "entregar",
    "shipping": "publicar el trabajo",
    "the work": "el trabajo",
    "generous": "generoso",
    "creative": "creativo",
    "creator": "creador",
    "assertion": "afirmación",
    "trust": "confianza",
    "craft": "oficio",
    "professional": "profesional",
    "amateur": "aficionado",
    "audience": "audiencia",
    "reader": "lector",
    "hack": "atajo",
    "intent": "intención",
    "skill": "habilidad",
}

SYSTEM_PROMPT_GENERIC = """Sos un traductor literario profesional especializado en no-ficción de negocios y creatividad. Traducís del inglés al español latinoamericano neutro para un lector peruano.

REGLAS ABSOLUTAS:

1. TOKENS OPACOS: los tokens ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos. NO los traduzcas, NO los modifiques, NO los elimines. Preservá su posición exacta en el texto.

2. PRESERVÁ todos los tags HTML exactamente: <em>, <strong>, <a href>, <span>, <sup>, <br/>, etc. Mismos atributos, mismas cantidades, mismo orden.

3. Traducí SOLO el texto visible entre tags.

4. ESPAÑOL LATAM NEUTRO, lector peruano:
   - "tú" como segunda persona (no "vos", no "usted" salvo registro formal original).
   - Nada de "chido", "guay", "chévere", "bacán".
   - Sin "vosotros" ni conjugaciones peninsulares.
   - Registro profesional, claro, directo.

5. TONO del autor: frases cortas, directas, punzantes. No suavices, no embellezcas.

6. NO expliques, NO resumas, NO agregues notas del traductor.

7. Respetá el glosario proporcionado.

8. FORMATO DE RESPUESTA — obligatorio:
   <<<BLOCK 0>>>
   <html traducido del bloque 0>
   <<<BLOCK 1>>>
   <html traducido del bloque 1>
   <<<END>>>

   Sin JSON, sin backticks, sin markdown, sin texto extra antes/después.
"""

# Perfil "ai_engineering" — para O'Reilly / libros técnicos ML/AI
GLOSSARY_AI_ENGINEERING: dict[str, str] = {
    # Términos del dominio que SE MANTIENEN EN INGLÉS (convención del campo)
    "foundation model": "foundation model",
    "foundation models": "foundation models",
    "large language model": "large language model",
    "large language models": "large language models",
    "LLM": "LLM",
    "LLMs": "LLMs",
    "prompt": "prompt",
    "prompts": "prompts",
    "prompt engineering": "prompt engineering",
    "prompting": "prompting",
    "token": "token",
    "tokens": "tokens",
    "tokenizer": "tokenizer",
    "embedding": "embedding",
    "embeddings": "embeddings",
    "fine-tuning": "fine-tuning",
    "fine-tune": "fine-tune",
    "pretraining": "pretraining",
    "pre-training": "pre-training",
    "post-training": "post-training",
    "RAG": "RAG",
    "retrieval-augmented generation": "retrieval-augmented generation",
    "transformer": "transformer",
    "transformers": "transformers",
    "attention": "attention",
    "self-attention": "self-attention",
    "context window": "context window",
    "chain-of-thought": "chain-of-thought",
    "in-context learning": "in-context learning",
    "zero-shot": "zero-shot",
    "few-shot": "few-shot",
    "LoRA": "LoRA",
    "PEFT": "PEFT",
    "RLHF": "RLHF",
    "DPO": "DPO",
    "alignment": "alignment",
    "guardrails": "guardrails",
    "benchmark": "benchmark",
    "benchmarks": "benchmarks",
    "dataset": "dataset",
    "datasets": "datasets",
    "pipeline": "pipeline",
    "pipelines": "pipelines",
    "framework": "framework",
    "workflow": "workflow",
    "throughput": "throughput",
    # Términos que SÍ se traducen
    "inference": "inferencia",
    "training": "entrenamiento",
    "evaluation": "evaluación",
    "latency": "latencia",
    "reasoning": "razonamiento",
    "agent": "agente",
    "agents": "agentes",
    "hallucination": "alucinación",
    "hallucinations": "alucinaciones",
    "deployment": "despliegue",
    "scaling": "escalado",
    "scalability": "escalabilidad",
    "safety": "seguridad",
    "bias": "sesgo",
    "biases": "sesgos",
    "data": "datos",
    "model": "modelo",
    "models": "modelos",
}

SYSTEM_PROMPT_AI_ENGINEERING = """Sos un traductor técnico profesional especializado en ingeniería de software, inteligencia artificial y machine learning. Traducís del inglés al español latinoamericano neutro para un lector peruano con background en programación.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders de contenido técnico protegido: código, ecuaciones MathML, gráficos SVG.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes. NO agregues espacios dentro.
   - Preservá su posición EXACTA dentro del texto. El espacio/puntuación alrededor puede ajustarse al español.

2. PRESERVÁ TODOS los tags HTML exactamente: <em>, <strong>, <a href>, <span>, <sup>, <code>, <br/>, etc. No cambies atributos (href, id, class, data-type, epub:type), no agregues tags, no quites tags.

3. NUNCA TRADUZCAS contenido dentro de <code>: son identificadores técnicos (nombres de funciones, métodos, clases, variables, flags, argumentos, rutas, URLs, nombres de archivos, parámetros). Preservalos EXACTAMENTE carácter por carácter. (Nota: la mayoría vendrán como tokens opacos, pero si ves algún <code> directo, aplicá la misma regla.)

4. TERMINOLOGÍA ML/AI — mantener en INGLÉS los términos establecidos del dominio:
   foundation model, prompt, token, embedding, transformer, attention, self-attention, fine-tuning, pretraining, RAG, RLHF, DPO, LoRA, PEFT, LLM, context window, chain-of-thought, in-context learning, zero-shot, few-shot, alignment, guardrails, benchmark, dataset, pipeline, framework, workflow, tokenizer, throughput.
   Estos NO se traducen, aunque aparezcan múltiples veces. El texto alrededor sí.

5. Términos que SÍ se traducen (consistentemente):
   inference → inferencia, training → entrenamiento, evaluation → evaluación, latency → latencia, reasoning → razonamiento, agent → agente, hallucination → alucinación, deployment → despliegue, scaling → escalado, safety → seguridad, bias → sesgo, model → modelo, data → datos.

6. ESPAÑOL LATAM NEUTRO, lector peruano técnico:
   - "tú" como segunda persona (no "vos", no "vosotros", no "usted" salvo formalidad explícita).
   - Sin modismos regionales.
   - Registro: técnico, claro, pragmático.

7. TONO del autor (Chip Huyen):
   - Técnico pero accesible.
   - Pragmático, orientado a ingeniería aplicada, no académico abstracto.
   - Respetá la estructura de explicaciones paso a paso y referencias a papers.

8. NO expliques, NO resumas, NO agregues notas del traductor.

9. FORMATO DE RESPUESTA — obligatorio:
   <<<BLOCK 0>>>
   <html traducido del bloque 0>
   <<<BLOCK 1>>>
   <html traducido del bloque 1>
   <<<END>>>

   Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

# Perfil "grokking_algorithms" — Manning / Aditya Bhargava
# Libro técnico de algoritmos con tono accesible, ilustrado, código Python
GLOSSARY_GROKKING: dict[str, str] = {
    # Estructuras de datos
    "array": "arreglo",
    "arrays": "arreglos",
    "list": "lista",
    "lists": "listas",
    "linked list": "lista enlazada",
    "linked lists": "listas enlazadas",
    "stack": "pila",
    "stacks": "pilas",
    "queue": "cola",
    "queues": "colas",
    "hash table": "tabla hash",
    "hash tables": "tablas hash",
    "hash function": "función hash",
    "hash map": "hash map",
    "tree": "árbol",
    "trees": "árboles",
    "binary tree": "árbol binario",
    "binary search tree": "árbol binario de búsqueda",
    "heap": "montículo",
    "graph": "grafo",
    "graphs": "grafos",
    "node": "nodo",
    "nodes": "nodos",
    "edge": "arista",
    "edges": "aristas",
    "weighted graph": "grafo ponderado",
    "directed graph": "grafo dirigido",
    "undirected graph": "grafo no dirigido",
    # Algoritmos
    "binary search": "búsqueda binaria",
    "simple search": "búsqueda simple",
    "linear search": "búsqueda lineal",
    "selection sort": "ordenamiento por selección",
    "quicksort": "quicksort",
    "merge sort": "merge sort",
    "breadth-first search": "búsqueda en anchura",
    "BFS": "BFS",
    "depth-first search": "búsqueda en profundidad",
    "DFS": "DFS",
    "Dijkstra’s algorithm": "algoritmo de Dijkstra",
    "Dijkstra's algorithm": "algoritmo de Dijkstra",
    "Bellman-Ford algorithm": "algoritmo de Bellman-Ford",
    "greedy algorithm": "algoritmo voraz",
    "greedy algorithms": "algoritmos voraces",
    "dynamic programming": "programación dinámica",
    "recursion": "recursión",
    "recursive": "recursivo",
    "base case": "caso base",
    "recursive case": "caso recursivo",
    "divide and conquer": "divide y vencerás",
    "k-nearest neighbors": "k vecinos más cercanos",
    "KNN": "KNN",
    # Análisis
    "running time": "tiempo de ejecución",
    "big O notation": "notación O grande",
    "Big O notation": "notación O grande",
    "time complexity": "complejidad temporal",
    "space complexity": "complejidad espacial",
    "logarithm": "logaritmo",
    "logarithmic": "logarítmico",
    "constant time": "tiempo constante",
    "linear time": "tiempo lineal",
    "NP-complete": "NP-completo",
    "NP-complete problems": "problemas NP-completos",
    "traveling salesperson": "vendedor viajero",
    "traveling salesman": "vendedor viajero",
    "shortest path": "camino más corto",
    "shortest-path": "camino más corto",
    # Programación
    "function": "función",
    "functions": "funciones",
    "variable": "variable",
    "variables": "variables",
    "index": "índice",
    "element": "elemento",
    "elements": "elementos",
    "key": "clave",
    "value": "valor",
    "pointer": "puntero",
    "pointers": "punteros",
    "loop": "bucle",
    "iteration": "iteración",
    "sorted": "ordenado",
    "unsorted": "desordenado",
    "data structure": "estructura de datos",
    "data structures": "estructuras de datos",
    "algorithm": "algoritmo",
    "algorithms": "algoritmos",
    "machine learning": "machine learning",
    # Términos técnicos que se MANTIENEN en inglés
    "null": "null",
    "None": "None",
    "True": "True",
    "False": "False",
    "Python": "Python",
    "JavaScript": "JavaScript",
}

SYSTEM_PROMPT_GROKKING = """Sos un traductor técnico profesional especializado en ciencias de la computación y algoritmos. Traducís del inglés al español latinoamericano neutro para un lector peruano que está aprendiendo a programar.

Este libro es "Grokking Algorithms" de Aditya Y. Bhargava (Manning, 2a edición). Es un libro ILUSTRADO, técnico pero accesible, con tono amigable, conversacional y divertido — no académico, no formal. El autor le habla al lector como un amigo que le explica algoritmos con humor y ejemplos cotidianos.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders de contenido técnico protegido: código inline, fórmulas, SVG.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes. NO agregues espacios dentro.
   - Preservá su posición EXACTA dentro del texto. El espacio/puntuación alrededor puede ajustarse al español.

2. PRESERVÁ TODOS los tags HTML exactamente: <i>, <b>, <em>, <strong>, <a href>, <span>, <code>, <sup>, <br/>, etc. Mismos atributos (class, id, href, data-*), mismas cantidades, mismo orden. Esto incluye <span class="fm-combinumeral">①</span>, <span class="fm-combinumeral">②</span>, etc. — los marcadores numerados ①②③④⑤⑥⑦⑧⑨⑩⑪⑫ se preservan EXACTAMENTE como están.

3. IDENTIFICADORES DE CÓDIGO: nombres de funciones, variables, métodos, clases y keywords de Python (def, if, else, elif, while, for, return, None, True, False, len, print, range, etc.) NUNCA se traducen. La mayoría vendrán como tokens opacos, pero si ves alguno directo, mantenelo carácter por carácter.

4. GLOSARIO DE ALGORITMOS — respetar SIEMPRE las traducciones del glosario proporcionado. "array" siempre es "arreglo", "binary search" siempre es "búsqueda binaria", "linked list" siempre es "lista enlazada", etc. Consistencia total a lo largo del libro.

5. ESPAÑOL LATAM NEUTRO, lector peruano principiante/intermedio en programación:
   - "tú" como segunda persona (no "vos", no "vosotros", no "usted").
   - Sin modismos regionales ("chido", "guay", "chévere", "bacán", "mola" — NINGUNO).
   - Sin conjugaciones peninsulares (nada de "vosotros tenéis", "podéis", etc.).
   - Registro: técnico pero amigable, como un profesor buena onda explicándote algo.

6. TONO del autor (Bhargava) — CRÍTICO:
   - Conversacional, directo, con humor suave.
   - Frases cortas. Párrafos cortos.
   - NO suavices, NO embellezcas, NO formalices. Si el original dice "Here's the thing", traducí "Aquí está la cosa" / "Mira", no "A continuación se presenta lo siguiente".
   - Preservá exclamaciones, preguntas retóricas y ejemplos cotidianos.

7. NO expliques, NO resumas, NO agregues notas del traductor, NO expandas siglas que el autor dejó sin expandir.

8. FORMATO DE RESPUESTA — obligatorio:
   <<<BLOCK 0>>>
   <html traducido del bloque 0>
   <<<BLOCK 1>>>
   <html traducido del bloque 1>
   <<<END>>>

   Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

# Archivos a saltear por perfil (sin traducir)
SKIP_PATTERNS_GENERIC = [
    r"^01_Cover\.",
    r"^02_Title_Page\.",
    r"^03_Copyright\.",
]

SKIP_PATTERNS_AI_ENGINEERING = [
    r"^titlepage",
    r"^copyright-page",
    r"^colophon",
    r"^ix\d+_split_",     # índices — no útiles traducidos en libros técnicos
]

SKIP_PATTERNS_GROKKING = [
    r"^title\.htm$",
    r"^copyright\.htm$",
    r"^IFC\.htm$",        # Inside Front Cover
    r"^IBC\.htm$",        # Inside Back Cover
    r"^index\.html$",     # Índice alfabético en inglés — no útil traducido
]

PROFILES = {
    "generic": {
        "glossary": GLOSSARY_GENERIC,
        "system_prompt": SYSTEM_PROMPT_GENERIC,
        "skip_patterns": SKIP_PATTERNS_GENERIC,
    },
    "ai_engineering": {
        "glossary": GLOSSARY_AI_ENGINEERING,
        "system_prompt": SYSTEM_PROMPT_AI_ENGINEERING,
        "skip_patterns": SKIP_PATTERNS_AI_ENGINEERING,
    },
    "grokking_algorithms": {
        "glossary": GLOSSARY_GROKKING,
        "system_prompt": SYSTEM_PROMPT_GROKKING,
        "skip_patterns": SKIP_PATTERNS_GROKKING,
    },
}

# ─── Detección de fuente y perfil ───────────────────────────────────────────

def find_source_epub() -> Path:
    """Elige el epub fuente: excluye *_es.epub; si hay varios, pide al usuario."""
    candidates = sorted(
        [p for p in PROJECT_DIR.glob("*.epub") if "_es" not in p.stem],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        sys.exit("✗ No se encontró ningún .epub fuente en la carpeta (se excluyen *_es.epub).")
    if len(candidates) == 1:
        return candidates[0]
    print("Múltiples epubs fuente encontrados. Elegí uno:")
    for i, c in enumerate(candidates):
        size_mb = c.stat().st_size / 1024 / 1024
        print(f"  [{i}] ({size_mb:.1f} MB)  {c.name}")
    while True:
        try:
            idx = int(input("Número: ").strip())
            if 0 <= idx < len(candidates):
                return candidates[idx]
        except (ValueError, EOFError):
            pass
        print("Ingresá un número válido.")


def detect_book_profile(epub_path: Path) -> str:
    """Detecta el perfil automáticamente mirando marcadores específicos del editor."""
    oreilly_hits = 0       # O'Reilly: data-type="programlisting"
    manning_hits = 0       # Manning: class="programlisting" + fm-code-in-text
    math_hits = 0
    pre_hits = 0
    with zipfile.ZipFile(epub_path) as zf:
        for name in zf.namelist():
            if not name.endswith((".xhtml", ".html", ".htm")):
                continue
            try:
                content = zf.read(name).decode("utf-8", errors="ignore")
            except Exception:
                continue
            oreilly_hits += content.count('data-type="programlisting"')
            manning_hits += content.count('class="programlisting"')
            manning_hits += content.count('fm-code-in-text')
            math_hits += content.count("<math")
            pre_hits += content.count("<pre")
    # Manning > O'Reilly → Grokking-style
    if manning_hits >= 5 and manning_hits > oreilly_hits:
        return "grokking_algorithms"
    # O'Reilly estructurado
    if oreilly_hits >= 5 or math_hits >= 5:
        return "ai_engineering"
    # Fallback: mucho <pre> pero sin marcadores conocidos → técnico genérico
    if pre_hits >= 10:
        return "ai_engineering"
    return "generic"


# ─── Localización de XHTMLs vía OPF manifest ────────────────────────────────

def find_opf_path(work_dir: Path) -> Path:
    """Localiza content.opf vía META-INF/container.xml."""
    container = work_dir / "META-INF" / "container.xml"
    if container.exists():
        soup = BeautifulSoup(container.read_text(encoding="utf-8"), "lxml-xml")
        rootfile = soup.find("rootfile")
        if rootfile and rootfile.get("full-path"):
            p = work_dir / rootfile.get("full-path")
            if p.exists():
                return p
    # Fallbacks
    for candidate in work_dir.rglob("content.opf"):
        return candidate
    for candidate in work_dir.rglob("*.opf"):
        return candidate
    sys.exit("✗ No se encontró content.opf en el epub.")


def find_xhtml_files(opf_path: Path) -> list[Path]:
    """Lee el OPF y devuelve todos los archivos XHTML/HTML del manifest en orden del spine."""
    opf_dir = opf_path.parent
    soup = BeautifulSoup(opf_path.read_text(encoding="utf-8"), "lxml-xml")

    # Construir mapa id → path
    id_to_path: dict[str, Path] = {}
    for item in soup.find_all("item"):
        href = item.get("href", "")
        media_type = (item.get("media-type") or "").lower()
        item_id = item.get("id", "")
        if not href or not item_id:
            continue
        if "xhtml+xml" in media_type or media_type == "text/html" or href.endswith((".xhtml", ".html")):
            full_path = (opf_dir / href).resolve()
            if full_path.exists():
                id_to_path[item_id] = full_path

    # Usar orden del spine si existe; si no, orden del manifest
    spine = soup.find("spine")
    if spine:
        ordered = []
        for itemref in spine.find_all("itemref"):
            ref = itemref.get("idref", "")
            if ref in id_to_path:
                ordered.append(id_to_path[ref])
        # Agregar cualquier xhtml no referenciado en spine al final
        referenced = set(ordered)
        for p in id_to_path.values():
            if p not in referenced:
                ordered.append(p)
        return ordered
    return sorted(id_to_path.values())


def should_skip_file(filename: str, skip_patterns: list[str]) -> bool:
    return any(re.search(pat, filename) for pat in skip_patterns)


# ─── Extracción de bloques traducibles ─────────────────────────────────────

BLOCK_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "figcaption", "dt", "dd",
    "caption", "th", "td",
}

# Subárboles que nunca se extraen como bloques (su contenido es intocable)
EXCLUDE_ANCESTOR_TAGS = {"pre", "math", "svg"}
EXCLUDE_ANCESTOR_DATA_TYPES = {"programlisting", "equation"}


def has_excluded_ancestor(tag) -> bool:
    for ancestor in tag.parents:
        if getattr(ancestor, "name", None) in EXCLUDE_ANCESTOR_TAGS:
            return True
        try:
            dt = ancestor.get("data-type")
        except AttributeError:
            continue
        if dt in EXCLUDE_ANCESTOR_DATA_TYPES:
            return True
    return False


def is_leaf_block(tag) -> bool:
    if tag.name not in BLOCK_TAGS:
        return False
    for desc in tag.descendants:
        if getattr(desc, "name", None) in BLOCK_TAGS:
            return False
    return True


def extract_translatable_blocks(soup: BeautifulSoup) -> list[tuple[Any, str]]:
    blocks = []
    for tag in soup.find_all(list(BLOCK_TAGS)):
        if not is_leaf_block(tag):
            continue
        if has_excluded_ancestor(tag):
            continue
        inner = tag.decode_contents()
        if inner and re.search(r"\w", inner):
            blocks.append((tag, inner.strip()))
    return blocks


# ─── Tokenización opaca de contenido técnico ────────────────────────────────

OPAQUE_TAGS = {"code", "math", "svg"}
OPAQUE_TOKEN_RE = re.compile(r"⟦OPAQUE_(\d+)⟧")


def tokenize_opaque(html_str: str) -> tuple[str, dict[str, str]]:
    """
    Reemplaza <code>, <math>, <svg> por tokens ⟦OPAQUE_N⟧.
    Devuelve (html_tokenizado, dict_de_restauración).
    """
    soup = BeautifulSoup(html_str, "html.parser")
    tokens: dict[str, str] = {}
    counter = 0
    for tag in soup.find_all(list(OPAQUE_TAGS)):
        counter += 1
        token = f"⟦OPAQUE_{counter}⟧"
        tokens[token] = str(tag)
        tag.replace_with(NavigableString(token))
    return str(soup), tokens


def restore_opaque(html_str: str, tokens: dict[str, str]) -> str:
    """Reinserta los tags opacos originales en las posiciones de los tokens."""
    result = html_str
    for token, original in tokens.items():
        result = result.replace(token, original)
    return result


# ─── Reinserción y validación ───────────────────────────────────────────────

def reinsert_html(tag, translated_html: str) -> None:
    new_soup = BeautifulSoup(translated_html, "html.parser")
    tag.clear()
    for child in list(new_soup.children):
        tag.append(child)


def tag_signature(html: str) -> tuple:
    """Huella estructural: tags + hrefs + ids + conteo de <code>/<math>."""
    s = BeautifulSoup(html, "html.parser")
    tags = tuple(sorted(t.name for t in s.find_all()))
    hrefs = tuple(sorted(a.get("href", "") for a in s.find_all("a")))
    ids = tuple(sorted(t.get("id", "") for t in s.find_all() if t.get("id")))
    # Para contenido técnico: verificar que el texto dentro de <code> no haya cambiado
    code_texts = tuple(sorted(c.get_text() for c in s.find_all("code")))
    math_count = len(s.find_all("math"))
    svg_count = len(s.find_all("svg"))
    return (tags, hrefs, ids, code_texts, math_count, svg_count)


def validate_translation(original: str, translated: str) -> bool:
    try:
        return tag_signature(original) == tag_signature(translated)
    except Exception:
        return False


def validate_opaque_preservation(translated: str, tokens: dict[str, str]) -> bool:
    found = set(OPAQUE_TOKEN_RE.findall(translated))
    expected = {t[len("⟦OPAQUE_"):-1] for t in tokens.keys()}
    return found == expected


# ─── Cliente Claude (Agent SDK) ─────────────────────────────────────────────

@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60))
async def call_claude(user_msg: str, system_prompt: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=MODEL,
        allowed_tools=[],
        max_turns=1,
    )
    result_text = ""
    async for msg in query(prompt=user_msg, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    result_text += block.text
    if not result_text.strip():
        raise ValueError("Respuesta vacía del Agent SDK")
    return result_text


def build_user_message(
    batch_htmls: list[str],
    context_tail: str,
    glossary: dict[str, str],
) -> str:
    glossary_lines = "\n".join(f"  {en}  →  {es}" for en, es in glossary.items())
    ctx_block = (
        f"\nContexto previo ya traducido (SOLO como referencia de tono, NO retraducir):\n{context_tail}\n"
        if context_tail else ""
    )
    input_blocks = [f"<<<BLOCK {i}>>>\n{h}" for i, h in enumerate(batch_htmls)]
    input_payload = "\n".join(input_blocks) + "\n<<<END>>>"

    return f"""Glosario obligatorio (respetá estas traducciones de forma consistente):
{glossary_lines}
{ctx_block}
Traducí los siguientes {len(batch_htmls)} bloques HTML al español neutro LATAM.
Preservá tags inline, preservá tokens ⟦OPAQUE_N⟧ exactamente. Respondé con formato <<<BLOCK N>>> / <<<END>>>.

Bloques a traducir:
{input_payload}"""


_BLOCK_RE = re.compile(
    r"<<<BLOCK\s+(\d+)>>>\s*\n?(.*?)(?=<<<BLOCK\s+\d+>>>|<<<END>>>)",
    re.DOTALL,
)


def parse_delimited_response(text: str, expected_count: int) -> list[str]:
    matches = _BLOCK_RE.findall(text)
    if not matches:
        raise ValueError(
            f"No se encontraron bloques <<<BLOCK N>>>. Primeros 200 chars: {text[:200]!r}"
        )
    result: list[str | None] = [None] * expected_count
    for idx_str, content in matches:
        idx = int(idx_str)
        if 0 <= idx < expected_count:
            result[idx] = content.strip()
    missing = [i for i, v in enumerate(result) if v is None]
    if missing:
        raise ValueError(f"Faltan bloques: {missing}")
    return [r for r in result if r is not None]


async def translate_batch(
    batch_htmls: list[str],
    context_tail: str,
    sem: asyncio.Semaphore,
    profile: dict,
) -> list[str]:
    """Tokeniza → traduce → restaura. Devuelve traducciones con tags opacos reinsertados."""
    # 1. Tokenizar contenido opaco
    tokenized = []
    token_maps = []
    for h in batch_htmls:
        t_html, tokens = tokenize_opaque(h)
        tokenized.append(t_html)
        token_maps.append(tokens)

    # 2. Llamar al modelo con la versión tokenizada
    async with sem:
        user_msg = build_user_message(tokenized, context_tail, profile["glossary"])
        raw = await call_claude(user_msg, profile["system_prompt"])
        tokenized_translations = parse_delimited_response(raw, len(batch_htmls))

    # 3. Restaurar tags opacos (antes de devolver, para validación posterior)
    restored = []
    for trans, tokens in zip(tokenized_translations, token_maps):
        # Si faltan tokens, devolver el texto tal cual — el validator exterior detectará el problema
        restored.append(restore_opaque(trans, tokens))
    return restored


# ─── Progreso / checkpoint ──────────────────────────────────────────────────

def load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {}


def save_progress(progress: dict) -> None:
    PROGRESS_FILE.write_text(
        json.dumps(progress, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ─── Procesamiento de un archivo XHTML ──────────────────────────────────────

async def translate_xhtml_file(
    path: Path,
    sem: asyncio.Semaphore,
    progress: dict,
    profile: dict,
) -> None:
    key = str(path.relative_to(WORK_DIR))
    if progress.get(key) == "done":
        return
    if should_skip_file(path.name, profile["skip_patterns"]):
        progress[key] = "skipped"
        save_progress(progress)
        return

    content = path.read_text(encoding="utf-8")
    # Parser tolerante para libros que usan .html no estricto
    try:
        soup = BeautifulSoup(content, "lxml-xml")
        # Si lxml-xml devuelve vacío, fallback
        if not soup.find("body") and not soup.find("html"):
            raise ValueError("parser vacío")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    blocks = extract_translatable_blocks(soup)

    if not blocks:
        progress[key] = "done"
        save_progress(progress)
        return

    context_tail = ""
    any_batch_failed = False
    for i in range(0, len(blocks), BATCH_SIZE):
        batch = blocks[i:i + BATCH_SIZE]
        htmls = [b[1] for b in batch]

        try:
            translations = await translate_batch(htmls, context_tail, sem, profile)
        except Exception as e:
            print(f"  [!] Error batch {i}-{i+len(batch)} en {path.name}: {e}")
            any_batch_failed = True
            continue

        for (tag, orig_html), trans in zip(batch, translations):
            if validate_translation(orig_html, trans):
                reinsert_html(tag, trans)
            else:
                # Reintento individual con instrucción correctiva + tokenización
                try:
                    t_html, tokens = tokenize_opaque(orig_html)
                    retry_msg = (
                        "La traducción anterior alteró la estructura de tags o de tokens opacos. "
                        "Retraducí este bloque preservando EXACTAMENTE todos los tags HTML, "
                        "todos los atributos, y todos los tokens ⟦OPAQUE_N⟧ en sus posiciones. "
                        "Respondé con el formato delimitado:\n\n"
                        "<<<BLOCK 0>>>\n"
                        f"{t_html}\n"
                        "<<<END>>>"
                    )
                    raw = await call_claude(retry_msg, profile["system_prompt"])
                    retry_tokenized = parse_delimited_response(raw, 1)[0]
                    retry_trans = restore_opaque(retry_tokenized, tokens)
                    if validate_translation(orig_html, retry_trans):
                        reinsert_html(tag, retry_trans)
                    else:
                        print(f"  [!] Bloque inválido en {path.name}, se mantiene original")
                        any_batch_failed = True
                except Exception as e:
                    print(f"  [!] Retry falló en {path.name}: {e}")
                    any_batch_failed = True

        tail_items = translations[-3:]
        context_tail = "\n".join(tail_items)[-1500:]

    path.write_text(str(soup), encoding="utf-8")
    progress[key] = "partial" if any_batch_failed else "done"
    save_progress(progress)


# ─── NCX y OPF ──────────────────────────────────────────────────────────────

async def translate_ncx(path: Path, sem: asyncio.Semaphore, profile: dict) -> None:
    content = path.read_text(encoding="utf-8")
    soup = BeautifulSoup(content, "lxml-xml")

    text_nodes = []
    originals = []
    for nav_label in soup.find_all("navLabel"):
        t = nav_label.find("text")
        if t and t.string and t.string.strip():
            text_nodes.append(t)
            originals.append(t.string.strip())

    if not originals:
        return

    wrapped = [f"<span>{s}</span>" for s in originals]
    try:
        translations = await translate_batch(wrapped, "", sem, profile)
    except Exception as e:
        print(f"  [!] Error traduciendo NCX: {e}")
        return

    for node, trans in zip(text_nodes, translations):
        clean = BeautifulSoup(trans, "html.parser").get_text().strip()
        node.string = clean

    path.write_text(str(soup), encoding="utf-8")


def update_opf(opf_path: Path) -> None:
    """Actualiza dc:language a es-419. Mantiene dc:title en inglés."""
    content = opf_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(content, "lxml-xml")
    lang = soup.find("dc:language") or soup.find("language")
    if lang:
        lang.string = "es-419"
    opf_path.write_text(str(soup), encoding="utf-8")


# ─── Empaquetado ────────────────────────────────────────────────────────────

def repack_epub(work_dir: Path, output: Path) -> None:
    if output.exists():
        output.unlink()
    with zipfile.ZipFile(output, "w") as zf:
        # mimetype primero, SIN compresión (requisito del formato epub)
        mimetype_path = work_dir / "mimetype"
        if mimetype_path.exists():
            zf.write(mimetype_path, "mimetype", compress_type=zipfile.ZIP_STORED)
        for root, _, files in os.walk(work_dir):
            for f in files:
                if f == "mimetype":
                    continue
                full = Path(root) / f
                rel = full.relative_to(work_dir)
                arcname = str(rel).replace(os.sep, "/")
                zf.write(full, arcname, compress_type=zipfile.ZIP_DEFLATED)


def extract_epub(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(src) as zf:
        zf.extractall(dest)


# ─── Main ───────────────────────────────────────────────────────────────────

async def main() -> None:
    source_epub = find_source_epub()
    output_epub = PROJECT_DIR / (source_epub.stem + "_es.epub")

    # Detectar o forzar perfil
    profile_name = FORCE_PROFILE or detect_book_profile(source_epub)
    profile = PROFILES[profile_name]

    print(f"Fuente   : {source_epub.name}")
    print(f"Perfil   : {profile_name} ({'auto-detectado' if not FORCE_PROFILE else 'forzado'})")
    print(f"Modelo   : {MODEL} (vía Agent SDK, auth Max)")
    print(f"Destino  : {output_epub.name}")
    print(f"Glosario : {len(profile['glossary'])} términos")

    # Extraer si hace falta
    if not PROGRESS_FILE.exists() or not WORK_DIR.exists():
        print("\nDescomprimiendo epub...")
        extract_epub(source_epub, WORK_DIR)
        save_progress({})
    else:
        print("\nReanudando desde progress.json existente...")

    # Localizar XHTMLs vía OPF manifest
    opf_path = find_opf_path(WORK_DIR)
    xhtml_files = find_xhtml_files(opf_path)
    print(f"OPF      : {opf_path.relative_to(WORK_DIR)}")
    print(f"XHTMLs   : {len(xhtml_files)} archivos a procesar")

    progress = load_progress()
    sem = asyncio.Semaphore(CONCURRENCY)

    # 1) Traducir XHTMLs
    print(f"\nTraduciendo XHTMLs (concurrency={CONCURRENCY})...")
    tasks = [translate_xhtml_file(p, sem, progress, profile) for p in xhtml_files]
    await atqdm.gather(*tasks, desc="XHTML")

    # 2) toc.ncx (si existe)
    ncx_candidates = list(WORK_DIR.rglob("*.ncx"))
    for ncx_path in ncx_candidates:
        ncx_key = f"ncx:{ncx_path.relative_to(WORK_DIR)}"
        if progress.get(ncx_key) == "done":
            continue
        print(f"\nTraduciendo {ncx_path.name} (índice navegable)...")
        await translate_ncx(ncx_path, sem, profile)
        progress[ncx_key] = "done"
        save_progress(progress)

    # 3) content.opf
    print(f"\nActualizando {opf_path.name} (dc:language → es-419)...")
    update_opf(opf_path)

    # 4) Reempaquetar
    print(f"\nEmpaquetando {output_epub.name}...")
    repack_epub(WORK_DIR, output_epub)

    print(f"\n✓ Listo: {output_epub.name}")
    print("  Verificá el resultado y después podés borrar ./work y ./progress.json.")


if __name__ == "__main__":
    asyncio.run(main())
