# Weekly Commit Analysis Script

`commit_week_analyzer.py` clones or updates a GitHub repository, inspects commits from the past week, estimates engineering hours, and reports code-quality findings.

The script is written for Python 3.14.2+ and uses only the Python standard library plus the `git` CLI.

## Quick start

```bash
python3.14 commit_week_analyzer.py https://github.com/OWNER/REPO.git --author you@example.com
```

By default the script:

- analyzes the last 7 days of commits;
- clones remote repositories into your temp directory or updates an existing local checkout;
- writes `weekly_commit_analysis.md`;
- estimates hours from commit timestamp clusters and diff size;
- scores code quality using deterministic heuristics for reviewability, tests, complexity, TODOs, and possible secrets.

## Useful options

```bash
python3.14 commit_week_analyzer.py /path/to/repo \
  --branch main \
  --author "Your Name" \
  --days 7 \
  --output reports/week.md \
  --json-output reports/week.json
```

To review the carefully tuned LLM prompt without making an API call:

```bash
python3.14 commit_week_analyzer.py /path/to/repo --print-prompt
```

To add an LLM-assisted narrative analysis, set `OPENAI_API_KEY` and pass `--use-openai`:

```bash
OPENAI_API_KEY=... python3.14 commit_week_analyzer.py /path/to/repo --use-openai
```

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
