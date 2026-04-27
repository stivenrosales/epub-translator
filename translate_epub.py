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
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString
from lxml import etree
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm.asyncio import tqdm as atqdm
from tqdm import tqdm

# ─── Config global ──────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
WORK_DIR = PROJECT_DIR / "work"
PROGRESS_FILE = PROJECT_DIR / "progress.json"

MODEL = "claude-sonnet-4-6"   # Último Sonnet. Si da problemas: "claude-sonnet-4-5"
BATCH_SIZE = 25
CONCURRENCY = 4

# Forzar perfil manualmente: "generic", "ai_engineering", "grokking_algorithms", "superagency", "practical_sql", "bismarck", "slow_looking", o None (auto-detect)
FORCE_PROFILE: str | None = "power_of_language"

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

# Perfil "superagency" — Reid Hoffman & Greg Beato (Authors Equity, 2025)
# No-ficción sobre IA y sociedad: sin código, sin math, con terminología AI/tech
GLOSSARY_SUPERAGENCY: dict[str, str] = {
    # Términos clave del libro que SE MANTIENEN EN INGLÉS
    "superagency": "superagency",
    "AI": "IA",
    "artificial intelligence": "inteligencia artificial",
    "machine learning": "machine learning",
    "deep learning": "deep learning",
    "large language model": "large language model",
    "large language models": "large language models",
    "LLM": "LLM",
    "LLMs": "LLMs",
    "GPT": "GPT",
    "ChatGPT": "ChatGPT",
    "OpenAI": "OpenAI",
    "AGI": "AGI",
    "artificial general intelligence": "inteligencia artificial general",
    "prompt": "prompt",
    "prompts": "prompts",
    "chatbot": "chatbot",
    "chatbots": "chatbots",
    "transformer": "transformer",
    "neural network": "red neuronal",
    "neural networks": "redes neuronales",
    "foundation model": "foundation model",
    "foundation models": "foundation models",
    "fine-tuning": "fine-tuning",
    "alignment": "alignment",
    "guardrails": "guardrails",
    "hallucination": "alucinación",
    "hallucinations": "alucinaciones",
    "bias": "sesgo",
    "biases": "sesgos",
    "dataset": "dataset",
    "open source": "open source",
    # Términos de tech/negocios
    "startup": "startup",
    "startups": "startups",
    "Silicon Valley": "Silicon Valley",
    "Big Tech": "Big Tech",
    "venture capital": "capital de riesgo",
    "scaling": "escalado",
    "platform": "plataforma",
    "platforms": "plataformas",
    "network effects": "efectos de red",
    "disruption": "disrupción",
    "innovation": "innovación",
    "deployment": "despliegue",
    "iterative deployment": "despliegue iterativo",
    # Conceptos centrales del libro
    "agency": "agencia",
    "human agency": "agencia humana",
    "superagent": "superagente",
    "co-pilot": "copiloto",
    "copilot": "copiloto",
    "existential risk": "riesgo existencial",
    "safety": "seguridad",
    "regulation": "regulación",
    "governance": "gobernanza",
    "accountability": "rendición de cuentas",
    "transparency": "transparencia",
    "automation": "automatización",
    "augmentation": "aumento",
    "workforce": "fuerza laboral",
    # Personas y organizaciones — NO traducir
    "Reid Hoffman": "Reid Hoffman",
    "LinkedIn": "LinkedIn",
    "Inflection AI": "Inflection AI",
    "Greylock": "Greylock",
    "Meta": "Meta",
    "Google": "Google",
    "Alphabet": "Alphabet",
    "Microsoft": "Microsoft",
    "Anthropic": "Anthropic",
    "Claude": "Claude",
}

SYSTEM_PROMPT_SUPERAGENCY = """Eres un traductor literario profesional especializado en no-ficción de tecnología, negocios y política pública. Traduces del inglés al español latinoamericano neutro para un lector peruano interesado en inteligencia artificial y su impacto social.

Este libro es "Superagency: What Could Possibly Go Right with Our AI Future" de Reid Hoffman y Greg Beato (2025). Es un ensayo argumentativo optimista sobre el futuro de la IA — reflexivo, persuasivo, con datos y anécdotas históricas. NO es técnico (sin código, sin math), pero usa terminología de AI/tech con precisión.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes.
   - Preserva su posición EXACTA dentro del texto.

2. PRESERVA TODOS los tags HTML exactamente: <em>, <strong>, <a href>, <span>, <sup>, <br/>, etc. Mismos atributos (href, id, class, data-*, epub:type), mismas cantidades, mismo orden.

3. NOTAS AL PIE: los <sup><a href="..."> son links a notas al pie. Preserva EXACTAMENTE la estructura <sup><a href="..."><span class="blue">N</span></a></sup>. No los traduzcas, no los muevas.

4. TERMINOLOGÍA AI/TECH — respeta SIEMPRE el glosario proporcionado:
   - Términos que se mantienen en inglés: machine learning, deep learning, LLM, GPT, ChatGPT, foundation model, fine-tuning, alignment, guardrails, prompt, dataset, open source, startup, Big Tech, Silicon Valley.
   - Términos que se traducen: AI → IA, hallucination → alucinación, bias → sesgo, agency → agencia, safety → seguridad, deployment → despliegue.
   - Nombres propios NUNCA se traducen: Reid Hoffman, LinkedIn, OpenAI, Meta, Google, Anthropic, Claude, etc.

5. ESPAÑOL LATAM NEUTRO, lector peruano:
   - "tú" como segunda persona (no "vos", no "vosotros", no "usted" salvo registro formal original).
   - Sin modismos regionales ("chido", "guay", "chévere", "bacán" — NINGUNO).
   - Sin conjugaciones peninsulares.
   - Registro: ensayístico, claro, profesional pero accesible.

6. TONO de los autores (Hoffman & Beato) — CRÍTICO:
   - Argumentativo, persuasivo, optimista pero fundamentado.
   - Prosa elegante: frases variadas, ritmo fluido, párrafos sustanciales.
   - Usan analogías históricas, datos, citas. Preserva el tono reflexivo y la cadencia.
   - NO simplifiques oraciones complejas. NO rompas párrafos. Preserva la sofisticación del original.
   - Preserva las referencias culturales (Taylor Swift, Ticketmaster, FTX, etc.) sin localizar.

7. TÍTULOS DE OBRAS: libros, películas, artículos entre <span class="ital"> se mantienen en su idioma original. NO los traduzcas.

8. URLs y links: preserva EXACTAMENTE como están, sin modificar.

9. NO expliques, NO resumas, NO agregues notas del traductor.

10. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_SUPERAGENCY = [
    r"Superagency_Cover\.xhtml$",
    r"Superagency_Titlepage\.xhtml$",
    r"Superagency_Copyright\.xhtml$",
    r"Superagency_Alsoby\.xhtml$",
    r"Superagency_Index\.xhtml$",       # Índice alfabético — no útil traducido
]

# Perfil "practical_sql" — Anthony DeBarros / No Starch Press (Practical SQL, 2nd Ed., 2022)
# Libro técnico de SQL/PostgreSQL con enfoque data journalism, datasets reales (Censo, taxis NYC, USGS)
GLOSSARY_PRACTICAL_SQL: dict[str, str] = {
    # ─── SQL keywords y comandos: SIEMPRE en inglés (mayúsculas) ───
    "SELECT": "SELECT",
    "FROM": "FROM",
    "WHERE": "WHERE",
    "GROUP BY": "GROUP BY",
    "ORDER BY": "ORDER BY",
    "HAVING": "HAVING",
    "LIMIT": "LIMIT",
    "OFFSET": "OFFSET",
    "DISTINCT": "DISTINCT",
    "JOIN": "JOIN",
    "INNER JOIN": "INNER JOIN",
    "LEFT JOIN": "LEFT JOIN",
    "RIGHT JOIN": "RIGHT JOIN",
    "FULL JOIN": "FULL JOIN",
    "FULL OUTER JOIN": "FULL OUTER JOIN",
    "CROSS JOIN": "CROSS JOIN",
    "LATERAL": "LATERAL",
    "USING": "USING",
    "ON": "ON",
    "UNION": "UNION",
    "UNION ALL": "UNION ALL",
    "INTERSECT": "INTERSECT",
    "EXCEPT": "EXCEPT",
    "INSERT": "INSERT",
    "INSERT INTO": "INSERT INTO",
    "UPDATE": "UPDATE",
    "DELETE": "DELETE",
    "TRUNCATE": "TRUNCATE",
    "MERGE": "MERGE",
    "RETURNING": "RETURNING",
    "CREATE": "CREATE",
    "CREATE TABLE": "CREATE TABLE",
    "CREATE VIEW": "CREATE VIEW",
    "CREATE INDEX": "CREATE INDEX",
    "CREATE DATABASE": "CREATE DATABASE",
    "CREATE SCHEMA": "CREATE SCHEMA",
    "ALTER": "ALTER",
    "ALTER TABLE": "ALTER TABLE",
    "DROP": "DROP",
    "DROP TABLE": "DROP TABLE",
    "COPY": "COPY",
    "WITH": "WITH",
    "AS": "AS",
    "CASE": "CASE",
    "WHEN": "WHEN",
    "THEN": "THEN",
    "ELSE": "ELSE",
    "END": "END",
    "IS NULL": "IS NULL",
    "IS NOT NULL": "IS NOT NULL",
    "NULL": "NULL",
    "NOT NULL": "NOT NULL",
    "PRIMARY KEY": "PRIMARY KEY",
    "FOREIGN KEY": "FOREIGN KEY",
    "REFERENCES": "REFERENCES",
    "CHECK": "CHECK",
    "UNIQUE": "UNIQUE",
    "DEFAULT": "DEFAULT",
    "IDENTITY": "IDENTITY",
    "SERIAL": "SERIAL",
    "AUTO_INCREMENT": "AUTO_INCREMENT",
    "EXPLAIN": "EXPLAIN",
    "ANALYZE": "ANALYZE",
    "VACUUM": "VACUUM",
    "BEGIN": "BEGIN",
    "COMMIT": "COMMIT",
    "ROLLBACK": "ROLLBACK",
    "TRANSACTION": "TRANSACTION",
    "GRANT": "GRANT",
    "REVOKE": "REVOKE",
    # ─── Productos, herramientas y formatos: en inglés ───
    "PostgreSQL": "PostgreSQL",
    "Postgres": "Postgres",
    "pgAdmin": "pgAdmin",
    "psql": "psql",
    "PostGIS": "PostGIS",
    "MySQL": "MySQL",
    "Oracle": "Oracle",
    "SQLite": "SQLite",
    "Microsoft SQL Server": "Microsoft SQL Server",
    "SQL Server": "SQL Server",
    "T-SQL": "T-SQL",
    "BigQuery": "BigQuery",
    "Snowflake": "Snowflake",
    "Redshift": "Redshift",
    "DuckDB": "DuckDB",
    "ANSI SQL": "ANSI SQL",
    "SQL standard": "estándar SQL",
    "SQL": "SQL",
    "JSON": "JSON",
    "JSONB": "JSONB",
    "CSV": "CSV",
    "XML": "XML",
    "ETL": "ETL",
    "CTE": "CTE",
    "ACID": "ACID",
    "RDBMS": "RDBMS",
    "ORM": "ORM",
    "GIS": "GIS",
    "GitHub": "GitHub",
    "Python": "Python",
    "JavaScript": "JavaScript",
    # ─── Conceptos relacionales: SE TRADUCEN consistentemente ───
    "database": "base de datos",
    "databases": "bases de datos",
    "relational database": "base de datos relacional",
    "relational databases": "bases de datos relacionales",
    "table": "tabla",
    "tables": "tablas",
    "row": "fila",
    "rows": "filas",
    "column": "columna",
    "columns": "columnas",
    "record": "registro",
    "records": "registros",
    "field": "campo",
    "fields": "campos",
    "view": "vista",
    "views": "vistas",
    "materialized view": "vista materializada",
    "materialized views": "vistas materializadas",
    "index": "índice",
    "indexes": "índices",
    "indices": "índices",
    "schema": "esquema",
    "schemas": "esquemas",
    "primary key": "clave primaria",
    "primary keys": "claves primarias",
    "foreign key": "clave foránea",
    "foreign keys": "claves foráneas",
    "natural key": "clave natural",
    "surrogate key": "clave subrogada",
    "composite key": "clave compuesta",
    "constraint": "restricción",
    "constraints": "restricciones",
    "data type": "tipo de dato",
    "data types": "tipos de datos",
    "data": "datos",
    "dataset": "dataset",
    "datasets": "datasets",
    "query": "consulta",
    "queries": "consultas",
    "subquery": "subconsulta",
    "subqueries": "subconsultas",
    "statement": "sentencia",
    "statements": "sentencias",
    "expression": "expresión",
    "expressions": "expresiones",
    "clause": "cláusula",
    "clauses": "cláusulas",
    "operator": "operador",
    "operators": "operadores",
    "function": "función",
    "functions": "funciones",
    "aggregate function": "función de agregación",
    "aggregate functions": "funciones de agregación",
    "window function": "función de ventana",
    "window functions": "funciones de ventana",
    "stored procedure": "procedimiento almacenado",
    "stored procedures": "procedimientos almacenados",
    "trigger": "trigger",
    "triggers": "triggers",
    "transaction": "transacción",
    "transactions": "transacciones",
    "join": "join",
    "joins": "joins",
    "inner join": "inner join",
    "left join": "left join",
    "outer join": "outer join",
    "self join": "self join",
    "cross join": "cross join",
    "set operation": "operación de conjuntos",
    "set operations": "operaciones de conjuntos",
    "result set": "conjunto de resultados",
    "result sets": "conjuntos de resultados",
    "execution plan": "plan de ejecución",
    "query plan": "plan de consulta",
    "query optimizer": "optimizador de consultas",
    "query planner": "planificador de consultas",
    "normalization": "normalización",
    "denormalization": "desnormalización",
    "cardinality": "cardinalidad",
    "relation": "relación",
    "relations": "relaciones",
    # ─── Tipos de datos: nombre técnico en inglés, descripción en español ───
    "string": "cadena",
    "strings": "cadenas",
    "integer": "entero",
    "integers": "enteros",
    "float": "float",
    "decimal": "decimal",
    "numeric": "numérico",
    "boolean": "booleano",
    "booleans": "booleanos",
    "timestamp": "timestamp",
    "timestamps": "timestamps",
    "date": "fecha",
    "dates": "fechas",
    "interval": "interval",
    "array": "arreglo",
    "arrays": "arreglos",
    # ─── Operaciones de análisis ───
    "aggregate": "agregación",
    "aggregation": "agregación",
    "grouping": "agrupamiento",
    "filter": "filtro",
    "filters": "filtros",
    "sort": "ordenar",
    "sorting": "ordenamiento",
    "import": "importar",
    "export": "exportar",
    "backup": "backup",
    "restore": "restauración",
    "dump": "dump",
    # ─── Datos espaciales (Capítulo 15) ───
    "spatial data": "datos espaciales",
    "geographic information system": "sistema de información geográfica",
    "geographic information systems": "sistemas de información geográfica",
    "geometry": "geometría",
    "geography": "geografía",
    "spatial reference system": "sistema de referencia espacial",
    "longitude": "longitud",
    "latitude": "latitud",
    "shapefile": "shapefile",
    "shapefiles": "shapefiles",
    # ─── Términos de análisis de datos / data journalism ───
    "data analysis": "análisis de datos",
    "data analyst": "analista de datos",
    "data journalism": "periodismo de datos",
    "data journalist": "periodista de datos",
    "data wrangling": "manipulación de datos",
    "data cleaning": "limpieza de datos",
    "data quality": "calidad de los datos",
    "exploratory data analysis": "análisis exploratorio de datos",
    "rolling average": "promedio móvil",
    "moving average": "media móvil",
    "median": "mediana",
    "mean": "media",
    "average": "promedio",
    "percentile": "percentil",
    "percentiles": "percentiles",
    "standard deviation": "desviación estándar",
    "variance": "varianza",
    "correlation": "correlación",
    "regression": "regresión",
    # ─── Conceptos generales que se mantienen ───
    "True": "True",
    "False": "False",
    "true": "true",
    "false": "false",
    "open source": "open source",
    "command line": "línea de comandos",
    "command-line": "línea de comandos",
}

SYSTEM_PROMPT_PRACTICAL_SQL = """Sos un traductor técnico profesional especializado en bases de datos SQL, PostgreSQL y análisis de datos. Traducís del inglés al español latinoamericano neutro para un lector peruano que está aprendiendo SQL desde cero o con conocimientos básicos.

Este libro es "Practical SQL, 2nd Edition" de Anthony DeBarros (No Starch Press, 2022). El autor es periodista de datos y analista. El enfoque del libro es PRÁCTICO: enseña SQL usando datasets reales del mundo (Censo de EEUU, taxis de Nueva York, terremotos del USGS) con la filosofía de "encontrar la historia en los datos". El motor usado es PostgreSQL con pgAdmin, y también cubre PostGIS para datos geoespaciales.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders de contenido técnico protegido: código SQL inline, nombres de tablas/columnas, comandos, rutas de archivos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes. NO agregues espacios dentro.
   - Preservá su posición EXACTA dentro del texto. El espacio/puntuación alrededor puede ajustarse al español.

2. PRESERVÁ TODOS los tags HTML exactamente: <em>, <strong>, <b>, <i>, <a href>, <span>, <code>, <var>, <sup>, <br/>, etc. No cambies atributos (href, id, class, epub:type), no agregues tags, no quites tags. Mismos atributos, mismas cantidades, mismo orden.

3. NUNCA TRADUZCAS contenido dentro de <code>: son identificadores técnicos (keywords SQL, nombres de funciones, nombres de tablas, columnas, comandos del shell, rutas de archivos, parámetros). Preservalos EXACTAMENTE carácter por carácter. (Nota: la mayoría vendrán como tokens opacos, pero si ves algún <code> directo, aplicá la misma regla.) Lo mismo aplica a <var>, que marca placeholders dentro del código (ej: <var>table_name</var>).

4. KEYWORDS SQL — JAMÁS se traducen. Mantenelos EXACTAMENTE en mayúsculas como están en el original:
   SELECT, FROM, WHERE, GROUP BY, ORDER BY, HAVING, LIMIT, JOIN, INNER JOIN, LEFT JOIN, RIGHT JOIN, FULL OUTER JOIN, CROSS JOIN, LATERAL, USING, ON, UNION, UNION ALL, INTERSECT, EXCEPT, INSERT, UPDATE, DELETE, TRUNCATE, RETURNING, CREATE TABLE, ALTER, DROP, COPY, WITH, AS, CASE, WHEN, THEN, ELSE, END, NULL, NOT NULL, PRIMARY KEY, FOREIGN KEY, REFERENCES, CHECK, UNIQUE, DEFAULT, IDENTITY, EXPLAIN, ANALYZE, VACUUM, BEGIN, COMMIT, ROLLBACK, TRANSACTION, etc.
   Esto vale TANTO dentro de <code> como cuando aparecen mencionados en prosa.

5. PRODUCTOS Y HERRAMIENTAS — se mantienen en inglés:
   PostgreSQL, Postgres, pgAdmin, psql, PostGIS, MySQL, Oracle, SQLite, Microsoft SQL Server, T-SQL, BigQuery, Snowflake, JSON, JSONB, CSV, XML, GitHub, Python.

6. TERMINOLOGÍA RELACIONAL — SE TRADUCE consistentemente (respetar el glosario):
   database → base de datos, table → tabla, row → fila, column → columna, record → registro, field → campo, view → vista, materialized view → vista materializada, index → índice, schema → esquema, primary key → clave primaria, foreign key → clave foránea, constraint → restricción, data type → tipo de dato, query → consulta, subquery → subconsulta, statement → sentencia, clause → cláusula, expression → expresión, function → función, aggregate function → función de agregación, window function → función de ventana, transaction → transacción, normalization → normalización, cardinality → cardinalidad.
   Consistencia TOTAL a lo largo del libro. "query" SIEMPRE es "consulta", nunca "petición" ni "interrogación".

7. CASO ESPECIAL: "join" / "joins" como sustantivo en prosa → "join" / "joins" (en minúsculas, en inglés). Es término de uso universal en la industria. PERO los keywords SQL en mayúsculas (INNER JOIN, LEFT JOIN, etc.) siempre quedan tal cual.

8. ESPAÑOL LATAM NEUTRO, lector peruano principiante en SQL:
   - "tú" como segunda persona (no "vos", no "vosotros", no "usted").
   - Sin modismos regionales ("chévere", "bacán", "guay", "mola", "chido" — NINGUNO).
   - Sin conjugaciones peninsulares (nada de "vosotros tenéis", "podéis", "haced", etc.).
   - Registro: técnico pero accesible, como un mentor pragmático que te enseña a usar la herramienta.

9. TONO del autor (DeBarros) — CRÍTICO:
   - Conversacional, directo, didáctico. Escribe como periodista: claro, ordenado, sin adornos.
   - Pragmático, orientado a "vamos a hacer esto en tu computadora". No académico, no abstracto.
   - Respetá las indicaciones paso a paso ("Click here", "Enter this", "You should see...").
   - Si dice "Let's start by..." traducí "Empecemos por...", no "Procedamos a iniciar mediante...".
   - Frases cortas, párrafos cortos. No agregues palabras de relleno.

10. NÚMEROS DE CAPÍTULO/FIGURA: "Chapter 5" → "Capítulo 5", "Figure 1-1" → "Figura 1-1", "Table 4-2" → "Tabla 4-2". Los IDs internos (id="figure1-1", href="#figure1-1") NO se tocan.

11. NO expliques, NO resumas, NO agregues notas del traductor, NO expandas siglas que el autor dejó sin expandir.

12. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_PRACTICAL_SQL = [
    r"^cover\.xhtml$",
    r"^f01\.xhtml$",      # Title page (solo subtítulo)
    r"^f02\.xhtml$",      # Copyright
    r"^b02\.xhtml$",      # Index alfabético en inglés — no útil traducido
]

# Perfil "bismarck" — Jonathan Steinberg / Oxford University Press (Bismarck: A Life, 2011)
# Biografía histórica académica. Prosa erudita, citas extensas en alemán, terminología
# político-militar del siglo XIX prusiano/alemán.
GLOSSARY_BISMARCK: dict[str, str] = {
    # ─── Términos políticos/históricos alemanes: SE MANTIENEN EN ALEMÁN (cursiva si el original lo está) ───
    "Realpolitik": "Realpolitik",
    "Kulturkampf": "Kulturkampf",
    "Ausgleich": "Ausgleich",
    "Vormärz": "Vormärz",
    "Zollverein": "Zollverein",
    "Reich": "Reich",
    "Kaiserreich": "Kaiserreich",
    "Reichstag": "Reichstag",
    "Bundesrat": "Bundesrat",
    "Bundestag": "Bundestag",
    "Landtag": "Landtag",
    "Herrenhaus": "Herrenhaus",
    "Abgeordnetenhaus": "Abgeordnetenhaus",
    "Junker": "Junker",
    "Junkers": "Junkers",
    "Bürgertum": "Bürgertum",
    "Mittelstand": "Mittelstand",
    "Geheimrat": "Geheimrat",
    "Staatsministerium": "Staatsministerium",
    "Auswärtiges Amt": "Auswärtiges Amt",
    "Wilhelmstrasse": "Wilhelmstrasse",
    "Wilhelmstraße": "Wilhelmstraße",
    "Schloss": "Schloss",
    "Sonderweg": "Sonderweg",
    "Weltpolitik": "Weltpolitik",
    "Machtpolitik": "Machtpolitik",
    "Gründerzeit": "Gründerzeit",
    "Maiengesetze": "Maiengesetze",
    "Sozialistengesetz": "Sozialistengesetz",
    "Lebensraum": "Lebensraum",
    "Heer": "Heer",
    "Großdeutsch": "Großdeutsch",
    "Kleindeutsch": "Kleindeutsch",
    # ─── Cargos y títulos: traducidos al español ───
    "Chancellor": "canciller",
    "chancellor": "canciller",
    "Imperial Chancellor": "canciller imperial",
    "Reich Chancellor": "canciller del Reich",
    "Minister-President": "ministro presidente",
    "Minister President": "ministro presidente",
    "Prime Minister": "primer ministro",
    "Foreign Minister": "ministro de Asuntos Exteriores",
    "Foreign Secretary": "ministro de Asuntos Exteriores",
    "Minister of Foreign Affairs": "ministro de Asuntos Exteriores",
    "Minister of War": "ministro de la Guerra",
    "Minister of Finance": "ministro de Finanzas",
    "Minister of the Interior": "ministro del Interior",
    "Secretary of State": "secretario de Estado",
    "ambassador": "embajador",
    "envoy": "enviado",
    "Kaiser": "káiser",
    "Emperor": "emperador",
    "Empress": "emperatriz",
    "King": "rey",
    "Queen": "reina",
    "Crown Prince": "príncipe heredero",
    "Crown Princess": "princesa heredera",
    "Prince": "príncipe",
    "Princess": "princesa",
    "Archduke": "archiduque",
    "Archduchess": "archiduquesa",
    "Grand Duke": "gran duque",
    "Grand Duchess": "gran duquesa",
    "Duke": "duque",
    "Duchess": "duquesa",
    "Count": "conde",
    "Countess": "condesa",
    "Baron": "barón",
    "Baroness": "baronesa",
    "Tsar": "zar",
    "Czar": "zar",
    "Tsarina": "zarina",
    "Pope": "papa",
    "Cardinal": "cardenal",
    "Field Marshal": "mariscal de campo",
    "General": "general",
    "Lieutenant General": "teniente general",
    "Major General": "mayor general",
    "Colonel": "coronel",
    "Captain": "capitán",
    # ─── Estados, regiones, ciudades alemanas y europeas ───
    "Prussia": "Prusia",
    "Prussian": "prusiano",
    "Prussians": "prusianos",
    "Bavaria": "Baviera",
    "Bavarian": "bávaro",
    "Saxony": "Sajonia",
    "Saxon": "sajón",
    "Württemberg": "Wurtemberg",
    "Hesse": "Hesse",
    "Hesse-Darmstadt": "Hesse-Darmstadt",
    "Hesse-Kassel": "Hesse-Kassel",
    "Baden": "Baden",
    "Hanover": "Hannover",
    "Holstein": "Holstein",
    "Schleswig": "Schleswig",
    "Schleswig-Holstein": "Schleswig-Holstein",
    "Lauenburg": "Lauenburgo",
    "Pomerania": "Pomerania",
    "Pomeranian": "pomerano",
    "Rhineland": "Renania",
    "Westphalia": "Westfalia",
    "Silesia": "Silesia",
    "Brandenburg": "Brandeburgo",
    "Mecklenburg": "Mecklemburgo",
    "Thuringia": "Turingia",
    "Alsace": "Alsacia",
    "Lorraine": "Lorena",
    "Alsace-Lorraine": "Alsacia-Lorena",
    "Berlin": "Berlín",
    "Vienna": "Viena",
    "Frankfurt": "Fráncfort",
    "Munich": "Múnich",
    "Cologne": "Colonia",
    "Hamburg": "Hamburgo",
    "Bremen": "Bremen",
    "Lübeck": "Lübeck",
    "Königsberg": "Königsberg",
    "Danzig": "Dánzig",
    "Strasbourg": "Estrasburgo",
    "Trieste": "Trieste",
    "Sedan": "Sedán",
    "Versailles": "Versalles",
    "Bad Ems": "Bad Ems",
    "Ems": "Ems",
    "Carlsbad": "Carlsbad",
    "Bohemia": "Bohemia",
    "Moravia": "Moravia",
    "Galicia": "Galitzia",
    "Silesian": "silesio",
    "Habsburg": "Habsburgo",
    "Habsburgs": "Habsburgo",
    "Hohenzollern": "Hohenzollern",
    "Romanov": "Romanov",
    "Wittelsbach": "Wittelsbach",
    "House of Hohenzollern": "Casa de Hohenzollern",
    "House of Habsburg": "Casa de Habsburgo",
    "Austria": "Austria",
    "Austrian": "austriaco",
    "Austrians": "austriacos",
    "Austria-Hungary": "Austria-Hungría",
    "Austro-Hungarian": "austrohúngaro",
    "Hungary": "Hungría",
    "Hungarian": "húngaro",
    "Russia": "Rusia",
    "Russian": "ruso",
    "France": "Francia",
    "French": "francés",
    "Britain": "Gran Bretaña",
    "Great Britain": "Gran Bretaña",
    "British": "británico",
    "England": "Inglaterra",
    "English": "inglés",
    "Italy": "Italia",
    "Italian": "italiano",
    "Piedmont": "Piamonte",
    "Sardinia": "Cerdeña",
    "Spain": "España",
    "Spanish": "español",
    "Ottoman": "otomano",
    "Ottoman Empire": "Imperio otomano",
    "Balkans": "Balcanes",
    "Balkan": "balcánico",
    "Crimea": "Crimea",
    "Bosnia": "Bosnia",
    "Herzegovina": "Herzegovina",
    "Serbia": "Serbia",
    "Bulgaria": "Bulgaria",
    "Romania": "Rumanía",
    "Greece": "Grecia",
    "Poland": "Polonia",
    "Polish": "polaco",
    "Denmark": "Dinamarca",
    "Danish": "danés",
    "Belgium": "Bélgica",
    "Netherlands": "Países Bajos",
    "Holland": "Holanda",
    "Dutch": "neerlandés",
    "Switzerland": "Suiza",
    "Swiss": "suizo",
    # ─── Entidades políticas y tratados ───
    "Holy Roman Empire": "Sacro Imperio Romano Germánico",
    "German Confederation": "Confederación Germánica",
    "North German Confederation": "Confederación de Alemania del Norte",
    "German Empire": "Imperio alemán",
    "Second Reich": "Segundo Reich",
    "First Reich": "Primer Reich",
    "Diet": "Dieta",
    "Frankfurt Diet": "Dieta de Fráncfort",
    "Frankfurt Parliament": "Parlamento de Fráncfort",
    "Frankfurt Assembly": "Asamblea de Fráncfort",
    "Customs Union": "Unión Aduanera",
    "Three Emperors' League": "Liga de los Tres Emperadores",
    "League of Three Emperors": "Liga de los Tres Emperadores",
    "Dreikaiserbund": "Dreikaiserbund",
    "Dual Alliance": "Doble Alianza",
    "Triple Alliance": "Triple Alianza",
    "Reinsurance Treaty": "Tratado de Reaseguro",
    "Treaty of Frankfurt": "Tratado de Fráncfort",
    "Treaty of Berlin": "Tratado de Berlín",
    "Treaty of Prague": "Tratado de Praga",
    "Treaty of Versailles": "Tratado de Versalles",
    "Congress of Berlin": "Congreso de Berlín",
    "Congress of Vienna": "Congreso de Viena",
    "Concert of Europe": "Concierto Europeo",
    "Holy Alliance": "Santa Alianza",
    "Quadruple Alliance": "Cuádruple Alianza",
    "Ems Dispatch": "Despacho de Ems",
    "Ems Telegram": "Telegrama de Ems",
    "Anti-Socialist Laws": "Leyes Antisocialistas",
    "May Laws": "Leyes de Mayo",
    "Falk Laws": "Leyes Falk",
    # ─── Guerras y conflictos ───
    "Franco-Prussian War": "guerra franco-prusiana",
    "Austro-Prussian War": "guerra austro-prusiana",
    "Seven Weeks' War": "guerra de las Siete Semanas",
    "Danish War": "guerra de los Ducados",
    "Second Schleswig War": "Segunda Guerra de Schleswig",
    "Crimean War": "guerra de Crimea",
    "Napoleonic Wars": "guerras napoleónicas",
    "Thirty Years' War": "guerra de los Treinta Años",
    "Seven Years' War": "guerra de los Siete Años",
    "First World War": "Primera Guerra Mundial",
    "World War I": "Primera Guerra Mundial",
    "Wars of Liberation": "guerras de Liberación",
    "Wars of Unification": "guerras de Unificación",
    # ─── Movimientos, partidos e ideologías ───
    "liberalism": "liberalismo",
    "liberal": "liberal",
    "liberals": "liberales",
    "conservatism": "conservadurismo",
    "conservative": "conservador",
    "conservatives": "conservadores",
    "Conservative Party": "Partido Conservador",
    "Free Conservative Party": "Partido Conservador Libre",
    "National Liberal Party": "Partido Nacional Liberal",
    "National Liberals": "nacional-liberales",
    "Progressive Party": "Partido Progresista",
    "Progressives": "progresistas",
    "Centre Party": "Zentrum",
    "Center Party": "Zentrum",
    "Catholic Centre": "Zentrum",
    "Zentrum": "Zentrum",
    "Social Democrats": "socialdemócratas",
    "Social Democratic Party": "Partido Socialdemócrata",
    "SPD": "SPD",
    "socialism": "socialismo",
    "socialist": "socialista",
    "socialists": "socialistas",
    "nationalism": "nacionalismo",
    "nationalist": "nacionalista",
    "Pan-Germanism": "pangermanismo",
    "Pan-Slavism": "paneslavismo",
    "ultramontane": "ultramontano",
    "ultramontanism": "ultramontanismo",
    "Catholicism": "catolicismo",
    "Protestantism": "protestantismo",
    "Lutheranism": "luteranismo",
    "Pietism": "pietismo",
    "pietist": "pietista",
    "Jewish": "judío",
    "Jews": "judíos",
    "anti-Semitism": "antisemitismo",
    "anti-Semitic": "antisemita",
    # ─── Conceptos historiográficos y políticos ───
    "blood and iron": "sangre y hierro",
    "iron and blood": "hierro y sangre",
    "balance of power": "equilibrio de poder",
    "great power": "gran potencia",
    "great powers": "grandes potencias",
    "raison d'état": "razón de Estado",
    "reason of state": "razón de Estado",
    "statesman": "estadista",
    "statesmen": "estadistas",
    "statecraft": "arte de gobernar",
    "diplomacy": "diplomacia",
    "diplomat": "diplomático",
    "diplomatic": "diplomático",
    "alliance": "alianza",
    "alliances": "alianzas",
    "treaty": "tratado",
    "treaties": "tratados",
    "constitution": "constitución",
    "constitutional": "constitucional",
    "parliament": "parlamento",
    "parliamentary": "parlamentario",
    "parliamentarism": "parlamentarismo",
    "deputy": "diputado",
    "deputies": "diputados",
    "suffrage": "sufragio",
    "universal suffrage": "sufragio universal",
    "franchise": "franquicia electoral",
    "estate": "estamento",
    "estates": "estamentos",
    "nobility": "nobleza",
    "aristocracy": "aristocracia",
    "aristocratic": "aristocrático",
    "bourgeoisie": "burguesía",
    "bourgeois": "burgués",
    "peasantry": "campesinado",
    "peasant": "campesino",
    "serf": "siervo",
    "serfdom": "servidumbre",
    "annexation": "anexión",
    "unification": "unificación",
    "hegemony": "hegemonía",
    "supremacy": "supremacía",
    "indemnity": "indemnización",
    "reparations": "reparaciones",
    "abdication": "abdicación",
    "regency": "regencia",
    "regent": "regente",
    "court": "corte",
    "cabinet": "gabinete",
    "ministry": "ministerio",
    # ─── Personajes (NO traducir nombres propios; sí cargos/contextos cuando aparecen aparte) ───
    "Otto von Bismarck": "Otto von Bismarck",
    "Bismarck": "Bismarck",
    "Wilhelm I": "Guillermo I",
    "King Wilhelm": "el rey Guillermo",
    "Wilhelm II": "Guillermo II",
    "Friedrich III": "Federico III",
    "Friedrich Wilhelm IV": "Federico Guillermo IV",
    "Friedrich Wilhelm": "Federico Guillermo",
    "Augusta": "Augusta",
    "Johanna": "Johanna",
    "Albrecht von Roon": "Albrecht von Roon",
    "Roon": "Roon",
    "Helmuth von Moltke": "Helmuth von Moltke",
    "Moltke": "Moltke",
    "Manteuffel": "Manteuffel",
    "Holstein": "Holstein",
    "Eulenburg": "Eulenburg",
    "Caprivi": "Caprivi",
    "Hohenlohe": "Hohenlohe",
    "Bülow": "Bülow",
    "Lassalle": "Lassalle",
    "Ferdinand Lassalle": "Ferdinand Lassalle",
    "Karl Marx": "Karl Marx",
    "Engels": "Engels",
    "Bebel": "Bebel",
    "Liebknecht": "Liebknecht",
    "Windthorst": "Windthorst",
    "Lasker": "Lasker",
    "Bennigsen": "Bennigsen",
    "Bleichröder": "Bleichröder",
    "Napoleon III": "Napoleón III",
    "Napoleon": "Napoleón",
    "Bonaparte": "Bonaparte",
    "Franz Joseph": "Francisco José",
    "Metternich": "Metternich",
    "Schwarzenberg": "Schwarzenberg",
    "Andrássy": "Andrássy",
    "Beust": "Beust",
    "Disraeli": "Disraeli",
    "Gladstone": "Gladstone",
    "Salisbury": "Salisbury",
    "Palmerston": "Palmerston",
    "Queen Victoria": "la reina Victoria",
    "Victoria": "Victoria",
    "Alexander II": "Alejandro II",
    "Alexander III": "Alejandro III",
    "Nicholas I": "Nicolás I",
    "Gorchakov": "Gorchakov",
    "Cavour": "Cavour",
    "Garibaldi": "Garibaldi",
    "Victor Emmanuel": "Víctor Manuel",
    "Pope Pius IX": "el papa Pío IX",
    "Pius IX": "Pío IX",
    "Leo XIII": "León XIII",
    # ─── Vocabulario de prosa académica ───
    "century": "siglo",
    "centuries": "siglos",
    "decade": "década",
    "decades": "décadas",
    "memoir": "memoria",
    "memoirs": "memorias",
    "diary": "diario",
    "letters": "cartas",
    "correspondence": "correspondencia",
    "biographer": "biógrafo",
    "historian": "historiador",
    "historians": "historiadores",
    "scholar": "estudioso",
    "scholarship": "investigación académica",
    "archive": "archivo",
    "archives": "archivos",
    "source": "fuente",
    "sources": "fuentes",
    "primary source": "fuente primaria",
    "secondary source": "fuente secundaria",
}

SYSTEM_PROMPT_BISMARCK = """Eres un traductor literario profesional especializado en historia europea, biografía política y prosa académica. Traduces del inglés al español latinoamericano neutro para un lector peruano culto interesado en historia del siglo XIX.

Este libro es "Bismarck: A Life" de Jonathan Steinberg (Oxford University Press, 2011). Es una biografía académica rigurosa de Otto von Bismarck (1815-1898), el canciller que unificó Alemania. Steinberg es historiador (Penn, Cambridge) y escribe en prosa erudita pero accesible: cita extensamente cartas, diarios y despachos diplomáticos en alemán, inglés y francés; alterna análisis político con retrato psicológico; mezcla historiografía con narrativa.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes.
   - Preserva su posición EXACTA dentro del texto.

2. PRESERVA TODOS los tags HTML exactamente: <em>, <strong>, <i>, <b>, <a href>, <span>, <sup>, <br/>, <cite>, <q>, etc. Mismos atributos (href, id, class, data-*, epub:type, lang, xml:lang), mismas cantidades, mismo orden.

3. CITAS EN ALEMÁN, FRANCÉS O LATÍN — CRÍTICO:
   - Steinberg cita CONSTANTEMENTE fragmentos en alemán (cartas de Bismarck, despachos), francés y latín. Estas citas NO se traducen al español.
   - Si una cita está marcada con <i>, <em>, <span lang="de">, <span class="ital">, o entre comillas en alemán/francés/latín, déjala IDÉNTICA al original.
   - Si Steinberg ofrece una traducción al inglés entre paréntesis o tras la cita, esa traducción SÍ se vierte al español.
   - Reconoce alemán por: ß, ä/ö/ü, palabras como "der/die/das/und/ich/nicht/sehr/aber/wir/sie", construcciones "zu...en", participios "ge-...-t/-en". Reconoce francés por: ç, accents (à/é/è/ê), "le/la/les/de/du/que/qui". Reconoce latín por: terminaciones "-us/-um/-orum/-itur", frases tipo "in statu", "ad hoc", "casus belli".
   - Cuando dudes si traducir o no: si la frase se siente como cita en lengua extranjera, NO la traduzcas.

4. NOMBRES PROPIOS — reglas estrictas:
   - APELLIDOS alemanes, austriacos, rusos, polacos, etc. NO se traducen NUNCA: Bismarck, Roon, Moltke, Holstein, Eulenburg, Manteuffel, Lassalle, Marx, Windthorst, Bennigsen, Bleichröder, Gorchakov, Andrássy, Disraeli, Gladstone.
   - NOMBRES DE PILA de monarcas y papas se ESPAÑOLIZAN: Wilhelm I → Guillermo I, Friedrich III → Federico III, Franz Joseph → Francisco José, Napoleon III → Napoleón III, Alexander II → Alejandro II, Victor Emmanuel → Víctor Manuel, Pius IX → Pío IX. Excepción: si el nombre aparece junto al apellido (Friedrich Wilhelm von Bismarck), se mantiene en alemán.
   - Otros nombres de pila NO se españolizan: Otto von Bismarck (no "Otón"), Karl Marx (no "Carlos"), Helmuth von Moltke.
   - PARTÍCULAS nobiliarias "von", "zu", "auf der" se mantienen en minúscula.
   - DINASTÍAS: Hohenzollern, Habsburgo, Romanov, Wittelsbach se preservan; "Casa de Hohenzollern", "Casa de Habsburgo".

5. TOPÓNIMOS — usar exónimos españoles tradicionales cuando existen:
   Berlin → Berlín, Vienna → Viena, Frankfurt → Fráncfort, Munich → Múnich, Cologne → Colonia, Hamburg → Hamburgo, Strasbourg → Estrasburgo, Versailles → Versalles, Sedan → Sedán, Hanover → Hannover, Bavaria → Baviera, Saxony → Sajonia, Württemberg → Wurtemberg, Rhineland → Renania, Westphalia → Westfalia, Brandenburg → Brandeburgo, Mecklenburg → Mecklemburgo, Galicia → Galitzia, Lorraine → Lorena, Bohemia → Bohemia.
   Sin exónimo asentado → mantener forma original (Königsberg, Bad Ems, Lübeck).

6. TÍTULOS Y CARGOS — minúscula en español (regla ortográfica):
   - "Chancellor Bismarck" → "el canciller Bismarck"
   - "Kaiser Wilhelm" → "el káiser Guillermo"
   - "Minister-President" → "ministro presidente"
   - "Crown Prince Friedrich" → "el príncipe heredero Federico"
   - Cuando el cargo encabeza la frase o aparece como título de capítulo, puede ir capitalizado.

7. TÉRMINOS HISTÓRICOS ALEMANES — SE MANTIENEN EN ALEMÁN, EN CURSIVA:
   Realpolitik, Kulturkampf, Junker(s), Reichstag, Bundesrat, Landtag, Reich, Zollverein, Vormärz, Sonderweg, Weltpolitik, Gründerzeit, Ausgleich, Bürgertum, Sozialistengesetz.
   Si el original ya los marca con <i> o <em>, conserva los tags. Si no, NO agregues cursiva — preserva el formato exacto del original.

8. CONCEPTOS POLÍTICOS — traducción consistente (respetar glosario):
   chancellor → canciller; statesman → estadista; balance of power → equilibrio de poder; great power(s) → gran(des) potencia(s); blood and iron → "sangre y hierro" (entre comillas); unification → unificación; raison d'état → razón de Estado; suffrage → sufragio; estate (clase) → estamento; bourgeoisie → burguesía; nobility → nobleza; serfdom → servidumbre.

9. NOMBRES DE GUERRAS Y TRATADOS — convención española:
   "Franco-Prussian War" → "guerra franco-prusiana" (minúscula); "Austro-Prussian War" → "guerra austro-prusiana"; "Seven Weeks' War" → "guerra de las Siete Semanas"; "Crimean War" → "guerra de Crimea"; "Treaty of Frankfurt" → "Tratado de Fráncfort"; "Congress of Berlin" → "Congreso de Berlín"; "Ems Dispatch" → "Despacho de Ems".

10. ESPAÑOL LATAM NEUTRO, lector peruano culto:
    - "tú" como segunda persona (no "vos", no "vosotros"). El autor casi nunca se dirige al lector, pero cuando lo haga, "tú".
    - Sin modismos regionales ("chévere", "bacán", "guay", "chido", "mola" — NINGUNO).
    - Sin conjugaciones peninsulares ("vosotros tenéis", "habríais" — NO).
    - Registro: académico, ensayístico, sintaxis cuidada. Permite oraciones largas con subordinadas; el autor escribe así y debes preservar la cadencia.

11. TONO DE STEINBERG — CRÍTICO:
    - Erudito pero vivaz. Mezcla análisis frío con juicios morales y observaciones psicológicas agudas (Bismarck era misógino, hipocondríaco, voraz, brillante).
    - Cita mucho y comenta. Las citas largas en bloque (<blockquote>) preservan su propia retórica; el comentario de Steinberg vuelve a la prosa académica.
    - Ironía sutil, sin caricaturizar. Steinberg admira y deplora a Bismarck; preserva esa ambivalencia. NO suavices, NO endurezcas.
    - NO simplifiques oraciones complejas. NO rompas párrafos. Preserva la sofisticación del original.
    - Evita anglicismos innecesarios y traducciones literales torpes ("interesante" pobre, "actually" → "en realidad", no "actualmente").

12. NÚMEROS, FECHAS Y MONEDAS:
    - Fechas: "March 14, 1871" → "14 de marzo de 1871". Días/meses en minúscula.
    - Siglos: "the nineteenth century" → "el siglo XIX" (números romanos).
    - Monedas: "thaler" → "tálero" (singular "tálero", plural "táleros"); "mark" → "marco"; "pound" → "libra"; "franc" → "franco". "Reichstaler" se mantiene.

13. TÍTULOS DE OBRAS y publicaciones (libros, revistas, periódicos): se mantienen en su idioma original. NO traduzcas títulos de libros citados. Si están entre <i> o <span class="ital">, preserva los tags.

14. URLs, IDs y links: preserva EXACTAMENTE como están. Las notas al pie <sup><a href="..."> son referencias estructurales — NO las traduzcas, NO las muevas.

15. NO expliques, NO resumas, NO agregues notas del traductor, NO expandas siglas que el autor dejó sin expandir.

16. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_BISMARCK = [
    r"^cover\.x?html$",
    r"titlepage\.x?html$",
    r"copyright\.x?html$",
    r"halftitle\.x?html$",
    r"^index\.x?html$",        # índice alfabético — no útil traducido
    r"^index\d*\.x?html$",
]

# Perfil "slow_looking" — Shari Tishman / Routledge (Slow Looking: The Art, Science, and History of Learning, 2018)
# Ensayo académico-divulgativo sobre observación, atención y aprendizaje. Project Zero / Harvard Graduate School of Education.
GLOSSARY_SLOW_LOOKING: dict[str, str] = {
    # ─── Conceptos centrales del libro ───
    "slow looking": "observación lenta",
    "Slow Looking": "Observación Lenta",
    "close looking": "observación atenta",
    "close reading": "lectura atenta",
    "noticing": "advertir",
    "to notice": "advertir",
    "observation": "observación",
    "observations": "observaciones",
    "observer": "observador",
    "observers": "observadores",
    "attention": "atención",
    "perception": "percepción",
    "perceptual": "perceptivo",
    "perceiving": "percibir",
    "awareness": "conciencia",
    "mindful": "consciente",
    "mindfulness": "atención plena",
    "inquiry": "indagación",
    "thinking routine": "rutina de pensamiento",
    "thinking routines": "rutinas de pensamiento",
    "visible thinking": "pensamiento visible",
    "Visible Thinking": "Pensamiento Visible",
    "meaning making": "construcción de significado",
    "sensemaking": "construcción de sentido",
    "See/Think/Wonder": "Veo/Pienso/Me pregunto",
    "See Think Wonder": "Veo, Pienso, Me pregunto",
    # ─── Educación / pedagogía ───
    "learning": "aprendizaje",
    "learner": "aprendiz",
    "learners": "aprendices",
    "teaching": "enseñanza",
    "teacher": "docente",
    "teachers": "docentes",
    "educator": "educador",
    "educators": "educadores",
    "student": "estudiante",
    "students": "estudiantes",
    "classroom": "aula",
    "classrooms": "aulas",
    "curriculum": "currículo",
    "curricula": "currículos",
    "pedagogy": "pedagogía",
    "pedagogical": "pedagógico",
    "education": "educación",
    "educational": "educativo",
    "schooling": "escolarización",
    "lesson": "lección",
    "lessons": "lecciones",
    "lesson plan": "plan de lección",
    "instruction": "instrucción",
    "instructional": "instruccional",
    "assessment": "evaluación",
    "skill": "habilidad",
    "skills": "habilidades",
    "competence": "competencia",
    "knowledge": "conocimiento",
    "understanding": "comprensión",
    "comprehension": "comprensión",
    "engagement": "implicación",
    "discipline": "disciplina",
    "subject matter": "materia",
    "K-12": "K-12",
    "elementary school": "escuela primaria",
    "middle school": "escuela media",
    "high school": "secundaria",
    "undergraduate": "pregrado",
    "graduate": "posgrado",
    # ─── Arte y museos ───
    "art": "arte",
    "artwork": "obra de arte",
    "artworks": "obras de arte",
    "work of art": "obra de arte",
    "works of art": "obras de arte",
    "art history": "historia del arte",
    "art historian": "historiador del arte",
    "art education": "educación artística",
    "artist": "artista",
    "artists": "artistas",
    "painting": "pintura",
    "paintings": "pinturas",
    "drawing": "dibujo",
    "drawings": "dibujos",
    "sculpture": "escultura",
    "sculptures": "esculturas",
    "photograph": "fotografía",
    "photography": "fotografía",
    "museum": "museo",
    "museums": "museos",
    "gallery": "galería",
    "galleries": "galerías",
    "exhibit": "exposición",
    "exhibition": "exposición",
    "exhibitions": "exposiciones",
    "curator": "curador",
    "curators": "curadores",
    "collection": "colección",
    "portrait": "retrato",
    "landscape": "paisaje",
    "still life": "naturaleza muerta",
    "canvas": "lienzo",
    "brushstroke": "pincelada",
    "composition": "composición",
    "abstract": "abstracto",
    "figurative": "figurativo",
    # ─── Ciencia y observación natural ───
    "science": "ciencia",
    "scientific": "científico",
    "scientist": "científico",
    "scientists": "científicos",
    "naturalist": "naturalista",
    "naturalists": "naturalistas",
    "specimen": "espécimen",
    "specimens": "especímenes",
    "discovery": "descubrimiento",
    "experiment": "experimento",
    "experimental": "experimental",
    "hypothesis": "hipótesis",
    "field guide": "guía de campo",
    "field notebook": "cuaderno de campo",
    "biology": "biología",
    "ecology": "ecología",
    "natural history": "historia natural",
    "evidence": "evidencia",
    "data": "datos",
    "phenomenon": "fenómeno",
    "phenomena": "fenómenos",
    # ─── Historia y cultura material ───
    "artifact": "artefacto",
    "artifacts": "artefactos",
    "archive": "archivo",
    "archives": "archivos",
    "primary source": "fuente primaria",
    "secondary source": "fuente secundaria",
    "historical": "histórico",
    "history": "historia",
    "historian": "historiador",
    # ─── Verbos y conceptos analíticos comunes ───
    "describe": "describir",
    "description": "descripción",
    "descriptive": "descriptivo",
    "interpret": "interpretar",
    "interpretation": "interpretación",
    "interpretive": "interpretativo",
    "analyze": "analizar",
    "analysis": "análisis",
    "analytical": "analítico",
    "reflect": "reflexionar",
    "reflection": "reflexión",
    "reflective": "reflexivo",
    "evidence-based": "basado en evidencia",
    "judgment": "juicio",
    "insight": "intuición",
    "insights": "intuiciones",
    # ─── Personas, instituciones, programas — NO traducir ───
    "Shari Tishman": "Shari Tishman",
    "Tishman": "Tishman",
    "Project Zero": "Project Zero",
    "Harvard": "Harvard",
    "Harvard Graduate School of Education": "Harvard Graduate School of Education",
    "Howard Gardner": "Howard Gardner",
    "David Perkins": "David Perkins",
    "Ron Ritchhart": "Ron Ritchhart",
    "Reggio Emilia": "Reggio Emilia",
    "Smithsonian": "Smithsonian",
    "MoMA": "MoMA",
    "Metropolitan Museum": "Metropolitan Museum",
    "Routledge": "Routledge",
    # ─── Conceptos misceláneos ───
    "mindset": "mentalidad",
    "habit of mind": "hábito mental",
    "habits of mind": "hábitos mentales",
    "framework": "marco",
    "case study": "estudio de caso",
    "case studies": "estudios de caso",
    "exercise": "ejercicio",
    "exercises": "ejercicios",
    "activity": "actividad",
    "activities": "actividades",
}

SYSTEM_PROMPT_SLOW_LOOKING = """Eres un traductor literario profesional especializado en ensayos académicos sobre educación, arte y ciencia. Traduces del inglés al español latinoamericano neutro para un lector peruano interesado en pedagogía, arte y aprendizaje activo.

Este libro es "Slow Looking: The Art, Science, and History of Learning" de Shari Tishman (Routledge, 2018). La autora es investigadora principal de Project Zero (Harvard Graduate School of Education). El libro propone la "observación lenta" (slow looking) como práctica deliberada de atención sostenida sobre objetos, obras de arte, fenómenos naturales y artefactos históricos. Es un ensayo accesible, ilustrado con ejemplos concretos de aulas, museos y campo, fundamentado en investigación pero escrito con voz cálida y didáctica.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes.
   - Preserva su posición EXACTA dentro del texto.

2. PRESERVA TODOS los tags HTML exactamente: <em>, <strong>, <i>, <b>, <a href>, <span>, <sup>, <br/>, <cite>, <q>, etc. Mismos atributos (href, id, class, data-*, epub:type, lang, xml:lang), mismas cantidades, mismo orden.

3. CONCEPTO CENTRAL — "slow looking" → "observación lenta":
   - Tradúcelo SIEMPRE así, sin excepción. Es el término técnico del libro.
   - Cuando aparezca en mayúsculas (título de capítulo, encabezado), usa "Observación Lenta".
   - "to look slowly" → "observar con lentitud" / "observar lentamente".
   - "looking slowly" → "observar con lentitud".

4. RUTINAS DE PENSAMIENTO de Project Zero — nombres canónicos:
   - "See/Think/Wonder" → "Veo/Pienso/Me pregunto" (con barras y mayúsculas iniciales).
   - "thinking routine" → "rutina de pensamiento" (siempre).
   - "visible thinking" → "pensamiento visible".
   - "Project Zero" → "Project Zero" (NO traducir, es el nombre del programa).

5. TERMINOLOGÍA EDUCATIVA — traducción consistente (respetar glosario):
   teacher → docente; student → estudiante; classroom → aula; learner → aprendiz; learning → aprendizaje; curriculum → currículo; pedagogy → pedagogía; lesson → lección; assessment → evaluación; engagement → implicación; understanding → comprensión.
   Consistencia TOTAL: "teacher" SIEMPRE es "docente", no "maestro" ni "profesor", a menos que el original distinga ("schoolteacher" → "maestro de escuela").

6. TERMINOLOGÍA ARTÍSTICA — traducción consistente:
   artwork → obra de arte; museum → museo; gallery → galería; curator → curador; exhibit/exhibition → exposición; portrait → retrato; landscape → paisaje; still life → naturaleza muerta; brushstroke → pincelada.
   Movimientos artísticos: "Impressionism" → "impresionismo" (minúscula); "Cubism" → "cubismo"; "Romanticism" → "romanticismo".

7. NOMBRES PROPIOS — reglas:
   - Personas (autores, artistas, científicos, educadores) NO se traducen: Shari Tishman, Howard Gardner, David Perkins, Charles Darwin, Vincent van Gogh, Leonardo da Vinci, etc.
   - Nombres de pila NO se españolizan (excepto monarcas/papas, que aquí casi no aparecen): Shari, no "Sary".
   - Instituciones NO se traducen: Project Zero, Harvard Graduate School of Education, MoMA, Smithsonian, Metropolitan Museum.
   - Topónimos: usar exónimo español si está asentado (Florence → Florencia, Athens → Atenas, New York → Nueva York). Sin exónimo → mantener original.

8. TÍTULOS DE OBRAS DE ARTE Y LIBROS — se mantienen en idioma original:
   - "The Starry Night" → "The Starry Night" (no "La noche estrellada").
   - Si están entre <em>, <i>, <cite> o <span class="ital">, conserva los tags.
   - EXCEPCIÓN: si Tishman ofrece la traducción literal entre paréntesis o el título es universalmente conocido en español, puedes españolizar (ej: "Las Meninas" se queda en español).

9. CITAS DE ESTUDIANTES Y AULAS — naturalidad:
   - Cuando Tishman cita comentarios de estudiantes o transcripciones de aula, traduce con naturalidad oral, NO formal. Mantén dudas, "uhms", repeticiones.
   - Conserva el género gramatical de quien habla cuando esté indicado.

10. ESPAÑOL LATAM NEUTRO, lector peruano:
    - "tú" como segunda persona (no "vos", no "vosotros"). Tishman se dirige al lector con frecuencia: "tú observas", "puedes ver", "intenta esto".
    - Sin modismos regionales ("chévere", "bacán", "guay", "chido", "mola" — NINGUNO).
    - Sin conjugaciones peninsulares ("vosotros tenéis", "habéis").
    - Registro: claro, didáctico, cálido. Evita la prosa académica seca; Tishman invita al lector a la práctica.

11. TONO DE TISHMAN — CRÍTICO:
    - Cálido, conversacional pero culto. Combina anécdotas (visitas a museos, clases, salidas de campo) con marco teórico.
    - Voz en primera persona del singular y plural ("I once watched...", "we tend to..."): preserva esa cercanía.
    - Invita al lector a probar las prácticas. Cuando dice "Try this:" → "Prueba esto:" o "Inténtalo:" según fluya.
    - NO endurezcas el registro. NO simplifiques las ideas. NO rompas el ritmo afirmativo.

12. EJEMPLOS, EJERCICIOS Y RECUADROS:
    - Tishman incluye recuadros con ejercicios prácticos (mira un objeto durante 10 minutos, lista lo que ves, etc.). Mantén el imperativo directo en segunda persona del singular (tú): "Elige un objeto", "Escribe lo que notas", "Comparte con un compañero".

13. NÚMEROS, MEDIDAS, FECHAS:
    - Fechas: "March 14, 2017" → "14 de marzo de 2017" (días/meses en minúscula).
    - Siglos: "the twenty-first century" → "el siglo XXI" (números romanos).
    - Medidas: pulgadas → pulgadas (con cm entre paréntesis si Tishman lo hace); pies → pies; mantener el sistema original cuando es citado.

14. URLs, IDs, hrefs, footnotes (<sup><a href="...">N</a></sup>): preserva EXACTAMENTE.

15. NO expliques, NO resumas, NO agregues notas del traductor.

16. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_SLOW_LOOKING = [
    r"^cover\.x?html$",
    r"titlepage\.x?html$",
    r"copyright\.x?html$",
    r"halftitle\.x?html$",
    r"^index\.x?html$",
    r"^index\d*\.x?html$",
]

# Perfil "the_score" — C. Thi Nguyen / Penguin Group (The Score: How to Stop Playing Somebody Else's Game, 2026)
# Filosofía social y ética. Nguyen es filósofo (Utah) conocido por trabajos sobre gamificación, valores,
# agencia, atrapamiento epistémico. El libro analiza cómo las métricas, rankings y juegos colonizan la vida.
GLOSSARY_THE_SCORE: dict[str, str] = {
    # ─── Conceptos centrales del libro / pensamiento de Nguyen ───
    "the score": "el score",
    "score": "score",
    "scores": "scores",
    "scoring": "puntuación",
    "value capture": "captura de valor",
    "value-capture": "captura de valor",
    "gamification": "gamificación",
    "gamified": "gamificado",
    "gamify": "gamificar",
    "epistemic trap": "trampa epistémica",
    "epistemic traps": "trampas epistémicas",
    "echo chamber": "cámara de eco",
    "echo chambers": "cámaras de eco",
    "moral outrage": "indignación moral",
    "agency": "agencia",
    "rational agency": "agencia racional",
    "self-trust": "autoconfianza",
    "intellectual autonomy": "autonomía intelectual",
    "moral clarity": "claridad moral",
    "thinking for oneself": "pensar por uno mismo",
    # ─── Métricas, rankings, cuantificación ───
    "metric": "métrica",
    "metrics": "métricas",
    "quantification": "cuantificación",
    "quantify": "cuantificar",
    "quantified": "cuantificado",
    "quantifying": "cuantificación",
    "ranking": "ranking",
    "rankings": "rankings",
    "leaderboard": "tabla de posiciones",
    "leaderboards": "tablas de posiciones",
    "KPI": "KPI",
    "KPIs": "KPIs",
    "key performance indicator": "indicador clave de desempeño",
    "performance metric": "métrica de desempeño",
    "performance review": "evaluación de desempeño",
    "Goodhart's Law": "Ley de Goodhart",
    "Goodhart's law": "ley de Goodhart",
    "proxy": "proxy",
    "proxies": "proxys",
    "legibility": "legibilidad",
    "legible": "legible",
    "audit": "auditoría",
    "audits": "auditorías",
    "auditing": "auditoría",
    "benchmark": "benchmark",
    "benchmarks": "benchmarks",
    "benchmarking": "evaluación comparativa",
    # ─── Conceptos filosóficos ───
    "philosophy": "filosofía",
    "philosopher": "filósofo",
    "philosophers": "filósofos",
    "philosophical": "filosófico",
    "ethics": "ética",
    "ethical": "ético",
    "moral": "moral",
    "morality": "moralidad",
    "value": "valor",
    "values": "valores",
    "valuing": "valoración",
    "intrinsic": "intrínseco",
    "intrinsically": "intrínsecamente",
    "instrumental": "instrumental",
    "instrumentally": "instrumentalmente",
    "end": "fin",
    "ends": "fines",
    "means": "medios",
    "normative": "normativo",
    "normativity": "normatividad",
    "epistemology": "epistemología",
    "epistemic": "epistémico",
    "epistemically": "epistémicamente",
    "phenomenology": "fenomenología",
    "phenomenological": "fenomenológico",
    "aesthetic": "estético",
    "aesthetics": "estética",
    "rationality": "racionalidad",
    "rational": "racional",
    "reason": "razón",
    "reasoning": "razonamiento",
    "reasonable": "razonable",
    "deliberation": "deliberación",
    "deliberate": "deliberar",
    "judgment": "juicio",
    "judgments": "juicios",
    "moral judgment": "juicio moral",
    "moral judgments": "juicios morales",
    "expertise": "experticia",
    "expert": "experto",
    "experts": "expertos",
    # ─── Juegos, deporte, estructura lúdica ───
    "game": "juego",
    "games": "juegos",
    "gameplay": "jugabilidad",
    "playing": "jugar",
    "to play": "jugar",
    "win": "ganar",
    "winning": "ganar",
    "loss": "pérdida",
    "rules": "reglas",
    "goal": "meta",
    "goals": "metas",
    "victory condition": "condición de victoria",
    "agency in games": "agencia en los juegos",
    "striving play": "juego como búsqueda",
    # ─── Tecnología, plataformas, redes sociales ───
    "platform": "plataforma",
    "platforms": "plataformas",
    "algorithm": "algoritmo",
    "algorithms": "algoritmos",
    "algorithmic": "algorítmico",
    "social media": "redes sociales",
    "social network": "red social",
    "Twitter": "Twitter",
    "Facebook": "Facebook",
    "Instagram": "Instagram",
    "TikTok": "TikTok",
    "YouTube": "YouTube",
    "Yelp": "Yelp",
    "engagement": "implicación",
    "user engagement": "implicación del usuario",
    "click": "clic",
    "clicks": "clics",
    "like": "like",
    "likes": "likes",
    "follower": "seguidor",
    "followers": "seguidores",
    "viral": "viral",
    "trending": "tendencia",
    # ─── Vida académica / institucional ───
    "academia": "academia",
    "academic": "académico",
    "scholar": "estudioso",
    "scholarship": "investigación académica",
    "university": "universidad",
    "universities": "universidades",
    "tenure": "tenure",
    "tenure-track": "tenure-track",
    "department": "departamento",
    "publication": "publicación",
    "peer review": "revisión por pares",
    "peer-reviewed": "revisado por pares",
    "citation": "cita",
    "citations": "citas",
    "h-index": "índice h",
    "impact factor": "factor de impacto",
    "research": "investigación",
    "research output": "producción investigativa",
    "academic capitalism": "capitalismo académico",
    "ranking system": "sistema de rankings",
    # ─── Burocracia, gobernanza, política pública ───
    "bureaucracy": "burocracia",
    "bureaucratic": "burocrático",
    "bureaucrat": "burócrata",
    "governance": "gobernanza",
    "policy": "política",
    "public policy": "política pública",
    "regulation": "regulación",
    "regulator": "regulador",
    "compliance": "cumplimiento",
    "managerialism": "gerencialismo",
    "neoliberal": "neoliberal",
    "neoliberalism": "neoliberalismo",
    "accountability": "rendición de cuentas",
    "transparency": "transparencia",
    "standardization": "estandarización",
    "standardize": "estandarizar",
    # ─── Atención y vida cotidiana ───
    "attention": "atención",
    "attentional": "atencional",
    "distraction": "distracción",
    "focus": "foco",
    "engagement": "implicación",
    "self": "yo",
    "selfhood": "individualidad",
    "identity": "identidad",
    "self-knowledge": "autoconocimiento",
    "well-being": "bienestar",
    "wellbeing": "bienestar",
    "flourishing": "florecimiento",
    # ─── Personas, instituciones — NO traducir ───
    "C. Thi Nguyen": "C. Thi Nguyen",
    "Nguyen": "Nguyen",
    "Iris Murdoch": "Iris Murdoch",
    "Murdoch": "Murdoch",
    "James C. Scott": "James C. Scott",
    "Bernard Suits": "Bernard Suits",
    "Aristotle": "Aristóteles",
    "Kant": "Kant",
    "Foucault": "Foucault",
    "Wittgenstein": "Wittgenstein",
    "John Dewey": "John Dewey",
    "Charles Goodhart": "Charles Goodhart",
    "Penguin": "Penguin",
    "Penguin Press": "Penguin Press",
    "Penguin Group": "Penguin Group",
    "MIT": "MIT",
    "Harvard": "Harvard",
    "Stanford": "Stanford",
    "Oxford": "Oxford",
    "Cambridge": "Cambridge",
    "Princeton": "Princeton",
    "University of Utah": "University of Utah",
    # ─── Conceptos auxiliares ───
    "framework": "marco",
    "case study": "estudio de caso",
    "case studies": "estudios de caso",
    "thought experiment": "experimento mental",
    "thought experiments": "experimentos mentales",
    "rule of thumb": "regla general",
    "trade-off": "compromiso",
    "tradeoff": "compromiso",
    "trade-offs": "compromisos",
    "incentive": "incentivo",
    "incentives": "incentivos",
    "feedback loop": "bucle de retroalimentación",
    "feedback loops": "bucles de retroalimentación",
}

SYSTEM_PROMPT_THE_SCORE = """Eres un traductor literario profesional especializado en filosofía social, ética y crítica cultural. Traduces del inglés al español latinoamericano neutro para un lector peruano culto interesado en filosofía, tecnología y vida pública.

Este libro es "The Score: How to Stop Playing Somebody Else's Game" de C. Thi Nguyen (Penguin Group, 2026). Nguyen es filósofo de la Universidad de Utah, conocido por su trabajo sobre gamificación, captura de valor (value capture), atrapamiento epistémico, agencia racional y la filosofía de los juegos. El libro examina cómo las métricas, rankings y sistemas de puntuación —desde KPIs corporativos hasta likes en redes sociales— colonizan nuestras vidas, sustituyen nuestros valores propios y nos hacen jugar el juego de otros. Es un ensayo filosófico riguroso pero accesible, con ejemplos del deporte, la academia, el arte y las redes sociales.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes.
   - Preserva su posición EXACTA.

2. PRESERVA TODOS los tags HTML EXACTAMENTE: <em>, <strong>, <i>, <b>, <a href>, <span>, <sup>, <br/>, <cite>, <q>, etc. Mismos atributos (href, id, class, data-*, epub:type, lang, xml:lang, aria-label), mismas cantidades, mismo orden. Esto incluye <span epub:type="pagebreak" id="page_X" title="X"/> que aparecen a media oración: quedan EXACTAMENTE donde están, aunque la sintaxis en español ya no fluya igual.

3. CONCEPTOS TÉCNICOS DE NGUYEN — traducción estable y consistente:
   - "value capture" → "captura de valor" SIEMPRE (concepto central; cuando aparezca en cursiva, conserva los tags).
   - "gamification" → "gamificación"; "gamified" → "gamificado".
   - "epistemic trap" → "trampa epistémica".
   - "the score" / "score" → "el score" / "score" (en minúscula, sin traducir; es el término técnico del libro). EXCEPCIÓN: cuando "score" aparezca en contextos no-técnicos (ej. partituras musicales) traduce según contexto.
   - "agency" → "agencia"; "rational agency" → "agencia racional".
   - "Goodhart's Law" → "Ley de Goodhart" (mayúsculas iniciales — es ley nombrada).

4. MÉTRICAS Y CUANTIFICACIÓN — consistencia total:
   metric → métrica; quantification → cuantificación; ranking → ranking (no traducir); leaderboard → tabla de posiciones; KPI → KPI; proxy → proxy; legibility → legibilidad; benchmark → benchmark.

5. VOCABULARIO FILOSÓFICO — traducción estándar académica:
   - "value(s)" → "valor(es)"; "intrinsic" → "intrínseco"; "instrumental" → "instrumental".
   - "end(s)" → "fin(es)"; "means" → "medios".
   - "epistemic" → "epistémico"; "epistemology" → "epistemología".
   - "judgment" → "juicio"; "moral judgment" → "juicio moral".
   - "rationality" → "racionalidad"; "reasoning" → "razonamiento".
   - "agency" → "agencia"; "autonomy" → "autonomía".
   - "expertise" → "experticia" (no "pericia", siguiendo uso filosófico latinoamericano).

6. NOMBRES PROPIOS:
   - Filósofos contemporáneos (Nguyen, Murdoch, Suits, Scott, Goodhart, Dewey, Foucault, Wittgenstein) NUNCA se traducen.
   - Filósofos clásicos SÍ se españolizan: Aristotle → Aristóteles, Plato → Platón, Descartes → Descartes (mismo).
   - Instituciones NUNCA se traducen: MIT, Harvard, Stanford, Oxford, Cambridge, Princeton, University of Utah, Penguin Press, Penguin Group.
   - Topónimos: usar exónimo español si está asentado (London → Londres, New York → Nueva York). Sin exónimo → mantener original.

7. PLATAFORMAS Y TECNOLOGÍA — preservar nombres:
   Twitter, Facebook, Instagram, TikTok, YouTube, Yelp, Google, Amazon — sin traducir, sin cursiva.
   "social media" → "redes sociales"; "engagement" → "implicación"; "click" → "clic"; "like(s)" → "like(s)" (en minúscula, sin traducir, es jerga universal).

8. CITAS Y PASAJES EN OTROS IDIOMAS:
   - Citas extensas en inglés DENTRO del texto en inglés (cita literaria, frase clave): se traducen, salvo que sean una frase consagrada del autor citado.
   - Citas en otros idiomas (francés, alemán, latín): NO traducir. Conservar idioma original.

9. ESPAÑOL LATAM NEUTRO, lector peruano culto:
   - "tú" como segunda persona (Nguyen se dirige al lector con frecuencia: "you might think", "imagine you").
   - Sin modismos regionales ("chévere", "bacán", "guay", "chido", "mola" — NINGUNO).
   - Sin conjugaciones peninsulares ("vosotros tenéis", "habríais").
   - Registro: ensayístico, agudo, claro pero sofisticado. Permite oraciones complejas con subordinadas — Nguyen escribe así.

10. TONO DE NGUYEN — CRÍTICO:
    - Filosóficamente preciso pero conversacional. Mezcla rigor analítico con anécdotas (jugar Mario, escalada en roca, redes sociales).
    - Crítico pero no cínico. Diagnostica problemas con clarividencia y propone caminos.
    - Voz en primera persona ("I argue", "I want to suggest"): preserva esa cercanía con el lector.
    - Usa preguntas retóricas con frecuencia ("What is going on here?"). Mantén la fuerza retórica.
    - NO endurezcas el registro. NO simplifiques las distinciones conceptuales finas.

11. EJEMPLOS Y CASOS:
    - Nguyen usa muchos ejemplos concretos: profesores rankeados, deportistas, jugadores de videojuegos, usuarios de Twitter. Tradúcelos con naturalidad oral en los diálogos, manteniendo el registro coloquial cuando aplique.

12. NÚMEROS, FECHAS Y MEDIDAS:
    - Fechas: "March 14, 2024" → "14 de marzo de 2024" (días/meses en minúscula).
    - Siglos: "the twenty-first century" → "el siglo XXI" (números romanos).
    - Mantener cifras en formato original con separadores españoles (1,000,000 → 1 000 000 si Nguyen los usa; respetar formato original si es ambiguo).

13. TÍTULOS DE OBRAS Y PUBLICACIONES (libros, papers, revistas): se mantienen en idioma original. NO traducir títulos. Si están entre <i> o <em>, conservar tags.

14. URLs, IDs, hrefs, footnotes (<sup><a href="...">N</a></sup>): preserva EXACTAMENTE.

15. NO expliques, NO resumas, NO agregues notas del traductor.

16. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_THE_SCORE = [
    r"_cvi_Cover\.xhtml$",      # imagen de portada
    r"_tit_Title_Page\.xhtml$", # solo logos/título
    r"_idx_Index\.xhtml$",      # índice analítico (268 KB, 1000+ entradas — no traducir)
    r"_nav\.xhtml$",            # nav de epub3 (auto-generado por TOC)
]

# Perfil "lake_como" — Romano Guardini / Eerdmans (Letters from Lake Como, serie Ressourcement)
# Cartas filosófico-teológicas (1923, original alemán "Briefe vom Comer See") sobre tecnología,
# naturaleza y cultura. Traducción inglesa de Geoffrey Bromiley con introducción de Louis Dupré.
GLOSSARY_LAKE_COMO: dict[str, str] = {
    # ─── Conceptos centrales del libro ───
    "Letters from Lake Como": "Cartas desde el lago de Como",
    "Dear Friend": "Querido amigo",
    "Lake Como": "lago de Como",
    "Como": "Como",
    "letters": "cartas",
    "letter": "carta",
    "technology": "tecnología",
    "technological": "tecnológico",
    "machine": "máquina",
    "machines": "máquinas",
    "machinery": "maquinaria",
    "the Machine": "la Máquina",
    "industry": "industria",
    "industrial": "industrial",
    "industrialism": "industrialismo",
    "mass": "masa",
    "masses": "masas",
    "the masses": "las masas",
    "civilization": "civilización",
    "modernity": "modernidad",
    "modern": "moderno",
    "tradition": "tradición",
    "traditional": "tradicional",
    "culture": "cultura",
    "cultural": "cultural",
    # ─── Filosofía / metafísica ───
    "nature": "naturaleza",
    "natural": "natural",
    "the organic": "lo orgánico",
    "the inorganic": "lo inorgánico",
    "organism": "organismo",
    "form": "forma",
    "Gestalt": "Gestalt",
    "structure": "estructura",
    "abstraction": "abstracción",
    "abstract": "abstracto",
    "concrete": "concreto",
    "consciousness": "conciencia",
    "self-consciousness": "autoconciencia",
    "reality": "realidad",
    "real": "real",
    "existence": "existencia",
    "being": "ser",
    "essence": "esencia",
    "becoming": "devenir",
    "humanity": "humanidad",
    "the human": "lo humano",
    "human being": "ser humano",
    "human beings": "seres humanos",
    "the earth": "la tierra",
    "the world": "el mundo",
    "world": "mundo",
    "spirit": "espíritu",
    "spiritual": "espiritual",
    "soul": "alma",
    "mind": "mente",
    "intellect": "intelecto",
    "intellectual": "intelectual",
    "reason": "razón",
    "rational": "racional",
    "will": "voluntad",
    "freedom": "libertad",
    "personhood": "carácter de persona",
    "the person": "la persona",
    "personality": "personalidad",
    "individuality": "individualidad",
    "individual": "individuo",
    "subject": "sujeto",
    "object": "objeto",
    "subjective": "subjetivo",
    "objective": "objetivo",
    # ─── Estética y experiencia ───
    "beauty": "belleza",
    "beautiful": "hermoso",
    "image": "imagen",
    "vision": "visión",
    "experience": "experiencia",
    "feeling": "sentimiento",
    "sensibility": "sensibilidad",
    "perception": "percepción",
    "intuition": "intuición",
    "wholeness": "totalidad",
    "the whole": "el todo",
    "harmony": "armonía",
    "rhythm": "ritmo",
    # ─── Acción / dominio ───
    "mastery": "dominio",
    "domination": "dominación",
    "power": "poder",
    "force": "fuerza",
    "action": "acción",
    "activity": "actividad",
    "work": "trabajo",
    "labor": "trabajo",
    "task": "tarea",
    # ─── Teología / Ressourcement ───
    "Catholic": "católico",
    "Catholicism": "catolicismo",
    "Christian": "cristiano",
    "Christianity": "cristianismo",
    "God": "Dios",
    "the divine": "lo divino",
    "providence": "providencia",
    "grace": "gracia",
    "creation": "creación",
    "creature": "criatura",
    "Church": "Iglesia",
    "the Church": "la Iglesia",
    "faith": "fe",
    "Ressourcement": "Ressourcement",
    "Eerdmans": "Eerdmans",
    # ─── Términos alemanes que SE MANTIENEN en alemán ───
    "Lebensgefühl": "Lebensgefühl",
    "Weltanschauung": "Weltanschauung",
    "Bildung": "Bildung",
    "Schildgenossen": "Schildgenossen",
    "Kultur": "Kultur",
    "Volk": "Volk",
    "Geist": "Geist",
    # ─── Topónimos italianos / europeos ───
    "Italy": "Italia",
    "Italian": "italiano",
    "Italians": "italianos",
    "Germany": "Alemania",
    "German": "alemán",
    "Germans": "alemanes",
    "Switzerland": "Suiza",
    "Swiss": "suizo",
    "Brescia": "Brescia",
    "Verona": "Verona",
    "Milan": "Milán",
    "Florence": "Florencia",
    "Rome": "Roma",
    "the Alps": "los Alpes",
    "the Mediterranean": "el Mediterráneo",
    # ─── Personas — NO traducir ───
    "Romano Guardini": "Romano Guardini",
    "Guardini": "Guardini",
    "Louis Dupré": "Louis Dupré",
    "Dupré": "Dupré",
    "Geoffrey Bromiley": "Geoffrey Bromiley",
    "Bromiley": "Bromiley",
    "Goethe": "Goethe",
    "Schiller": "Schiller",
    "Hegel": "Hegel",
    "Kant": "Kant",
    "Nietzsche": "Nietzsche",
    "Heidegger": "Heidegger",
    # ─── Vocabulario de cartas ───
    "I have": "he",
    "I had": "había",
    "you and I": "tú y yo",
    "we": "nosotros",
}

SYSTEM_PROMPT_LAKE_COMO = """Eres un traductor literario profesional especializado en filosofía continental, teología católica y ensayismo cultural alemán. Traduces del inglés al español neutro (lector culto), preservando el vuelo metafísico del original.

Este libro es "Letters from Lake Como: Explorations in Technology and the Human Race" de Romano Guardini (1885-1968), traducido del alemán "Briefe vom Comer See" (1923, 1927) por Geoffrey Bromiley, con introducción de Louis Dupré, en la serie Ressourcement de Eerdmans (1994). Es una colección de nueve cartas filosófico-teológicas escritas en el norte de Italia, donde Guardini —teólogo italo-alemán, sacerdote católico, una de las figuras intelectuales más finas del siglo XX— reflexiona sobre el impacto de la técnica sobre la naturaleza, la cultura y el ser humano. Es prosa contemplativa, de gran densidad conceptual, escrita en tono íntimo y meditativo (a un amigo).

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_1⟧, ⟦OPAQUE_2⟧, etc. son placeholders protegidos.
   - NO los traduzcas. NO los modifiques. NO los elimines. NO los reordenes.

2. PRESERVA TODOS los tags HTML EXACTAMENTE: <p>, <h1>-<h6>, <div>, <a>, <i>, <em>, <span>, etc. Mismos atributos (class, id, href, lang), mismas cantidades, mismo orden. El libro tiene mucho `<div class="calibre1">` y `<p class="cl-1p-v">` o similares — preservalos exactos.

3. APERTURA DE CADA CARTA:
   - "Dear Friend," → "Querido amigo:" (con dos puntos, no coma).
   - El registro es íntimo pero sustantivo: Guardini escribe a un destinatario singular, culto, con quien comparte una conversación filosófica continua.

4. CONCEPTO CENTRAL — TECNOLOGÍA Y NATURALEZA:
   - "technology" → "tecnología" SIEMPRE (no "técnica" salvo cuando el original dice "technique").
   - "the machine" → "la máquina"; "machinery" → "maquinaria"; "industry" → "industria".
   - "nature" → "naturaleza"; "the organic"/"the inorganic" → "lo orgánico"/"lo inorgánico".
   - "form" → "forma" (concepto morfológico fuerte en Guardini, no "format").
   - "Gestalt" si aparece en alemán → SE MANTIENE en alemán.
   - "the masses" → "las masas" (concepto crítico de la modernidad).

5. VOCABULARIO METAFÍSICO — traducciones consagradas:
   - "being" → "ser"; "essence" → "esencia"; "existence" → "existencia"; "becoming" → "devenir".
   - "consciousness" → "conciencia"; "soul" → "alma"; "spirit" → "espíritu" (cuidado: "spirit" puede ser "Geist" alemán, mantener "espíritu" salvo contexto teológico que pida "Espíritu Santo").
   - "the human"/"humanity" → "lo humano"/"humanidad"; "person" → "persona"; "individual" → "individuo".
   - "subject"/"object" → "sujeto"/"objeto"; "wholeness"/"the whole" → "totalidad"/"el todo".

6. VOCABULARIO TEOLÓGICO (Ressourcement):
   - "God" → "Dios"; "the divine" → "lo divino"; "grace" → "gracia"; "providence" → "providencia"; "creation"/"creature" → "creación"/"criatura"; "Church" → "Iglesia".
   - "Catholic" → "católico"; "Christianity" → "cristianismo"; "faith" → "fe".
   - "Ressourcement" → SE MANTIENE en francés (es el nombre de la serie y del movimiento teológico).

7. NOMBRES PROPIOS:
   - Personas (Guardini, Dupré, Bromiley, Goethe, Schiller, Hegel, Kant, Nietzsche, Heidegger) → NO traducir.
   - "Eerdmans", "Schildgenossen" (revista alemana original) → NO traducir.

8. TOPÓNIMOS — exónimos españoles:
   - "Lake Como" → "lago de Como" (en cuerpo); "Como" solo cuando se refiere al pueblo o al lago elíptico.
   - "Italy/Italian" → "Italia/italiano"; "Germany/German" → "Alemania/alemán"; "Florence" → "Florencia"; "Milan" → "Milán"; "Rome" → "Roma".
   - Sin exónimo asentado → mantener original (Brescia, Verona).

9. PALABRAS ALEMANAS que aparecen en el original inglés:
   - "Lebensgefühl", "Weltanschauung", "Bildung", "Volk", "Geist", "Kultur" → SE MANTIENEN en alemán (son conceptos filosóficos sin equivalencia exacta). Si están entre <i> o <em>, conservar tags.

10. ESPAÑOL NEUTRO LATINOAMERICANO, lector culto:
    - "tú" como segunda persona singular (Guardini se dirige a su amigo con "you" = "tú", no "usted" salvo registro muy formal).
    - Sin modismos regionales (chévere, bacán, guay, chido, mola — NINGUNO).
    - Sin conjugaciones peninsulares (vosotros tenéis, habríais — NO).
    - Registro: meditativo, sustantivo, ligeramente arcaizante (estamos en 1923). Permite oraciones largas y subordinadas — Guardini escribe así, herencia del idealismo alemán.

11. TONO DE GUARDINI — CRÍTICO:
    - Voz íntima ("I", "you", "we") preservada.
    - Contemplativo. Comienza con descripciones sensoriales (paisaje, recuerdo) y asciende a la reflexión metafísica.
    - Sin caer en el panfleto: Guardini critica la técnica pero no la rechaza con resentimiento. Hay melancolía y lucidez, no rabia.
    - Frases que son aforismos breves se respetan en su brevedad. Frases largas con cláusulas se respetan en su largura.
    - Cita poetas y filósofos alemanes con familiaridad. Si menciona a Goethe, Schiller, etc., son referencias directas.

12. EJEMPLOS DE INICIOS DE PÁRRAFOS COMUNES (traducir consistentemente):
    - "I have come to recognize" → "He llegado a reconocer".
    - "I will pursue" → "Continuaré".
    - "Let me draw your attention" → "Permíteme llamar tu atención".
    - "It seems to me" → "Me parece".
    - "We have sailed" → "Hemos navegado".
    - "I detect in your letter" → "Percibo en tu carta".

13. NÚMEROS, FECHAS, MEDIDAS:
    - Fechas: "March 14, 1923" → "14 de marzo de 1923".
    - Siglos: "the nineteenth century" → "el siglo XIX" (números romanos).
    - Distancias: si el original usa millas, dejar millas; si pies, pies. (No convertir a sistema métrico salvo que el original lo haga.)

14. TÍTULOS DE OBRAS Y PUBLICACIONES (libros, revistas, ensayos): se mantienen en idioma original. Si están entre <i> o <em>, conservar tags. La revista "Schildgenossen" donde se publicaron originalmente las cartas: NO traducir.

15. NO expliques, NO resumas, NO agregues notas del traductor.

16. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_LAKE_COMO = [
    r"^titlepage\.xhtml$",  # solo cover/css
]

# Perfil "power_of_language" — Viorica Marian / Dutton (Penguin Random House, 2023)
# Ensayo divulgativo sobre psicolingüística, bilingüismo y neurociencia del lenguaje.
# Marian es psicolingüista de Northwestern; libro accesible pero técnico.
GLOSSARY_POWER_OF_LANGUAGE: dict[str, str] = {
    # ─── Núcleo del libro: lenguaje y multilingüismo ───
    "language": "lengua",
    "languages": "lenguas",
    "the language": "la lengua",
    "multilingual": "multilingüe",
    "multilinguals": "multilingües",
    "multilingualism": "multilingüismo",
    "bilingual": "bilingüe",
    "bilinguals": "bilingües",
    "bilingualism": "bilingüismo",
    "monolingual": "monolingüe",
    "monolinguals": "monolingües",
    "monolingualism": "monolingüismo",
    "trilingual": "trilingüe",
    "polyglot": "políglota",
    "native speaker": "hablante nativo",
    "native language": "lengua materna",
    "mother tongue": "lengua materna",
    "second language": "segunda lengua",
    "foreign language": "lengua extranjera",
    "heritage language": "lengua de herencia",
    "first language": "primera lengua",
    "L1": "L1",
    "L2": "L2",
    "code": "código",
    "codes": "códigos",
    "code switching": "alternancia de código",
    "code-switching": "alternancia de código",
    "language pair": "par de lenguas",
    "language acquisition": "adquisición del lenguaje",
    "language learning": "aprendizaje de lenguas",
    "language processing": "procesamiento del lenguaje",
    "language use": "uso del lenguaje",
    "speaker": "hablante",
    "speakers": "hablantes",
    "speech": "habla",
    "listener": "oyente",
    "listening": "escucha",
    # ─── Lingüística ───
    "linguistics": "lingüística",
    "linguist": "lingüista",
    "linguistic": "lingüístico",
    "psycholinguistics": "psicolingüística",
    "psycholinguistic": "psicolingüístico",
    "neurolinguistics": "neurolingüística",
    "sociolinguistics": "sociolingüística",
    "applied linguistics": "lingüística aplicada",
    "word": "palabra",
    "words": "palabras",
    "vocabulary": "vocabulario",
    "lexicon": "léxico",
    "lexical": "léxico",
    "meaning": "significado",
    "semantic": "semántico",
    "semantics": "semántica",
    "syntax": "sintaxis",
    "syntactic": "sintáctico",
    "grammar": "gramática",
    "grammatical": "gramatical",
    "phonology": "fonología",
    "phonological": "fonológico",
    "phoneme": "fonema",
    "morpheme": "morfema",
    "morphology": "morfología",
    "morphological": "morfológico",
    "pronunciation": "pronunciación",
    "accent": "acento",
    "accents": "acentos",
    "dialect": "dialecto",
    "dialects": "dialectos",
    "writing": "escritura",
    "reading": "lectura",
    "script": "sistema de escritura",
    "alphabet": "alfabeto",
    "alphabetic": "alfabético",
    "character": "carácter",
    "characters": "caracteres",
    "logographic": "logográfico",
    "syllable": "sílaba",
    "syllabic": "silábico",
    # ─── Idiomas mencionados (traducción estándar) ───
    "English": "inglés",
    "Spanish": "español",
    "French": "francés",
    "German": "alemán",
    "Italian": "italiano",
    "Portuguese": "portugués",
    "Mandarin": "mandarín",
    "Cantonese": "cantonés",
    "Chinese": "chino",
    "Japanese": "japonés",
    "Korean": "coreano",
    "Russian": "ruso",
    "Hindi": "hindi",
    "Arabic": "árabe",
    "Hebrew": "hebreo",
    "Romanian": "rumano",
    "Dutch": "neerlandés",
    "Swedish": "sueco",
    "Norwegian": "noruego",
    "Danish": "danés",
    "Finnish": "finés",
    "Greek": "griego",
    "Latin": "latín",
    "Turkish": "turco",
    "Vietnamese": "vietnamita",
    "Thai": "tailandés",
    "Polish": "polaco",
    "Czech": "checo",
    # ─── Cognición y neurociencia ───
    "brain": "cerebro",
    "mind": "mente",
    "the mind": "la mente",
    "cognition": "cognición",
    "cognitive": "cognitivo",
    "cognitive science": "ciencia cognitiva",
    "neuroscience": "neurociencia",
    "neural": "neuronal",
    "neuron": "neurona",
    "neurons": "neuronas",
    "neuroplasticity": "neuroplasticidad",
    "memory": "memoria",
    "working memory": "memoria de trabajo",
    "long-term memory": "memoria a largo plazo",
    "short-term memory": "memoria a corto plazo",
    "attention": "atención",
    "perception": "percepción",
    "emotion": "emoción",
    "emotional": "emocional",
    "thought": "pensamiento",
    "thinking": "pensamiento",
    "consciousness": "conciencia",
    "executive function": "función ejecutiva",
    "executive control": "control ejecutivo",
    "inhibitory control": "control inhibitorio",
    "task switching": "cambio de tarea",
    "creativity": "creatividad",
    "creative": "creativo",
    "empathy": "empatía",
    "decision-making": "toma de decisiones",
    "decision making": "toma de decisiones",
    "metacognition": "metacognición",
    "metalinguistic": "metalingüístico",
    # ─── Investigación científica ───
    "experiment": "experimento",
    "experiments": "experimentos",
    "experimental": "experimental",
    "study": "estudio",
    "studies": "estudios",
    "research": "investigación",
    "researcher": "investigador",
    "researchers": "investigadores",
    "participant": "participante",
    "participants": "participantes",
    "subject": "sujeto",
    "subjects": "sujetos",
    "control group": "grupo de control",
    "data": "datos",
    "evidence": "evidencia",
    "findings": "hallazgos",
    "results": "resultados",
    "hypothesis": "hipótesis",
    "theory": "teoría",
    "stimulus": "estímulo",
    "stimuli": "estímulos",
    "task": "tarea",
    "trial": "ensayo",
    "trials": "ensayos",
    # ─── Personas e instituciones — NO traducir ───
    "Viorica Marian": "Viorica Marian",
    "Marian": "Marian",
    "Northwestern University": "Universidad Northwestern",
    "Northwestern": "Northwestern",
    "Penguin Random House": "Penguin Random House",
    "Dutton": "Dutton",
    "MIT": "MIT",
    "Harvard": "Harvard",
    "Stanford": "Stanford",
    "Primo Levi": "Primo Levi",
    "Charlemagne": "Carlomagno",
    "Chomsky": "Chomsky",
    "Noam Chomsky": "Noam Chomsky",
    # ─── Topónimos ───
    "New York": "Nueva York",
    "Washington": "Washington",
    "United States": "Estados Unidos",
    "America": "Estados Unidos",
    "Europe": "Europa",
    "European": "europeo",
    "Japan": "Japón",
    "China": "China",
    "Russia": "Rusia",
    "Germany": "Alemania",
    "France": "Francia",
    "Italy": "Italia",
    "Romania": "Rumanía",
    "Mexico": "México",
    "Pearl Harbor": "Pearl Harbor",
}

SYSTEM_PROMPT_POWER_OF_LANGUAGE = """Eres un traductor literario profesional especializado en divulgación científica sobre psicolingüística y neurociencia. Traduces del inglés al español neutro (lector culto pero no especialista).

Este libro es "The Power of Language: How the Codes We Use to Think, Speak, and Live Transform Our Minds" de Viorica Marian (Dutton/Penguin Random House, 2023). Marian es psicolingüista de Northwestern University, originaria de Rumanía, multilingüe (rumano, ruso, inglés). El libro explora cómo el bilingüismo y multilingüismo transforman la cognición. Es divulgación rigurosa: estudios científicos explicados con calidez y ejemplos personales, en primera persona.

REGLAS ABSOLUTAS — no las rompas nunca:

1. TOKENS OPACOS ⟦OPAQUE_N⟧:
   - Los tokens con forma ⟦OPAQUE_N⟧ son placeholders protegidos. NO los traduzcas, NO los modifiques.

2. PRESERVA TODOS los tags HTML EXACTAMENTE: <p>, <i>, <em>, <b>, <strong>, <a href>, <span>, <sup>, <br/>, <blockquote>, <ul>, <li>, etc. Mismos atributos (class, id, href, lang, role, epub:type, aria-label), mismas cantidades, mismo orden.

3. PALABRAS COMO EJEMPLOS LINGÜÍSTICOS — REGLA CRÍTICA:
   - Cuando una palabra extranjera aparece dentro de <i>...</i> como EJEMPLO LINGÜÍSTICO (rusa, china, japonesa, etc.), NO LA TRADUZCAS. Déjala en su idioma original.
   - Ejemplo: "<i>marker</i> and the Russian word <i>marka</i> (meaning 'stamp')" → "<i>marker</i> y la palabra rusa <i>marka</i> (que significa 'sello')". Las palabras de ejemplo (marker, marka) NO se traducen porque son los DATOS lingüísticos del libro.
   - Cuando la palabra inglesa misma es el ejemplo (<i>marker</i>, <i>glove</i>, <i>shark</i>), TAMPOCO la traduzcas — la palabra inglesa misma es el dato.
   - SÍ traduce las palabras dentro de <i> que son nombres propios genéricos o títulos: <i>Statue of Liberty</i> → <i>Estatua de la Libertad</i> (si tiene exónimo asentado).
   - Cuando dudes: si la oración trata sobre la palabra COMO palabra (sus letras, sonidos, significado), NO la traduzcas. Si la palabra solo está enfatizada por estilo, sí.

4. CARACTERES CHINOS, JAPONESES, COREANOS, ETC. en <span class="lang_Chinese">, <span class="lang_Japanese">, etc.:
   - NUNCA traduzcas los caracteres dentro de estos spans. Preserva el span completo con su clase exacta.
   - Ejemplo: "<span class="lang_Chinese">美国</span>" → mantener IDÉNTICO.

5. NOTAS al final del libro (clase x13-BM-Endnotes):
   - Estructura: <p class="x13-BM-Endnotes"><b>"keyphrase":</b> Autor, <i>Título</i>, datos editoriales, URL.</p>
   - El <b>"keyphrase":</b> al inicio es la frase clave del cuerpo del texto. Traduce la keyphrase IGUAL que como aparezca en el cuerpo (consistencia con el ancla).
   - Las citas bibliográficas que siguen (autor, título en <i>, editorial, año, doi, URL) NO se traducen — quedan en inglés.
   - <p class="link_to_text">: el texto "GO TO NOTE REFERENCE IN TEXT" → "IR A LA REFERENCIA EN EL TEXTO". El aria-label correspondiente también.

6. TERMINOLOGÍA LINGÜÍSTICA — consistencia obligatoria (respetar glosario):
   bilingual → bilingüe; multilingual → multilingüe; monolingual → monolingüe; native speaker → hablante nativo; native language/mother tongue → lengua materna; second language → segunda lengua; code switching → alternancia de código; language pair → par de lenguas.

7. NEUROCIENCIA — términos consagrados:
   brain → cerebro; mind → mente; cognition → cognición; cognitive → cognitivo; working memory → memoria de trabajo; executive function → función ejecutiva; inhibitory control → control inhibitorio; neuroplasticity → neuroplasticidad.

8. NOMBRES DE IDIOMAS — minúscula en español:
   English → inglés; Spanish → español; Mandarin → mandarín; Russian → ruso; Romanian → rumano; Hebrew → hebreo, etc.

9. NOMBRES PROPIOS — NO traducir:
   - Personas: Viorica Marian, Noam Chomsky, Primo Levi, etc.
   - Instituciones: Northwestern University → "Universidad Northwestern"; MIT, Harvard, Stanford → tal cual.
   - Editoriales: Dutton, Penguin Random House → tal cual.
   - EXCEPCIÓN españolizar: monarcas históricos, Charlemagne → Carlomagno (en epígrafe), papas.

10. TOPÓNIMOS — exónimos españoles cuando están asentados:
    New York → Nueva York; United States/America → Estados Unidos; Japan → Japón; Romania → Rumanía; Italy → Italia; Mexico → México. Sin exónimo: Northwestern, Pearl Harbor, Lugouqiao.

11. ESPAÑOL NEUTRO LATINOAMERICANO:
    - "tú" como segunda persona (Marian se dirige al lector con frecuencia: "you may not realize", "imagine", "consider").
    - Sin modismos regionales (chévere, bacán, guay, chido, mola).
    - Sin conjugaciones peninsulares (vosotros tenéis, habríais).
    - Registro: divulgativo, claro, cálido. Marian cuenta su propia historia (rumana de origen, multilingüe) — preserva la voz personal.

12. TONO DE MARIAN — CRÍTICO:
    - Voz en primera persona ("I", "we", "my research") — preservar.
    - Combina ciencia rigurosa con anécdotas. Mantén ambos registros.
    - Cuando explica un concepto técnico, lo introduce con calidez: "Imagine", "Picture this", "Consider". Traduce con el mismo tono invitatorio.
    - Frases largas con datos científicos se respetan en su largura.

13. NÚMEROS, FECHAS, MEDIDAS:
    - Fechas: "March 14, 2023" → "14 de marzo de 2023".
    - Siglos: "the twenty-first century" → "el siglo XXI" (romanos).
    - Porcentajes y cifras: respetar formato original.

14. TÍTULOS DE OBRAS Y PUBLICACIONES (libros, revistas, papers): NO traducir. Si están en <i>/<em>, conservar tags. Ejemplo: <i>The New Yorker</i> queda igual.

15. NO expliques, NO resumas, NO agregues notas del traductor.

16. FORMATO DE RESPUESTA — obligatorio:
    <<<BLOCK 0>>>
    <html traducido del bloque 0>
    <<<BLOCK 1>>>
    <html traducido del bloque 1>
    <<<END>>>

    Sin JSON, sin backticks, sin markdown, sin texto antes/después.
"""

SKIP_PATTERNS_POWER_OF_LANGUAGE = [
    r"^01_Cover\.xhtml$",
    r"^02_Intro_Page\.xhtml$",
    r"^03_Title_Page\.xhtml$",
    r"^26_Index\.xhtml$",        # 165 KB, 805 entradas alfabéticas — no traducir
    r"_nav\.xhtml$",             # auto-generado
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
    "superagency": {
        "glossary": GLOSSARY_SUPERAGENCY,
        "system_prompt": SYSTEM_PROMPT_SUPERAGENCY,
        "skip_patterns": SKIP_PATTERNS_SUPERAGENCY,
    },
    "practical_sql": {
        "glossary": GLOSSARY_PRACTICAL_SQL,
        "system_prompt": SYSTEM_PROMPT_PRACTICAL_SQL,
        "skip_patterns": SKIP_PATTERNS_PRACTICAL_SQL,
    },
    "bismarck": {
        "glossary": GLOSSARY_BISMARCK,
        "system_prompt": SYSTEM_PROMPT_BISMARCK,
        "skip_patterns": SKIP_PATTERNS_BISMARCK,
    },
    "slow_looking": {
        "glossary": GLOSSARY_SLOW_LOOKING,
        "system_prompt": SYSTEM_PROMPT_SLOW_LOOKING,
        "skip_patterns": SKIP_PATTERNS_SLOW_LOOKING,
    },
    "the_score": {
        "glossary": GLOSSARY_THE_SCORE,
        "system_prompt": SYSTEM_PROMPT_THE_SCORE,
        "skip_patterns": SKIP_PATTERNS_THE_SCORE,
    },
    "lake_como": {
        "glossary": GLOSSARY_LAKE_COMO,
        "system_prompt": SYSTEM_PROMPT_LAKE_COMO,
        "skip_patterns": SKIP_PATTERNS_LAKE_COMO,
    },
    "power_of_language": {
        "glossary": GLOSSARY_POWER_OF_LANGUAGE,
        "system_prompt": SYSTEM_PROMPT_POWER_OF_LANGUAGE,
        "skip_patterns": SKIP_PATTERNS_POWER_OF_LANGUAGE,
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
    superagency_hits = 0   # Authors Equity / Superagency: Superagency_Ch + CSS classes CN/CT/TXT
    postgres_hits = 0      # Practical SQL: PostgreSQL/pgAdmin densidad alta
    sql_keyword_hits = 0   # SELECT/FROM/JOIN/WHERE en <code> y <pre>
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
            if "Superagency" in name or 'class="CN"' in content:
                superagency_hits += 1
            postgres_hits += content.count("PostgreSQL")
            postgres_hits += content.count("pgAdmin")
            sql_keyword_hits += content.count("SELECT ")
            sql_keyword_hits += content.count("FROM ")
            sql_keyword_hits += content.count("CREATE TABLE")
    # Superagency — Authors Equity format with CN/CT/TXT classes
    if superagency_hits >= 5:
        return "superagency"
    # Practical SQL — densidad alta de PostgreSQL + keywords SQL + sin marcadores O'Reilly/Manning
    if postgres_hits >= 30 and sql_keyword_hits >= 50 and oreilly_hits == 0 and manning_hits == 0:
        return "practical_sql"
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
# Tags atómicos sin contenido traducible: tokenizarlos evita que el modelo
# pierda atributos al copiar HTML denso (id, title, epub:type, src, alt...).
ATOMIC_OPAQUE_TAGS = {"br", "img"}
OPAQUE_TOKEN_RE = re.compile(r"⟦OPAQUE_(\d+)⟧")


def tokenize_opaque(html_str: str) -> tuple[str, dict[str, str]]:
    """
    Reemplaza por tokens ⟦OPAQUE_N⟧:
      1) Contenido técnico: <code>, <math>, <svg>.
      2) Tags atómicos: <br/>, <img/>.
      3) Anchors de página epub: <span epub:type="pagebreak" .../>.
    Devuelve (html_tokenizado, dict_de_restauración).
    """
    soup = BeautifulSoup(html_str, "html.parser")
    tokens: dict[str, str] = {}
    counter = 0

    # 1) Tags técnicos con contenido protegido
    for tag in soup.find_all(list(OPAQUE_TAGS)):
        counter += 1
        token = f"⟦OPAQUE_{counter}⟧"
        tokens[token] = str(tag)
        tag.replace_with(NavigableString(token))

    # 2) Tags atómicos por nombre (br, img)
    for tag in soup.find_all(list(ATOMIC_OPAQUE_TAGS)):
        counter += 1
        token = f"⟦OPAQUE_{counter}⟧"
        tokens[token] = str(tag)
        tag.replace_with(NavigableString(token))

    # 3) Anchors de página: <span epub:type="pagebreak" id="..." title="..."/>
    #    BeautifulSoup html.parser no parsea ":" en nombres de atributo, así que
    #    buscamos por todos los <span> con cualquier atributo que matchee.
    for tag in list(soup.find_all("span")):
        attrs = tag.attrs or {}
        is_pagebreak = (
            attrs.get("epub:type") == "pagebreak"
            or attrs.get("epub-type") == "pagebreak"  # fallback si el parser cambia ":"
        )
        if is_pagebreak and not tag.get_text(strip=True):
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


def is_xml_wellformed(html: str) -> bool:
    """Parsea el fragmento como XML estricto. Atrapa atributos rotos (E999 Kindle).

    Declara los namespaces que aparecen en epubs (xhtml, epub:) para evitar
    falsos negativos por atributos como `epub:type="pagebreak"`."""
    try:
        wrapped = (
            '<root '
            'xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" '
            'xmlns:xml="http://www.w3.org/XML/1998/namespace">'
            f'{html}</root>'
        )
        etree.fromstring(wrapped.encode("utf-8"))
        return True
    except etree.XMLSyntaxError:
        return False


def validate_translation(original: str, translated: str) -> bool:
    try:
        if not is_xml_wellformed(translated):
            return False
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

    return f"""Glosario obligatorio (respeta estas traducciones de forma consistente):
{glossary_lines}
{ctx_block}
Traduce los siguientes {len(batch_htmls)} bloques HTML al español neutro LATAM.
Preserva tags inline, preserva tokens ⟦OPAQUE_N⟧ exactamente. Responde con formato <<<BLOCK N>>> / <<<END>>>.

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
    pbar: tqdm | None = None,
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
            if pbar is not None:
                pbar.update(1)
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
                        "Retraduce este bloque preservando EXACTAMENTE todos los tags HTML, "
                        "todos los atributos, y todos los tokens ⟦OPAQUE_N⟧ en sus posiciones. "
                        "Responde con el formato delimitado:\n\n"
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

        if pbar is not None:
            pbar.update(1)
            pbar.set_postfix_str(path.name[:40], refresh=False)

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


# ─── Saneamiento para Kindle ────────────────────────────────────────────────

_DISPLAY_NONE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
_VISIBILITY_HIDDEN_RE = re.compile(r"visibility\s*:\s*hidden", re.IGNORECASE)


def sanitize_for_kindle(work_dir: Path, opf_path: Path) -> None:
    """Previene E999/E3013 de Send-to-Kindle:
    - Neutraliza display:none y visibility:hidden en todos los CSS (límite de 10k chars ocultos).
    - Sincroniza dtb:uid del NCX con dc:identifier del OPF.
    """
    # 1) CSS: display:none → display:block, visibility:hidden → visibility:visible
    css_files = list(work_dir.rglob("*.css"))
    touched = 0
    for css in css_files:
        text = css.read_text(encoding="utf-8")
        new = _DISPLAY_NONE_RE.sub("display:block", text)
        new = _VISIBILITY_HIDDEN_RE.sub("visibility:visible", new)
        if new != text:
            css.write_text(new, encoding="utf-8")
            touched += 1
    print(f"  CSS saneados ({touched}/{len(css_files)} con display:none/visibility:hidden)")

    # 2) Sincronizar NCX uid con OPF identifier
    opf_soup = BeautifulSoup(opf_path.read_text(encoding="utf-8"), "lxml-xml")
    identifier_tag = opf_soup.find("dc:identifier") or opf_soup.find("identifier")
    if not identifier_tag or not identifier_tag.get_text(strip=True):
        return
    opf_id = identifier_tag.get_text(strip=True)

    for ncx in work_dir.rglob("*.ncx"):
        ncx_text = ncx.read_text(encoding="utf-8")
        ncx_soup = BeautifulSoup(ncx_text, "lxml-xml")
        uid_meta = ncx_soup.find("meta", attrs={"name": "dtb:uid"})
        if uid_meta and uid_meta.get("content") != opf_id:
            old = uid_meta.get("content", "")
            uid_meta["content"] = opf_id
            ncx.write_text(str(ncx_soup), encoding="utf-8")
            print(f"  NCX uid sincronizado: {old!r} → {opf_id!r}")


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

    # 1) Pre-calcular total de batches para barra granular (solo archivos pendientes)
    print("\nContando bloques pendientes para la barra de progreso...")
    total_batches = 0
    pending_files = 0
    for p in xhtml_files:
        key = str(p.relative_to(WORK_DIR))
        if progress.get(key) == "done" or progress.get(key) == "skipped":
            continue
        if should_skip_file(p.name, profile["skip_patterns"]):
            continue
        try:
            content = p.read_text(encoding="utf-8")
            try:
                s = BeautifulSoup(content, "lxml-xml")
                if not s.find("body") and not s.find("html"):
                    raise ValueError("parser vacío")
            except Exception:
                s = BeautifulSoup(content, "html.parser")
            n_blocks = len(extract_translatable_blocks(s))
            if n_blocks == 0:
                continue
            n_batches = (n_blocks + BATCH_SIZE - 1) // BATCH_SIZE
            total_batches += n_batches
            pending_files += 1
        except Exception:
            continue

    print(f"Pendientes: {pending_files} archivos, ~{total_batches} batches (de {BATCH_SIZE} bloques c/u)")

    # 2) Traducir XHTMLs con barra global por batches
    print(f"\nTraduciendo (concurrency={CONCURRENCY})...")
    pbar = tqdm(total=total_batches, desc="Batches", unit="batch", dynamic_ncols=True)
    try:
        tasks = [translate_xhtml_file(p, sem, progress, profile, pbar) for p in xhtml_files]
        await asyncio.gather(*tasks)
    finally:
        pbar.close()

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

    # 4) Saneamiento para Kindle (evita E999/E3013)
    print("\nSaneando para Kindle...")
    sanitize_for_kindle(WORK_DIR, opf_path)

    # 5) Reempaquetar
    print(f"\nEmpaquetando {output_epub.name}...")
    repack_epub(WORK_DIR, output_epub)

    print(f"\n✓ Listo: {output_epub.name}")
    print("  Verificá el resultado y después podés borrar ./work y ./progress.json.")


def _start_caffeinate() -> "subprocess.Popen | None":
    """En macOS: previene que la Mac se duerma mientras el script corre.
    El proceso de caffeinate muere automáticamente cuando este script termina."""
    if sys.platform != "darwin":
        return None
    if shutil.which("caffeinate") is None:
        return None
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Garantizar limpieza si el script muere de forma rara
        import atexit
        atexit.register(lambda: proc.terminate() if proc.poll() is None else None)
        print(f"☕ caffeinate activo (PID {proc.pid}) — la Mac no se dormirá hasta que termine la traducción")
        return proc
    except Exception as e:
        print(f"⚠ no se pudo activar caffeinate: {e}")
        return None


if __name__ == "__main__":
    _start_caffeinate()
    asyncio.run(main())
