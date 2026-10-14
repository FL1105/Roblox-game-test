"""A feature plugin may compute things from its Context. It may not read files, import the data loaders, or reach for the dataset
another way: such a plugin could see the future, and no amount of testing the Context can catch that. The source is checked before it runs.

This is a guard against accidents and shortcuts, not a security boundary against a determined attacker: it keeps the honest
paths honest and makes the dishonest ones loud (and the red-team review tries to break it)."""

from __future__ import annotations

import ast

ALLOWED_IMPORTS = {"math", "statistics", "re", "collections", "itertools", "functools", "dataclasses", "typing", "string", "unicodedata", "numpy",
                   "research.registry", "research.context", "research.features"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr", "input", "breakpoint",
                   "memoryview", "help", "dir", "type", "super", "object", "classmethod", "staticmethod", "property"}
FORBIDDEN_ATTRS = {"load", "loads", "fromfile", "memmap", "genfromtxt", "loadtxt", "read", "readlines", "readline", "read_csv", "read_json", "open", "system",
                   "popen", "run", "check_output", "check_call", "Popen", "environ", "getenv", "modules", "path", "walk", "listdir", "glob",
                   "__dict__", "__globals__", "__builtins__", "__code__", "__class__", "__subclasses__", "__bases__", "__mro__", "__import__", "f_globals",
                   "f_back", "gi_frame", "cr_frame", "tokens", "_tokens", "_launch_times", "_features", "outcomes", "ledger", "returns", "events"}
FORBIDDEN_MODULE_NAMES = {"sys", "os", "pickle", "subprocess", "importlib", "inspect", "gc", "builtins", "ctypes", "socket", "shutil", "pathlib", "json", "glob"}


class PluginRejected(ValueError):
    pass


def check_source(source: str, filename: str = "<plugin>") -> None:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS and a.name.split(".")[0] not in {m for m in ALLOWED_IMPORTS if "." not in m}:
                    problems.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
                if a.name.split(".")[0] in FORBIDDEN_MODULE_NAMES:
                    problems.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or (mod not in ALLOWED_IMPORTS and mod.split(".")[0] not in {m for m in ALLOWED_IMPORTS if "." not in m}):
                problems.append(f"line {node.lineno}: 'from {'.' * node.level}{mod} import ...' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id in FORBIDDEN_MODULE_NAMES):
            problems.append(f"line {node.lineno}: use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            problems.append(f"line {node.lineno}: access to '.{node.attr}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {node.lineno}: global/nonlocal state is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and (node.value.startswith("__") and node.value.endswith("__") and len(node.value) > 4):
            problems.append(f"line {node.lineno}: dunder string '{node.value}' is not allowed")
    # module level: only imports, defs, docstrings and simple constants (so nothing runs on import except registering the sets)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Assign) and _simple_constant(node.value):
            continue
        problems.append(f"line {node.lineno}: module-level {type(node).__name__} is not allowed (only imports, functions and plain constants)")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                ok = isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "feature_set"
                if not ok:
                    problems.append(f"line {node.lineno}: only @feature_set(...) may decorate a function")
    if problems:
        raise PluginRejected(f"{filename} rejected: " + "; ".join(problems[:8]))


def _simple_constant(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_simple_constant(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _simple_constant(k) and _simple_constant(v) for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.UnaryOp):
        return _simple_constant(node.operand)
    if isinstance(node, ast.BinOp):
        return _simple_constant(node.left) and _simple_constant(node.right)
    if isinstance(node, ast.Call):  # frozenset({...}) / set(...) / tuple(...) of constants
        return isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "tuple", "set") and all(_simple_constant(a) for a in node.args) and not node.keywords
    return False
