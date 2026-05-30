# Open Source Checklist

Run this checklist before pushing FinRep to a public GitHub repository.

## Private Data

Confirm private paths are not tracked:

```bash
git ls-files data reports .env src/secrets.json
```

Expected output: nothing.

Check whether private paths ever appeared in git history:

```bash
git log --all --name-only --pretty=format: | rg '^(data/|reports/|\.env$|src/secrets\.json$)'
```

Expected output: nothing. If anything appears, publish from a clean fresh repository or rewrite history before release.

## Secret Scan

Search tracked source/docs for common token shapes:

```bash
git grep -n -E 'ghp_|github_pat_|AKIA|AIza|xox[baprs]-|BEGIN (RSA|OPENSSH|PRIVATE) KEY|[0-9]{8,10}:[A-Za-z0-9_-]{20,}|token[[:space:]]*=|api[_-]?key[[:space:]]*=|password[[:space:]]*=' -- . ':!uv.lock' ':!AGENTS_MD/OPEN_SOURCE_CHECKLIST.md'
```

Expected output: nothing actionable. Rotate any token that was ever exposed, including local bot tokens copied into `.env`.

## Demo Data

Validate the public demo dataset:

```bash
FINREP_DATA_DIR=sample_data uv run python -c "from src.data.validation import validate_all_data; issues = validate_all_data(False); print(f'issues={len(issues)}'); assert not issues"
```

Smoke-check the Dash app layout on demo data:

```bash
FINREP_DATA_DIR=sample_data FINREP_REPORTS_DIR=/private/tmp/finrep_reports uv run python -c "from src.dashboard.app import create_app; app = create_app(); assert app.layout is not None; print(app.title)"
```

Run the syntax check:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/finrep_pycache python3 -m compileall main.py src
```

## Generated Outputs

Confirm generated reports are still ignored:

```bash
git status --short reports data .env
```

Expected output: nothing tracked or staged.

## Final Review

Review these files before release:

```bash
git diff -- README.md .env.example .gitignore .dockerignore Dockerfile docker-compose.yml src/config.py
```

Make sure the public docs describe local-only storage, demo data, and safe Docker binding.
