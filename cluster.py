"""Compare a single-model answer with a multi-agent candidate and keep evidence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re
import secrets
import time

import local_tools


def _judge_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("winner") not in ("A", "B", "tie"):
        raise ValueError("审查结果格式无效")
    scores = value.get("scores")
    if not isinstance(scores, dict) or any(not isinstance(scores.get(k), (int, float)) or not 0 <= scores[k] <= 10 for k in ("A", "B")):
        raise ValueError("审查评分格式无效")
    return {"winner": value["winner"], "scores": {"A": scores["A"], "B": scores["B"]}, "reason": str(value.get("reason", ""))[:1000]}


def _hard_rules(prompt, criteria):
    text = prompt + "\n" + criteria
    limits = [int(value) for pair in re.findall(r"(?:不超过|最多|至多|限于)\s*(\d{1,5})\s*字|\b(\d{1,5})\s*字以内", text) for value in pair if value]
    return {
        "max_characters": min(limits) if limits else None,
        "three_numbered_steps": bool(re.search(r"(?:3|三)\s*个?\s*编号步骤", text)),
        "fallback_rule": "回退规则" in text or "失败回退" in text,
    }


def _check_answer(answer, rules):
    characters = len(re.sub(r"\s", "", answer))
    checks = {}
    if rules["max_characters"] is not None:
        checks["max_characters"] = characters <= rules["max_characters"]
    if rules["three_numbered_steps"]:
        numbers = re.findall(r"(?m)^\s*([123])[.．、]\s*", answer)
        checks["three_numbered_steps"] = numbers == ["1", "2", "3"]
    if rules["fallback_rule"]:
        checks["fallback_rule"] = "回退" in answer
    return {"characters": characters, "checks": checks, "passed": all(checks.values())}


def compare(prompt, criteria, api_call, tool_run, baseline_model="deepseek-flash", objective_eval=None):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError("请输入 1–20000 字的对比任务")
    if not isinstance(criteria, str) or len(criteria) > 3000:
        raise ValueError("验收标准最多 3000 字")
    statuses = local_tools.status()
    missing = [local_tools.TOOLS[k]["name"] for k in ("pi_cli", "dsh_cli") if not statuses[k]["ready"]]
    if missing:
        raise ValueError("集群对比需要这些本机工具：" + "、".join(missing))
    started = time.monotonic()
    task = f"任务：\n{prompt.strip()}\n\n验收标准：\n{criteria.strip() or '准确回答任务；明确不确定性；避免无依据断言。'}"
    baseline = api_call("deepseek", baseline_model, [
        {"role": "system", "content": "独立完成任务。准确、完整、简洁；不要编造事实。"},
        {"role": "user", "content": task},
    ], max_tokens=1600, timeout=90)

    def candidate(tool):
        try:
            options = {"model": "deepseek-flash"} if tool == "hermes_cli" else {}
            return {"status": "ok", **tool_run(tool, "独立完成以下任务。不要参考其他模型。\n\n" + task, timeout=120, **options)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    tools = ["pi_cli", "dsh_cli"] + (["hermes_cli"] if statuses["hermes_cli"]["ready"] else [])
    with ThreadPoolExecutor(max_workers=len(tools)) as pool:
        futures = {tool: pool.submit(candidate, tool) for tool in tools}
        candidates = {tool: future.result() for tool, future in futures.items()}
    good = [(key, result["text"]) for key, result in candidates.items() if result["status"] == "ok"]
    if not good:
        return {"status": "fallback", "selected": "baseline", "baseline": baseline, "cluster": None, "candidates": candidates, "judge": None, "elapsed_ms": round((time.monotonic() - started) * 1000), "note": "独立候选均失败，保留单模型答案"}

    synth_input = task + "\n\n单模型基线答案：\n" + baseline["text"][:5000]
    for key, answer in good:
        synth_input += f"\n\n{local_tools.TOOLS[key]['name']} 的独立答案：\n{answer[:5000]}"
    synth_input += "\n\n请综合各答案，只保留有依据且符合验收标准的内容；发现冲突时说明不确定性。直接给出最终答案，不要讨论投票过程。"
    try:
        cluster = api_call("deepseek", baseline_model, [
            {"role": "system", "content": "综合多份候选答案，严格按验收标准只保留有依据的内容。直接给出最终答案。"},
            {"role": "user", "content": synth_input},
        ], max_tokens=2048, timeout=90, thinking=False)
    except Exception as exc:
        return {"status": "fallback", "selected": "baseline", "baseline": baseline, "cluster": None, "candidates": candidates, "judge": None, "elapsed_ms": round((time.monotonic() - started) * 1000), "note": "合成失败，保留单模型答案：" + str(exc)[:300]}

    if objective_eval is not None:
        synthesis = cluster
        objective = {"baseline": objective_eval(baseline["text"]), "synthesis": objective_eval(cluster["text"])}
        best = cluster
        best_source = "synthesis"
        best_score = objective["synthesis"]["passed"] if objective["synthesis"].get("scored", True) else -1
        for key, answer in good:
            objective[key] = objective_eval(answer)
            if objective[key].get("scored", True) and objective[key]["passed"] > best_score:
                best = candidates[key]
                best_source = key
                best_score = objective[key]["passed"]
        repair = None
        repaired = None
        if (objective["baseline"].get("scored", True) and best_score >= 0
                and best_score <= objective["baseline"]["passed"]
                and objective["baseline"]["passed"] < objective["baseline"]["total"]):
            feedback = objective[best_source]
            repair_prompt = (task + "\n\n候选代码：\n" + best["text"][:12000]
                             + "\n\n自动测试结果：\n" + json.dumps(feedback, ensure_ascii=False)
                             + "\n\n请根据失败用例修复代码。只输出完整的函数代码，不要解释。")
            try:
                repaired = api_call("deepseek", baseline_model, [
                    {"role": "system", "content": "你是 Python 代码修复器。依据测试失败用例修复缺陷；只输出函数代码。"},
                    {"role": "user", "content": repair_prompt},
                ], max_tokens=1200, timeout=90, thinking=False)
                objective["repair"] = objective_eval(repaired["text"])
                repair = {"attempted": True, "usage": repaired.get("usage", {}), "passed": objective["repair"]["passed"]}
                if objective["repair"].get("scored", True) and objective["repair"]["passed"] > best_score:
                    best, best_source, best_score = repaired, "repair", objective["repair"]["passed"]
            except Exception as exc:
                repair = {"attempted": True, "error": str(exc)[:300]}
        baseline_scored = objective["baseline"].get("scored", True)
        improved = baseline_scored and best_score > objective["baseline"]["passed"]
        reason = ("单模型代码超出受限执行器范围，本轮无法公平判优" if not baseline_scored
                  else f"固定测试：单模型 {objective['baseline']['passed']}/{objective['baseline']['total']}，集群最佳 {max(best_score, 0)}/{objective['baseline']['total']}；" + ("集群严格更高" if improved else "未超过单模型，保留基线"))
        return {"status": "completed", "selected": "cluster" if improved else "baseline", "baseline": baseline,
                "cluster": best, "synthesis": synthesis, "repair_candidate": repaired,
                "selected_source": best_source, "synthesizer": "deepseek", "candidates": candidates,
                "judge": None, "objective": objective, "revision": repair,
                "selection_reason": reason,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "note": "只代表这道固定编程题；测试集公开且有限，不能推断所有任务质量"}

    rules = _hard_rules(prompt, criteria)
    baseline_check = _check_answer(baseline["text"], rules)
    cluster_check = _check_answer(cluster["text"], rules)
    revision = None
    if not cluster_check["passed"]:
        failed = "、".join(key for key, passed in cluster_check["checks"].items() if not passed)
        repair_prompt = (task + f"\n\n当前集群答案未通过硬性检查（{failed}）。请只重写下面的答案，严格满足验收标准。"
                         + (f"最多 {rules['max_characters']} 个非空白字符。" if rules["max_characters"] else "")
                         + "保持事实和结论，不添加未验证内容。只输出修订答案。\n\n" + cluster["text"][:12000])
        try:
            revised = api_call("deepseek", baseline_model, [
                {"role": "system", "content": "你是格式修订器。只修复硬性要求，不添加新事实。"},
                {"role": "user", "content": repair_prompt},
            ], max_tokens=1024, timeout=90, thinking=False)
            revision = {"attempted": True, "usage": revised.get("usage", {})}
            revised_check = _check_answer(revised["text"], rules)
            revision["passed"] = revised_check["passed"]
            if revised_check["passed"]:
                cluster = revised
                cluster_check = revised_check
        except Exception as exc:
            revision = {"attempted": True, "passed": False, "error": str(exc)[:300]}
    hard_checks = {"rules": rules, "baseline": baseline_check, "cluster": cluster_check}
    if not cluster_check["passed"]:
        return {"status": "completed", "selected": "baseline", "baseline": baseline, "cluster": cluster, "synthesizer": "deepseek", "candidates": candidates, "judge": None, "hard_checks": hard_checks, "revision": revision, "selection_reason": "集群答案未通过可核验的硬性要求，保留单模型答案", "elapsed_ms": round((time.monotonic() - started) * 1000), "note": "本次未达到集群优于单模型的要求"}

    cluster_is_a = bool(secrets.randbelow(2))
    answers = {"A": cluster["text"] if cluster_is_a else baseline["text"], "B": baseline["text"] if cluster_is_a else cluster["text"]}
    judge_prompt = (task + "\n\n以下两份答案已随机标为 A 和 B。按准确性、完整性、验收标准逐项比较。"
                    "如果无法明确区分，返回 tie。只输出 JSON 对象，字段为 winner（A/B/tie）、scores（A、B 各 0-10）、reason。"
                    f"\n\n答案 A：\n{answers['A'][:14000]}\n\n答案 B：\n{answers['B'][:14000]}")
    try:
        raw_judge = api_call("deepseek", baseline_model, [
            {"role": "system", "content": '你是审查员。只按给出的任务和验收标准评分；不偏好更长的答案。严格输出 json，例如 {"winner":"tie","scores":{"A":8,"B":8},"reason":"差异不足"}。'},
            {"role": "user", "content": judge_prompt},
        ], max_tokens=1024, timeout=90, thinking=False, response_format={"type": "json_object"})
        judged = _judge_json(raw_judge["text"])
        cluster_label = "A" if cluster_is_a else "B"
        baseline_label = "B" if cluster_is_a else "A"
        improved = judged["winner"] == cluster_label and judged["scores"][cluster_label] > judged["scores"][baseline_label]
        if not baseline_check["passed"]:
            improved = True
        judge = {"estimated_winner": "cluster" if improved else "baseline", "scores": {"baseline": judged["scores"][baseline_label], "cluster": judged["scores"][cluster_label]}, "reason": judged["reason"], "model": raw_judge["model"], "usage": raw_judge.get("usage", {})}
    except Exception as exc:
        improved = False
        judge = {"estimated_winner": "baseline", "scores": None, "reason": "审查未能完成：" + str(exc)[:300]}
    if not baseline_check["passed"]:
        improved = True
        judge["estimated_winner"] = "cluster"
        judge["reason"] = "单模型未通过硬性要求，集群通过；" + judge["reason"]
    reason = "单模型未通过硬性要求，集群通过" if improved and not baseline_check["passed"] else "集群通过硬性要求且审查估分更高" if improved else "审查未判定集群更优，保留单模型答案"
    return {"status": "completed", "selected": "cluster" if improved else "baseline", "baseline": baseline, "cluster": cluster, "synthesizer": "deepseek", "candidates": candidates, "judge": judge, "hard_checks": hard_checks, "revision": revision, "selection_reason": reason, "elapsed_ms": round((time.monotonic() - started) * 1000), "note": "模型审查仍是估计；本次结果不能代表所有任务"}
