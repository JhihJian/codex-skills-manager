import dataclasses
import unittest

from feedback_detector import (
    EvidenceSpan,
    FeedbackCandidate,
    ProcessAnomaly,
    ProcessPlan,
    detect_process_anomalies,
    detect_assistant_claims,
    detect_user_feedback,
    extract_content_spans,
    normalize_process_plan,
    redact_excerpt,
)


def categories(candidates):
    return [candidate.category for candidate in candidates]


def pi_failure(agent="designer", *, source="unknown", turns=0, exit_code=1, stderr=None):
    return {
        "agent": agent,
        "agentSource": source,
        "exitCode": exit_code,
        "stderr": stderr or f'Unknown agent: "{agent}". Available agents: reviewer, worker',
        "messages": [],
        "usage": {"turns": turns, "input": 0, "output": 0},
    }


def pi_success(agent="reviewer", *, result_id=None):
    result = {
        "agent": agent,
        "agentSource": "user",
        "exitCode": 0,
        "messages": [{"role": "assistant", "content": "done"}],
        "usage": {"turns": 2, "input": 100, "output": 20},
    }
    if result_id:
        result["resultId"] = result_id
    return result


class ContentProjectionTests(unittest.TestCase):
    def test_plain_text_is_user_authored(self):
        spans = extract_content_spans("结果还是不对", event_id="u1")
        self.assertEqual([(span.origin, span.start, span.end) for span in spans],
                         [("user-authored", 0, 6)])
        self.assertEqual(spans[0].event_id, "u1")

    def test_complete_skill_block_is_removed(self):
        text = '前文<skill name="x" location="/x/SKILL.md">不对</skill>后文'
        spans = extract_content_spans(text)
        self.assertEqual([span.origin for span in spans],
                         ["user-authored", "skill-injected", "user-authored"])
        self.assertEqual([span.redacted_excerpt for span in spans],
                         ["前文", '<skill name="x" location="[REDACTED_PATH]">不对</skill>', "后文"])

    def test_multiple_skill_blocks_preserve_between_text(self):
        text = ('A<skill name="a" location="a/SKILL.md">X</skill>B'
                '<skill name="b" location="b/SKILL.md">Y</skill>C')
        spans = extract_content_spans(text)
        self.assertEqual([span.origin for span in spans], [
            "user-authored", "skill-injected", "user-authored",
            "skill-injected", "user-authored",
        ])
        self.assertEqual("".join(span.redacted_excerpt or "" for span in spans if span.origin == "user-authored"), "ABC")

    def test_skill_blocks_do_not_cross_content_blocks(self):
        blocks = [
            {"type": "text", "text": '<skill name="x" location="x/SKILL.md">bad'},
            {"type": "text", "text": "still bad</skill>"},
        ]
        spans = extract_content_spans(blocks)
        self.assertEqual([span.origin for span in spans], ["unknown-origin", "unknown-origin"])
        self.assertEqual([span.block_index for span in spans], [0, 1])

    def test_unclosed_skill_fails_closed(self):
        text = '评价<skill name="x" location="x/SKILL.md">里面不对'
        spans = extract_content_spans(text)
        self.assertEqual([span.origin for span in spans], ["user-authored", "unknown-origin"])
        self.assertEqual(detect_user_feedback(text), ())

    def test_malformed_skill_attributes_fail_closed(self):
        text = '<skill name=x location="x/SKILL.md">还是不行</skill>'
        self.assertEqual(extract_content_spans(text)[0].origin, "unknown-origin")
        self.assertFalse(detect_user_feedback(text))

    def test_nested_skill_fails_closed(self):
        text = ('<skill name="a" location="a/SKILL.md">'
                '<skill name="b" location="b/SKILL.md">不对</skill></skill>')
        spans = extract_content_spans(text)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].origin, "unknown-origin")

    def test_stray_close_tag_fails_closed_from_tag(self):
        spans = extract_content_spans("正常文字</skill>还是不对")
        self.assertEqual([span.origin for span in spans], ["unknown-origin"])
        self.assertFalse(detect_user_feedback("正常文字</skill>还是不对"))

    def test_markdown_quote_is_marked_and_excluded(self):
        text = "> 结果还是不对\n现在可以了"
        spans = extract_content_spans(text)
        self.assertEqual(spans[0].origin, "quoted")
        self.assertFalse(detect_user_feedback(text))

    def test_fenced_code_is_marked_and_excluded(self):
        text = "```text\n测试失败\n```\n没问题"
        spans = extract_content_spans(text)
        self.assertIn("quoted", [span.origin for span in spans])
        self.assertFalse(detect_user_feedback(text))

    def test_unclosed_fenced_code_is_fail_closed_as_quote(self):
        spans = extract_content_spans("```\n测试失败")
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].origin, "quoted")

    def test_inline_code_is_marked_and_excluded(self):
        spans = extract_content_spans("示例 `测试失败` 没问题")
        self.assertIn("quoted", [span.origin for span in spans])
        self.assertFalse(detect_user_feedback("示例 `测试失败` 没问题"))

    def test_obvious_log_quote_is_marked_and_excluded(self):
        text = "错误日志如下：测试失败"
        self.assertEqual(extract_content_spans(text)[0].origin, "quoted")
        self.assertFalse(detect_user_feedback(text))

    def test_indented_code_and_error_log_are_quoted(self):
        text = "    test failed\nERROR: build failed"
        spans = extract_content_spans(text)
        self.assertTrue(all(span.origin == "quoted" for span in spans))
        self.assertFalse(detect_user_feedback(text))

    def test_span_offsets_hash_and_locator_are_stable(self):
        first = extract_content_spans("前面。还是不行", event_id="evt")
        second = extract_content_spans("前面。还是不行", event_id="evt")
        self.assertEqual(first, second)
        self.assertRegex(first[0].excerpt_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(first[0].protocol_locator, "content[0].text:0-7")

    def test_non_text_block_keeps_original_block_index(self):
        spans = extract_content_spans([
            {"type": "image", "data": "ignored"},
            {"type": "text", "text": "不对"},
        ])
        self.assertEqual(spans[0].block_index, 1)

    def test_spans_and_candidates_are_frozen_dataclasses(self):
        span = extract_content_spans("不对")[0]
        candidate = detect_user_feedback("不对")[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            span.start = 2
        with self.assertRaises(dataclasses.FrozenInstanceError):
            candidate.confidence = 0
        self.assertIsInstance(span, EvidenceSpan)
        self.assertIsInstance(candidate, FeedbackCandidate)


class UserFeedbackRuleTests(unittest.TestCase):
    def test_result_rejection_chinese(self):
        candidate = detect_user_feedback("这完全不对")[0]
        self.assertEqual((candidate.category, candidate.severity), ("result-rejection", "high"))

    def test_result_rejection_english(self):
        self.assertEqual(categories(detect_user_feedback("This answer is completely wrong.")), ["result-rejection"])

    def test_observed_defect_chinese(self):
        self.assertEqual(categories(detect_user_feedback("你改完后测试还是失败")), ["observed-defect"])

    def test_observed_defect_english(self):
        self.assertEqual(categories(detect_user_feedback("The button still doesn't work.")), ["observed-defect"])

    def test_still_fails_is_observed_defect(self):
        self.assertEqual(categories(detect_user_feedback("The build still fails.")), ["observed-defect"])

    def test_requirement_gap_chinese(self):
        candidate = detect_user_feedback("权限校验漏了")[0]
        self.assertEqual(candidate.category, "requirement-gap")
        self.assertIn("explicit-object-reference", [item.reason for item in candidate.adjustments])

    def test_requirement_gap_english(self):
        self.assertEqual(categories(detect_user_feedback("You missed the permission check.")), ["requirement-gap"])

    def test_rework_correction(self):
        self.assertEqual(categories(detect_user_feedback("撤销这次改动，重新做")), ["rework-correction"])

    def test_process_critique_retrospective_is_not_instruction(self):
        self.assertEqual(categories(detect_user_feedback("你不应该修改这些文件")), ["process-critique"])
        self.assertEqual(categories(detect_user_feedback("You should not have modified these files.")),
                         ["process-critique"])

    def test_process_critique_missing_parallel_execution(self):
        self.assertEqual(categories(detect_user_feedback("要求并行评审，但首轮没执行")), ["process-critique"])

    def test_external_negative_acceptance(self):
        candidate = detect_user_feedback("验收平台仍判定失败")[0]
        self.assertEqual((candidate.category, candidate.authority), ("external-negative-acceptance", "external"))

    def test_mixed_positive_and_negative(self):
        self.assertEqual(categories(detect_user_feedback("功能可以，但数据还是不对")), ["mixed-or-unclear"])

    def test_weak_quality_is_low_confidence_mixed(self):
        candidate = detect_user_feedback("这个结果不太好")[0]
        self.assertEqual((candidate.category, candidate.severity, candidate.confidence),
                         ("mixed-or-unclear", "low", 0.65))

    def test_instructional_negation_is_excluded(self):
        self.assertFalse(detect_user_feedback("不要修改文件"))
        self.assertFalse(detect_user_feedback("- Do not end the turn while required work is missing"))

    def test_review_prompts_background_and_checklists_are_not_requirement_feedback(self):
        excluded = [
            "@codex review 请重点发现缺少测试和安全问题",
            "希望配置自动检查，重点发现明显回归风险、缺少测试",
            "已接入自动开发，但 PR 阶段还缺少稳定审查机制",
            r"\- 缺少必要测试，尤其是权限相关逻辑",
            "then ensure bootstrap comment exists, create if missing",
        ]
        self.assertTrue(all(not detect_user_feedback(item) for item in excluded))
        self.assertEqual(categories(detect_user_feedback("你刚才漏了权限校验")), ["requirement-gap"])

    def test_cancel_or_pause_is_excluded(self):
        self.assertFalse(detect_user_feedback("先别继续"))

    def test_scope_change_is_excluded(self):
        self.assertFalse(detect_user_feedback("再增加导出功能"))

    def test_preference_change_is_excluded(self):
        self.assertFalse(detect_user_feedback("把主题换成绿色"))

    def test_positive_negation_is_excluded(self):
        self.assertFalse(detect_user_feedback("没问题，现在可以了"))
        self.assertFalse(detect_user_feedback("The build no longer fails."))

    def test_hypothetical_is_excluded(self):
        self.assertFalse(detect_user_feedback("如果失败就回滚"))

    def test_short_rejection_without_previous_result_is_downgraded(self):
        candidate = detect_user_feedback("不对", has_previous_result=False)[0]
        self.assertEqual(candidate.confidence, 0.75)
        self.assertIn("missing-previous-result", [item.reason for item in candidate.adjustments])

    def test_target_ambiguity_caps_confidence(self):
        candidate = detect_user_feedback("这个方案完全不对", target_ambiguous=True)[0]
        self.assertEqual(candidate.confidence, 0.79)
        self.assertEqual(candidate.adjustments[-1].reason, "ambiguous-target-cap")

    def test_structured_symptom_adjustment(self):
        candidate = detect_user_feedback("接口仍然返回 500")[0]
        reasons = {item.reason for item in candidate.adjustments}
        self.assertEqual(candidate.severity, "high")
        self.assertTrue({"explicit-object-reference", "structured-symptom"} <= reasons)

    def test_multiple_authored_blocks_produce_ordered_candidates(self):
        blocks = [
            {"type": "text", "text": "还是不行"},
            {"type": "text", "text": "权限校验漏了"},
        ]
        found = detect_user_feedback(blocks)
        self.assertEqual(categories(found), ["observed-defect", "requirement-gap"])
        self.assertEqual([item.span.block_index for item in found], [0, 1])

    def test_skill_negative_text_does_not_hide_authored_feedback(self):
        text = ('<skill name="x" location="x/SKILL.md">测试失败</skill>\n'
                '按钮还是没反应')
        found = detect_user_feedback(text)
        self.assertEqual(categories(found), ["observed-defect"])
        self.assertGreater(found[0].span.start, text.index("</skill>"))

    def test_codex_synthetic_user_blocks_are_excluded(self):
        text = (
            '<subagent_notification>{"status":"测试失败，缺少实现"}</subagent_notification>\n'
            "按钮还是没反应"
        )
        spans = extract_content_spans(text)
        self.assertEqual([span.origin for span in spans], ["system-injected", "user-authored"])
        self.assertEqual(categories(detect_user_feedback(text)), ["observed-defect"])
        for tag in ("environment_context", "system-reminder", "developer_message", "user_instructions"):
            self.assertFalse(detect_user_feedback(f"<{tag}>测试失败，缺少实现</{tag}>"))

    def test_unclosed_synthetic_block_fails_closed(self):
        spans = extract_content_spans("<subagent_notification>还是不行")
        self.assertEqual(spans[0].origin, "unknown-origin")
        self.assertFalse(detect_user_feedback("<subagent_notification>还是不行"))

    def test_orchestration_user_role_envelopes_are_system_injected(self):
        messages = [
            "You are working on a Linear ticket `JIE-1`\nIssue context:\n缺少测试\n6. Start over",
            "Goal mode is active.\n<goal_objective>修复失败</goal_objective>",
            "Continue the active /goal until it is complete: missing requirements",
        ]
        for text in messages:
            with self.subTest(text=text[:20]):
                spans = extract_content_spans(text)
                self.assertEqual([span.origin for span in spans], ["system-injected"])
                self.assertFalse(detect_user_feedback(text))

    def test_detection_is_idempotent_and_output_stable(self):
        text = "这个方案完全不对。权限校验漏了"
        self.assertEqual(detect_user_feedback(text, event_id="same"),
                         detect_user_feedback(text, event_id="same"))

    def test_sensitive_excerpt_is_redacted_but_hash_is_raw(self):
        token = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        text = f"结果不对，Bearer {token}，路径 /home/alice/private/report.txt"
        candidate = detect_user_feedback(text)[0]
        self.assertNotIn(token, candidate.span.redacted_excerpt)
        self.assertNotIn("/home/alice", candidate.span.redacted_excerpt)
        self.assertRegex(candidate.span.excerpt_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(candidate.span.redaction_status, "redacted")

    def test_standard_base64_feedback_secret_is_redacted(self):
        secret = "q7+/N2/aZ9+vL4/Tx1+Qm8/rK3+WpQ=="
        candidate = detect_user_feedback(f"结果不对，token={secret}")[0]
        self.assertNotIn(secret, candidate.span.redacted_excerpt)

    def test_excerpt_length_is_bounded(self):
        excerpt, truncated, status = redact_excerpt("结果完全不对。" + "需要重新检查。" * 100)
        self.assertTrue(truncated)
        self.assertLessEqual(len(excerpt), 512)
        self.assertEqual(status, "clean")

    def test_assistant_self_critique_is_separate_zero_weight_channel(self):
        claims = detect_assistant_claims("抱歉，我刚才判断错了。")
        self.assertEqual(len(claims), 1)
        self.assertEqual(
            (claims[0].channel, claims[0].authority, claims[0].category),
            ("assistant-claim", "assistant", "assistant-self-assessment"),
        )
        self.assertLess(claims[0].confidence, 0.85)
        self.assertFalse(detect_user_feedback("抱歉，我刚才判断错了。"))


class ProcessPlanTests(unittest.TestCase):
    def test_single_plan_normalization(self):
        plan = normalize_process_plan("subagent", {"agent": "worker", "task": "x"},
                                      {"mode": "single", "results": [pi_success("worker")]})
        self.assertEqual((plan.mode, plan.planned_count, plan.started_count, plan.completed_count),
                         ("single", 1, 1, 1))
        self.assertEqual(plan.requested_agents, ("worker",))

    def test_parallel_plan_normalization(self):
        args = {"tasks": [{"agent": "one", "task": "a"}, {"agent": "two", "task": "b"}]}
        plan = normalize_process_plan("functions.subagent", args, {"results": [pi_success("one"), pi_success("two")]})
        self.assertEqual((plan.mode, plan.planned_count, plan.started_count, plan.completed_count),
                         ("parallel", 2, 2, 2))

    def test_chain_plan_normalization(self):
        args = {"chain": [{"agent": "one", "task": "a"}, {"agent": "two", "task": "{previous}"}]}
        plan = normalize_process_plan("subagent", args, {"results": [pi_success("one"), pi_success("two")]})
        self.assertEqual((plan.mode, plan.requested_agents), ("chain", ("one", "two")))

    def test_available_agents_are_canonicalized(self):
        args = {"agent": "designer"}
        details = {"results": [pi_failure()]}
        plan = normalize_process_plan("subagent", args, details, available_agents=("reviewer",))
        self.assertEqual(plan.available_agents, ("reviewer", "worker"))

    def test_all_zero_unknown_result_failed_before_start(self):
        plan = normalize_process_plan("subagent", {"agent": "designer"}, {"results": [pi_failure()]})
        self.assertEqual((plan.started_count, plan.completed_count, plan.failed_before_start_count), (0, 0, 1))

    def test_child_and_result_ids_are_normalized(self):
        args = {"agent": "worker", "expectedResultId": "expected-1"}
        details = {"results": [{**pi_success("worker", result_id="expected-1"), "childSessionId": "child-1"}]}
        plan = normalize_process_plan("subagent", args, details)
        self.assertEqual(plan.expected_result_ids, ("expected-1",))
        self.assertEqual(plan.child_session_ids, ("child-1",))
        self.assertEqual(plan.returned_result_ids, ("expected-1",))

    def test_plan_is_frozen(self):
        plan = normalize_process_plan("subagent", {"agent": "worker"}, {"results": [pi_success()]})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.planned_count = 4
        self.assertIsInstance(plan, ProcessPlan)

    def test_malformed_results_are_ignored(self):
        plan = normalize_process_plan("subagent", {"agent": "worker"}, {"results": [None, "bad"]})
        self.assertEqual((plan.planned_count, plan.started_count), (1, 0))


class ProcessAnomalyTests(unittest.TestCase):
    def test_real_pi_unknown_agent_details_override_outer_false(self):
        args = {"tasks": [{"agent": "designer", "task": "review"}]}
        details = {"mode": "parallel", "results": [pi_failure()]}
        found = detect_process_anomalies("functions.subagent", args, details, {"isError": False})
        self.assertEqual(categories(found), ["agent-unavailable", "dispatch-not-executed"])
        self.assertEqual(found[0].plan.available_agents, ("reviewer", "worker"))

    def test_unknown_agent_source_without_stderr(self):
        result = pi_failure(stderr="agent lookup failed")
        found = detect_process_anomalies("subagent", {"agent": "missing"}, {"results": [result]}, "returned")
        self.assertIn("agent-unavailable", categories(found))

    def test_all_zero_parallel_dispatch_not_executed(self):
        args = {"tasks": [{"agent": "a"}, {"agent": "b"}]}
        details = {"results": [pi_failure("a"), pi_failure("b")]}
        found = detect_process_anomalies("subagent", args, details, {"isError": False})
        self.assertEqual(found[0].plan.planned_count, 2)
        self.assertIn("dispatch-not-executed", categories(found))

    def test_partial_dispatch(self):
        args = {"tasks": [{"agent": "a"}, {"agent": "b"}, {"agent": "c"}]}
        details = {"results": [pi_success("a"), pi_failure("b"), pi_failure("c")]}
        found = detect_process_anomalies("subagent", args, details, {"isError": False})
        self.assertIn("partial-dispatch", categories(found))
        self.assertEqual(found[0].plan.started_count, 1)

    def test_successful_followup_has_no_anomaly(self):
        args = {"tasks": [{"agent": "reviewer"}, {"agent": "worker"}]}
        details = {"results": [pi_success("reviewer"), pi_success("worker")]}
        self.assertFalse(detect_process_anomalies("subagent", args, details, {"isError": False}))

    def test_child_result_missing(self):
        args = {"agent": "worker", "expectedResultId": "expected"}
        details = {"results": [{**pi_success("worker"), "childSessionId": "child"}]}
        self.assertIn("child-result-missing", categories(
            detect_process_anomalies("subagent", args, details, {"isError": False})
        ))

    def test_open_episode_does_not_report_child_result_missing(self):
        args = {"agent": "worker", "expectedResultId": "expected"}
        details = {"results": [{**pi_success("worker"), "childSessionId": "child"}]}
        self.assertNotIn("child-result-missing", categories(
            detect_process_anomalies("subagent", args, details, {"isError": False}, episode_closed=False)
        ))

    def test_nested_nonzero_exit_code_is_tool_error(self):
        result = {"exitCode": 2, "agentSource": "user", "usage": {"turns": 2}, "stderr": "compile failed"}
        found = detect_process_anomalies("subagent", {"agent": "worker"}, {"results": [result]}, {"isError": False})
        self.assertIn("tool-error", categories(found))

    def test_top_level_nonzero_exit_code_is_tool_error(self):
        found = detect_process_anomalies("bash", {"command": "false"}, {"exitCode": 1}, {"isError": False})
        self.assertEqual(categories(found), ["tool-error"])

    def test_tool_blocked(self):
        found = detect_process_anomalies("bash", {}, {"status": "blocked"}, {"isError": False})
        self.assertEqual(categories(found), ["tool-blocked"])

    def test_tool_timeout(self):
        found = detect_process_anomalies("bash", {}, {"status": "timed out"}, {"isError": False})
        self.assertEqual(categories(found), ["tool-timeout"])

    def test_result_missing_when_no_result_payload(self):
        found = detect_process_anomalies("bash", {"command": "test"}, None, None)
        self.assertEqual(categories(found), ["result-missing"])

    def test_explicit_result_missing_status(self):
        found = detect_process_anomalies("bash", {}, {"status": "result-missing"}, {"isError": False})
        self.assertEqual(categories(found), ["result-missing"])

    def test_generic_outer_error(self):
        found = detect_process_anomalies("read", {}, {"message": "bad"}, {"isError": True})
        self.assertEqual(categories(found), ["tool-error"])

    def test_anomaly_output_is_idempotent(self):
        args = {"agent": "designer"}
        details = {"results": [pi_failure()]}
        first = detect_process_anomalies("subagent", args, details, {"isError": False})
        second = detect_process_anomalies("subagent", args, details, {"isError": False})
        self.assertEqual(first, second)
        self.assertTrue(all(isinstance(item, ProcessAnomaly) for item in first))

    def test_successful_generic_tool_result_is_clean(self):
        self.assertFalse(detect_process_anomalies("bash", {}, {"exitCode": 0}, {"isError": False}))

    def test_successful_exit_ignores_non_failure_stderr_words(self):
        details = {"results": [{
            "exitCode": 0, "stderr": "error count: 0", "usage": {"turns": 1},
        }]}
        self.assertFalse(detect_process_anomalies("bash", {}, details, {"isError": False}))


if __name__ == "__main__":
    unittest.main()