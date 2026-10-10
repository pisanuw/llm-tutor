"""Plan step 6.1 (finding A22): a test behind every Phase 0 to 2 fix.

REGISTRY names, for each finding those phases closed, the tests that fail
when its fix is reverted. test_every_listed_test_exists fails when one of
them is deleted or renamed, so a fix cannot lose its test in silence, and
test_every_phase_0_to_2_finding_is_registered fails when a finding the plan
says those phases closed has no entry.

The ten fixes step 6.1 names (A1, A4, A2, A5, A3, A10, A6, the FK-enforced
erasure, the cross-course navigator, the gate-chain walk) were reverted one
at a time and each named test failed; the others were checked the same way
when their step shipped. Steps 0.1 and 0.2 and the indexes of 2.8 had no
test of their own and get one here.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
HERE = "test_fix_registry.py"

REGISTRY: dict[str, list[str]] = {
    # Phase 0
    "E1": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "F1": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "F2": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "F3": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "F4": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "F5": [f"{HERE}::test_student_materials_stay_out_of_git_and_the_image"],
    "E3": [f"{HERE}::test_the_nightly_dump_is_encrypted_before_it_is_stored"],
    "E19": [f"{HERE}::test_the_daily_report_uploads_no_student_files"],
    "E4": ["test_audit_turns.py::test_student_pseudonym",
           "test_audit_turns.py::test_no_workflow_pushes_and_audit_is_read_only"],
    "B1": ["test_erasure.py::test_delete_removes_every_row_for_the_student_with_foreign_keys_enforced",
           "test_erasure.py::test_anonymize_removes_free_text_everywhere_with_foreign_keys_enforced"],
    "B19": ["test_erasure.py::test_erasure_scope_covers_every_table_that_holds_student_rows",
            "test_erasure.py::test_anonymize_policy_decides_every_table_in_scope"],
    # Renamed with the workflow in d9e6965b: deploying is a manual dispatch, so
    # E2's "every push shipped before CI finished" is closed by there being no
    # automatic trigger at all, and E18's "a fork branch named main could
    # deploy" by a dispatch needing write access plus the green-run check on
    # the exact commit (the job-level `if` those two used to live in is gone).
    "E2": ["test_deploy_gate.py::test_deploy_is_triggered_by_hand_and_nothing_else",
           "test_deploy_gate.py::test_deploy_refuses_a_commit_with_no_green_tests_run",
           "test_deploy_gate.py::test_render_blueprint_turns_auto_deploy_off"],
    "E18": ["test_deploy_gate.py::test_deploy_is_triggered_by_hand_and_nothing_else",
            "test_deploy_gate.py::test_deploy_refuses_a_commit_with_no_green_tests_run"],
    "A1": ["test_turn_persistence.py::test_normal_turn_is_committed_before_done",
           "test_turn_persistence.py::test_exam_clarification_is_committed_before_done",
           "test_turn_persistence.py::test_api_chat_reads_the_whole_turn"],
    "D1": ["test_failure_states.py::test_failed_turn_sends_error_then_done_and_saves_the_real_cause"],
    "D2": ["test_failure_states.py::test_page_without_session_redirects_to_login_with_next",
           "test_failure_states.py::test_fetch_and_post_keep_the_json_401"],
    "C2": ["test_failure_states.py::test_stale_cookie_on_a_page_also_redirects"],
    "A21": ["test_failure_states.py::test_partial_reply_is_kept_and_marked_incomplete"],
    "C9": ["test_activity_logging.py::test_content_enabled_default_false"],
    "C10": ["test_sentry_scrub.py::test_request_body_cookies_query_and_auth_headers_are_dropped",
            "test_sentry_scrub.py::test_sql_and_parameters_are_cut_from_exception_messages"],
    "E6": ["test_prod_fences.py::test_no_cli_calls_init_db_directly",
           "test_prod_fences.py::test_audit_refuses_production_without_allow_prod"],
    "B18": ["test_prod_fences.py::test_mining_curation_refuses_production_without_allow_prod"],
    "F6": ["test_isolation.py::test_database_is_local_sqlite_outside_the_repo"],
    "F10": ["test_isolation.py::test_dotenv_loading_is_disabled_for_the_suite",
            "test_isolation.py::test_paid_service_credentials_are_dummies_or_absent"],
    # Phase 1
    "A2": ["test_refinement_supervision.py::test_lockdown_from_the_slide_turns_on_judge_and_strict_detector",
           "test_refinement_supervision.py::test_band_refinement_keeps_the_ceiling_the_turn_started_with"],
    "A7": ["test_judge_fail_closed.py::test_judge_failure_on_a_lockdown_turn_yields_the_fallback"],
    "A8": ["test_single_actor.py::test_supervisor_client_is_bounded",
           "test_single_actor.py::test_anthropic_client_constructed_only_in_allowed_modules"],
    "A10": ["test_detector_blocks.py::test_tilde_solution_is_cut_on_a_judge_free_turn",
            "test_detector_blocks.py::test_gate_and_batch_agree"],
    "B4": ["test_rubric_evidence.py::test_pasted_code_credits_only_the_classified_goal_and_never_masters"],
    "B5": ["test_rubric_evidence.py::test_rubric_frames_the_code_as_data_at_temperature_zero"],
    "B2": ["test_review_grade_guards.py::test_looping_on_the_same_goal_is_refused",
           "test_review_grade_guards.py::test_goal_never_reached_or_not_yet_due_is_refused"],
    "C3": ["test_review_grade_guards.py::test_due_goal_is_graded_and_its_cost_ledgered",
           "test_review_grade_guards.py::test_monthly_spend_cap_covers_review"],
    "B14": ["test_exam_machinery.py::test_a_pending_ast101_exam_does_not_grade_a_ast102_message"],
    "B17": ["test_exam_machinery.py::test_attempt_three_never_redraws_attempt_one"],
    "B21": ["test_exam_machinery.py::test_missing_goal_voids_the_attempt_without_spending_a_try",
            "test_exam_machinery.py::test_classifier_outage_falls_back_to_keywords"],
    "B3": ["test_cross_course_gate.py::test_a_ast102_student_without_ast101_records_still_sees_every_root",
           "test_cross_course_gate.py::test_all_root_ast102_goals_are_recommended_once_ast101_is_mastered"],
    "B13": ["test_cross_course_gate.py::test_the_gate_chain_walk_on_the_real_graph",
            "test_cross_course_gate.py::test_one_prerequisite_exam_per_lecture_per_day",
            "test_cross_course_gate.py::test_a_deferral_snoozes_the_whole_lecture"],
    "A11": ["test_injection_framing.py::test_frame_neutralizes_every_closing_tag",
            "test_injection_framing.py::test_rolling_summary_is_framed_in_the_user_turn",
            "test_injection_framing.py::test_retrieval_results_carry_a_data_notice_and_mark_community_posts"],
    "B10": ["test_fsrs_reference.py::test_an_hour_across_midnight_does_not_confirm",
            "test_fsrs_reference.py::test_attempt_window_is_rolling_across_midnight"],
    "B11": ["test_fsrs_reference.py::test_a_shaky_review_is_not_a_successful_retrieval",
            "test_fsrs_reference.py::test_a_shaky_review_is_not_retained"],
    "B12": ["test_fsrs_reference.py::test_interval_sequence_matches_py_fsrs",
            "test_fsrs_reference.py::test_difficulty_reverts_toward_easy_not_again"],
    "B16": ["test_state.py::test_only_state_module_writes_learner_state",
            "test_state.py::test_writer_guard_catches"],
    # Phase 2
    "A3": ["test_cost_ledger.py::test_cache_writes_are_priced",
           "test_cost_ledger.py::test_a_cold_cache_turn_moves_the_month_cost"],
    "B15": ["test_cost_ledger.py::test_an_exam_moves_the_month_cost",
            "test_cost_ledger.py::test_exam_labeller_and_clarifier_are_ledgered",
            "test_cost_ledger.py::test_the_rubric_evaluator_gets_its_own_ledger_row"],
    "A17": ["test_cost_ledger.py::test_the_fallback_model_is_recorded_and_priced"],
    "A6": ["test_concurrency_guards.py::test_two_concurrent_sends_produce_one_turn",
           "test_concurrency_guards.py::test_two_concurrent_exam_answers_grade_once"],
    "C22": ["test_concurrency_guards.py::test_student_path_insert_that_loses_the_race_returns_the_winner"],
    "C21": ["test_concurrency_guards.py::test_login_insert_that_loses_the_race_returns_the_winner",
            "test_input_and_access.py::test_asking_for_a_link_creates_no_student",
            "test_input_and_access.py::test_a_link_is_consumed_by_one_conditional_update",
            "test_input_and_access.py::test_requesting_a_link_prunes_that_addresses_dead_links"],
    "A9": ["test_stable_tools.py::test_stream_round_declares_every_tool_even_when_the_envelope_filters",
           "test_stable_tools.py::test_the_revise_call_keeps_the_cacheable_prefix"],
    "A4": ["test_history_and_submissions.py::test_the_current_message_reaches_the_actor_once"],
    "A5": ["test_history_and_submissions.py::test_a_300_line_submission_is_evaluated_in_full",
           "test_history_and_submissions.py::test_an_empty_reply_is_reported_and_never_saved_as_an_answer"],
    "A13": ["test_two_stage_persistence.py::test_a_bookkeeping_failure_after_streaming_keeps_reply_tokens_and_message",
            "test_two_stage_persistence.py::test_a_failure_while_saving_the_reply_still_leaves_the_message_and_the_turn"],
    "A12": ["test_two_stage_persistence.py::test_an_exam_pass_with_a_replay_sends_one_done_and_a_section"],
    "C1": ["test_db_off_loop.py::test_the_app_engine_is_shared_and_pooled",
           "test_db_off_loop.py::test_a_db_stall_in_one_request_does_not_delay_another_sse_stream",
           "test_db_off_loop.py::test_async_route_handlers_keep_database_work_off_the_loop"],
    "C12": ["test_db_off_loop.py::test_the_500_handler_alerts_off_the_loop"],
    "C11": ["test_db_off_loop.py::test_healthz_throttles_an_address_with_no_recent_result",
            "test_db_off_loop.py::test_healthz_never_runs_more_than_the_slot_count_at_once"],
    "A19": ["test_db_off_loop.py::test_classify_and_judge_report_usage_instead_of_writing"],
    "C4": ["test_boot_resilience.py::test_a_failed_slide_fetch_still_starts_uvicorn",
           "test_boot_resilience.py::test_the_app_starts_on_a_down_database_and_healthz_finishes_the_setup"],
    "E5": ["test_boot_resilience.py::test_a_truncated_chroma_seed_is_retried_on_the_next_boot",
           "test_boot_resilience.py::test_a_failed_new_seed_keeps_the_old_store"],
    "C7": [f"{HERE}::test_the_student_lookups_are_indexed",
           "test_migrations.py::test_a_migrated_database_matches_the_models"],
    "C6": ["test_migrations.py::test_a_migrated_database_matches_the_models"],
    "C25": ["test_migrations.py::test_a_migrated_database_matches_the_models"],
    "C13": ["test_migrations.py::test_running_migrations_leaves_app_loggers_on"],
    "E7": ["test_boot_resilience.py::test_boot_migrates_the_database_to_head",
           "test_migrations.py::test_a_database_the_app_built_at_boot_is_adopted"],
    "C15": ["test_input_and_access.py::test_a_nine_char_course_is_a_422",
            "test_input_and_access.py::test_the_json_api_rejects_it_too"],
    "B24": ["test_input_and_access.py::test_valid_courses_still_work"],
    "C5": ["test_input_and_access.py::test_a_removed_students_cookie_is_401",
           "test_input_and_access.py::test_turning_the_whitelist_on_locks_out_the_unlisted"],
    "C14": ["test_input_and_access.py::test_boot_warns_when_no_admin_is_configured"],
    "C8": ["test_input_and_access.py::test_decks_need_a_session",
           "test_input_and_access.py::test_a_removed_students_cookie_opens_no_deck"],
}


def _test_names(filename: str) -> set[str]:
    tree = ast.parse((TESTS / filename).read_text())
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_every_listed_test_exists():
    missing = []
    for finding, nodes in REGISTRY.items():
        for node in nodes:
            filename, name = node.split("::")
            if not (TESTS / filename).exists() or name not in _test_names(filename):
                missing.append(f"{finding}: {node}")
    assert missing == []


# The findings the design review's phases 0 to 2 closed. The review document
# is not part of this repository, so its list is frozen here: an entry cannot
# leave REGISTRY without this test failing.
CLOSED_BY_PHASES_0_TO_2 = frozenset("""
    A1 A2 A3 A4 A5 A6 A7 A8 A9 A10 A11 A12 A13 A17 A19 A21
    B1 B2 B3 B4 B5 B10 B11 B12 B13 B14 B15 B16 B17 B18 B19 B21 B24
    C1 C2 C3 C4 C5 C6 C7 C8 C9 C10 C11 C12 C13 C14 C15 C21 C22 C25
    D1 D2
    E1 E2 E3 E4 E5 E6 E7 E18 E19
    F1 F2 F3 F4 F5 F6 F10
""".split())


def test_every_phase_0_to_2_finding_is_registered():
    assert len(CLOSED_BY_PHASES_0_TO_2) == 69
    assert sorted(CLOSED_BY_PHASES_0_TO_2 - set(REGISTRY)) == []


# ---------------------------------------------------------------------------
# The fixes that had no test of their own
# ---------------------------------------------------------------------------

def test_student_materials_stay_out_of_git_and_the_image():
    """Step 0.1 (E1, F1 to F5): an instructor's raw course materials (student
    records, textbooks, exams) in source-materials/ are untracked and kept
    out of the Docker context."""
    for name in (".gitignore", ".dockerignore"):
        lines = [line.strip() for line in (ROOT / name).read_text().splitlines()]
        assert "source-materials/" in lines, name
    try:
        tracked = subprocess.run(["git", "ls-files", "--", "source-materials"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available")
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    assert tracked.stdout.strip() == ""


def _backup_steps() -> list[dict]:
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "db-backup.yml").read_text())
    return wf["jobs"]["backup"]["steps"]


def test_the_nightly_dump_is_encrypted_before_it_is_stored():
    """Step 0.2 (E3): only the age-encrypted dump is uploaded, the plain dump
    is removed first, and a missing or malformed recipient fails the job."""
    steps = _backup_steps()
    names = [s.get("name") for s in steps]
    uploads = [s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact")]
    assert [u["with"]["path"] for u in uploads] == ["tutor.dump.age"]
    encrypt = steps[names.index("Encrypt dump")]["run"]
    assert names.index("Encrypt dump") < names.index("Upload artifact")
    assert "age " in encrypt and "-o tutor.dump.age tutor.dump" in encrypt
    assert "rm -f tutor.dump" in encrypt
    assert 'if [ -z "$BACKUP_AGE_RECIPIENT" ]' in encrypt and "age1*)" in encrypt
    assert encrypt.count("exit 1") >= 2                                  # unset, or not a key


def test_the_daily_report_uploads_no_student_files():
    """Step 0.2 (E19): the per-student markdown is not kept as an artifact."""
    text = (ROOT / ".github" / "workflows" / "daily-report.yml").read_text()
    assert "actions/upload-artifact" not in text


def test_the_student_lookups_are_indexed():
    """Step 2.8 (C7): history, feedback, and class metrics look rows up by
    student; the migration test compares models with migrations, so it
    would not notice the indexes leaving both."""
    from tutor.db.models import Base

    def indexed(table: str) -> set[tuple[str, ...]]:
        return {tuple(c.name for c in ix.columns) for ix in Base.metadata.tables[table].indexes}

    assert ("student_id", "course", "created_at") in indexed("messages")
    assert ("student_id",) in indexed("exercise_attempts")
    assert ("student_id",) in indexed("topics_accessed")
