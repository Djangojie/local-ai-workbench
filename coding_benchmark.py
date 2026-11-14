"""One reproducible coding/bug-fix benchmark with constrained code execution."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys


TASK = """修复下面 Python 函数。它找出两个不同位置的元素，其和为 target，返回 [i,j]（i<j）。
要求返回所有有效位置对中字典序最小的一对；没有则返回 None。不要修改 nums。
只输出完整的 def first_pair(nums, target): 函数代码，不要导入模块。

现有错误实现：
def first_pair(nums, target):
    seen = {}
    for j, x in enumerate(nums):
        if target - x in seen:
            return [seen[target - x], j]
        seen[x] = j
    return None

例如 first_pair([1, 2, 3, 4], 5) 应返回 [0, 3]，而原函数会返回 [1, 2]。"""
CRITERIA = "必须返回字典序最小位置对；处理重复值、负数、无解、空列表；不修改输入。以固定测试通过数为准。"
CASES = [
    ([], 1, None), ([1], 2, None), ([1, 2, 3, 4], 5, [0, 3]),
    ([3, 3], 6, [0, 1]), ([3, 3, 3], 6, [0, 1]),
    ([-3, 8, 1, 4, 7], 5, [0, 1]), ([0, 0, 0], 0, [0, 1]),
    ([1, 4, 2, 3, 4], 5, [0, 1]), ([5, 1, 9, 4], 10, [1, 2]),
    ([1, 2, 4], 10, None), ([2, -1, 4, 3, 1], 5, [0, 3]),
]

ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return,
    ast.Assign, ast.AugAssign, ast.AnnAssign, ast.If, ast.For, ast.While, ast.Break,
    ast.Continue, ast.Pass, ast.Expr, ast.Name, ast.Constant, ast.List,
    ast.Tuple, ast.Dict, ast.Subscript, ast.Slice, ast.Attribute, ast.Compare, ast.BoolOp,
    ast.BinOp, ast.UnaryOp, ast.IfExp, ast.Call, ast.Load, ast.Store,
    ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod, ast.Div, ast.USub,
    ast.UAdd, ast.And, ast.Or, ast.Not, ast.Eq, ast.NotEq, ast.Lt,
    ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
)
SAFE_BUILTINS = {"range": range, "len": len, "enumerate": enumerate,
                 "min": min, "max": max, "abs": abs, "int": int,
                 "list": list, "tuple": tuple, "sorted": sorted}
SAFE_METHODS = {"append", "get", "add", "sort", "copy"}


def _source(answer):
    match = re.search(r"```(?:python|py)?\s*\n(.*?)```", answer, re.I | re.S)
    return match.group(1).strip() if match else answer.strip()


def _validate(source):
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef) or tree.body[0].name != "first_pair":
        raise ValueError("只接受一个 first_pair 函数")
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError("代码包含未开放的语法")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or "__" in node.id):
            raise ValueError("代码包含未开放的名称")
        if isinstance(node, ast.Attribute) and node.attr not in SAFE_METHODS:
            raise ValueError("只允许调用受限的容器方法")
        if isinstance(node, ast.Call):
            safe_name = isinstance(node.func, ast.Name) and node.func.id in SAFE_BUILTINS
            safe_method = isinstance(node.func, ast.Attribute) and node.func.attr in SAFE_METHODS
            if not (safe_name or safe_method):
                raise ValueError("只允许调用受限的内置函数或容器方法")
    fn = tree.body[0]
    if len(fn.args.args) != 2 or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        raise ValueError("函数必须只接收 nums 和 target")
    fn.returns = None
    for arg in fn.args.args:
        arg.annotation = None
    return tree


def _worker(payload):
    tree = _validate(payload["source"])
    scope = {"__builtins__": SAFE_BUILTINS}
    exec(compile(tree, "<candidate>", "exec"), scope)
    function = scope["first_pair"]
    results = []
    for nums, target, expected in CASES:
        original = nums[:]
        try:
            actual = function(nums, target)
            if isinstance(actual, tuple):
                actual = list(actual)
            passed = actual == expected and nums == original and type(actual) in (list, type(None))
            results.append({"passed": passed, "input": [original, target], "expected": expected,
                            "actual": actual if isinstance(actual, (list, int, str, type(None))) else str(type(actual))})
        except Exception as exc:
            results.append({"passed": False, "input": [original, target], "expected": expected,
                            "error": type(exc).__name__})
    return results


def evaluate(answer):
    """Run a candidate in a separate process with a strict AST allowlist and timeout."""
    source = _source(answer)
    if len(source) > 12000:
        return {"scored": False, "passed": 0, "total": len(CASES), "error": "代码过长", "failures": []}
    try:
        _validate(source)
    except (SyntaxError, ValueError) as exc:
        return {"scored": False, "passed": 0, "total": len(CASES), "error": str(exc)[:180], "failures": []}
    try:
        completed = subprocess.run([sys.executable, "-I", __file__, "--worker"],
                                   input=json.dumps({"source": source}), text=True,
                                   capture_output=True, timeout=3)
        if completed.returncode:
            raise ValueError((completed.stderr or "运行失败")[-180:])
        cases = json.loads(completed.stdout)
        if not isinstance(cases, list) or len(cases) != len(CASES):
            raise ValueError("测试结果格式无效")
        return {"scored": True, "passed": sum(bool(item["passed"]) for item in cases), "total": len(CASES),
                "failures": [item for item in cases if not item["passed"]][:5]}
    except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        return {"scored": False, "passed": 0, "total": len(CASES), "error": "运行超时" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)[:180], "failures": []}


if __name__ == "__main__" and len(sys.argv) == 2 and sys.argv[1] == "--worker":
    try:
        print(json.dumps(_worker(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
