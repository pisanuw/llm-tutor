#!/bin/sh
# Seed the data the app reads (ChromaDB store, lecture audio, slide decks, the
# mastery exam bank), then start the app.
#
# The persistent disk is mounted at /app/knowledge-base/chromadb (see
# render.yaml). The ChromaDB store and the lecture audio live on it and are
# seeded once, and the chat's voice cache (TTS_CACHE_DIR, <mount>/tts-cache)
# grows there so its clips survive a deploy; the slide decks and the exam bank live on the ephemeral
# container filesystem and are fetched on every boot. To refresh content:
# clear the disk (or bump the URL) and redeploy.
#
# Seed blobs are GitHub Release assets on this (PRIVATE) repo, so their
# download URLs are the GitHub API asset endpoint
#   https://api.github.com/repos/<owner>/<repo>/releases/assets/<id>
# which requires a read-only token in GITHUB_TOKEN (see DEPLOY.md). Hosting them
# on GitHub Releases (free egress) instead of object storage avoids egress bills.
#
# Boot never stops on a seed (plan step 2.7, findings C4 and E5). Each seed is
# downloaded to a file with retries, unpacked into a scratch directory, checked,
# and only then moved into place, with a .seed-ok marker written last. A failed
# or truncated download leaves the previous copy (or nothing) plus no marker, so
# the next boot tries again, and the app starts without that data. Every such
# problem is written to BOOT_WARNINGS, which the app emails once it is up.
set -e

CHROMA_DIR="${CHROMA_DIR:-/app/knowledge-base/chromadb}"
AUDIO_DIR="${AUDIO_DIR:-$CHROMA_DIR/lecture-audio}"
OUTPUT_DIR="${OUTPUT_DIR:-/app/output}"
MASTERY_PATH="${MASTERY_PATH:-/app/curriculum/mastery_exams.json}"
BOOT_WARNINGS="${BOOT_WARNINGS:-/tmp/tutor-boot-warnings.txt}"
SEED_RETRIES="${SEED_RETRIES:-5}"
export BOOT_WARNINGS
mkdir -p "$(dirname "$BOOT_WARNINGS")" 2>/dev/null || true
: > "$BOOT_WARNINGS" 2>/dev/null || BOOT_WARNINGS=/dev/null

warn() {
    echo "[entrypoint] WARNING: $*" >&2
    echo "$*" >> "$BOOT_WARNINGS" 2>/dev/null || true
}

# fetch URL FILE: download one seed asset to FILE, retrying transient failures.
# For api.github.com URLs it adds the asset headers (Accept + bearer token); any
# other URL is fetched plainly, so old/public seed URLs keep working.
fetch() {
    case "$1" in
        https://api.github.com/*)
            if [ -n "$GITHUB_TOKEN" ]; then
                curl -fsSL --retry "$SEED_RETRIES" --retry-all-errors --retry-delay 3 \
                     -H "Accept: application/octet-stream" \
                     -H "Authorization: Bearer $GITHUB_TOKEN" -o "$2" "$1"
            else
                echo "[entrypoint] WARNING: $1 needs GITHUB_TOKEN (private-repo asset) but it is unset" >&2
                curl -fsSL --retry "$SEED_RETRIES" --retry-all-errors --retry-delay 3 \
                     -H "Accept: application/octet-stream" -o "$2" "$1"
            fi ;;
        *)
            curl -fsSL --retry "$SEED_RETRIES" --retry-all-errors --retry-delay 3 -o "$2" "$1" ;;
    esac
}

# download DIR URL...: fetch each URL into DIR/part-NNN, in order.
download() {
    _dir=$1
    shift
    rm -rf "$_dir"
    mkdir -p "$_dir" || return 1
    _n=0
    for _u in "$@"; do
        fetch "$_u" "$_dir/part-$(printf '%03d' "$_n")" || return 1
        _n=$((_n + 1))
    done
    [ "$_n" -gt 0 ]
}

# unpack_parts DIR DEST: the parts are pieces of ONE archive (CHROMA_SEED_URL
# and AUDIO_SEED_URL may be split); join them in order and untar into DEST.
unpack_parts() {
    cat "$1"/part-* | tar --warning=no-unknown-keyword --no-same-owner --no-same-permissions -xz -C "$2"
}

# unpack_each DIR DEST: each part is its own archive (OUTPUT_SEED_URL lists
# separate tarballs); untar every one into DEST.
unpack_each() {
    for _p in "$1"/part-*; do
        tar --warning=no-unknown-keyword --no-same-owner --no-same-permissions -xzf "$_p" -C "$2" || return 1
    done
}

# seed_dir URLS DEST MODE: seed a directory we own outright. Unpack beside it,
# then swap it into place and mark it complete.
seed_dir() {
    _urls=$1 _dest=$2 _mode=$3
    rm -rf "$_dest.new"
    mkdir -p "$_dest.new" || return 1
    # shellcheck disable=SC2086  # the URL list is space-separated on purpose
    if ! download "$_dest.download" $_urls || ! "unpack_$_mode" "$_dest.download" "$_dest.new"; then
        rm -rf "$_dest.download" "$_dest.new"
        return 1
    fi
    rm -rf "$_dest.download" "$_dest"
    mv "$_dest.new" "$_dest" || return 1
    touch "$_dest/.seed-ok"
}

# seed_chroma: the store's directory is the disk's mount point, which cannot be
# swapped, so replace its entries instead, leaving the lecture audio alone.
seed_chroma() {
    _new="$CHROMA_DIR/.seed-new"
    _dl="$CHROMA_DIR/.seed-download"
    rm -rf "$_new"
    mkdir -p "$_new" || return 1
    # shellcheck disable=SC2086
    if ! download "$_dl" $CHROMA_SEED_URL || ! unpack_parts "$_dl" "$_new"; then
        rm -rf "$_dl" "$_new"
        return 1
    fi
    rm -rf "$_dl"
    if ! python3 -m tutor.seed_check chroma "$_new"; then
        rm -rf "$_new"
        return 1
    fi
    rm -f "$CHROMA_DIR/.seed-ok"
    for _e in "$CHROMA_DIR"/* "$CHROMA_DIR"/.[!.]*; do
        [ -e "$_e" ] || continue
        case "${_e##*/}" in
            .seed-new|lost+found|lecture-audio|lecture-audio.new|lecture-audio.download|tts-cache) continue ;;
        esac
        rm -rf "$_e" || return 1
    done
    for _e in "$_new"/* "$_new"/.[!.]*; do
        [ -e "$_e" ] || continue
        mv "$_e" "$CHROMA_DIR"/ || return 1
    done
    rmdir "$_new"
    touch "$CHROMA_DIR/.seed-ok"
}

# ---- ChromaDB store (persistent disk, seeded once) --------------------------
mkdir -p "$CHROMA_DIR"
if [ -f "$CHROMA_DIR/.seed-ok" ]; then
    echo "[entrypoint] ChromaDB already seeded — skipping."
elif [ -f "$CHROMA_DIR/chroma.sqlite3" ] && python3 -m tutor.seed_check chroma "$CHROMA_DIR"; then
    # A store seeded before the marker existed: adopt it once it checks out.
    touch "$CHROMA_DIR/.seed-ok"
    echo "[entrypoint] ChromaDB present and readable — marked as seeded."
elif [ -n "$CHROMA_SEED_URL" ]; then
    echo "[entrypoint] ChromaDB missing or incomplete — seeding from CHROMA_SEED_URL ..."
    if seed_chroma; then
        echo "[entrypoint] ChromaDB seed complete."
    else
        warn "ChromaDB seed failed; the tutor answers without retrieval until a later boot seeds it."
    fi
else
    warn "ChromaDB is empty and CHROMA_SEED_URL is unset; find_topic returns nothing."
fi

# ---- Lecture narration audio (persistent disk, seeded once) -----------------
# ~270 MB of MP3s served at /lecture-audio, in a subdirectory of the same disk.
# The GitHub Release asset stays the source/backup; the disk is the runtime copy.
if [ -f "$AUDIO_DIR/.seed-ok" ]; then
    echo "[entrypoint] Lecture audio already seeded — skipping."
elif [ -d "$AUDIO_DIR" ] && [ -n "$(ls -A "$AUDIO_DIR" 2>/dev/null)" ]; then
    # Seeded before the marker existed. MP3s have no cheap integrity check, so
    # an existing copy is kept; delete the directory to force a fresh seed.
    touch "$AUDIO_DIR/.seed-ok"
    echo "[entrypoint] Lecture audio present — marked as seeded."
elif [ -n "$AUDIO_SEED_URL" ]; then
    echo "[entrypoint] Lecture audio absent — seeding from AUDIO_SEED_URL ..."
    if seed_dir "$AUDIO_SEED_URL" "$AUDIO_DIR" parts; then
        echo "[entrypoint] Lecture audio seed complete ($(du -sh "$AUDIO_DIR" | cut -f1))."
    else
        warn "Lecture audio seed failed; /lecture-audio/... returns 404 until a later boot seeds it."
    fi
else
    echo "[entrypoint] AUDIO_SEED_URL unset — lecture narration (/lecture-audio/...) will 404."
fi

# ---- Slide decks (ephemeral filesystem, every boot) -------------------------
# OUTPUT_SEED_URL may be a space-separated list of tarball URLs. A non-empty
# directory without a marker (a local bind mount of generated lectures) is
# never replaced.
if [ -f "$OUTPUT_DIR/.seed-ok" ] || [ -n "$(ls -A "$OUTPUT_DIR" 2>/dev/null)" ]; then
    echo "[entrypoint] Slides already present — skipping."
    # A present tree is kept, but a partial one is said (review step 0.3,
    # F8): every lecture of both courses must have its deck.
    if ! python3 -m tutor.seed_check slides "$OUTPUT_DIR"; then
        warn "Slides present but incomplete; the missing lectures answer 404 under /slides (see the seed check above)."
    fi
elif [ -n "$OUTPUT_SEED_URL" ]; then
    echo "[entrypoint] Seeding slides from OUTPUT_SEED_URL ..."
    if seed_dir "$OUTPUT_SEED_URL" "$OUTPUT_DIR" each; then
        # Narration scripts are not for students (plan step 2.9, decision 2);
        # an older seed may still carry them.
        find "$OUTPUT_DIR" -name script.md -type f -delete 2>/dev/null || true
        echo "[entrypoint] Slide seed complete."
        if ! python3 -m tutor.seed_check slides "$OUTPUT_DIR"; then
            warn "Slide seed is incomplete; the missing lectures answer 404 under /slides (see the seed check above)."
        fi
    else
        warn "Slide seed failed; /slides/... returns 404 until the next boot."
    fi
else
    echo "[entrypoint] No OUTPUT_SEED_URL set — slide images (/slides/...) will 404."
fi

# ---- Mastery exam bank (ephemeral filesystem, every boot) -------------------
# A small JSON file (~470 KB); installed only once it downloads and parses.
if [ -f "$MASTERY_PATH" ]; then
    echo "[entrypoint] Mastery exam bank already present — skipping download."
elif [ -n "$MASTERY_EXAMS_URL" ]; then
    echo "[entrypoint] Downloading mastery exam bank from MASTERY_EXAMS_URL ..."
    mkdir -p "$(dirname "$MASTERY_PATH")"
    if fetch "$MASTERY_EXAMS_URL" "$MASTERY_PATH.download" \
            && python3 -m tutor.seed_check json "$MASTERY_PATH.download" \
            && mv "$MASTERY_PATH.download" "$MASTERY_PATH"; then
        echo "[entrypoint] Mastery exam bank ready ($(wc -c < "$MASTERY_PATH") bytes)."
    else
        rm -f "$MASTERY_PATH.download"
        warn "Mastery exam bank download failed; prerequisite exams are off until the next boot."
    fi
else
    echo "[entrypoint] MASTERY_EXAMS_URL unset — mastery exam gating disabled."
fi

# ---- Drop root (plan step 6.5, finding E16) ---------------------------------
# The seeds above run as root: the persistent disk holds files a root process
# wrote before the image had a user of its own. Everything from here (the
# database wait, the migrations, the app) runs as the image's `app` user, which
# is handed what the app writes: the disk (the store's SQLite files), the slide
# directory, and /app/data. HOME moves too, since the embedding model is cached
# under the app user's home. Outside the image (the tests, a developer's shell)
# there is no root or no `app` user, and nothing changes.
AS_APP=""
if [ "$(id -u)" = 0 ] && id -u app > /dev/null 2>&1; then
    for _d in "$CHROMA_DIR" "$OUTPUT_DIR" /app/data; do
        [ -d "$_d" ] && find "$_d" ! -user app -exec chown app:app {} +
    done
    export HOME=/home/app
    AS_APP="setpriv --reuid=app --regid=app --init-groups --"
    echo "[entrypoint] Running the app as $(id -un app) (uid $(id -u app))."
fi

# Wait for the DB to accept connections before handing off to uvicorn. This is
# a backstop behind the "Restore Supabase project if paused" step in
# .github/workflows/deploy.yml: that step waits up to 5 min for Supabase to
# report ACTIVE_HEALTHY before it even triggers this deploy, but a manual
# Render deploy (dashboard button, not through the GitHub Action) skips that
# wait entirely. The app itself also starts on a database that is still down
# (tutor/startup.py prepare_database, retried from /healthz). No-op instantly
# on SQLite (DATABASE_URL unset).
if [ -n "$DATABASE_URL" ]; then
    echo "[entrypoint] Waiting for database connection..."
    $AS_APP python3 -c "
import sys, time
from tutor.db.models import get_engine

engine = get_engine()
deadline = time.time() + 120
attempt = 0
while True:
    attempt += 1
    try:
        with engine.connect():
            print(f'[entrypoint] Database reachable (attempt {attempt}).')
            sys.exit(0)
    except Exception as exc:
        if time.time() >= deadline:
            print(f'[entrypoint] WARNING: database still unreachable after {attempt} attempts: {exc}')
            print('[entrypoint]          starting uvicorn anyway; it will retry per-request.')
            sys.exit(0)
        print(f'[entrypoint]   attempt {attempt} failed ({exc.__class__.__name__}); retrying in 5s...')
        time.sleep(5)
" || true
fi

# ---- Schema migrations (plan step 2.8, decision 8) -------------------------
# Bring the database to the Alembic head before the app starts, so new tables
# are born with row-level security. tutor.migrate stamps a database the app
# built at boot (no alembic_version row) at the baseline first; every later
# revision is guarded. A failure warns and boots anyway: the app's own boot
# setup (startup.ensure_schema) still adds any missing column.
# RUN_MIGRATIONS=0 skips the step.
if [ "${RUN_MIGRATIONS:-1}" = 1 ]; then
    echo "[entrypoint] Applying database migrations ..."
    if ! $AS_APP python3 -m tutor.migrate; then
        warn "Database migration failed; the app starts on its boot-time schema setup. See the log above."
    fi
fi

# --timeout-graceful-shutdown: on a deploy's SIGTERM, uvicorn waits this
# long for open replies, then cancels them, and the lifespan records each one
# a restart cut off (docs/bulletproofing-plan-2026-10.md, R31). Without it,
# Render's SIGKILL after its 30 s grace left nothing but the boot sweep.
# shellcheck disable=SC2086  # AS_APP is a command prefix, or nothing
exec $AS_APP uvicorn tutor.main:app --host 0.0.0.0 --port "${PORT:-8000}" --timeout-graceful-shutdown 15
