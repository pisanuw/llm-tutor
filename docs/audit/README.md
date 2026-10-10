# Turn audits

The nightly auditor's reports land here. Each run of
`python -m pipeline audit-turns` writes one `YYYY-MM-DD.md`: a re-judge, by
the stronger auditor model (`AUDITOR` in `tutor/agent/models.py`), of the
last 24 hours of risky turns plus a sample of routine ones, with the agreement
rates (under-blocks: the live tutor was too lenient; over-blocks: too strict),
move compliance, and prompt-cache health.

```bash
python -m pipeline audit-turns --dry-run                  # count what would be audited; no model calls, no writes
python -m pipeline audit-turns                            # local SQLite: audit, write docs/audit/<date>.md
python -m pipeline audit-turns --allow-prod               # the same against production (what audit.yml runs)
python -m pipeline audit-turns --hours 48 --sample-rate 0.1
```

Each audited turn is one billed call. The auditor never blocks the live
tutor; it records each verdict as a `JudgeSample` row (`judge = auditor:<model>`)
and writes the report. Disagreements surface in the admin Supervision tab.

Reports name students only as `s<id>` (the student row id), never by email or
name. The nightly `audit.yml` workflow keeps each report as a workflow
artifact for 14 days (the run's page, artifact `turn-audit-<run id>`) and
never commits it: a commit would keep student-derived data in git history,
out of reach of the deletion and anonymization tools. The dated files are
gitignored, so a local run cannot be committed by accident either.
