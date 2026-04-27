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

### Técnicos / divulgación tecnológica

| Perfil | Editorial | Tipo de libro | Ejemplo |
|--------|-----------|---------------|---------|
| `generic` | Penguin, HarperCollins, etc. | No-ficción, negocios, creatividad | *The Practice* — Seth Godin |
| `ai_engineering` | O'Reilly | Técnico con código, math y terminología ML | *AI Engineering* — Chip Huyen |
| `grokking_algorithms` | Manning | CS ilustrado con código Python | *Grokking Algorithms* — Aditya Bhargava |
| `practical_sql` | No Starch Press | Técnico con SQL y análisis de datos | *Practical SQL* — Anthony DeBarros |

### Ensayo, biografía y filosofía

| Perfil | Editorial | Tipo de libro | Ejemplo |
|--------|-----------|---------------|---------|
| `superagency` | Authors Equity | Ensayo sobre IA y sociedad | *Superagency* — Reid Hoffman |
| `bismarck` | Oxford University Press | Biografía histórica académica | *Bismarck: A Life* — Jonathan Steinberg |
| `slow_looking` | Routledge | Educación + arte + ciencia (Project Zero) | *Slow Looking* — Shari Tishman |
| `the_score` | Penguin / Dutton | Filosofía social, juegos y métricas | *The Score* — C. Thi Nguyen |
| `lake_como` | Eerdmans (Ressourcement) | Filosofía-teología sobre técnica y naturaleza | *Letters from Lake Como* — Romano Guardini |
| `power_of_language` | Penguin / Dutton | Psicolingüística y bilingüismo | *The Power of Language* — Viorica Marian |

Cada perfil incluye su propio **glosario de términos** (a veces de varios cientos), **system prompt** adaptado al tono del autor, y **patrones de archivos a saltar** (por ej. índices alfabéticos masivos que no aportan valor traducidos).

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
FORCE_PROFILE = "the_score"  # cualquiera de los 10 perfiles disponibles
```

### Dashboard en vivo (read-only)

En otra terminal, mientras corre la traducción:

```bash
python3 dashboard.py
```

Muestra un panel TUI con `rich`: barra de progreso global con ETA real (calculado por throughput observado, no el inflado de `tqdm`), conteos `done`/`partial`/`pending`/`skipped` por archivo, eventos recientes con timestamps (warns, errores, fases de cierre), y estado del log y `progress.json`. **Read-only**: ábrelo y ciérralo cuando quieras, no afecta la traducción. `Ctrl+C` cierra solo el dashboard. Auto-detecta el `translate*.log` más reciente.

### Mantener la Mac despierta automáticamente

En macOS, `translate_epub.py` lanza `caffeinate -i -s -w PID` al inicio para evitar que la máquina se duerma durante traducciones largas. El proceso se cierra solo cuando termina el script. Si estás en Linux/Windows, no se hace nada (silencioso).

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
| **4. Tokenizar** | Reemplaza por `⟦OPAQUE_N⟧`: `<code>`, `<math>`, `<svg>`, `<br/>`, `<img/>`, `<span epub:type="pagebreak">` |
| **5. Traducir** | Envía batches de 25 bloques a Claude Sonnet con contexto rolling y glosario |
| **6. Validar XML** | `lxml.etree` parsea cada bloque (con namespaces `xhtml` y `epub:` declarados) — si rompe un tag, retry |
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

## 🧪 Lecciones del proyecto

Cada libro reveló algo nuevo sobre cómo traducir EPUBs con LLMs sin perder fidelidad estructural.

### 1. El namespace `epub:` rompía la validación silenciosamente

`is_xml_wellformed()` envolvía cada fragmento con `xmlns="…/xhtml"` pero **no declaraba `xmlns:epub`**. Resultado: cualquier bloque con `<span epub:type="pagebreak"/>` fallaba la validación XML aunque estuviera bien traducido. Era el villano oculto detrás de muchos rechazos. **Fix**: declarar también `xmlns:epub` y `xmlns:xml` en el wrapper. Eso solo bajó el ratio de errores en libros con paginación rica de ~12 % a ~1 %.

### 2. Tokenización extendida a elementos atómicos

Pedirle al modelo que copie literalmente `<span epub:type="pagebreak" id="page_42" title="42"/>` (56 caracteres con tres atributos) era una invitación al error. La solución no era un prompt más detallado: era hacer que el modelo **no vea ese HTML**. Agregamos `<br/>`, `<img/>` y `<span epub:type="pagebreak"/>` al pool de tokens opacos `⟦OPAQUE_N⟧`. El modelo ve un placeholder de 11 caracteres y lo reproduce sin alterar atributos. Determinístico, cero riesgo nuevo.

### 3. Prompts concisos > prompts detallados

Cuando un libro empezó a fallar, la tentación era agregar más reglas al system prompt. Empíricamente: **a más reglas, peor resultado**. Pasar de 6.5k a 9.2k caracteres de prompt **dobló la tasa de errores**. Sonnet rinde mejor con instrucciones cortas y enfáticas en lo crítico. La regla de los 22 nombres de clase CSS específicos es atención mal gastada — preferí confiar en la regla genérica "preserva todos los tags y atributos".

### 4. Skip masivo > traducir todo

El índice analítico (`<index>`) de un libro académico puede ocupar el 30-40% del peso del epub y aportar valor cero al lector hispanohablante (que busca con `Cmd+F` por texto, no por entradas alfabéticas en inglés). Skipearlo ahorra cuota Max y elimina cientos de bloques candidatos a errores estructurales.

### 5. Retry quirúrgico paralelo para fallos masivos

Cuando un libro queda con 100+ bloques en inglés (porque su markup era especialmente denso, ej. *Slow Looking*), la solución más eficiente es **dividir los bloques en chunks balanceados y lanzar varios subagentes Sonnet en paralelo** (un Agent tool con 5 invocaciones simultáneas). Cada uno devuelve un JSON con `inner_es`. Los pegas con un merge robusto por `(archivo, idx)` que ignora bloques "extras" que algunos modelos inventan, y aplicas un mini-retry final para los que falten.

### 6. `json-repair` como red de seguridad

Sonnet a veces escribe JSON con comillas dobles sin escapar dentro de strings (`"miren..."` en vez de `\"miren...\"`). Antes de fallar el pipeline entero, pasa el output por [`json-repair`](https://github.com/mangiucugna/json_repair) — repara casos comunes y permite continuar.

### 7. Versos y citas poéticas: bilingüe en línea

Para los 24 versos de *The Power of Language* implementé un patrón que respeta la voz original sin sacrificar la lectura: **el verso original arriba con su clase CSS intacta, y debajo la traducción del modelo en cursiva atenuada** (`opacity: 0.75`). El lector ve ambos; los lectores epub respetan el inline style.

### 8. Pre-análisis estructural antes de armar el perfil

Llegué a esto **después** de fallar con *Slow Looking* (asumí markup similar a Bismarck y me equivoqué). Antes de crear el glosario o el system prompt, conviene contar `<i>/<p>`, `pagebreaks`, `<br/>` agrupados, clases CSS dominantes y mirar samples de cada tipo de archivo. Diez minutos de análisis ahorran horas de retry.

---

## 📄 Licencia

Este proyecto está bajo la licencia **MIT** — ver [LICENSE](LICENSE) para más detalles.

---

<div align="center">

**Hecho con 🧉 y Claude AI**

</div>
