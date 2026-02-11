# AI GUIDE - Instructions for AI Agents

このファイルは Claude Code, Codex, Cursor などの AI コーディングツール向けの指示書です。

---

## A. First Actions

コード変更の前に **必ず** 以下を実行:

1. **Read CONSTITUTION.md** - プロジェクトの原則を理解
2. **Read README.md** - プロジェクト概要とセットアップを確認
3. **Identify scope** - 変更範囲を最小化
4. **Plan verification** - 動作確認の方法を決める

---

## B. Core Rules

### B.1 Security

**絶対に出力・コミットしないもの:**
```
*.env
*_key.json
*_secret.json
API keys (sk-*, any key pattern)
Credentials
Personal emails/phones
Real client data
```

**If you find secrets:**
```python
# Move to .env
OPENAI_API_KEY=os.getenv('OPENAI_API_KEY')
```

### B.2 Output Format

**Code changes:**
- Return complete, working code
- No truncation, no placeholders like `# ... rest of code ...`
- Include all imports, all functions

**Explanations:**
- Brief (3 bullets max)
- Focus on WHY, not WHAT

**Plans:**
- Step-by-step with exact commands

---

## C. KIMOTO Principles (AI Implementation)

### C.1 Gradient-Driven Development
When adding features, always include:
```python
stats = {
    'total': count_total,
    'success': count_success,
    'failure': count_failure,
    'success_rate': count_success / count_total,
}
logger.info(f"[STAGE_NAME] Processed: {count} items")
```

### C.2 Stats-First Implementation
Every function that processes data should return stats:
```python
def process_leads(leads: list) -> Tuple[list, dict]:
    results = []
    stats = {'total': len(leads), 'kept': 0, 'filtered': 0}
    for lead in leads:
        if should_keep(lead):
            results.append(process(lead))
            stats['kept'] += 1
        else:
            stats['filtered'] += 1
    return results, stats
```

### C.3 Demo-First Design
- Make outputs immediately shareable
- Include preview/demo functionality
- Generate visual examples

### C.4 Template-Based Implementation
When you see repeated code patterns (3+ times), extract to a function.

---

## D. Code Style

### D.1 Python
```python
import os
from typing import Dict, List, Tuple

import pandas as pd

from src.processor import process_lead

MAX_RETRIES = 3

def calculate_score(lead: dict, weights: dict) -> float:
    """Calculate weighted score for a lead."""
    pass

class LeadProcessor:
    def __init__(self, config: dict):
        self.config = config
```

### D.2 JavaScript
```javascript
const MAX_RESULTS = 100;

function processLeads(leads, config) {
    return leads.map(lead => calculateScore(lead, config));
}
```

---

## E. File Operations

### E.1 CSV Output
```python
# UTF-8 with BOM for Excel compatibility
with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f)
    writer.writerows(data)
```

### E.2 Logging
```python
logger.info(f"[STAGE] Description: {count} items")
logger.error(f"[ERROR] {context}: {error_message}")
```

---

## F. Error Handling

```python
# Always provide context
try:
    result = risky_operation()
except SpecificError as e:
    logger.error(f"Failed to process lead {lead_url}: {e}")
    stats['failed'] += 1
    continue
```

---

## G. Testing

Every change must include a test path:
```bash
# In commit message or PR description
Test: python -m pytest tests/ -v
Expected: All tests pass
```

---

## H. Git Discipline

### H.1 Commit Messages
Format: `[TYPE] Brief description`

Types: `[ADD]`, `[FIX]`, `[UPDATE]`, `[REFACTOR]`, `[DOCS]`, `[CONFIG]`

### H.2 Keep Commits Small
One logical change per commit.

### H.3 Don't Commit
```
*.env, *.log, __pycache__/, output/, logs/, *_key.json
```

---

## I. Common Patterns

### I.1 Pipeline Processing
```python
def process_pipeline(data: list) -> Tuple[list, dict]:
    stats = {'total': len(data)}
    data = filter_stage(data)
    stats['after_filter'] = len(data)
    data = transform_stage(data)
    stats['final'] = len(data)
    return data, stats
```

### I.2 Retry Logic
```python
def retry_operation(func, max_retries=3):
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
```

---

## J. Project-Specific Rules

### J.1 lead-finder
- Always return stats dict
- CSV output must be UTF-8 with BOM
- Log at each pipeline stage
- Japanese content detection mandatory
- Keep all safety filters (Japanese filter, blocked domains, medical/corporate)

### J.2 demo-generator
- Templates must have fallback values
- Demo URL must be immediately shareable
- Support multiple business types

### J.3 list_clean.py
- No AI API dependencies
- Fast processing
- Detailed filter reason statistics

---

## K. Definition of Done

A task is complete when:

1. Code works (tests pass)
2. Documentation updated (if needed)
3. No secrets in code
4. Commit message explains change
5. Verification path provided

---

## L. Questions to Ask Before Implementing

1. Does this align with KIMOTO principles?
2. Is this the minimal change?
3. How will this be verified?
4. Does this help営業成約率?
5. Will 3-month-future-me understand this?

---

## M. Revision History

- v1.0 (2026-02-03): Initial AI guide
