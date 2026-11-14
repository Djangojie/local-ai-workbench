"""Read local workbench status or run its fixed coding benchmark; print a brief summary."""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8765/api/"


def fetch(path: str, post: bool = False) -> dict:
    request = Request(BASE + path, data=b"{}" if post else None,
                      headers={"Content-Type": "application/json"},
                      method="POST" if post else "GET")
    try:
        with urlopen(request, timeout=300 if post else 5) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc).get("error", f"HTTP {exc.code}")
        except Exception:
            detail = f"HTTP {exc.code}"
        raise RuntimeError(detail) from None
    except URLError as exc:
        raise RuntimeError(f"无法连接本机工作台：{exc.reason}") from None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "coding-benchmark"))
    args = parser.parse_args()
    try:
        if args.action == "status":
            data = fetch("state")
            print("工作台服务：可连接")
            for name, ready in data["keys"].items():
                print(f"API {name}: {'密钥已配置' if ready else '尚未配置'}")
            for name, state in data["local_status"].items():
                label = "人工交接" if state.get("mode") == "handoff" else "可用" if state["ready"] else "不可用"
                print(f"本机 {name}: {label}")
            return 0
        data = fetch("coding-benchmark", post=True)
        print(data.get("selection_reason") or data.get("note") or "无选择理由")
        print(f"耗时：{data.get('elapsed_ms', 0) / 1000:.1f} 秒")
        for name, result in data.get("objective", {}).items():
            outcome = f"{result['passed']}/{result['total']}" if result.get("scored", True) else "未评分"
            print(f"{name}: {outcome}" + (f"；{result['error']}" if result.get("error") else ""))
        for name, result in data.get("candidates", {}).items():
            if result.get("status") == "error":
                print(f"{name}: 执行失败；{result.get('error', '')[:180]}")
        return 0 if data.get("status") == "completed" else 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
