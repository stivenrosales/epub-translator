# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CLI tool that translates EPUB books from English to Latin American Spanish (es-419) using the Claude Agent SDK. Runs on Claude Max subscription (no API billing). Single-file Python application (`translate_epub.py`, ~4,840 lines).

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Drop the source EPUB in the project ROOT (this is the inbox — see Repository Layout)
cp ~/Downloads/some_book.epub .

# 2. Run translation (interactive menu if several EPUBs sit in the root)
python3 translate_epub.py

# 3. File the output away when the run finishes
mv some_book_es.epub libros/traducidos/
mv some_book.epub    libros/originales/

# Live progress of a running translation (reads progress.json + newest log)
python3 dashboard.py

# Enrich an already-translated book (reading guides + concept maps)
python3 enrich_epub.py libros/traducidos/some_book_es.epub
```

There are no tests, linting, or build steps configured.

## Architecture

### Translation Pipeline

`main()` orchestrates an 8-step pipeline:

1. **Source selection** (`find_source_epub`) — menu-based EPUB picker, excludes `*_es.epub`
2. **Profile detection** (`detect_book_profile`) — pattern-matches markup for O'Reilly/Manning/generic
3. **EPUB extraction** (`extract_epub`) — unzips to `./work/`
4. **OPF/spine parsing** (`find_opf_path` → `find_xhtml_files`) — ordered XHTML file list from `content.opf`
5. **Per-file translation** (`translate_xhtml_file`) — core loop:
   - `extract_translatable_blocks` → leaf blocks (`p`, `h1`-`h6`, `li`, `td`, etc.)
   - Batches of `BATCH_SIZE` blocks → `translate_batch`:
     - `tokenize_opaque` → replaces `<code>`, `<math>`, `<svg>` with `⟦OPAQUE_N⟧`
     - `call_claude` → Agent SDK call with profile system prompt + glossary
     - `parse_delimited_response` → extracts `<<<BLOCK N>>>...<<<END>>>` chunks
     - `restore_opaque` → reinjects original technical content
     - `validate_translation` via `tag_signature` → structural hash comparison
6. **TOC translation** (`translate_ncx`) — navLabel/text nodes
7. **Metadata update** (`update_opf`) — sets `dc:language` to `es-419`
8. **Repack** (`repack_epub`) — valid EPUB with uncompressed mimetype first

### Profile System

Each profile provides three things: `GLOSSARY_*`, `SYSTEM_PROMPT_*`, `SKIP_PATTERNS_*`. Registered in the `PROFILES` dict.

| Profile | Target | Glossary |
|---------|--------|----------|
| `generic` | Literary non-fiction (Seth Godin) | 17 |
| `ai_engineering` | O'Reilly ML/AI technical | 64 |
| `grokking_algorithms` | Manning CS illustrated | 92 |
| `superagency` | AI/society non-fiction | 69 |
| `practical_sql` | No Starch SQL technical | 246 |
| `bismarck` | Political biography / German history | 383 |
| `slow_looking` | Art observation / pedagogy | 171 |
| `the_score` | Narrative non-fiction | 208 |
| `lake_como` | Travel / place writing | 145 |
| `power_of_language` | Linguistics / popular science | 191 |
| `how_to_be_enough` | Clinical psychology (WPS markup) | 192 |
| `art_of_community` | Leadership / belonging (Berrett-Koehler) | 205 |
| `life_in_three_dimensions` | Psychology (Knopf/PRH, heavy endnotes) | 264 |
| `dewey_art_experience` | Philosophy/aesthetics (anchored to the Claramonte translation) | 137 |
| `crossan_jesus` | Biblical scholarship (omnibus: only book B1 is translated) | 116 |

Re-check this list against the code with the venv interpreter (the system `python3` has no `bs4`):

```bash
.venv/bin/python3 -c "import translate_epub as t; print(len(t.PROFILES), list(t.PROFILES))"
```

### Key Constants (top of file)

- `MODEL = "claude-sonnet-4-6"` — translation model
- `BATCH_SIZE = 25` — blocks per Claude call
- `CONCURRENCY = 4` — async semaphore for parallel API calls
- `FORCE_PROFILE: str | None = None` — override auto-detection

### Resumability

`progress.json` tracks per-file status (`done`, `partial`, `skipped`). Re-running skips completed files. Delete `progress.json` to start fresh.

### Content Protection

- **Opaque tokenization**: `<code>`, `<math>`, `<svg>` → `⟦OPAQUE_N⟧` tokens, restored after translation
- **Excluded ancestors**: blocks inside `<pre>`, `<math>`, `<svg>`, or `data-type="programlisting"` are never sent to Claude
- **Structural validation**: `tag_signature()` computes a hash of tags/hrefs/ids/code/math/svg counts — mismatch triggers per-block retry

### Response Format

Claude returns translations in a delimiter-based format (not JSON):
```
<<<BLOCK 0>>>
translated content here
<<<BLOCK 1>>>
more translated content
<<<END>>>
```

## Adding a New Profile

1. Define `GLOSSARY_<name>`: `dict[str, str]` of English→Spanish terms
2. Define `SYSTEM_PROMPT_<name>`: full system prompt with translation rules, tone, and glossary reference
3. Define `SKIP_PATTERNS_<name>`: `list[str]` of regex patterns for files to skip
4. Register in `PROFILES` dict

## Repository Layout

The root is the **inbox**, not a library. `find_source_epub` calls
`PROJECT_DIR.glob("*.epub")` — **not recursive** — so only EPUBs sitting directly in
the root show up in the selection menu. Everything already processed is filed away:

```
.
├── translate_epub.py        main pipeline (single file)
├── enrich_epub.py           reading guides + concept maps for a translated EPUB
├── dashboard.py             live TUI for a running translation
├── enrichment.css           styles injected by enrich_epub.py
├── queue_next.sh            one-off chaining script from April (obsolete; see note)
├── progress.json            ACTIVE checkpoint — the pipeline reads/writes it here
├── work/                    ACTIVE extraction dir — recreated each run
│
├── libros/
│   ├── originales/          source EPUBs in English
│   ├── traducidos/          deliverables: *_es.epub and Spanish-titled files
│   └── intermedios/         *_pre_*, *_vN, *_clean, *_fixed, *.bak, .azw3, conversion logs
├── logs/                    translate_*.log, run.log, enrich_run.log
├── estado/                  archived progress_*.json from finished books
├── scripts/                 one-off repair scripts (see below)
└── por-borrar/              discard candidates, kept until reviewed by hand
```

### Where things go

| What | Where | Why |
|------|-------|-----|
| EPUB you want to translate next | project root | the menu only globs the root |
| Finished `*_es.epub` | `libros/traducidos/` | keeps the menu clean for the next run |
| Its English source | `libros/originales/` | same reason |
| `progress.json` of a finished book | `estado/progress_<book>_done.json` | frees the name for the next run |
| `progress.json` of the run in flight | project root | hardcoded as `PROJECT_DIR / "progress.json"` |

### Path assumptions baked into the code

- `translate_epub.py`: `PROJECT_DIR = Path(__file__).parent`, `WORK_DIR = PROJECT_DIR / "work"`,
  `PROGRESS_FILE = PROJECT_DIR / "progress.json"`. Output is written to
  `PROJECT_DIR / (source.stem + "_es.epub")` — i.e. the root, then moved by hand.
- `dashboard.py` looks for logs in the root **and** in `logs/`.
- `enrich_epub.py` looks for `*_es.epub` in the root **and** in `libros/traducidos/`.
- `scripts/*` live one level down and resolve the project root as
  `Path(__file__).parent.parent`; the Python ones insert it into `sys.path` before
  `import translate_epub`. Keep that line if you add a new script there.
- `queue_next.sh` still assumes the pre-reorg layout (it runs `touch TheScore.epub` in the
  root). Don't run it as-is — it would create an empty EPUB in the inbox.

### Identifying a book's language

File names lie: `Life in three dimensions.epub` is a renamed *translation*. Read the OPF
instead before filing anything:

```bash
unzip -p book.epub $(unzip -l book.epub | grep -oE '[^ ]+\.opf' | head -1) \
  | grep -oE '<dc:language[^>]*>[^<]*'
```
