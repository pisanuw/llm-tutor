# Tutor web app: production image for Render (Docker runtime).
# The ChromaDB store is NOT in this image (it's gitignored / too large); it is
# seeded onto the mounted persistent disk on first boot by entrypoint.sh.

# ---------------------------------------------------------------------------
# Stage 1: render the Quarto book from source (book/*.qmd) to book/_book/.
# The generated HTML used to be committed (5.7 MB, 70 files); it is now built
# here so the repo no longer tracks derived output. The .qmd sources are pure
# markdown (no knitr/jupyter compute engine), so a plain `quarto render` runs
# offline with no language runtime beyond Quarto itself.
# ---------------------------------------------------------------------------
FROM ghcr.io/quarto-dev/quarto:1.5.57 AS book
WORKDIR /book
COPY book/ ./
RUN quarto render

# ---------------------------------------------------------------------------
# Stage 2: the application image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# curl and ca-certificates for the seed downloads in entrypoint.sh. No compiler:
# every requirement ships a wheel for this platform, and a new one that does not
# fails the build here instead of shipping build tools (plan step 6.5).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The app runs as `app`: entrypoint.sh seeds the data as root, hands it over,
# and drops to this user before the migrations and uvicorn.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

WORKDIR /app

COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt

# Chroma's ONNX embedding model, fetched now into the app user's cache
# (~/.cache/chroma), so the first /find_topic query never downloads it. A failed
# fetch fails the build.
USER app
RUN python -c "from chromadb.utils import embedding_functions as ef; ef.DefaultEmbeddingFunction()(['warmup'])"
USER root

# App code and the tracked data it reads at runtime. The raw knowledge-base
# sources are not here: the app reads only the ChromaDB store, which the
# entrypoint seeds onto the persistent disk.
COPY tutor/                ./tutor/
# Your course content (ADDING-KNOWLEDGE.md): who the tutor is, the curriculum,
# the persona and profile the system prompt is built from, the explorables.
COPY courses.yaml         ./courses.yaml
COPY persona/             ./persona/
COPY instructor-profile/  ./instructor-profile/
COPY curriculum/          ./curriculum/
COPY explorables/         ./explorables/
# Alembic runs at boot (entrypoint.sh -> python -m tutor.migrate).
COPY alembic.ini          ./alembic.ini
COPY migrations/          ./migrations/
# Quarto book HTML comes from the build stage above (not from git).
COPY --from=book /book/_book/ ./book/_book/
COPY entrypoint.sh        ./entrypoint.sh

# Bytecode compiled now, since the app user cannot write __pycache__ under
# /app. The directories the app writes to belong to it: /app/output (the slide
# seed, which the app also creates at import) and /app/data (the SQLite file
# when DATABASE_URL is unset, and the TTS cache unless TTS_CACHE_DIR moves it
# onto the persistent disk, as render.yaml does).
RUN chmod +x entrypoint.sh \
    && python -m compileall -q tutor migrations \
    && install -d -o app -g app /app/output /app/data

ENV PORT=8000 PYTHONUNBUFFERED=1 \
    TTS_CACHE_DIR=/app/data/tts-cache TUTOR_DB=/app/data/tutor.db
EXPOSE 8000
CMD ["./entrypoint.sh"]
