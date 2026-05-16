# Weekly Commit Analysis Script

`commit_week_analyzer.py` clones or updates a GitHub repository, inspects commits from the past week, estimates engineering hours, and reports code-quality findings.

The script is written for Python 3.14.2+, uses the `git` CLI, and loads project settings from a `.env` file using the Python standard library.

## Quick setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The repository includes a ready-to-run `.env` file. By default it analyzes this project itself and writes reports into `reports/`:

```bash
python commit_week_analyzer.py
```

You can also pass the repository explicitly, which overrides `ANALYZER_REPO` from `.env`:

```bash
python commit_week_analyzer.py https://github.com/OWNER/REPO.git --author you@example.com
```

## `.env` configuration

Update `.env` to make repeated local testing simple:

```dotenv
ANALYZER_REPO=.
ANALYZER_BRANCH=
ANALYZER_AUTHOR=
ANALYZER_DAYS=7
ANALYZER_WORKDIR=/tmp/weekly-commit-analysis
ANALYZER_OUTPUT=reports/weekly_commit_analysis.md
ANALYZER_JSON_OUTPUT=reports/weekly_commit_analysis.json
ANALYZER_PRINT_PROMPT=false
ANALYZER_USE_GEMINI=false
GEMINI_MODEL=gemini-2.5-flash
GEMINI_API_KEY=
```

> Keep real API keys private. The committed `.env` contains only blank or safe default values so the app works immediately for local smoke tests.

## Useful options

```bash
python commit_week_analyzer.py /path/to/repo \
  --branch main \
  --author "Your Name" \
  --days 7 \
  --output reports/week.md \
  --json-output reports/week.json
```

By default the script:

- analyzes the last 7 days of commits;
- clones remote repositories into `ANALYZER_WORKDIR` or your temp directory, or updates an existing local checkout;
- writes a Markdown report to `ANALYZER_OUTPUT` or `weekly_commit_analysis.md`;
- optionally writes JSON when `ANALYZER_JSON_OUTPUT` or `--json-output` is set;
- estimates hours from commit timestamp clusters and diff size;
- scores code quality using deterministic heuristics for reviewability, tests, complexity, TODOs, and possible secrets.

To review the carefully tuned LLM prompt without making an API call:

```bash
python commit_week_analyzer.py /path/to/repo --print-prompt
```

To add an LLM-assisted narrative analysis, set `GEMINI_API_KEY` in `.env` or your shell and enable Gemini:

```bash
ANALYZER_USE_GEMINI=true python commit_week_analyzer.py /path/to/repo
```

## Easy test commands

```bash
python -m py_compile commit_week_analyzer.py
python commit_week_analyzer.py --help
python commit_week_analyzer.py
```

The final command uses the committed `.env` defaults, analyzes this repository, and writes:

- `reports/weekly_commit_analysis.md`
- `reports/weekly_commit_analysis.json`

## Prompt accuracy notes

The built-in LLM prompt is designed to reduce overconfident analysis. It instructs the model to:

1. analyze only the JSON evidence generated from Git history;
2. separate facts from estimates;
3. treat timestamps as activity evidence, not proof of all work time;
4. calibrate hours using both commit spacing and diff size;
5. cite exact commit SHAs or file paths for every observation;
6. rank recommendations by impact;
7. explicitly state what evidence is missing when confidence is low.

These constraints make the code-quality and hours analysis more accurate than a free-form prompt that asks for a subjective review.
