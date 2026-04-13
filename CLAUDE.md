# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CLI tool that translates EPUB books from English to Latin American Spanish (es-419) using the Claude Agent SDK. Runs on Claude Max subscription (no API billing). Single-file Python application (`translate_epub.py`, ~1450 lines).

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run translation (interactive menu if multiple EPUBs present)
python3 translate_epub.py
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

| Profile | Target | Glossary size |
|---------|--------|---------------|
| `generic` | Literary non-fiction (Seth Godin) | ~15 terms |
| `ai_engineering` | O'Reilly ML/AI technical | ~70+ terms |
| `grokking_algorithms` | Manning CS illustrated | ~130+ terms |
| `superagency` | AI/society non-fiction | — |
| `practical_sql` | No Starch SQL technical | — |

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

## Working Directories

- `./work/` — extracted EPUB contents (transient, recreated each run)
- `./progress.json` — translation checkpoint (delete to restart)
- Output: `<original_name>_es.epub` in project root
