"""Small stdio MCP bridge so Codex can invoke the saved local workflow."""

import json
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765/api/"

TOOLS = [
    {
        "name": "run_workflow",
        "title": "运行本地模型流程",
        "description": "把一个有界任务交给工作台中已配置的模型流程，并返回每一步结果。仅调用模型 API，不执行模型提出的命令。",
        "inputSchema": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "明确的任务、输入和验收条件"}},
            "required": ["prompt"],
        },
    },
    {
        "name": "provider_status",
        "title": "查看模型连接状态",
        "description": "只读取工作台已配置的服务商和密钥状态，不返回密钥。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "resume_workflow",
        "title": "继续桌面应用交接流程",
        "description": "将用户从 Kimi 或豆包工作版贴回的结果交给已暂停的本地流程。",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "result": {"type": "string"}}, "required": ["run_id", "result"]},
    },
    {
        "name": "compare_workflow",
        "title": "对比单模型与集群答案",
        "description": "对同一任务运行单模型基线和多模型候选，返回盲位次审查、两份答案与调用记录。审查结论只是估计。",
        "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}, "criteria": {"type": "string"}}, "required": ["prompt"]},
    },
]

def emit(item):
    sys.stdout.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def workbench(path, payload=None):
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(BASE + path, data=raw, headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
    try:
        with urlopen(request, timeout=600) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("error", "请求失败")
        except Exception:
            detail = f"HTTP {exc.code}"
        raise ValueError(detail) from None

def handle(msg):
    if "id" not in msg:
        return None
    mid = msg["id"]
    method = msg.get("method")
    try:
        if method == "initialize":
            requested = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
            result = {"protocolVersion": requested, "capabilities": {"tools": {}}, "serverInfo": {"name": "local-ai-workbench", "version": "0.1.0"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "run_workflow":
                result = {"content": [{"type": "text", "text": json.dumps(workbench("run", {"prompt": args.get("prompt")}), ensure_ascii=False)}]}
            elif name == "provider_status":
                data = workbench("state")
                result = {"content": [{"type": "text", "text": json.dumps({"api": {p: {"name": info["name"], "configured": data["keys"][p]} for p, info in data["catalog"].items() if info["kind"] == "api"}, "local": data["local_status"]}, ensure_ascii=False)}]}
            elif name == "resume_workflow":
                result = {"content": [{"type": "text", "text": json.dumps(workbench("resume", {"run_id": args.get("run_id"), "result": args.get("result")}), ensure_ascii=False)}]}
            elif name == "compare_workflow":
                result = {"content": [{"type": "text", "text": json.dumps(workbench("compare", {"prompt": args.get("prompt"), "criteria": args.get("criteria", "")}), ensure_ascii=False)}]}
            else:
                raise ValueError("未知工具")
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as exc:
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}

def main():
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
            if response is not None:
                emit(response)
        except Exception as exc:
            print(f"MCP input error: {exc}", file=sys.stderr)

if __name__ == "__main__":
    main()
