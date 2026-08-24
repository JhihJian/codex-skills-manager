import json
import unittest

from effect_adapters import (
    build_session_edges,
    build_task_episodes,
    extract_task_facts,
    parse_codex_jsonl_line,
    parse_pi_jsonl_line,
)


class EffectAdapterTests(unittest.TestCase):
    def test_codex_messages_calls_outputs_and_redaction(self) -> None:
        context = {"session_id": "c1", "session_family": "family-c"}
        user = parse_codex_jsonl_line(
            {"type": "response_item", "timestamp": "2026-08-24T01:00:00Z", "payload": {
                "id": "m1", "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "run tests"}],
            }}, **context
        )[0]
        call = parse_codex_jsonl_line(
            {"type": "response_item", "timestamp": "2026-08-24T01:00:01Z", "payload": {
                "id": "fc1", "type": "function_call", "call_id": "call-1", "name": "bash",
                "arguments": json.dumps({"command": "./gradlew test --token=inline-secret", "token": "secret"}),
            }}, **context
        )[0]
        result = parse_codex_jsonl_line(
            {"type": "response_item", "timestamp": "2026-08-24T01:00:02Z", "payload": {
                "id": "out1", "type": "function_call_output", "call_id": "call-1",
                "output": "Process exited with code 0",
            }}, **context
        )[0]

        self.assertEqual((user.event_type, user.text), ("user_message", "run tests"))
        self.assertEqual((call.call_id, call.tool_name), ("call-1", "bash"))
        self.assertEqual(call.args["token"], "[REDACTED]")
        self.assertNotIn("inline-secret", call.args["command"])
        self.assertEqual(len(call.payload_hash), 64)
        self.assertNotIn("secret", json.dumps(call.payload))
        self.assertEqual(result.outcome, "returned")
        self.assertNotEqual(result.outcome, "success")

        false_error = parse_codex_jsonl_line(
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-2",
                                                   "output": "ok", "isError": "false"}}, **context
        )[0]
        self.assertFalse(false_error.is_error)

        structured_error = parse_codex_jsonl_line(
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-3",
                                                   "output": {"error": "network unavailable"}}}, **context
        )[0]
        self.assertEqual(structured_error.outcome, "error")

    def test_pi_message_tool_call_result_and_text_limit(self) -> None:
        context = {"session_id": "p1", "session_family": "family-p", "text_limit": 32}
        events = parse_pi_jsonl_line({
            "type": "message", "id": "pm1", "parentId": "root", "timestamp": "2026-08-24T02:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "x" * 80},
                {"type": "toolCall", "id": "pc1", "name": "read", "arguments": {"path": "README.md"}},
            ]},
        }, **context)
        result = parse_pi_jsonl_line({
            "type": "message", "id": "pr1", "parentId": "pm1", "timestamp": "2026-08-24T02:00:01Z",
            "message": {"role": "toolResult", "toolCallId": "pc1", "toolName": "read",
                        "content": [{"type": "text", "text": "denied"}], "blocked": True},
        }, **context)[0]

        self.assertEqual([event.event_type for event in events], ["assistant_message", "tool_call"])
        self.assertLessEqual(len(events[0].text), 32)
        self.assertEqual((result.call_id, result.tool_name, result.outcome), ("pc1", "read", "blocked"))
        self.assertTrue(result.is_blocked)

    def test_parallel_wrapper_expands_nested_calls(self) -> None:
        events = parse_codex_jsonl_line({
            "type": "response_item", "timestamp": "2026-08-24T03:00:00Z", "payload": {
                "id": "wrapper-event", "type": "function_call", "call_id": "wrapper-call",
                "name": "multi_tool_use.parallel", "arguments": json.dumps({"tool_uses": [
                    {"recipient_name": "functions.read", "parameters": {"path": "a.md"}},
                    {"recipient_name": "functions.bash", "parameters": {"command": "./gradlew test"}},
                ]}),
            }}, session_id="c1", session_family="f1")

        self.assertEqual(len(events), 3)
        self.assertEqual([event.tool_name for event in events[1:]], ["functions.read", "functions.bash"])
        self.assertTrue(all(event.parent_id == "wrapper-call" for event in events[1:]))
        self.assertNotEqual(events[1].call_id, events[2].call_id)

    def test_interleaved_results_remain_paired_and_failures_are_structured(self) -> None:
        context = {"session_id": "p1", "session_family": "f1"}
        calls = parse_pi_jsonl_line({
            "type": "message", "id": "m", "timestamp": "2026-08-24T04:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "toolCall", "id": "a", "name": "bash", "arguments": {"command": "one"}},
                {"type": "toolCall", "id": "b", "name": "bash", "arguments": {"command": "two"}},
            ]},
        }, **context)[1:]
        result_b = parse_pi_jsonl_line({
            "type": "message", "id": "rb", "timestamp": "2026-08-24T04:00:01Z",
            "message": {"role": "toolResult", "toolCallId": "b", "content": "failed", "isError": True},
        }, **context)[0]
        result_a = parse_pi_jsonl_line({
            "type": "message", "id": "ra", "timestamp": "2026-08-24T04:00:02Z",
            "message": {"role": "toolResult", "toolCallId": "a", "content": "cancelled", "cancelled": True},
        }, **context)[0]

        self.assertEqual([event.call_id for event in calls], ["a", "b"])
        self.assertEqual((result_b.call_id, result_b.outcome), ("b", "error"))
        self.assertEqual((result_a.call_id, result_a.outcome), ("a", "cancelled"))

        user = parse_pi_jsonl_line({
            "type": "message", "id": "u", "timestamp": "2026-08-24T03:59:59Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "run"}]},
        }, **context)[0]
        self.assertEqual(build_task_episodes([user, *calls, result_b, result_a])[0].outcome, "cancelled")

    def test_missing_result_does_not_make_episode_successful(self) -> None:
        context = {"session_id": "c1", "session_family": "f1"}
        user = parse_codex_jsonl_line({
            "type": "response_item", "timestamp": "2026-08-24T05:00:00Z",
            "payload": {"id": "u", "type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "build"}]},
        }, **context)[0]
        call = parse_codex_jsonl_line({
            "type": "response_item", "timestamp": "2026-08-24T05:00:01Z",
            "payload": {"id": "fc", "type": "function_call", "call_id": "missing",
                        "name": "bash", "arguments": "{\"command\":\"make\"}"},
        }, **context)[0]
        episode = build_task_episodes([user, call])[0]

        self.assertEqual(episode.outcome, "unknown")
        self.assertEqual(episode.event_fingerprints, (user.fingerprint, call.fingerprint))

    def test_session_parent_fork_and_info(self) -> None:
        codex = parse_codex_jsonl_line({
            "type": "session_meta", "timestamp": "2026-08-24T06:00:00Z",
            "payload": {"id": "child-c", "parent_thread_id": "parent-c", "thread_source": "subagent"},
        })[0]
        pi = parse_pi_jsonl_line({
            "type": "session", "id": "child-p", "timestamp": "2026-08-24T06:00:00Z",
            "parentSession": "parent-p", "forkedFrom": "fork-entry",
        })[0]
        info = parse_pi_jsonl_line({
            "type": "session_info", "id": "i", "parentId": "x", "timestamp": "2026-08-24T06:00:01Z",
            "name": "A title",
        }, session_id="child-p", session_family="parent-p")[0]
        edges = build_session_edges([codex, pi])

        self.assertEqual(codex.parent_session_id, "parent-c")
        self.assertEqual((pi.fork_from_id, info.text), ("fork-entry", "A title"))
        self.assertEqual([(edge.relation, edge.target_session_id) for edge in edges],
                         [("parent", "parent-c"), ("fork", "fork-entry")])

    def test_fingerprint_without_id_is_stable_after_line_reordering(self) -> None:
        one = {"type": "response_item", "timestamp": "2026-08-24T07:00:00Z", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "same"}],
        }}
        unrelated = {"type": "event_msg", "timestamp": "2026-08-24T06:59:59Z",
                     "payload": {"type": "token_count", "input_tokens": 1}}
        before = parse_codex_jsonl_line(one, session_id="c", session_family="f")[0]
        parse_codex_jsonl_line(unrelated, session_id="c", session_family="f")
        after = parse_codex_jsonl_line(one, session_id="c", session_family="f")[0]

        self.assertEqual(before.fingerprint, after.fingerprint)

    def test_pi_skill_block_and_episode_boundaries(self) -> None:
        context = {"session_id": "p", "session_family": "f"}
        first = parse_pi_jsonl_line({
            "type": "message", "id": "u1", "timestamp": "2026-08-24T08:00:00Z",
            "message": {"role": "user", "content": [{"type": "text", "text":
                '<skill name="deploy-memory" location="/skills/deploy-memory/SKILL.md">instructions</skill>'}]},
        }, **context)
        assistant = parse_pi_jsonl_line({
            "type": "message", "id": "a1", "timestamp": "2026-08-24T08:00:01Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
        }, **context)
        second = parse_pi_jsonl_line({
            "type": "message", "id": "u2", "timestamp": "2026-08-24T08:00:02Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "follow up"}]},
        }, **context)
        episodes = build_task_episodes([*first, *assistant, *second])

        self.assertEqual([event.event_type for event in first], ["user_message", "skill"])
        self.assertEqual(first[1].metadata["skill_name"], "deploy-memory")
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[1].continuation_of, episodes[0].episode_id)

    def test_task_fact_extraction_is_deterministic(self) -> None:
        context = {"session_id": "c", "session_family": "f"}
        event = parse_codex_jsonl_line({
            "type": "response_item", "timestamp": "2026-08-24T09:00:00Z", "payload": {
                "id": "fc", "type": "function_call", "call_id": "g", "name": "bash",
                "arguments": json.dumps({"command": "./gradlew test && docker deploy", "report": "docs/result.md"}),
            }}, **context)[0]
        first = extract_task_facts([event])
        second = extract_task_facts([event])

        self.assertEqual(first, second)
        self.assertIn(("tool-family", "shell"), {(fact.predicate, fact.value) for fact in first})
        self.assertTrue({"gradle", "test", "deploy", "document"} <= {
            fact.value for fact in first if fact.predicate == "task-tag"
        })
        self.assertTrue(all(fact.status == "accepted" for fact in first))


if __name__ == "__main__":
    unittest.main()