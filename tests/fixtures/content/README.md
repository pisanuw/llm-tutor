# Test fixture content

`tests/conftest.py` sets `TUTOR_CONTENT_DIR` to this folder before the app is
imported, so the suite always runs against this frozen copy of the example
course (plus a few extra exercises tests need), never against the content you
are writing at the repository root. Do not edit it to match your course.
