"""Unified CLI entry point for the llm-tutor content pipeline.

Usage: python -m pipeline <command> [options]
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from tutor.courses import COURSES, course_label, default_course

console = Console()
DEFAULT_COURSE = default_course()
# --course on every command: a course id from courses.yaml.
COURSE_CHOICE = click.Choice(list(COURSES), case_sensitive=False)
# Every default path is anchored here, never on the working directory (plan
# step 5.8, finding E17): run from anywhere, the CLI reads and writes the
# same folders. A path typed on the command line is the caller's, as usual.
ROOT = Path(__file__).resolve().parent.parent


def _lectures_or_fail(course: str, nums: list[int], param_hint: str = "--lecture") -> list[int]:
    """``nums`` when every one is a lecture of ``course``; otherwise a usage
    error, before any generation call is billed (plan step 5.8, E23)."""
    from pipeline.config import lecture_numbers

    valid = lecture_numbers(course)
    bad = [n for n in nums if n not in valid]
    if bad or not nums:
        raise click.BadParameter(
            f"{course_label(course)} has lectures {valid[0]} to {valid[-1]}; got {bad or 'none'}",
            param_hint=param_hint)
    return nums


def _render_deck(cc: dict, course: str, num: int) -> Path:
    """Render a lecture's Reveal deck from its content.md; the path, or a
    RuntimeError when the content has no slide to render."""
    from pipeline.render.html_renderer import render_lecture

    lec_dir = Path(cc["output_dir"]) / f"lecture-{num:02d}"
    out = lec_dir / "index.html"
    course_name = cc.get("course_name", course_label(course))
    if not render_lecture(lec_dir / "content.md", out, f"{course_name} \u2014 Lecture {num:02d}"):
        raise RuntimeError(f"{lec_dir / 'content.md'} has no slide to render")
    return out


@click.group()
def cli():
    """llm-tutor pipeline: ingest, analyze, and generate course materials."""


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE,
              help="Course id from courses.yaml")
@click.option("--dry-run", is_flag=True, help="Show what would be ingested without running")
@click.option("--quiet", is_flag=True, help="Suppress verbose output")
def ingest(course: str, dry_run: bool, quiet: bool):
    """Ingest slides, textbook, exams, and assignments from source-materials/."""
    from pipeline.ingest.batch_ingest import run_ingest
    console.print(f"[bold cyan]Ingesting course {course}...[/bold cyan]")
    run_ingest(course, dry_run=dry_run, verbose=not quiet)
    if dry_run:
        console.print("[yellow]Dry run complete. No files written.[/yellow]")
    else:
        console.print("[green]Ingestion complete.[/green]")


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--recordings-dir", default=None, help="Folder of recordings (default: config.yaml recordings.dir)")
@click.option("--output-dir", default=None, help="Output directory for transcripts (default: knowledge-base/raw/transcripts)")
@click.option("--model", default=None, help="Whisper model size, tiny to large (default: config.yaml whisper.model)")
@click.option("--skip-existing/--no-skip", default=True, show_default=True)
@click.option("--quiet", is_flag=True)
def transcribe(recordings_dir: str, output_dir: str, model: str, skip_existing: bool, quiet: bool):
    """Transcribe lecture recordings using local Whisper model."""
    from pipeline.config import get_kb_config, get_recordings_config, get_whisper_config
    from pipeline.ingest.transcriber import batch_transcribe
    kb = get_kb_config()
    whisper_cfg = get_whisper_config()
    source = Path(recordings_dir) if recordings_dir else get_recordings_config()["dir"]
    out = Path(output_dir) if output_dir else kb["raw_dir"] / "transcripts"
    model = model or whisper_cfg["model"]
    console.print(f"[bold cyan]Transcribing from {source} -> {out} (model={model})...[/bold cyan]")
    written = batch_transcribe(
        recordings_dir=source,
        output_dir=out,
        model_size=model,
        language=whisper_cfg["language"],
        skip_existing=skip_existing,
        verbose=not quiet,
    )
    console.print(f"[green]Transcribed {len(written)} file(s).[/green]")


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--rebuild", is_flag=True,
              help="Drop the collections first, so chunks of removed or re-chunked files go too.")
@click.option("--quiet", is_flag=True)
def index(rebuild: bool, quiet: bool):
    """Build ChromaDB vector index from all ingested raw data.

    Writes manifest.json into the store (inputs, hashes, counts, embedder,
    commit). A file that is not the JSON its collection expects is skipped and
    listed, and the command then exits 1 (plan step 6.7).
    """
    from pipeline.knowledge_base.indexer import MANIFEST, BuildLog, build_index
    from pipeline.config import get_kb_config

    console.print("[bold cyan]Building vector index" + (" from scratch" if rebuild else "") + "...[/bold cyan]")
    log = BuildLog()
    counts = build_index(verbose=not quiet, rebuild=rebuild, log=log)
    for col, count in counts.items():
        console.print(f"  {col}: {count} documents")
    for note in log.notes:
        console.print(f"  {note}", style="yellow", markup=False, soft_wrap=True)
    console.print(f"Manifest: {get_kb_config()['db_dir'] / MANIFEST}", markup=False, soft_wrap=True)
    if log.failures:
        console.print(f"[red]{len(log.failures)} file(s) could not be read and were skipped:[/red]")
        for failure in log.failures:
            console.print(f"  {failure}", markup=False, soft_wrap=True)
        raise SystemExit(1)
    console.print("[green]Index built.[/green]")


# ---------------------------------------------------------------------------
# index-transcripts
# ---------------------------------------------------------------------------

@cli.command("index-transcripts")
@click.option("--transcripts-dir", default=None, multiple=True,
              help="Directory containing transcript JSON files (can specify multiple; default: both pipeline and whisper-CLI dirs)")
@click.option("--collection", default="transcripts", show_default=True, help="ChromaDB collection name")
@click.option("--quiet", is_flag=True)
def index_transcripts(transcripts_dir: tuple[str, ...], collection: str, quiet: bool):
    """Index transcript JSON files into ChromaDB. Handles pipeline and whisper CLI JSON formats."""
    from pipeline.config import get_kb_config
    from pipeline.knowledge_base.indexer import BuildLog, _index_transcripts_from_dir, transcript_dirs, transcript_files
    from pipeline.knowledge_base.vectorstore import get_client
    kb = get_kb_config()
    client = get_client(kb["db_dir"])
    # Default: the folders `index` builds this collection from (one list).
    dirs = ([Path(d) for d in transcripts_dir] if transcripts_dir
            else transcript_dirs(kb["raw_dir"]).get(collection, []))
    total = 0
    for t_dir in dirs:
        if not t_dir.exists() and not quiet:
            console.print(f"  [yellow]Skipping {t_dir} (not found)[/yellow]")
    log = BuildLog()
    # A recording in two of the folders is indexed once (transcript_files).
    # Every folder, missing ones included: the first is the pipeline's own,
    # whose copy of a recording wins over a bare Whisper copy (transcript_files).
    for t_dir, files in transcript_files(dirs, log):
        if not t_dir.exists():
            continue
        console.print(f"[bold cyan]Indexing {t_dir} -> {collection}...[/bold cyan]")
        count = _index_transcripts_from_dir(client, collection, t_dir, verbose=not quiet, log=log, files=files)
        total += count
        console.print(f"  {count} segments indexed from {t_dir.name}")
    for note in log.notes:
        console.print(f"  {note}", markup=False, soft_wrap=True)
    console.print(f"[green]Total: {total} transcript segments indexed into '{collection}'.[/green]")
    if log.failures:
        console.print(f"[red]{len(log.failures)} file(s) could not be read and were skipped:[/red]")
        for failure in log.failures:
            console.print(f"  {failure}", markup=False, soft_wrap=True)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--quiet", is_flag=True)
def analyze(course: str, quiet: bool):
    """Extract instructor style, curriculum map, and examples library."""
    from pipeline.config import get_course_config, get_curriculum_config, get_instructor_profile_config, get_kb_config
    from pipeline.analyze.style_extractor import extract_style
    from pipeline.analyze.curriculum_mapper import map_curriculum
    from pipeline.analyze.example_extractor import extract_examples

    cc = get_course_config(course)
    kb = get_kb_config()
    ip = get_instructor_profile_config()
    curr = get_curriculum_config()

    console.print("[bold cyan]Extracting instructor style...[/bold cyan]")
    extract_style(kb["raw_dir"], ip["output_dir"], verbose=not quiet)

    console.print("[bold cyan]Mapping curriculum...[/bold cyan]")
    map_curriculum(cc, kb["raw_dir"], curr["output_dir"], verbose=not quiet)

    console.print("[bold cyan]Extracting examples library...[/bold cyan]")
    extract_examples(kb["raw_dir"], ip["output_dir"], verbose=not quiet)

    console.print("[green]Analysis complete.[/green]")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--quiet", is_flag=True)
def plan(course: str, quiet: bool):
    """Draft the course's lecture plan (curriculum/<course>-lecture-plan.md)
    from its topic map, with as many lectures as courses.yaml gives it."""
    from pipeline.config import get_course_config, get_curriculum_config, get_instructor_profile_config, get_kb_config
    from pipeline.generate.curriculum_planner import generate_curriculum_plan

    cc = get_course_config(course)
    kb = get_kb_config()
    ip = get_instructor_profile_config()
    curr = get_curriculum_config()

    console.print("[bold cyan]Generating curriculum plan...[/bold cyan]")
    out = generate_curriculum_plan(
        course_config=cc,
        curriculum_dir=curr["output_dir"],
        instructor_profile_dir=ip["output_dir"],
        raw_dir=kb["raw_dir"],
        verbose=not quiet,
    )
    console.print(f"[green]Plan written to {out}[/green]")


# ---------------------------------------------------------------------------
# slides
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--lecture", required=True, type=int, help="Lecture number (1 to the course's lecture count)")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--pptx", "with_pptx", is_flag=True, help="Also write the retired slides.pptx")
@click.option("--quiet", is_flag=True)
def slides(lecture: int, course: str, with_pptx: bool, quiet: bool):
    """Generate slide content.md and render its deck (index.html) for one lecture."""
    from pipeline.config import get_course_config, get_curriculum_config, get_instructor_profile_config
    from pipeline.generate.slide_generator import generate_slide_content

    _lectures_or_fail(course, [lecture])
    cc = get_course_config(course)
    ip = get_instructor_profile_config()
    curr = get_curriculum_config()
    output_dir = cc["output_dir"]

    console.print(f"[bold cyan]Generating slides for Lecture {lecture:02d}...[/bold cyan]")
    generate_slide_content(
        lecture_num=lecture,
        course_config=cc,
        curriculum_dir=curr["output_dir"],
        instructor_profile_dir=ip["output_dir"],
        output_dir=output_dir,
        verbose=not quiet,
    )
    deck = _render_deck(cc, course, lecture)
    console.print(f"[green]Slides written to {deck}[/green]")
    if with_pptx:
        from pipeline.generate.pptx_writer import write_pptx
        console.print(f"[green]PowerPoint written to {write_pptx(lecture, output_dir, verbose=not quiet)}[/green]")


# ---------------------------------------------------------------------------
# script
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--lecture", required=True, type=int, help="Lecture number (1 to the course's lecture count)")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--use-api", is_flag=True, help="Bill the metered Claude API instead of the local claude CLI subscription")
@click.option("--quiet", is_flag=True)
def script(lecture: int, course: str, use_api: bool, quiet: bool):
    """Generate lecture script.md for one lecture. Uses the local claude CLI
    subscription by default (no per-token cost); pass --use-api to bill the
    metered API instead (e.g. for unattended CI without a subscription login)."""
    from pipeline.config import get_course_config, get_curriculum_config, get_instructor_profile_config
    from pipeline.generate.script_generator import generate_script

    _lectures_or_fail(course, [lecture])
    cc = get_course_config(course)
    ip = get_instructor_profile_config()
    curr = get_curriculum_config()

    console.print(f"[bold cyan]Generating script for Lecture {lecture:02d}...[/bold cyan]")
    out = generate_script(
        lecture_num=lecture,
        course_config=cc,
        curriculum_dir=curr["output_dir"],
        instructor_profile_dir=ip["output_dir"],
        output_dir=cc["output_dir"],
        verbose=not quiet,
        use_api=use_api,
    )
    console.print(f"[green]Script written to {out}[/green]")


# ---------------------------------------------------------------------------
# generate-all
# ---------------------------------------------------------------------------

@cli.command("generate-all")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--lectures", default=None, help="Range or list, e.g. '1-12', '1,4,8', or '2-3,5-7' (default: every lecture)")
@click.option("--resume", is_flag=True,
              help="Build only what is missing; a lecture `status` calls complete keeps its slides and deck")
@click.option("--skip-existing", is_flag=True, hidden=True, help="Deprecated: the same as --resume")
@click.option("--pptx", "with_pptx", is_flag=True, help="Also write the retired slides.pptx")
@click.option("--use-api", is_flag=True, help="Bill the metered Claude API for script generation instead of the local claude CLI subscription")
@click.option("--quiet", is_flag=True)
def generate_all(course: str, lectures: str | None, resume: bool, skip_existing: bool, with_pptx: bool,
                 use_api: bool, quiet: bool):
    """Generate slides, decks, and scripts for all (or selected) lectures.

    Each lecture gets content.md, its Reveal deck (index.html, what the app
    serves), and script.md. With --resume, a step whose output is present is
    skipped, by the same test `status` uses (pipeline/lectures.py), and a
    content.md with no parseable slide is regenerated rather than stalling
    the run.
    """
    from pipeline.config import get_course_config, get_curriculum_config, get_instructor_profile_config, lecture_numbers
    from pipeline.generate.slide_generator import generate_slide_content
    from pipeline.generate.script_generator import generate_script
    from pipeline.lectures import lecture_dir, lecture_state, parse_lecture_range, steps_to_run

    cc = get_course_config(course)
    ip = get_instructor_profile_config()
    curr = get_curriculum_config()
    output_dir = cc["output_dir"]
    resume = resume or skip_existing
    try:
        lecture_nums = parse_lecture_range(lectures) if lectures else lecture_numbers(course)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--lectures") from exc
    _lectures_or_fail(course, lecture_nums, param_hint="--lectures")

    console.print(f"[bold cyan]Generating {len(lecture_nums)} lectures for course {course}...[/bold cyan]")

    failed: list[int] = []
    for num in lecture_nums:
        steps = steps_to_run(lecture_state(lecture_dir(output_dir, num)), resume=resume, pptx=with_pptx)
        if not steps:
            console.print(f"  Lecture {num:02d}... already complete, skipped")
            continue
        console.print(f"  Lecture {num:02d}... ({', '.join(steps)})")
        try:
            if "content" in steps:
                generate_slide_content(
                    lecture_num=num,
                    course_config=cc,
                    curriculum_dir=curr["output_dir"],
                    instructor_profile_dir=ip["output_dir"],
                    output_dir=output_dir,
                    verbose=False,
                )
            if "html" in steps:
                _render_deck(cc, course, num)
            if "pptx" in steps:
                from pipeline.generate.pptx_writer import write_pptx
                write_pptx(num, output_dir, verbose=not quiet)
            if "script" in steps:
                generate_script(
                    lecture_num=num,
                    course_config=cc,
                    curriculum_dir=curr["output_dir"],
                    instructor_profile_dir=ip["output_dir"],
                    output_dir=output_dir,
                    verbose=False,
                    use_api=use_api,
                )
        except Exception as e:
            console.print(f"  [red]Lecture {num:02d} failed: {e}[/red]")
            failed.append(num)
            continue

    if failed:
        console.print(f"[yellow]Failed lectures: {failed}. Re-run with --lectures {','.join(str(n) for n in failed)} --resume[/yellow]")
    console.print(f"[green]Done. Output in {output_dir}[/green]")


# ---------------------------------------------------------------------------
# canvas
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE,
              help="Course id from courses.yaml")
@click.option("--match", default=None,
              help="Text to find the Canvas course by (default: the course's label from courses.yaml)")
@click.option("--course-id", type=int, default=None, help="Canvas course ID (auto-detected if omitted)")
@click.option("--output-dir", default=None, help="Where to save downloaded files (default: source-materials/<course>/canvas/)")
@click.option("--token-file", default=None, help="Canvas API token file (default: config.yaml canvas.token_file)")
@click.option("--list-courses", "list_only", is_flag=True, help="List all Canvas courses and exit")
@click.option("--quiet", is_flag=True)
def canvas(course: str, match: str | None, course_id: int | None, output_dir: str | None, token_file: str,
           list_only: bool, quiet: bool):
    """Download course materials (slides, PDFs, docs) from Canvas LMS."""
    from pipeline.config import get_canvas_config
    from pipeline.ingest.canvas_reader import download_course_materials, find_course, list_courses
    token_path = Path(token_file) if token_file else get_canvas_config()["token_file"]

    if list_only:
        console.print("[bold cyan]Listing Canvas courses...[/bold cyan]")
        courses = list_courses(token_path)
        for c in courses:
            console.print(f"  {c['id']:>8}  {c['course_code']:<20}  {c['name']}")
        return

    if course_id is None:
        needle = match or course_label(course)
        console.print(f"[cyan]Looking for a Canvas course matching {needle!r}...[/cyan]")
        found = find_course(needle, token_path)
        if not found:
            console.print(f"[red]No Canvas course matches {needle!r}. Use --match, or "
                          "--list-courses then --course-id.[/red]")
            return
        course_id = found["id"]
        console.print(f"  Found: {found['name']} (id={course_id})")

    dest = Path(output_dir) if output_dir else ROOT / "source-materials" / course / "canvas"
    console.print(f"[bold cyan]Downloading Canvas materials for course {course_id} -> {dest}...[/bold cyan]")
    downloaded = download_course_materials(
        course_id=course_id,
        output_dir=dest,
        token_file=token_path,
        verbose=not quiet,
    )
    console.print(f"[green]Downloaded {len(downloaded)} file(s) to {dest}[/green]")


# ---------------------------------------------------------------------------
# canvas-ingest (process already-downloaded canvas files into knowledge base)
# ---------------------------------------------------------------------------

@cli.command("canvas-ingest")
@click.option("--course", default=None, type=COURSE_CHOICE,
              help="Course id; sets default --canvas-dir to source-materials/{course}/canvas")
@click.option("--canvas-dir", default=None,
              help="Override directory to ingest from (default: source-materials/{course}/canvas or source-materials/canvas)")
@click.option("--no-skip", is_flag=True, help="Re-process files even if output already exists")
@click.option("--quiet", is_flag=True)
def canvas_ingest(course: str, canvas_dir: str, no_skip: bool, quiet: bool):
    """Ingest already-downloaded Canvas course files into the knowledge base."""
    from pipeline.config import get_kb_config
    from pipeline.ingest.canvas_ingest import ingest_all_canvas
    kb = get_kb_config()
    source = Path(canvas_dir) if canvas_dir else (
        ROOT / "source-materials" / course / "canvas" if course else ROOT / "source-materials" / "canvas")
    console.print(f"[bold cyan]Ingesting Canvas materials from {source}...[/bold cyan]")
    results = ingest_all_canvas(
        canvas_base=source,
        raw_dir=kb["raw_dir"],
        skip_existing=not no_skip,
        verbose=not quiet,
    )
    console.print(f"[green]Canvas ingest complete: {len(results)} offering(s) processed.[/green]")


# ---------------------------------------------------------------------------
# discord
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--exports-dir", default=None,
              help="Directory containing DiscordChatExporter JSON or TXT files (default: source-materials/discord-exports)")
@click.option("--no-filter", is_flag=True, help="Include all messages, not just course-relevant ones")
@click.option("--quiet", is_flag=True)
def discord(exports_dir: str, no_filter: bool, quiet: bool):
    """Ingest Discord channel exports (DiscordChatExporter JSON format) into the forum collection's folder."""
    from pipeline.config import get_kb_config
    from pipeline.ingest.discord_reader import ingest_discord_exports
    kb = get_kb_config()
    output_dir = kb["raw_dir"] / "forum"
    source = Path(exports_dir) if exports_dir else ROOT / "source-materials" / "discord-exports"
    console.print(f"[bold cyan]Ingesting Discord exports from {source}...[/bold cyan]")
    written = ingest_discord_exports(
        exports_dir=source,
        output_dir=output_dir,
        filter_relevant=not no_filter,
        verbose=not quiet,
    )
    console.print(f"[green]Processed {len(written)} Discord export(s).[/green]")
    if not written:
        console.print(f"[yellow]Tip: Export channels using DiscordChatExporter and place JSON files in {source}/[/yellow]")


# ---------------------------------------------------------------------------
# discord-fetch (fetch via bot API)
# ---------------------------------------------------------------------------

@cli.command("discord-fetch")
@click.option("--messages", default=500, show_default=True, help="Max messages per channel")
@click.option("--no-filter", is_flag=True, help="Don't filter for course-relevant messages")
@click.option("--quiet", is_flag=True)
def discord_fetch(messages: int, no_filter: bool, quiet: bool):
    """Fetch Discord messages from the bot's servers via Discord API into the forum collection's folder."""
    from dotenv import load_dotenv
    load_dotenv()
    from pipeline.config import get_kb_config
    from pipeline.ingest.discord_api_reader import fetch_all_guilds
    kb = get_kb_config()
    console.print("[bold cyan]Fetching Discord messages via bot API...[/bold cyan]")
    written = fetch_all_guilds(
        output_dir=kb["raw_dir"] / "forum",
        messages_per_channel=messages,
        filter_relevant=not no_filter,
        verbose=not quiet,
    )
    console.print(f"[green]Discord fetch complete: {len(written)} file(s) written.[/green]")


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--repos", multiple=True, help="GitHub repo URLs to clone and index")
@click.option("--org", multiple=True, help="GitHub org names to clone all repos from (e.g. my-class-org)")
@click.option("--clone-dir", default=None, help="Where to clone (default: source-materials/github-repos)")
@click.option("--quiet", is_flag=True)
def github(repos: tuple[str, ...], org: tuple[str, ...], clone_dir: str, quiet: bool):
    """Clone GitHub repos or all repos in an org, and extract code files into the code collection's folder."""
    from pipeline.config import get_kb_config
    from pipeline.ingest.github_reader import ingest_github_repos, list_org_repos

    kb = get_kb_config()
    output_dir = kb["raw_dir"] / "code"

    all_repo_urls = list(repos)

    for org_name in org:
        console.print(f"[cyan]Listing repos in org: {org_name}...[/cyan]")
        try:
            org_repos = list_org_repos(org_name)
            urls = [r["clone_url"] for r in org_repos if not r.get("archived")]
            all_repo_urls.extend(urls)
            console.print(f"  Found {len(urls)} active repos in {org_name}")
        except Exception as e:
            console.print(f"  [red]Error listing {org_name}: {e}[/red]")

    if not all_repo_urls:
        console.print("[yellow]No repos found. Use --repos or --org.[/yellow]")
        return

    console.print(f"[bold cyan]Cloning {len(all_repo_urls)} GitHub repo(s)...[/bold cyan]")
    written = ingest_github_repos(
        repo_urls=all_repo_urls,
        clone_dir=Path(clone_dir) if clone_dir else ROOT / "source-materials" / "github-repos",
        output_dir=output_dir,
        verbose=not quiet,
    )
    console.print(f"[green]Ingested {len(written)} repo(s).[/green]")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--course", default=None, type=COURSE_CHOICE, help="Course id to check (omit for every course)")
@click.option("--verbose", "-v", is_flag=True, help="Show per-lecture stats (slide count, deck size, script lines if present)")
def status(course: str, verbose: bool):
    """Show generation status for all lectures.

    Complete means content.md with at least one slide plus its rendered deck
    (index.html): the test `generate-all --resume` skips on
    (pipeline/lectures.py). Flags: C content, H deck, S script.
    """
    from pipeline.config import get_course_config, lecture_numbers
    from pipeline.lectures import lecture_dir, lecture_state
    courses = list(COURSES) if course is None else [course]
    for c in courses:
        cc = get_course_config(c)
        out_dir = Path(cc["output_dir"])
        nums = lecture_numbers(c)
        console.print(f"\n[bold]{course_label(c)}: {out_dir}[/bold]")
        complete = partial = missing = 0
        for num in nums:
            lec_dir = lecture_dir(out_dir, num)
            st = lecture_state(lec_dir)
            flags = ("C" if st.has_content else "-") + ("H" if st.has_html else "-") + ("S" if st.has_script else "-")
            if st.complete:
                complete += 1
                if verbose:
                    deck_kb = (lec_dir / "index.html").stat().st_size // 1024
                    extra = f"{st.slides} slides, {deck_kb}KB deck"
                    if st.has_script:
                        extra += f", {len((lec_dir / 'script.md').read_text().splitlines())} script lines"
                    status_str = f"[green]complete[/green] ({extra})"
                else:
                    status_str = "[green]complete[/green]"
            elif not st.has_content and not st.has_html:
                missing += 1
                status_str = "[red]missing[/red]"
            else:
                partial += 1
                status_str = f"[yellow]partial ({flags})[/yellow]"
            console.print(f"  L{num:02d}: {status_str}")
        console.print(f"  [bold]Total: {complete}/{len(nums)} complete, {partial} partial, {missing} missing[/bold]")


# ---------------------------------------------------------------------------
# mark-slides
# ---------------------------------------------------------------------------

@cli.command("mark-slides")
@click.option("--course", default=None, type=COURSE_CHOICE, help="Course id (omit for every course)")
@click.option("--lecture", default=None, type=int, help="Single lecture number to mark")
@click.option("--quiet", is_flag=True)
def mark_slides(course: str, lecture: int, quiet: bool):
    """Insert <!-- ADVANCE SLIDE N --> markers into script.md files.

    Idempotent: re-running is safe. Markers enable the render pipeline to
    advance slide frames in sync with TTS narration.
    """
    from pipeline.generate.slide_marker import mark_course
    from pipeline.config import get_course_config, lecture_numbers

    courses = list(COURSES) if course is None else [course]
    for c in courses:
        cc = get_course_config(c)
        out_dir = Path(cc["output_dir"])
        results = mark_course(out_dir, lecture=lecture, lectures=lecture_numbers(c))
        total_added = sum(results.values())
        marked_lecs = [n for n, cnt in results.items() if cnt > 0]
        if not quiet:
            console.print(f"[bold]{course_label(c)}[/bold] — marked {len(results)} scripts, "
                          f"{total_added} new markers inserted")
            if marked_lecs:
                console.print(f"  Updated: {marked_lecs}")
            else:
                console.print("  [dim]All scripts already marked (idempotent)[/dim]")


# ---------------------------------------------------------------------------
# render-frames  (PNG export)
# ---------------------------------------------------------------------------

@cli.command("render-frames")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE, help="Course id from courses.yaml")
@click.option("--lecture", default=None, type=int, help="Single lecture (omit = all)")
@click.option("--force", is_flag=True, help="Re-render even if frames exist")
@click.option("--quiet", is_flag=True)
def render_frames(course: str, lecture: int, force: bool, quiet: bool):
    """Render slide frames (PNG) from content.md for each lecture.

    Output: output/<course>/lecture-NN/frames/slide-NN.png
    """
    from pipeline.render.slide_renderer import render_lecture_frames
    from pipeline.config import get_course_config, lecture_numbers

    cc = get_course_config(course)
    out_dir = Path(cc["output_dir"])
    nums = [lecture] if lecture else lecture_numbers(course)

    for num in nums:
        lec_dir = out_dir / f"lecture-{num:02d}"
        if not lec_dir.exists():
            continue
        try:
            frames = render_lecture_frames(lec_dir, force=force)
            if not quiet:
                console.print(f"  L{num:02d}: {len(frames)} frames -> {lec_dir/'frames'}")
        except Exception as exc:
            console.print(f"  [red]L{num:02d} error: {exc}[/red]")


# ---------------------------------------------------------------------------
# generate-mastery-exams
# ---------------------------------------------------------------------------

@cli.command("generate-mastery-exams")
@click.option("--course",   default=None, type=COURSE_CHOICE, help="Filter by course id")
@click.option("--goal-id",  default=None, help="Generate for a single goal ID")
@click.option("--resume",   is_flag=True, help="Skip goals already in mastery_exams.json")
def generate_mastery_exams(course: str, goal_id: str, resume: bool):
    """Generate mini-mastery exam questions for all learning goals via claude CLI.

    Reads  curriculum/learning_goals.json
    Writes curriculum/mastery_exams.json  (saves after every goal)

    Requires the claude CLI to be authenticated (uses subscription, not API key).
    Re-run with --resume after any interruption to fill in the gaps.
    """
    from pipeline.generate_mastery_exams import run_generation
    try:
        total, errors = run_generation(course=course, goal_id=goal_id, resume=resume)
        if errors:
            console.print(f"[yellow]{errors} error(s) — re-run with --resume to retry.[/yellow]")
        console.print(f"[green]{total} exams saved.[/green]")
    except FileNotFoundError as exc:
        console.print(f"[red]ERROR: {exc}[/red]")


# ---------------------------------------------------------------------------
# upload-mastery-exams  (push generated JSON as a GitHub Release asset)
# ---------------------------------------------------------------------------

@cli.command("upload-mastery-exams")
@click.option("--tag", required=True,
              help="The deploy-assets release to attach the asset to (DEPLOY.md names the current one)")
@click.option("--repo", required=True, envvar="GITHUB_REPOSITORY",
              help="owner/name of your GitHub repo (or set GITHUB_REPOSITORY)")
@click.option("--src",  default=None,                       help="Local file to upload (default: curriculum/mastery_exams.json)")
def upload_mastery_exams(tag: str, repo: str, src: str | None):
    """Upload curriculum/mastery_exams.json as a GitHub Release asset.

    Seed blobs live as GitHub Release assets (free egress), not object storage;
    see DEPLOY.md Step 2. This shells out to the gh CLI
    (gh release upload --clobber), then prints the asset's API URL to set as
    MASTERY_EXAMS_URL on Render. Re-uploading mints a new asset id, so the
    URL must be refreshed on Render after every upload.
    """
    import json as _json
    import subprocess

    from tutor.paths import CURRICULUM_DIR

    local_path = Path(src) if src else CURRICULUM_DIR / "mastery_exams.json"
    if not local_path.exists():
        console.print(f"[red]ERROR: {local_path} not found — run generate-mastery-exams first.[/red]")
        raise SystemExit(1)

    size_kb = local_path.stat().st_size // 1024
    console.print(f"[cyan]Uploading {local_path.name} ({size_kb} KB) to release {tag} on {repo} ...[/cyan]")

    up = subprocess.run(
        ["gh", "release", "upload", tag, str(local_path), "--clobber", "--repo", repo],
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        console.print(f"[red]Upload failed:[/red] {up.stderr.strip() or up.stdout.strip()}")
        raise SystemExit(1)

    console.print("[green]Uploaded successfully.[/green]")

    # entrypoint.sh fetches the asset's api.github.com URL (the *_SEED_URL form);
    # re-uploading mints a fresh id, so read it back rather than guessing.
    view = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "assets"],
        capture_output=True, text=True,
    )
    asset_url = None
    if view.returncode == 0:
        try:
            assets = _json.loads(view.stdout).get("assets", [])
            match = next((a for a in assets if a.get("name") == local_path.name), None)
            if match:
                asset_url = match.get("apiUrl") or match.get("url")
        except _json.JSONDecodeError:
            pass

    if asset_url:
        console.print(f"Set on Render: [bold]MASTERY_EXAMS_URL={asset_url}[/bold]")
    else:
        console.print(
            f"Could not auto-detect the new asset URL. Run:\n"
            f"  gh release view {tag} --repo {repo} --json assets\n"
            f"and set MASTERY_EXAMS_URL to the {local_path.name} asset's apiUrl."
        )


# ---------------------------------------------------------------------------
# render-html  (Reveal.js standalone HTML — replaces PNG frames)
# ---------------------------------------------------------------------------

@cli.command("render-html")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE, help="Course id from courses.yaml")
@click.option("--lecture", default=None, type=int, help="Single lecture (omit = all)")
@click.option("--force", is_flag=True, help="Re-render even if index.html already exists")
@click.option("--quiet", is_flag=True)
def render_html(course: str, lecture: int, force: bool, quiet: bool):
    """Render Reveal.js HTML presentations from content.md for each lecture.

    Output: output/<course>/lecture-NN/index.html
    Replaces the PNG frame approach: smaller files, real text, fully accessible.
    """
    from pipeline.config import get_course_config, lecture_numbers
    from pipeline.lectures import lecture_state

    cc = get_course_config(course)
    out_dir = Path(cc["output_dir"])
    nums = [lecture] if lecture else lecture_numbers(course)

    for num in nums:
        lec_dir = out_dir / f"lecture-{num:02d}"
        if not (lec_dir / "content.md").exists():
            continue
        if (lec_dir / "index.html").exists() and not force:
            if not quiet:
                console.print(f"  L{num:02d}: skip (index.html exists)")
            continue
        try:
            out_html = _render_deck(cc, course, num)
            if not quiet:
                console.print(f"  L{num:02d}: {lecture_state(lec_dir).slides} slides -> {out_html}")
        except Exception as exc:
            console.print(f"  [red]L{num:02d} error: {exc}[/red]")


# ---------------------------------------------------------------------------
# render-video  (ffmpeg MP4)
# ---------------------------------------------------------------------------

@cli.command("render-video")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--lecture", default=None, type=int, help="Single lecture (omit = all)")
@click.option("--audio-dir", default=None, help="Directory with lecture-NN.mp3 audio files")
@click.option("--force", is_flag=True, help="Re-render even if MP4 exists")
@click.option("--quiet", is_flag=True)
def render_video(course: str, lecture: int, audio_dir: str, force: bool, quiet: bool):
    """Combine slide frames + audio into MP4 lecture videos.

    Timing comes from: (1) lec_dir/timing.json if present, else (2) ~X min
    hints in script.md. Audio is optional; omitting it produces a silent
    preview video useful for checking slide order and transitions.

    Typical workflow:
        python -m pipeline render-frames --course AST101
        python -m pipeline render-video  --course AST101 --audio-dir output/AST101/audio/
    """
    from pipeline.render.video_renderer import render_course_videos
    from pipeline.config import get_course_config

    cc = get_course_config(course)
    out_dir = Path(cc["output_dir"])
    audio_path = Path(audio_dir) if audio_dir else None

    results = render_course_videos(
        out_dir,
        lecture=lecture,
        audio_dir=audio_path,
        force=force,
    )
    if not quiet:
        for num, mp4 in results.items():
            size_mb = mp4.stat().st_size / 1_048_576
            console.print(f"  L{num:02d}: {mp4.name} ({size_mb:.1f} MB)")
        console.print(f"[green]{len(results)} video(s) complete.[/green]")


# ---------------------------------------------------------------------------
# render-audio  (ElevenLabs per-slide narration)
# ---------------------------------------------------------------------------

@cli.command("render-book-audio")
@click.option("--chapter", "chapters", multiple=True,
              help="A chapter slug (the .qmd file's stem) such as ast101-01-the-sky (repeatable; default: every chapter)")
@click.option("--spend", is_flag=True, help="Call ElevenLabs and write the clips; without it, a dry run")
@click.option("--budget", type=int, default=None, help="Stop before a chapter that would pass this many billed characters")
@click.option("--out", "out_dir", default=str(ROOT / "output" / "book"), show_default=True,
              help="Where the clips go: <lecture audio dir>/book")
@click.option("--model", default=None, help="ElevenLabs model_id (default: the renderer's)")
@click.option("--quiet", is_flag=True)
def render_book_audio_cmd(chapters: tuple[str, ...], spend: bool, budget, out_dir: str, model, quiet: bool):
    """Narrate the course book, one MP3 per chapter, for signed-in students.

    A dry run (the default) lists every chapter with its spoken characters and
    what a run would bill. --spend synthesizes, chapter by chapter, through the
    lecture renderer's cached chunk call; a chapter whose clip exists is
    skipped, so a stopped run resumes. The clips are served at
    /lecture-audio/book/<slug>.mp3 by book/narration.js (DEPLOY.md says how they
    reach the disk).

        python -m pipeline render-book-audio                       # dry run
        python -m pipeline render-book-audio --chapter ast101-01-the-sky --spend
        python -m pipeline render-book-audio --spend --budget 120000
    """
    import os
    from dotenv import load_dotenv
    from pipeline.render.book_audio import render_book_audio

    load_dotenv()
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")
    if not api_key or not voice_id:
        raise click.ClickException("ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must both be set in .env")
    summary = render_book_audio(
        ROOT / "book", Path(out_dir), api_key=api_key, voice_id=voice_id,
        cache_dir=ROOT / "tts-cache" / "book", only=list(chapters) or None,
        spend=spend, budget=budget, model=model, verbose=not quiet,
    )
    rows = summary["chapters"]
    todo = [r for r in rows if not r["exists"]]
    chars = sum(r["chars"] for r in todo)
    if not quiet:
        for r in rows:
            console.print(f"  {r['slug']:<34} {r['chars']:>7,} chars  {'cached' if r['exists'] else ''}")
    console.print(f"{len(rows)} chapters: {len(rows) - len(todo)} cached, {len(todo)} to render, "
                  f"{chars:,} characters to bill (about {chars // 2:,} to {chars:,} credits).")
    if spend:
        console.print(f"Rendered {summary['rendered']} chapters, {summary['billed_chars']:,} characters billed"
                      + (f"; {summary['stopped']}." if summary["stopped"] else "."))
    else:
        console.print("Dry run: nothing rendered. Add --spend to render.")


@cli.command("render-audio")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--lecture", required=True, type=int, help="Lecture number (1 to the course's lecture count)")
@click.option("--model", default=None, help="ElevenLabs model_id (default: the renderer's, the app's TTS model)")
@click.option("--force", is_flag=True, help="Re-synthesize slides even if slide-NN.mp3 exists")
@click.option("--quiet", is_flag=True)
def render_audio(course: str, lecture: int, model: str, force: bool, quiet: bool):
    """Synthesize per-slide narration audio from a lecture script with ElevenLabs.

    Reads the spoken `> "..."` lines from script.md, writes one MP3 per slide to
    `output/<course>/lecture-NN/audio/slide-NN.mp3`, a concatenated
    `.../audio/lecture-NN.mp3`, and `lecture-NN/timing.json` (per-slide seconds)
    that `render-video` consumes for frame-accurate timing.

    Requires ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID in the environment and
    ffmpeg/ffprobe on PATH. This calls the paid ElevenLabs API (chunk responses
    are cached, so re-runs are free for unchanged text).

    Typical workflow:
        python -m pipeline render-audio  --course AST101 --lecture 1
        python -m pipeline render-video  --course AST101 --lecture 1 \\
            --audio-dir output/AST101/lecture-01/audio/
    """
    import os
    from dotenv import load_dotenv
    from pipeline.config import get_course_config
    from pipeline.render.audio_renderer import render_lecture_audio

    load_dotenv()
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "")
    if not api_key or not voice_id:
        raise click.ClickException(
            "ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID must both be set in .env"
        )

    cc = get_course_config(course)
    lec_dir = Path(cc["output_dir"]) / f"lecture-{lecture:02d}"
    if not lec_dir.exists():
        raise click.ClickException(f"Lecture dir not found: {lec_dir}")

    cache_dir = ROOT / "tts-cache" / "lectures"
    console.print(f"[bold cyan]Synthesizing narration for {course} Lecture {lecture:02d}...[/bold cyan]")
    summary = render_lecture_audio(
        lec_dir,
        api_key=api_key,
        voice_id=voice_id,
        model=model,
        cache_dir=cache_dir,
        force=force,
        verbose=not quiet,
    )
    console.print(
        f"[green]{summary['slides']} slides "
        f"({summary['synthesized_slides']} newly synthesized, "
        f"{summary['billed_chars']:,} chars billed) -> "
        f"{summary['total_seconds'] / 60:.1f} min audio.[/green]"
    )
    console.print(f"[green]Audio: {summary['lecture_mp3']}[/green]")


# ---------------------------------------------------------------------------
# generate-teachme  (cached "Teach me" openers via subscription CLI)
# ---------------------------------------------------------------------------

@cli.command("generate-teachme")
@click.option("--course", default=DEFAULT_COURSE, show_default=True, type=COURSE_CHOICE)
@click.option("--lecture", default=None, type=int, help="Only goals for this lecture (omit = all)")
@click.option("--goal", "goal_id", default=None, help="Only this goal id (for spot-checking)")
@click.option("--limit", default=None, type=int, help="Cap number of goals (for a validation slice)")
@click.option("--force", is_flag=True, help="Regenerate even if the goal's cache file exists")
@click.option("--quiet", is_flag=True)
def generate_teachme(course: str, lecture: int, goal_id: str, limit: int, force: bool, quiet: bool):
    """Pre-generate cached "Teach me" opener explanations via the Claude CLI.

    For every learning goal (optionally filtered), writes
    curriculum/teachme/<course>/<goal_id>.json with an opener per track at three
    depths (brief/standard/thorough), grounded in ChromaDB. Uses the local
    `claude` subscription CLI — no metered API — so it can run offline and the
    committed cache serves at runtime with zero API calls.
    """
    import json as _json
    from pipeline.generate.teachme_generator import generate_teachme as _run

    from tutor.paths import CURRICULUM_DIR

    goals_path = CURRICULUM_DIR / "learning_goals.json"
    tracks_path = CURRICULUM_DIR / "tracks.json"
    goals = [g for g in _json.loads(goals_path.read_text())["goals"] if g.get("course") == course]
    tracks = [t for t in _json.loads(tracks_path.read_text())["tracks"] if t.get("course") == course]

    if lecture is not None:
        goals = [g for g in goals if g.get("lecture") == lecture]
    if goal_id:
        goals = [g for g in goals if g["id"] == goal_id]
    if limit:
        goals = goals[:limit]

    if not goals:
        raise click.ClickException("No goals matched the filter")
    if not tracks:
        raise click.ClickException(f"No tracks found for course {course}")

    console.print(f"[bold cyan]Generating Teach-me openers: {len(goals)} goals x "
                  f"{len(tracks)} tracks x 3 depths for {course}...[/bold cyan]")
    summary = _run(goals, tracks, course, force=force, verbose=not quiet)
    console.print(f"[green]{summary['written']} written, {summary['skipped']} skipped "
                  f"-> {summary['out_dir']}[/green]")
    if summary.get("failed"):
        console.print(f"[yellow]{len(summary['failed'])} failed (re-run to retry): "
                      f"{', '.join(summary['failed'])}[/yellow]")


# ---------------------------------------------------------------------------
# extract-goals
# ---------------------------------------------------------------------------

@cli.command("extract-goals")
@click.option("--course", "courses", multiple=True, type=COURSE_CHOICE,
              help="Only this course (repeatable; default: every course). The others' goals are kept.")
@click.option("--dry-run", is_flag=True, help="Parse the lecture plans and print the first lecture; call nothing")
@click.option("--out", default=None, help="Where to write (default: curriculum/learning_goals.json)")
def extract_goals_cmd(courses: tuple[str, ...], dry_run: bool, out: str | None):
    """Draft learning goals from the lecture plans into curriculum/learning_goals.json.

    One claude CLI call per course (your subscription, never the API key) for
    the goals; the prerequisite edges within a course come from each
    lecture's "Prerequisite Lectures" line. Edges between courses are yours
    to add (python -m pipeline audit-goals --propose suggests some).
    """
    from pipeline.extract_goals import OUTPUT_PATH, run

    try:
        run(list(courses) or None, dry_run=dry_run, out=Path(out) if out else OUTPUT_PATH)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc


# ---------------------------------------------------------------------------
# goal graph audit (Session 12)
# ---------------------------------------------------------------------------

from pipeline.audit_goals import apply_goals_diff_cmd, audit_goals_cmd
from pipeline.anchor_goals import apply_anchors_cmd, propose_anchors_cmd
from pipeline.build_exercises import build_exercises
from pipeline.build_exercise_tests import build_exercise_tests
from pipeline.audit_turns import audit_turns
from pipeline.check_content import check_content_cmd
from pipeline.cost_report import cost_report
from pipeline.init_tracks import init_tracks_cmd
from pipeline.order_tracks import order_tracks_cmd
from pipeline.sample_preferences import sample_preferences

cli.add_command(audit_goals_cmd)
cli.add_command(apply_goals_diff_cmd)
cli.add_command(propose_anchors_cmd)
cli.add_command(apply_anchors_cmd)
cli.add_command(build_exercises)
cli.add_command(build_exercise_tests)
cli.add_command(audit_turns)
cli.add_command(cost_report)
cli.add_command(order_tracks_cmd)
cli.add_command(sample_preferences)
cli.add_command(check_content_cmd)
cli.add_command(init_tracks_cmd)


if __name__ == "__main__":
    cli()
