"""Bounded adapters for locally installed agents. No shell interpolation."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

HOME = Path.home()

TOOLS = {
    "pi_cli": {"name": "Pi 编程 Agent", "kind": "cli", "model": "default", "path": HOME / ".pi/agent/bin/pi.cmd", "mode": "automatic", "note": "无工具、无扩展、无会话的文本模式"},
    "dsh_cli": {"name": "DeepSeek Harness", "kind": "cli", "model": "default", "path": HOME / "AppData/Local/Programs/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd", "mode": "automatic", "note": "Headless 文本任务；Harness 自身仍可能调用其内置工具"},
    "hermes_cli": {"name": "Hermes Agent", "kind": "cli", "model": "deepseek-flash", "path": HOME / "AppData/Local/hermes/bin/hermes.exe", "mode": "automatic", "note": "已验证文本模式；显式使用 deepseek-flash，空工具集，最多 1 个工具迭代"},
    "kimi_desktop": {"name": "Kimi 桌面版", "kind": "manual", "model": "desktop", "path": HOME / "AppData/Local/Programs/Kimi/Kimi.exe", "mode": "handoff", "note": "桌面订阅与 API 凭证分开；初版用人工交接"},
    "doubao_work": {"name": "豆包工作版", "kind": "manual", "model": "desktop", "path": None, "mode": "handoff", "note": "初版用人工交接；不假定桌面版提供可调用 API"},
}


def status():
    result = {}
    for key, info in TOOLS.items():
        path = info["path"]
        installed = path is not None and path.is_file()
        if key == "doubao_work":
            installed = (HOME / "AppData/Local/DoubaoWork").exists()
        result[key] = {"installed": installed, "mode": info["mode"], "ready": installed and info["mode"] != "unavailable", "note": info["note"]}
    return result


def run(tool: str, prompt: str, model: str = "default", timeout: int = 120):
    if tool not in ("pi_cli", "dsh_cli", "hermes_cli"):
        raise ValueError("该本机工具暂不支持自动执行")
    info = TOOLS[tool]
    if not info["path"].is_file():
        raise ValueError(f"{info['name']} 命令行入口未找到")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 26000:
        raise ValueError("本机工具任务文本无效")
    if tool == "pi_cli":
        command = [str(info["path"]), "--print", "--no-tools", "--no-extensions", "--no-mcp", "--no-skills", "--no-session"]
        if model and model != "default":
            command += ["--model", model]
        command += ["请完成标准输入中的任务，只返回答案。"]
    elif tool == "dsh_cli":
        # DSH accepts stdin, keeping the task out of the process command line.
        command = [str(info["path"]), "headless", "-"]
        prompt = "只输出文本。不要读写文件、执行命令或调用工具。\n\n" + prompt
    else:
        command = [str(info["path"]), "chat", "--query-file", "-", "--oneshot", "--quiet", "--toolsets", "", "--max-turns", "1", "--source", "tool", "--model", model if model != "default" else info["model"]]
    started = time.monotonic()
    try:
        completed = subprocess.run(command, input=prompt,
                                   text=True, encoding="utf-8", errors="replace",
                                   capture_output=True, timeout=timeout,
                                   cwd=os.environ.get("TEMP") or str(HOME), shell=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{info['name']} 超时（{timeout} 秒）") from None
    except OSError as exc:
        raise RuntimeError(f"无法启动 {info['name']}：{exc.strerror or type(exc).__name__}") from None
    output = completed.stdout.strip()
    if tool == "hermes_cli" and "\nsession_id:" in output:
        output = output.split("\nsession_id:", 1)[0].strip()
    if completed.returncode:
        lines = [line.strip() for line in completed.stderr.splitlines() if line.strip() and not line.strip().startswith("session_id:")]
        if not lines:
            lines = [line.strip() for line in completed.stdout.splitlines() if line.strip() and not line.strip().startswith("session_id:")]
        detail = " | ".join(lines[-3:])[:600] or "没有诊断信息"
        for env_name in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ARK_API_KEY", "OPENAI_API_KEY"):
            secret = os.environ.get(env_name)
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        raise RuntimeError(f"{info['name']} 退出码 {completed.returncode}：{detail}")
    if not output:
        raise RuntimeError(f"{info['name']} 没有返回文本")
    return {"text": output[:100000], "usage": {}, "elapsed_ms": round((time.monotonic() - started) * 1000), "model": model or "default"}
