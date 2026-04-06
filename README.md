# epub-translator

Automated EPUB translator (EN → ES LATAM) powered by Claude via the Agent SDK.

Translates entire EPUB books from English to neutral Latin American Spanish while preserving HTML structure, code blocks, images, and technical content.

## Features

- **Multi-profile system**: auto-detects book type and applies the right translation strategy
  - `generic` — non-fiction, business, self-help (e.g., Seth Godin)
  - `ai_engineering` — O'Reilly-style technical books with code, math, and ML terminology
  - `grokking_algorithms` — Manning-style illustrated CS books with Python code
- **Code-aware translation**: `<pre>`, `<code>`, `<math>`, `<svg>` blocks are never touched
- **Opaque tokenization**: inline `<code>` tags are replaced with `⟦OPAQUE_N⟧` tokens before translation, then restored — the model never sees technical content it shouldn't translate
- **Mandatory glossary per profile**: enforces consistent terminology across the entire book
- **Structural validation**: compares tag signatures before/after translation, auto-retries on mismatch
- **Resumable**: checkpoint via `progress.json` — if interrupted, re-run and it picks up where it left off
- **Spec-compliant EPUB output**: mimetype first (uncompressed), correct `dc:language`, NCX translation

## Requirements

- Python 3.10+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) with an active Max subscription (the script uses the Agent SDK, which authenticates through your Claude Code session — no API key needed, $0 incremental cost)

## Setup

```bash
git clone https://github.com/stivenrosales/epub-translator.git
cd epub-translator

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

1. Place your `.epub` file in the project directory
2. Run the translator:

```bash
source .venv/bin/activate
python3 translate_epub.py
```

3. If multiple EPUBs are found, you'll get a selection menu
4. The translated file is saved as `<original_name>_es.epub`

### Resuming an interrupted translation

Just re-run the same command. The script reads `progress.json` and skips already-translated files.

### Starting fresh on a new book

```bash
rm -rf work progress.json
python3 translate_epub.py
```

### Forcing a specific profile

Edit `translate_epub.py` and set:

```python
FORCE_PROFILE = "grokking_algorithms"  # or "generic", "ai_engineering"
```

## How it works

```
┌─────────┐     ┌──────────┐     ┌───────────┐     ┌──────────┐
│  EPUB   │────▶│  Extract  │────▶│ Translate │────▶│ Repack   │
│ (input) │     │  to work/ │     │  XHTMLs   │     │ _es.epub │
└─────────┘     └──────────┘     └───────────┘     └──────────┘
                                       │
                              ┌────────┴────────┐
                              │                 │
                        ┌─────▼─────┐    ┌──────▼──────┐
                        │ Tokenize  │    │  Translate   │
                        │ <code>    │    │  text-only   │
                        │ as opaque │    │  via Claude  │
                        └─────┬─────┘    └──────┬──────┘
                              │                 │
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │   Restore       │
                              │   opaque tokens  │
                              │   + validate    │
                              └─────────────────┘
```

1. **Extract**: unzips EPUB into `work/`
2. **Detect profile**: scans for Manning/O'Reilly markup patterns to pick the right glossary and system prompt
3. **Extract blocks**: finds translatable leaf `<p>`, `<h1>`–`<h6>`, `<li>`, etc., skipping `<pre>`, `<math>`, `<svg>` subtrees
4. **Tokenize**: replaces inline `<code>`, `<math>`, `<svg>` with `⟦OPAQUE_N⟧` placeholders
5. **Translate**: sends batches of 15 blocks to Claude Sonnet with rolling context and glossary
6. **Validate**: checks tag signature matches between original and translated HTML
7. **Restore**: re-inserts original opaque content at token positions
8. **Repack**: builds spec-compliant EPUB with mimetype first

## Adding a new profile

To support a new book type, add three things in `translate_epub.py`:

1. A `GLOSSARY_*` dictionary with mandatory term translations
2. A `SYSTEM_PROMPT_*` string with translation rules and tone guidance
3. A `SKIP_PATTERNS_*` list of filename regexes to skip
4. Register them in the `PROFILES` dict

Then update `detect_book_profile()` if you want auto-detection.

## Limitations

- **Images with embedded text** (rasterized diagrams, infographics) stay in English — this is a DOM-level translator, not an OCR tool
- **Translation quality** depends on Claude's output — structural validation catches broken HTML but not semantic errors
- Batch size and concurrency are tunable but limited by the Agent SDK's rate limits

## License

MIT
