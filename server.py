"""Local, single-user AI workflow workbench. Standard library only."""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import local_tools
import cluster
import coding_benchmark

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
VAULT = ROOT / "secrets.dpapi.json"
UI = ROOT / "index.html"
LOCK = threading.RLock()
RUNS = {}
MAX_BODY = 1_000_000

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "host": "api.deepseek.com", "docs": "https://api-docs.deepseek.com/guides/your_first_api_call"},
    "qwen": {"name": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus", "host": "dashscope.aliyuncs.com", "docs": "https://help.aliyun.com/zh/model-studio/base-url"},
    "kimi": {"name": "Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-k3", "host": "api.moonshot.cn", "docs": "https://platform.kimi.com/docs/get-api-key"},
    "glm": {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.5", "host": "open.bigmodel.cn", "docs": "https://docs.bigmodel.cn/"},
    "doubao": {"name": "火山方舟·豆包", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": "doubao-seed-2-1-pro-260915", "host": "ark.cn-beijing.volces.com", "docs": "https://docs.volcengine.com/docs/ark/base-url-and-authentication"},
}

def default_state():
    return {
        "providers": {key: {"base_url": p["base_url"], "model": p["model"]} for key, p in PROVIDERS.items()},
        "steps": [
            {"id": "plan", "name": "拆解任务", "provider": "deepseek", "model": "deepseek-flash", "enabled": True, "system": "把用户目标拆成可执行步骤。指出缺失信息；不得捏造已完成的工作。", "temperature": 0.2, "max_tokens": 1000, "timeout": 90},
            {"id": "work", "name": "执行草稿", "provider": "qwen", "model": "qwen-plus", "enabled": True, "system": "根据原始任务和上一阶段结果完成草稿。明确区分事实、假设与建议。", "temperature": 0.3, "max_tokens": 1800, "timeout": 120},
            {"id": "review", "name": "独立审查", "provider": "kimi", "model": "kimi-k3", "enabled": True, "system": "独立检查草稿中的遗漏、无证据断言和逻辑错误。给出修正后的答案。", "temperature": 0.1, "max_tokens": 1800, "timeout": 120},
        ],
        "max_runs": 6,
    }

def load_state():
    if not STATE.exists():
        return default_state()
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        return validate_state(data)
    except Exception:
        return default_state()

def validate_state(data):
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 8:
        raise ValueError("需要 1–8 个流程节点")
    steps = []
    ids = set()
    for item in raw_steps:
        if not isinstance(item, dict):
            raise ValueError("节点格式无效")
        sid = str(item.get("id", ""))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", sid) or sid in ids:
            raise ValueError("节点 ID 无效或重复")
        ids.add(sid)
        provider = str(item.get("provider", ""))
        if provider not in PROVIDERS and provider not in local_tools.TOOLS:
            raise ValueError("未知服务商")
        model = str(item.get("model", "")).strip()
        if not model or len(model) > 100 or not re.fullmatch(r"[\w./:-]+", model):
            raise ValueError("模型 ID 无效")
        name = str(item.get("name", "")).strip()[:60]
        system = str(item.get("system", ""))[:6000]
        temperature = float(item.get("temperature", 0.2))
        max_tokens = int(item.get("max_tokens", 1024))
        timeout = int(item.get("timeout", 90))
        if not 0 <= temperature <= 2 or not 64 <= max_tokens <= 8192 or not 10 <= timeout <= 300:
            raise ValueError("参数超出范围")
        steps.append({"id": sid, "name": name or sid, "provider": provider, "model": model, "enabled": bool(item.get("enabled", True)), "system": system, "temperature": temperature, "max_tokens": max_tokens, "timeout": timeout})
    providers = {}
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ValueError("服务商配置无效")
    for key, info in PROVIDERS.items():
        record = raw_providers.get(key, {})
        if not isinstance(record, dict):
            raise ValueError("服务商配置无效")
        base_url = str(record.get("base_url", info["base_url"])).rstrip("/")
        # Only the verified official host is accepted. A UI edit cannot redirect a saved key.
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != info["host"] or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise ValueError(f"{info['name']} 只能使用官方 HTTPS 域名")
        providers[key] = {"base_url": base_url, "model": str(record.get("model", info["model"]))[:100]}
    max_runs = int(data.get("max_runs", 6))
    if not 1 <= max_runs <= 8:
        raise ValueError("最多调用次数需为 1–8")
    return {"providers": providers, "steps": steps, "max_runs": max_runs}

def save_state(data):
    clean = validate_state(data)
    with LOCK:
        STATE.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

def _blob(raw):
    buf = ctypes.create_string_buffer(raw)
    return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf

def dpapi(raw, decrypt=False):
    if sys.platform != "win32":
        raise RuntimeError("持久密钥保存仅支持 Windows DPAPI；其他系统请使用环境变量")
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    source, keep = _blob(raw)
    output = DATA_BLOB()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(output))
    if not ok:
        raise OSError("Windows DPAPI 无法处理密钥")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(output.pbData)

def load_vault():
    try:
        return json.loads(VAULT.read_text(encoding="utf-8")) if VAULT.exists() else {}
    except Exception:
        return {}

def get_key(provider):
    env_name = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY", "kimi": "MOONSHOT_API_KEY", "glm": "ZHIPUAI_API_KEY", "doubao": "ARK_API_KEY"}[provider]
    if os.getenv(env_name):
        return os.environ[env_name]
    encoded = load_vault().get(provider)
    return dpapi(base64.b64decode(encoded), True).decode("utf-8") if encoded else None

def key_source(provider):
    env_name = {"deepseek": "DEEPSEEK_API_KEY", "qwen": "DASHSCOPE_API_KEY", "kimi": "MOONSHOT_API_KEY", "glm": "ZHIPUAI_API_KEY", "doubao": "ARK_API_KEY"}[provider]
    if os.getenv(env_name):
        return "environment"
    return "vault" if provider in load_vault() else "none"

def set_key(provider, value):
    if provider not in PROVIDERS:
        raise ValueError("未知服务商")
    with LOCK:
        vault = load_vault()
        if value:
            vault[provider] = base64.b64encode(dpapi(value.encode("utf-8"))).decode("ascii")
        else:
            vault.pop(provider, None)
        VAULT.write_text(json.dumps(vault), encoding="utf-8")
    return bool(value)

def call_model(provider, model, messages, temperature=0.2, max_tokens=1024, timeout=90, thinking=None, response_format=None):
    key = get_key(provider)
    if not key:
        raise ValueError(f"{PROVIDERS[provider]['name']} 尚未配置密钥")
    base = load_state()["providers"][provider]["base_url"]
    payload = {"model": model, "messages": messages, "stream": False, "max_tokens": max_tokens}
    if provider == "deepseek" and thinking is not None:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if provider == "deepseek" and response_format:
        payload["response_format"] = response_format
    # Reasoning models often reject temperature; omit it for a portable baseline.
    if provider in ("qwen", "glm"):
        payload["temperature"] = temperature
    request = Request(base + "/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": "LocalAIWorkbench/0.1"}, method="POST")
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(4_000_000)
        data = json.loads(body)
    except HTTPError as exc:
        detail = exc.read(800).decode("utf-8", "replace").replace(key, "[REDACTED]")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
    except URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from None
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("服务商没有返回 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("模型没有返回文本；当前版本不自动执行模型提出的工具调用")
    return {"text": content, "usage": data.get("usage") or {}, "elapsed_ms": round((time.monotonic() - started) * 1000), "model": data.get("model", model)}

def _step_input(prompt, current):
    combined = f"原始任务：\n{prompt}"
    if current:
        combined += f"\n\n上一阶段结果：\n{current[:24000]}"
    return combined


def _continue_flow(run):
    state, prompt, outputs = run["state"], run["prompt"], run["steps"]
    current = run["current"]
    while run["index"] < len(state["steps"]):
        step = state["steps"][run["index"]]
        run["index"] += 1
        if not step["enabled"]:
            outputs.append({"id": step["id"], "name": step["name"], "status": "skipped"})
            continue
        if len([x for x in outputs if x.get("status") == "ok"]) >= state["max_runs"]:
            outputs.append({"id": step["id"], "name": step["name"], "status": "stopped", "error": "达到流程调用上限"})
            break
        combined = _step_input(prompt, current)
        provider = step["provider"]
        if provider in local_tools.TOOLS and local_tools.TOOLS[provider]["kind"] == "manual":
            handoff = f"你是流程中的“{step['name']}”节点。\n\n{step['system']}\n\n{combined}\n\n请只返回本步骤结果。"
            run["pending"] = step
            return {"status": "paused", "run_id": run["id"], "handoff": {"provider": provider, "name": step["name"], "prompt": handoff}, "steps": outputs, "final": current}
        try:
            messages = [{"role": "system", "content": step["system"]}, {"role": "user", "content": combined}]
            if provider in PROVIDERS:
                result = call_model(provider, step["model"], messages, step["temperature"], step["max_tokens"], step["timeout"])
            else:
                task = f"{step['system']}\n\n{combined}"
                result = local_tools.run(provider, task, step["model"], step["timeout"])
            current = result["text"]
            run["current"] = current
            outputs.append({"id": step["id"], "name": step["name"], "provider": provider, "status": "ok", **result})
        except Exception as exc:
            outputs.append({"id": step["id"], "name": step["name"], "provider": provider, "status": "error", "error": str(exc)})
            break
    with LOCK:
        RUNS.pop(run["id"], None)
    return {"status": "error" if outputs and outputs[-1]["status"] == "error" else "completed", "steps": outputs, "final": current}


def run_flow(prompt):
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError("请输入 1–20000 字的任务")
    state = load_state()
    active = [step for step in state["steps"] if step["enabled"]][:state["max_runs"]]
    missing = sorted({PROVIDERS[step["provider"]]["name"] for step in active if step["provider"] in PROVIDERS and not get_key(step["provider"])})
    if missing:
        raise ValueError("运行前请先配置密钥：" + "、".join(missing))
    unavailable = sorted({local_tools.TOOLS[step["provider"]]["name"] for step in active if step["provider"] in local_tools.TOOLS and local_tools.TOOLS[step["provider"]]["kind"] == "cli" and not local_tools.status()[step["provider"]]["ready"]})
    if unavailable:
        raise ValueError("本机工具尚不可运行：" + "、".join(unavailable))
    run_id = secrets.token_urlsafe(18)
    run = {"id": run_id, "state": state, "prompt": prompt, "steps": [], "current": "", "index": 0, "pending": None, "created": time.time()}
    with LOCK:
        for old_id, old in list(RUNS.items()):
            if time.time() - old["created"] > 3600:
                RUNS.pop(old_id, None)
        if len(RUNS) >= 50:
            raise ValueError("待继续的流程过多，请稍后重试")
        RUNS[run_id] = run
    return _continue_flow(run)


def resume_flow(run_id, result):
    if not isinstance(result, str) or not result.strip() or len(result) > 100000:
        raise ValueError("请粘贴 1–100000 字的桌面应用结果")
    with LOCK:
        run = RUNS.get(run_id)
        if not run or not run["pending"] or time.time() - run["created"] > 3600:
            raise ValueError("流程已过期，请重新运行")
        step = run["pending"]
        run["pending"] = None
        run["current"] = result.strip()
        run["steps"].append({"id": step["id"], "name": step["name"], "provider": step["provider"], "status": "ok", "text": result.strip(), "usage": {}, "model": "desktop"})
    return _continue_flow(run)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Never log request bodies, keys, or prompts.
        print("[%s] %s" % (self.log_date_time_string(), format % args))

    def reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def guard(self):
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            self.reply(403, {"error": "仅允许本机访问"})
            return False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://127.0.0.1:8765", "http://localhost:8765"):
                self.reply(403, {"error": "来源无效"})
                return False
        return True

    def do_GET(self):
        if not self.guard():
            return
        if self.path == "/":
            raw = UI.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(raw)
        elif self.path == "/api/state":
            catalog = {**{p: {**info, "kind": "api"} for p, info in PROVIDERS.items()}, **{p: {"name": info["name"], "kind": info["kind"], "model": info["model"], "note": info["note"]} for p, info in local_tools.TOOLS.items()}}
            self.reply(200, {"state": load_state(), "catalog": catalog, "keys": {p: bool(get_key(p)) for p in PROVIDERS}, "key_sources": {p: key_source(p) for p in PROVIDERS}, "local_status": local_tools.status()})
        else:
            self.reply(404, {"error": "未找到"})

    def do_POST(self):
        if not self.guard():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("请求大小无效")
            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/state":
                result = {"state": save_state(payload)}
            elif self.path == "/api/key":
                provider = payload.get("provider")
                value = payload.get("key", "")
                if not isinstance(value, str) or len(value) > 1000:
                    raise ValueError("密钥无效")
                result = {"saved": set_key(provider, value)}
            elif self.path == "/api/test":
                provider = payload.get("provider")
                if provider not in PROVIDERS:
                    raise ValueError("未知服务商")
                model = str(payload.get("model") or load_state()["providers"][provider]["model"])
                result = call_model(provider, model, [{"role": "user", "content": "请只回答：连接成功"}], max_tokens=64, timeout=45)
            elif self.path == "/api/run":
                result = run_flow(payload.get("prompt"))
            elif self.path == "/api/compare":
                if not get_key("deepseek"):
                    raise ValueError("请先配置 DeepSeek 密钥，用于单模型基线和盲审")
                result = cluster.compare(payload.get("prompt"), payload.get("criteria", ""), call_model, local_tools.run, load_state()["providers"]["deepseek"]["model"])
            elif self.path == "/api/coding-benchmark":
                if not get_key("deepseek"):
                    raise ValueError("请先配置 DeepSeek 密钥")
                result = cluster.compare(coding_benchmark.TASK, coding_benchmark.CRITERIA, call_model,
                                         local_tools.run, load_state()["providers"]["deepseek"]["model"],
                                         objective_eval=coding_benchmark.evaluate)
            elif self.path == "/api/resume":
                result = resume_flow(payload.get("run_id"), payload.get("result"))
            else:
                return self.reply(404, {"error": "未找到"})
            self.reply(200, result)
        except (ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception as exc:
            self.reply(502, {"error": str(exc)})

def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("AI 工作台：http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
