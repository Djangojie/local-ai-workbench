import json
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

import mcp_bridge
import server
import local_tools
import cluster
import coding_benchmark


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps({"model": "deepseek-flash", "choices": [{"message": {"content": "完成"}}], "usage": {"total_tokens": 12}}).encode()


class WorkbenchTests(unittest.TestCase):
    def test_coding_benchmark_detects_original_bug_and_fixed_code(self):
        buggy = "def first_pair(nums,target):\n seen={}\n for j,x in enumerate(nums):\n  if target-x in seen:return [seen[target-x],j]\n  seen[x]=j\n return None"
        fixed = "def first_pair(nums,target):\n for i in range(len(nums)):\n  for j in range(i+1,len(nums)):\n   if nums[i]+nums[j]==target:return [i,j]\n return None"
        bad = coding_benchmark.evaluate(buggy)
        good = coding_benchmark.evaluate(fixed)
        self.assertLess(bad["passed"], bad["total"])
        self.assertEqual(good["passed"], good["total"])

    def test_coding_benchmark_rejects_unsafe_code(self):
        result = coding_benchmark.evaluate("def first_pair(nums,target):\n return __import__('os').listdir('.')")
        self.assertEqual(result["passed"], 0)
        self.assertFalse(result["scored"])
        self.assertIn("error", result)

    def test_objective_does_not_claim_win_when_baseline_unscored(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            return {"text": "unsupported" if calls == 1 else "fixed", "usage": {}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "fixed", "usage": {}, "elapsed_ms": 1, "model": "default"}
        def evaluate(answer):
            return {"scored": answer != "unsupported", "passed": 0 if answer == "unsupported" else 11, "total": 11, "failures": []}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready):
            result = cluster.compare("修复代码", "", api_call, tool_run, objective_eval=evaluate)
        self.assertEqual(result["selected"], "baseline")
        self.assertIn("无法公平判优", result["selection_reason"])

    def test_coding_benchmark_accepts_safe_while_and_append(self):
        code = "def first_pair(nums,target):\n pairs=[]\n i=0\n while i<len(nums):\n  for j in range(i+1,len(nums)):\n   if nums[i]+nums[j]==target:pairs.append([i,j])\n  i=i+1\n if pairs:return pairs[0]\n return None"
        result = coding_benchmark.evaluate(code)
        self.assertEqual(result["passed"], result["total"])

    def test_objective_comparison_selects_strictly_better_candidate(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            return {"text": "baseline" if calls == 1 else "synthesis", "usage": {}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "fixed" if tool == "pi_cli" else "other", "usage": {}, "elapsed_ms": 1, "model": "default"}
        def evaluate(text):
            return {"passed": {"baseline": 5, "synthesis": 7, "fixed": 11, "other": 6}[text], "total": 11, "failures": []}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready):
            result = cluster.compare("修复代码", "通过测试", api_call, tool_run, objective_eval=evaluate)
        self.assertEqual(result["selected"], "cluster")
        self.assertEqual(result["selected_source"], "pi_cli")
        self.assertEqual(result["objective"]["baseline"]["passed"], 5)

    def test_windows_secret_round_trip(self):
        if server.sys.platform != "win32":
            self.skipTest("Windows only")
        encrypted = server.dpapi(b"example-secret")
        self.assertNotIn(b"example-secret", encrypted)
        self.assertEqual(server.dpapi(encrypted, True), b"example-secret")

    def test_official_domain_guard(self):
        state = server.default_state()
        state["providers"]["deepseek"]["base_url"] = "https://evil.example/steal"
        with self.assertRaisesRegex(ValueError, "官方 HTTPS"):
            server.validate_state(state)

    def test_model_request_and_usage(self):
        captured = {}

        def fake_open(request, timeout):
            captured["url"] = request.full_url
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(server, "get_key", return_value="secret"), patch.object(server, "load_state", return_value=server.default_state()), patch.object(server, "urlopen", side_effect=fake_open):
            result = server.call_model("deepseek", "deepseek-flash", [{"role": "user", "content": "hello"}], max_tokens=64, timeout=40)
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["payload"]["model"], "deepseek-flash")
        self.assertEqual(captured["timeout"], 40)
        self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_deepseek_structured_review_request(self):
        captured = {}
        def fake_open(request, timeout):
            captured.update(json.loads(request.data))
            return FakeResponse()
        with patch.object(server, "get_key", return_value="secret"), patch.object(server, "load_state", return_value=server.default_state()), patch.object(server, "urlopen", side_effect=fake_open):
            server.call_model("deepseek", "deepseek-flash", [{"role": "user", "content": "json"}], thinking=False, response_format={"type": "json_object"})
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertEqual(captured["response_format"], {"type": "json_object"})

    def test_all_five_official_routes(self):
        urls = []

        def fake_open(request, timeout):
            urls.append(request.full_url)
            return FakeResponse()

        with patch.object(server, "get_key", return_value="secret"), patch.object(server, "load_state", return_value=server.default_state()), patch.object(server, "urlopen", side_effect=fake_open):
            for provider, info in server.PROVIDERS.items():
                server.call_model(provider, info["model"], [{"role": "user", "content": "hello"}])
        self.assertEqual(urls, [info["base_url"] + "/chat/completions" for info in server.PROVIDERS.values()])

    def test_flow_passes_prior_output(self):
        seen = []

        def fake_call(provider, model, messages, *args):
            seen.append(messages[1]["content"])
            return {"text": f"结果{len(seen)}", "usage": {}, "elapsed_ms": 1, "model": model}

        with patch.object(server, "load_state", return_value=server.default_state()), patch.object(server, "get_key", return_value="secret"), patch.object(server, "call_model", side_effect=fake_call):
            result = server.run_flow("原始需求")
        self.assertEqual(len(seen), 3)
        self.assertIn("结果1", seen[1])
        self.assertIn("原始需求", seen[2])
        self.assertEqual(result["final"], "结果3")

    def test_flow_stops_after_failed_step(self):
        state = server.default_state()
        with patch.object(server, "load_state", return_value=state), patch.object(server, "get_key", return_value="secret"), patch.object(server, "call_model", side_effect=ValueError("missing key")) as call:
            result = server.run_flow("做一个计划")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["steps"][0]["status"], "error")
        self.assertEqual(result["final"], "")

    def test_missing_keys_prevent_all_calls(self):
        with patch.object(server, "load_state", return_value=server.default_state()), patch.object(server, "get_key", return_value=None), patch.object(server, "call_model") as call:
            with self.assertRaisesRegex(ValueError, "运行前请先配置密钥"):
                server.run_flow("做一个计划")
        call.assert_not_called()

    def test_mcp_tool_catalog(self):
        response = mcp_bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual([x["name"] for x in response["result"]["tools"]], ["run_workflow", "provider_status", "resume_workflow", "compare_workflow"])

    def test_cluster_selects_judged_improvement(self):
        calls = []
        def api_call(provider, model, messages, **kwargs):
            calls.append(messages[0]["content"])
            text = ["基线答案", "改进答案", '{"winner":"B","scores":{"A":7,"B":9},"reason":"B 更完整"}'][len(calls) - 1]
            return {"text": text, "usage": {"total_tokens": 10}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "改进答案" if tool == "hermes_cli" else f"{tool} 答案", "usage": {}, "elapsed_ms": 1, "model": "default"}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready), patch.object(cluster.secrets, "randbelow", return_value=0):
            result = cluster.compare("写出可执行计划", "包含风险", api_call, tool_run)
        self.assertEqual(result["selected"], "cluster")
        self.assertEqual(result["cluster"]["text"], "改进答案")
        self.assertEqual(result["judge"]["scores"], {"baseline": 7, "cluster": 9})

    def test_cluster_keeps_baseline_when_judge_invalid(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            return {"text": "基线答案" if calls == 1 else "无法解析", "usage": {}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "候选答案", "usage": {}, "elapsed_ms": 1, "model": "default"}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready):
            result = cluster.compare("测试", "", api_call, tool_run)
        self.assertEqual(result["selected"], "baseline")
        self.assertIn("审查未能完成", result["judge"]["reason"])

    def test_cluster_uses_api_synthesis_when_hermes_fails(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            text = ["基线", "备用合成", '{"winner":"B","scores":{"A":6,"B":8},"reason":"更符合标准"}'][calls - 1]
            return {"text": text, "usage": {"total_tokens": 5}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            if tool == "hermes_cli":
                raise RuntimeError("Hermes 故障")
            return {"text": "候选", "usage": {}, "elapsed_ms": 1, "model": "default"}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready), patch.object(cluster.secrets, "randbelow", return_value=0):
            result = cluster.compare("测试", "", api_call, tool_run)
        self.assertEqual(result["synthesizer"], "deepseek")
        self.assertEqual(result["selected"], "cluster")
        self.assertEqual(calls, 3)

    def test_hard_length_gate_overrides_model_score(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            text = ["合格", "这个答案明显太长", "修订后仍然超过五个字"][calls - 1]
            return {"text": text, "usage": {}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "候选", "usage": {}, "elapsed_ms": 1, "model": "default"}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready):
            result = cluster.compare("回答不超过5字", "", api_call, tool_run)
        self.assertEqual(result["selected"], "baseline")
        self.assertIsNone(result["judge"])
        self.assertTrue(result["hard_checks"]["baseline"]["passed"])
        self.assertFalse(result["hard_checks"]["cluster"]["passed"])
        self.assertEqual(calls, 3)

    def test_hard_gate_repairs_before_judging(self):
        calls = 0
        def api_call(provider, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            text = ["基线", "这个答案很长很长", "更好", '{"winner":"B","scores":{"A":7,"B":8},"reason":"B 更好"}'][calls - 1]
            return {"text": text, "usage": {}, "elapsed_ms": 1, "model": model}
        def tool_run(tool, prompt, **kwargs):
            return {"text": "候选", "usage": {}, "elapsed_ms": 1, "model": "default"}
        ready = {key: {"ready": True} for key in local_tools.TOOLS}
        with patch.object(cluster.local_tools, "status", return_value=ready), patch.object(cluster.secrets, "randbelow", return_value=0):
            result = cluster.compare("回答不超过5字", "", api_call, tool_run)
        self.assertEqual(result["selected"], "cluster")
        self.assertTrue(result["revision"]["passed"])
        self.assertEqual(result["cluster"]["text"], "更好")
        self.assertEqual(calls, 4)

    def test_pi_adapter_disables_tools_and_uses_stdin(self):
        completed = CompletedProcess([], 0, stdout="Pi 连接成功", stderr="")
        with patch.object(local_tools.Path, "is_file", return_value=True), patch.object(local_tools.subprocess, "run", return_value=completed) as call:
            result = local_tools.run("pi_cli", "隐私任务", timeout=30)
        args, kwargs = call.call_args
        self.assertIn("--no-tools", args[0])
        self.assertIn("--no-session", args[0])
        self.assertNotIn("隐私任务", args[0])
        self.assertEqual(kwargs["input"], "隐私任务")
        self.assertEqual(result["text"], "Pi 连接成功")

    def test_hermes_adapter_uses_text_only_flags(self):
        completed = CompletedProcess([], 0, stdout="Hermes 连接成功\n\nsession_id: local-test", stderr="")
        with patch.object(local_tools.Path, "is_file", return_value=True), patch.object(local_tools.subprocess, "run", return_value=completed) as call:
            result = local_tools.run("hermes_cli", "核对这段文字", timeout=30)
        command = call.call_args.args[0]
        self.assertEqual(command[command.index("--query-file") + 1], "-")
        self.assertEqual(command[command.index("--toolsets") + 1], "")
        self.assertEqual(command[command.index("--model") + 1], "deepseek-flash")
        self.assertNotIn("核对这段文字", command)
        self.assertEqual(result["text"], "Hermes 连接成功")

    def test_manual_handoff_resumes_next_step(self):
        state = server.default_state()
        state["steps"] = [{**state["steps"][0], "provider": "kimi_desktop", "model": "desktop"}, state["steps"][1]]
        seen = []
        def fake_call(provider, model, messages, *args):
            seen.append(messages[1]["content"])
            return {"text": "最终答案", "usage": {}, "elapsed_ms": 1, "model": model}
        with patch.object(server, "load_state", return_value=state), patch.object(server, "get_key", return_value="secret"), patch.object(server, "call_model", side_effect=fake_call):
            paused = server.run_flow("原始需求")
            self.assertEqual(paused["status"], "paused")
            self.assertIn("原始需求", paused["handoff"]["prompt"])
            finished = server.resume_flow(paused["run_id"], "Kimi 的结果")
        self.assertEqual(finished["status"], "completed")
        self.assertIn("Kimi 的结果", seen[0])
        self.assertEqual(finished["final"], "最终答案")


if __name__ == "__main__":
    unittest.main()
