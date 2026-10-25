# My Development Path — AI Learning Path Recommender

> 🏆 Part of the solution set that took **1st place in the "AI Agents" track** — Case Championship of the St. Petersburg Corporate University (2026).
> 🇷🇺 Русская версия: [README.ru.md](README.ru.md)

A personalized learning recommender for St. Petersburg civil servants. For each employee it builds a shortlist of training programs based on their position, completed training and what colleagues in the same role take — and invites them to learn rather than assigning courses.

![Home screen](docs/screenshots/02-glavnaya.png)

## Key ideas

- **Deterministic engine first.** The shortlist is built offline by a rule-based scoring engine that never recommends a completed course or a program unavailable for the person's position — zero hallucinations by construction.
- **LLM as an optional layer.** A language model can only **re-rank** already selected programs and write an inviting explanation; it cannot add or replace programs. If the model is unavailable, the product silently falls back to the engine's result.
- **Three model groups, benchmarked.** Russian (GigaChat), international (OpenRouter) and **local** (any OpenAI-compatible server: Ollama, LM Studio, llama.cpp) — measured on the product's own task with a built-in benchmark.
- **Data import without code changes.** Drop in new Excel/CSV exports: file roles and columns are detected by content with confidence scores ("What I recognized" screen), with manual override and one-click rollback.
- **Catalog import from PDF.** A new program booklet (PDF, Excel, CSV, Word) is parsed, diffed ("what will change") and applied with rollback.

## Features

- Home screen with top-3 programs and a one-line "why recommended"
- 5-question preference wizard (optional)
- Catalog of 161 programs with search and filters
- "My learning": next steps, progress by area, history
- Feedback loop: "want it / not now / not relevant" (rate-limited)
- Self-diagnostics (`--doctor`) and data validation from the CLI

## Tech stack

- **Backend:** Python 3.10+, FastAPI, Uvicorn, Pydantic, openpyxl, xlrd, pdfplumber, RapidFuzz
- **LLM:** GigaChat SDK, OpenAI-compatible client (OpenRouter / local models), benchmark harness
- **Frontend:** vanilla JS + CSS, no build step
- **Delivery:** one-click `START.bat` / `start.sh` launcher, Dockerfile + docker-compose

~11.6k lines of Python.

## Quick start

```bash
git clone https://github.com/DANIILDISA/ai-learning-path-recommender.git
cd ai-learning-path-recommender
cp .env.example .env      # optional: GigaChat / OpenRouter keys, or a local model URL
./start.sh                # Windows: double-click START.bat
```

Menu options 1–3 check readiness of a model group, option 4 runs without an LLM. Sample data in `data/` is anonymized.

## Screenshots

| Preference wizard | Catalog | My learning |
|---|---|---|
| ![](docs/screenshots/03-master-shag-1.png) | ![](docs/screenshots/09-katalog-programm.png) | ![](docs/screenshots/10-moyo-obuchenie.png) |

## Author

**Daniil Burnashov** — design, development and documentation.
