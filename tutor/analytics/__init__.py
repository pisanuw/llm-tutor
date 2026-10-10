"""Offline analysis of how students use the tutor
(phases 2 and 3).

``python -m tutor.analytics export`` copies what the analysis needs from the
production database (or from the app over HTTP) into a local SQLite file,
pseudonymous by default; every other command reads that file and never
touches production. The modules:

    store.py      the export and the local store's schema
    visits.py     sessionization and on-screen time
    lectures.py   slides shown, dwell, narration listened
    questions.py  the time between a student's questions
    topics.py     topic labels, switches, disengagement points, timelines
    cohort.py     per-student metrics, class summaries, comparisons
    render.py     markdown, CSV, JSON and HTML output
"""
