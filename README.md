<div align="center">

# 📚 epub-translator

**Traductor automático de libros EPUB (EN → ES LATAM) con Claude AI**

Traduce libros EPUB completos del inglés al español latinoamericano neutro, preservando estructura HTML, bloques de código, imágenes y contenido técnico.

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![Claude](https://img.shields.io/badge/Claude-Agent_SDK-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![BeautifulSoup](https://img.shields.io/badge/BeautifulSoup-4-43B02A?style=for-the-badge&logo=python&logoColor=white)](https://www.crummy.com/software/BeautifulSoup/)
[![lxml](https://img.shields.io/badge/lxml-strict_XML-0E7C86?style=for-the-badge&logo=xml&logoColor=white)](https://lxml.de/)
[![License](https://img.shields.io/badge/LICENSE-MIT-green?style=for-the-badge)](LICENSE)

[![Kindle-ready](https://img.shields.io/badge/📱_Kindle--ready-Send--to--Kindle_✓-FF9900?style=flat-square)](https://www.amazon.com/sendtokindle)
[![EPUB 3](https://img.shields.io/badge/EPUB-3.0-85C1E9?style=flat-square&logo=epubjs&logoColor=white)](https://www.w3.org/TR/epub-33/)
[![API Cost](https://img.shields.io/badge/API_cost-$0_(Max_plan)-success?style=flat-square)](https://www.anthropic.com/pricing)
[![Resumable](https://img.shields.io/badge/Resumable-checkpointed-blueviolet?style=flat-square)](#reanudar-una-traducción-interrumpida)
[![Last commit](https://img.shields.io/github/last-commit/stivenrosales/epub-translator?style=flat-square)](https://github.com/stivenrosales/epub-translator/commits/main)
[![GitHub stars](https://img.shields.io/github/stars/stivenrosales/epub-translator?style=flat-square)](https://github.com/stivenrosales/epub-translator/stargazers)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](https://github.com/stivenrosales/epub-translator/pulls)

<br/>

*Traduce libros técnicos y de no-ficción completos — con glosarios por perfil, protección de código, validación estructural automática y saneamiento Kindle.*

---

</div>

## 🧠 ¿Qué es epub-translator?

Una herramienta CLI que traduce libros EPUB completos usando **Claude Sonnet** a través del Agent SDK. No es un traductor genérico — es un sistema inteligente que **detecta el tipo de libro**, aplica el perfil correcto, y protege todo el contenido técnico que no debe traducirse.

### ✨ Características principales

- 🔍 **Sistema multi-perfil** — auto-detecta el tipo de libro y aplica la estrategia correcta
- 🛡️ **Code-aware** — `<pre>`, `<code>`, `<math>`, `<svg>` nunca se tocan
- 🔒 **Tokenización opaca** — los tags `<code>` inline se reemplazan con tokens `⟦OPAQUE_N⟧` antes de traducir, y se restauran después
- 📖 **Glosario obligatorio por perfil** — terminología consistente en todo el libro
- ✅ **Validación XML estricta** — cada bloque traducido pasa por `lxml` antes de aceptarse (atrapa atributos rotos que disparan E999 en Kindle)
- 💾 **Reanudable** — checkpoint en `progress.json`, si se interrumpe se retoma donde quedó
- 📦 **EPUB válido** — mimetype primero (sin compresión), `dc:language` correcto, traducción de NCX
- 📱 **Kindle-ready automático** — saneamiento previo al empaquetado: neutraliza `display:none` (E3013), sincroniza NCX uid ↔ OPF identifier (NCX-001)

---

## 📋 Perfiles disponibles

| Perfil | Editorial | Tipo de libro | Ejemplo |
|--------|-----------|---------------|---------|
| `generic` | Penguin, HarperCollins, etc. | No-ficción, negocios, creatividad | *The Practice* — Seth Godin |
| `ai_engineering` | O'Reilly | Técnico con código, math y terminología ML | *AI Engineering* — Chip Huyen |
| `grokking_algorithms` | Manning | CS ilustrado con código Python | *Grokking Algorithms* — Aditya Bhargava |
| `superagency` | Simon & Schuster | Ensayo sobre AI y sociedad | *Superagency* — Reid Hoffman |
| `practical_sql` | No Starch Press | Técnico con SQL y análisis de datos | *Practical SQL* — Anthony DeBarros |

Cada perfil incluye su propio **glosario de términos**, **system prompt** adaptado al tono del autor, y **patrones de archivos a saltar**.

---

## 🚀 Instalación

### Requisitos previos

- **Python 3.10+**
- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** con suscripción Max activa
  > El script usa el Agent SDK, que se autentica con tu sesión de Claude Code. No necesita API key y tiene costo incremental $0.

### Setup

```bash
git clone https://github.com/stivenrosales/epub-translator.git
cd epub-translator

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 💻 Uso

### Traducción básica

```bash
# Activar el entorno virtual
source .venv/bin/activate

# Colocar el .epub en la carpeta del proyecto y ejecutar
python3 translate_epub.py
```

Si hay múltiples EPUBs, aparece un menú de selección. El archivo traducido se guarda como `<nombre_original>_es.epub`.

### Reanudar una traducción interrumpida

```bash
# Simplemente volver a ejecutar — lee progress.json y salta archivos ya traducidos
python3 translate_epub.py
```

### Empezar de cero con un libro nuevo

```bash
# Limpiar estado anterior e iniciar
rm -rf work progress.json
python3 translate_epub.py
```

### Forzar un perfil específico

Editar `translate_epub.py` y cambiar:

```python
FORCE_PROFILE = "grokking_algorithms"  # o "generic", "ai_engineering", "superagency", "practical_sql"
```

---

## ⚙️ ¿Cómo funciona?

### Pipeline general

```mermaid
flowchart LR
    A[📄 EPUB fuente] --> B[📂 Extraer a work/]
    B --> C[🔎 Detectar perfil]
    C --> D[📑 Parsear OPF spine]
    D --> E[🔁 Traducir XHTMLs]
    E --> F[📑 Traducir NCX]
    F --> G[🌐 Actualizar OPF<br/>dc:language es-419]
    G --> H[🧼 Sanitizar Kindle]
    H --> I[📦 Empaquetar _es.epub]
    I --> J[✅ EPUB válido<br/>Kindle-ready]

    style A fill:#FFE4B5,stroke:#E67E22,color:#000
    style H fill:#FFD6E0,stroke:#E91E63,color:#000
    style J fill:#C8E6C9,stroke:#388E3C,color:#000
```

### Loop de traducción por bloque

```mermaid
flowchart TD
    A[Bloque HTML original] --> B[🔒 tokenize_opaque<br/>code/math/svg → ⟦OPAQUE_N⟧]
    B --> C[📦 Batch de 25 bloques]
    C --> D[🤖 call_claude<br/>system prompt + glosario]
    D --> E[📥 parse_delimited_response<br/>BLOCK N / END ]
    E --> F[🔓 restore_opaque<br/>reinjecta contenido técnico]
    F --> G{is_xml_wellformed?}
    G -->|❌ roto| R[🔁 retry]
    G -->|✅ ok| H{tag_signature coincide?}
    H -->|❌ no| R
    H -->|✅ sí| I[💾 Aceptado]
    R --> C

    style G fill:#FFE4B5,stroke:#E67E22,color:#000
    style H fill:#FFE4B5,stroke:#E67E22,color:#000
    style I fill:#C8E6C9,stroke:#388E3C,color:#000
    style R fill:#FFCDD2,stroke:#C62828,color:#000
```

### Saneamiento Kindle (post-procesamiento)

```mermaid
flowchart LR
    A[work/ traducido] --> B[🧼 sanitize_for_kindle]
    B --> C[🎨 CSS<br/>display:none → block<br/>visibility:hidden → visible]
    B --> D[🔗 NCX dtb:uid ↔<br/>OPF dc:identifier]
    C --> E[Previene E3013]
    D --> F[Previene NCX-001]
    E --> G[📦 repack_epub]
    F --> G
    G --> H[📱 Kindle acepta]

    style B fill:#FFD6E0,stroke:#E91E63,color:#000
    style E fill:#FFF9C4,stroke:#F9A825,color:#000
    style F fill:#FFF9C4,stroke:#F9A825,color:#000
    style H fill:#C8E6C9,stroke:#388E3C,color:#000
```

### Tabla de pasos

| Paso | Descripción |
|------|-------------|
| **1. Extraer** | Descomprime el EPUB en `work/` |
| **2. Detectar perfil** | Escanea patrones de markup (Manning vs O'Reilly vs genérico) |
| **3. Extraer bloques** | Encuentra `<p>`, `<h1>`–`<h6>`, `<li>`, etc. excluyendo subárboles `<pre>`, `<math>`, `<svg>` |
| **4. Tokenizar** | Reemplaza `<code>`, `<math>`, `<svg>` inline con placeholders `⟦OPAQUE_N⟧` |
| **5. Traducir** | Envía batches de 25 bloques a Claude Sonnet con contexto rolling y glosario |
| **6. Validar XML** | `lxml.etree` parsea cada bloque — si rompe un tag, retry |
| **7. Validar estructura** | Compara firma de tags (nombres, atributos, conteo) entre original y traducción |
| **8. Restaurar** | Reinserta contenido opaco original en las posiciones de tokens |
| **9. Sanear Kindle** | Neutraliza `display:none`/`visibility:hidden` en CSS, sincroniza NCX uid |
| **10. Empaquetar** | Construye EPUB válido (mimetype sin comprimir primero, según spec) |

---

## 📱 Compatibilidad con Kindle

Send-to-Kindle es notoriamente opaco — devuelve **E999 Internal Error** sin decir qué falla. Después de depurar varios libros, el script ahora previene automáticamente las tres causas reales:

| Error Kindle | Causa real | Fix automático |
|--------------|-----------|----------------|
| **E999** | Atributo HTML roto por la traducción (ej. `itemid` partido en varios atributos) | `is_xml_wellformed()` valida cada bloque con `lxml` y fuerza retry |
| **E3013** | >10k caracteres ocultos con `display:none` (TOCs/landmarks gigantes) | `sanitize_for_kindle()` convierte todo `display:none` → `display:block` |
| **NCX-001** | `dtb:uid` del NCX ≠ `dc:identifier` del OPF | `sanitize_for_kindle()` copia el valor del OPF al NCX |

> 💡 **Tip de diagnóstico**: si aparece algún E999 en un libro futuro, usá [Kindle Previewer 3](https://kdp.amazon.com/en_US/help/topic/G202131170) — es la única herramienta que expone el error real detrás del genérico de Amazon.

---

## 🆕 Agregar un nuevo perfil

Para soportar un nuevo tipo de libro, agregá 3 cosas en `translate_epub.py`:

1. **`GLOSSARY_*`** — diccionario con traducciones obligatorias de términos
2. **`SYSTEM_PROMPT_*`** — prompt del sistema con reglas de traducción y guía de tono
3. **`SKIP_PATTERNS_*`** — lista de regex de nombres de archivo a saltar

Registrá el perfil en el diccionario `PROFILES` y actualizá `detect_book_profile()` si querés auto-detección.

---

## ⚠️ Limitaciones

| Limitación | Detalle |
|------------|---------|
| 🖼️ Imágenes con texto | Los diagramas y figuras rasterizadas quedan en inglés — esto es un traductor DOM, no OCR |
| 🎯 Calidad semántica | La validación estructural detecta HTML roto, pero no errores de significado |
| ⏱️ Rate limits | El batch size y concurrencia están limitados por el Agent SDK |
| 👁️ Contenido oculto visible | El saneamiento Kindle hace visibles bloques que estaban `display:none` (landmarks, listas de figuras, etc.) — aparecen como secciones extras en el TOC |

---

## 📄 Licencia

Este proyecto está bajo la licencia **MIT** — ver [LICENSE](LICENSE) para más detalles.

---

<div align="center">

**Hecho con 🧉 y Claude AI**

</div>
