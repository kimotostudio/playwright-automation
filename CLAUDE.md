# Claude Code Instructions

This file is specifically for Claude Code / Claude AI coding assistants.

**Before any code change, read:**
1. [CONSTITUTION.md](CONSTITUTION.md) - Core principles
2. [AI_GUIDE.md](AI_GUIDE.md) - Full AI agent instructions

---

## Quick Reference

### Project: Lead Finder
- Flask web app for finding small business leads in Japan
- 3-pass search strategy (Pass 1: broad, Pass 2: solo-focused, Pass 3: variations)
- Japanese content filtering (pre-crawl URL filter + post-crawl content check)
- Pipeline: Query generation -> URL collection -> JP filter -> Prefilter -> Hard filter -> Precheck -> Crawl -> Score -> Output

### Key Files
- `web_app/app.py` - Main Flask app + search pipeline
- `src/japanese_detector.py` - Japanese content detection
- `src/processor.py` - Lead processing pipeline
- `src/filters.py` - Hard exclusion filters
- `src/content_analyzer.py` - Content analysis
- `config/cities_data.py` - 47 prefectures, 778 cities
- `tests/` - Test suite (`python -m pytest tests/ -v`)

### KIMOTO Principles
1. **Gradient-Driven** - Optimize for conversion rate improvement
2. **Stats-First** - Every function returns stats dict
3. **Demo-First** - Show before sell
4. **Template-Based** - Repeat 3x? Make it a template

### Non-Negotiables
- Never commit API keys or .env files
- Never break CSV output format (add columns, don't rename)
- Never remove safety filters (Japanese filter, blocked domains, medical/corporate)
- Never add "max collection mode" toggle (was added and reverted twice)
- Always run `python -m pytest tests/ -v` before considering a task done
- All pipeline stages must log counts

### Config Knobs (web_app/app.py)
```
MAX_URLS_TO_PROCESS=1500  MAX_QUERIES_TOTAL=800
MIN_URLS_PER_PAIR=3       MAX_URLS_PER_DOMAIN=15
LEAD_PROCESS_WORKERS=12   FOREIGN_FILTER_MODE=strict
```

### Testing
```bash
python -m pytest tests/ -v
# Currently 168 tests
```
