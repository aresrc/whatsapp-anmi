# Repository Guidelines

## Project Structure & Module Organization

- `app.py` is the Flask entry point. It verifies and receives Meta WhatsApp webhooks, chooses a response, and calls the Cloud API.
- `motor_conocimientos.py` loads, normalizes, scores, and formats knowledge rules.
- `data/motor_conocimientos.csv` is the maintained knowledge base. Preserve its headers and UTF-8-with-BOM encoding.
- `respuestas.py` contains legacy in-code rules; it is not imported by the current application. Prefer the CSV-backed engine for new content.
- `requirements.txt` lists runtime dependencies. There is currently no `tests/` directory.

## Build, Test, and Development Commands

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure `.env` with `VERIFY_TOKEN`, `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, and `GRAPH_API_VERSION`, then run the webhook service:

```powershell
python app.py
```

The service listens on `PORT` (default `5000`); use `GET /` as a basic health check. No build step or test runner is configured yet.

## Coding Style & Naming Conventions

Use Python with four-space indentation, type hints, `snake_case` for functions and variables, and `PascalCase` for classes. Keep imports grouped as standard library, third-party, then local modules. Favor small, single-purpose functions and Spanish user-facing messages consistent with the existing bot. Run a syntax check before committing:

```powershell
python -m compileall app.py motor_conocimientos.py respuestas.py
```

## Testing Guidelines

When adding behavior, add focused tests under `tests/` using `unittest` (or introduce `pytest` and document it). Name files `test_<module>.py` and test functions `test_<behavior>`. Cover normalization, CSV validation/scoring, webhook payload extraction, and error responses. Run the suite with `python -m unittest discover -s tests` once tests exist.

## Commit & Pull Request Guidelines

Git history currently uses short Spanish imperative summaries (for example, `Primer commit`). Continue with concise, scoped messages such as `Agregar validacion de webhook`. Keep each commit focused. Pull requests should state the behavior change, list configuration or CSV changes, link the relevant issue when available, and include request/response examples for webhook changes. Never commit `.env`, tokens, phone IDs, or other credentials.

## Knowledge Base Safety

Treat all health content and mandatory disclaimers in `data/motor_conocimientos.csv` as reviewed material. Do not weaken, remove, or silently rewrite medical guidance; preserve the required disclaimer when changing a rule.
