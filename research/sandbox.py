"""A feature plugin may compute things from its Context. It may not read files, import the data loaders, keep state between calls, or carry an
answer key: any of those could let it see the future, and testing the Context alone cannot catch them. The source is checked before it runs, and the
plugin is only ever run in a fresh worker process (research/worker.py) that also checks its behaviour.

This is a guard against accidents and shortcuts, not a security boundary against a determined attacker: it keeps the honest paths honest and makes the
dishonest ones loud. The real protection against a smuggled answer is the sealed recording, which no plugin has seen."""

from __future__ import annotations

import ast
import re

ALLOWED_IMPORTS = {"math", "statistics", "re", "collections", "itertools", "functools", "dataclasses", "typing", "string", "unicodedata", "numpy",
                   "research.registry", "research.context", "research.features"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars", "getattr", "setattr", "delattr", "input", "breakpoint",
                   "memoryview", "help", "dir", "type", "super", "object", "classmethod", "staticmethod", "property", "id", "hash"}
FORBIDDEN_ATTRS = {"load", "loads", "fromfile", "memmap", "genfromtxt", "loadtxt", "read", "readlines", "readline", "read_csv", "read_json", "open", "system",
                   "popen", "run", "check_output", "check_call", "Popen", "environ", "getenv", "modules", "path", "walk", "listdir", "glob",
                   "__dict__", "__globals__", "__builtins__", "__code__", "__class__", "__subclasses__", "__bases__", "__mro__", "__import__", "f_globals",
                   "f_back", "gi_frame", "cr_frame", "tokens", "_tokens", "_launch_times", "_features", "outcomes", "ledger", "returns", "events",
                   "cache_clear", "cache_info", "random", "seed", "default_rng", "RandomState", "shuffle", "permutation", "choice", "time", "sleep"}
FORBIDDEN_MODULE_NAMES = {"sys", "os", "pickle", "subprocess", "importlib", "inspect", "gc", "builtins", "ctypes", "socket", "shutil", "pathlib", "json", "glob",
                          "random", "time", "datetime", "threading", "multiprocessing", "weakref", "copyreg", "marshal", "tempfile"}
MAX_MODULE_CONSTANTS = 400      # a word list is fine; a table of answers is not
MAX_STRING = 24                 # a mint address is 32-44 characters
MINT_LIKE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{28,}")


class PluginRejected(ValueError):
    pass


def check_source(source: str, filename: str = "<plugin>") -> None:
    tree = ast.parse(source, filename)
    problems: list[str] = []
    top = {m.split(".")[0] for m in ALLOWED_IMPORTS if "." not in m}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_IMPORTS and a.name.split(".")[0] not in top:
                    problems.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
                if a.name.split(".")[0] in FORBIDDEN_MODULE_NAMES:
                    problems.append(f"line {node.lineno}: import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level or (mod not in ALLOWED_IMPORTS and mod.split(".")[0] not in top):
                problems.append(f"line {node.lineno}: 'from {'.' * node.level}{mod} import ...' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id in FORBIDDEN_MODULE_NAMES):
            problems.append(f"line {node.lineno}: use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRS:
                problems.append(f"line {node.lineno}: access to '.{node.attr}' is not allowed")
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                problems.append(f"line {node.lineno}: assigning to an attribute is not allowed (no state may be kept between calls)")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append(f"line {node.lineno}: global/nonlocal state is not allowed")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.AsyncFor, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom)):
            problems.append(f"line {node.lineno}: {type(node).__name__} is not allowed")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("__") and node.value.endswith("__") and len(node.value) > 4:
                problems.append(f"line {node.lineno}: dunder string '{node.value}' is not allowed")
    # functions: no default values (a mutable default is state that survives between calls), no *args/**kwargs, no nesting
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            a = node.args
            if a.defaults or a.kw_defaults or a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs:
                problems.append(f"line {node.lineno}: function '{node.name}' may only take plain positional parameters without defaults")
            decorated = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "feature_set" for d in node.decorator_list)
            if decorated and len(a.args) != 1:
                problems.append(f"line {node.lineno}: a feature set takes exactly one parameter (the context)")
            for d in node.decorator_list:
                if not (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "feature_set"):
                    problems.append(f"line {node.lineno}: only @feature_set(...) may decorate a function")
            for inner in ast.walk(node):
                if inner is not node and isinstance(inner, ast.FunctionDef):
                    problems.append(f"line {inner.lineno}: nested functions are not allowed")
    # module level: only imports, functions, docstrings and plain immutable constants (so nothing runs on import except registering the sets)
    weight = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.Assign) and _simple_constant(node.value):
            weight += sum(1 for n in ast.walk(node.value) if isinstance(n, ast.Constant))
            for n in ast.walk(node.value):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and (len(n.value) > MAX_STRING or MINT_LIKE.fullmatch(n.value)):
                    problems.append(f"line {node.lineno}: string constants longer than {MAX_STRING} characters are not allowed (no answer keys)")
            continue
        problems.append(f"line {node.lineno}: module-level {type(node).__name__} is not allowed (only imports, functions and plain constants)")
    if weight > MAX_MODULE_CONSTANTS:
        problems.append(f"module defines {weight} constants (limit {MAX_MODULE_CONSTANTS}): no lookup tables of answers")
    if problems:
        raise PluginRejected(f"{filename} rejected: " + "; ".join(problems[:8]))


def _simple_constant(node: ast.AST) -> bool:
    """Immutable constants only: numbers, strings, and tuples/frozensets of them. Lists, dicts and sets are mutable (state); a dict literal is also an answer key."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Tuple):
        return all(_simple_constant(e) for e in node.elts)
    if isinstance(node, ast.UnaryOp):
        return _simple_constant(node.operand)
    if isinstance(node, ast.BinOp):
        return _simple_constant(node.left) and _simple_constant(node.right)
    if isinstance(node, ast.Call):  # frozenset({...}) / tuple((...)) of constants
        return (isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "tuple") and len(node.args) == 1 and not node.keywords
                and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple)) and all(_simple_constant(e) for e in node.args[0].elts))
    return False
