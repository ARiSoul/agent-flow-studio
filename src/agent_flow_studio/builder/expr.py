"""Edge conditions as expressions (`if:` in an edge), evaluated by the studio without an LLM.

A small, safe language parsed with `ast` and interpreted here (never `eval`):
- references: `input.<name>`, `<agent>.<output>` (with `.field` or `[0]` to go inside JSON) and
  `<gate>.decision` / `<gate>.comment`;
- literals: strings, numbers, true/false/null (also True/False/None), lists;
- operators: == != < <= > >=, in, not in, and, or, not, + - (numbers);
- functions: len(x), exists(x), lower(x), upper(x), contains(x, y).
Anything else (calls, attributes outside a reference, dunders, lambdas, comprehensions…) is refused.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from ..messages import msg

MAX_LEN = 500
FUNCS = {"len", "exists", "lower", "upper", "contains"}
_CONST_NAMES = {"true": True, "false": False, "null": None, "True": True, "False": False,
                "None": None}
_CMP = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)


class ExprError(ValueError):
    pass


class _Missing:
    """A reference without a value yet (comparisons with it are False)."""

    def __repr__(self) -> str:
        return "missing"


MISSING = _Missing()


def _ref(node: ast.AST) -> list[str | int] | None:
    """`a.b.c[0]` → ["a", "b", "c", 0]; None when it is not a reference."""
    path: list[str | int] = []
    while True:
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ExprError(msg("expr.name", "name {name} is not allowed", name=repr(node.attr)))
            path.append(node.attr)
            node = node.value
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if not (isinstance(key, ast.Constant) and isinstance(key.value, (int, str))):
                raise ExprError(msg("expr.subscript", "only [number] or ['key'] are allowed inside a reference"))
            if isinstance(key.value, str) and key.value.startswith("_"):
                raise ExprError(msg("expr.key", "key {key} is not allowed", key=repr(key.value)))
            path.append(key.value)
            node = node.value
        elif isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ExprError(msg("expr.name", "name {name} is not allowed", name=repr(node.id)))
            path.append(node.id)
            return path[::-1]
        else:
            return None


def parse(text: str) -> ast.Expression:
    text = (text or "").strip()
    if not text:
        raise ExprError(msg("expr.empty", "empty expression"))
    if len(text) > MAX_LEN:
        raise ExprError(msg("expr.too_long", "longer than {n} characters", n=MAX_LEN))
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        raise ExprError(msg("expr.syntax", "syntax error: {detail}", detail=e.msg)) from None
    _check(tree.body)
    return tree


def _check(node: ast.AST) -> None:
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _check(v)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.Not, ast.USub)):
            raise ExprError(msg("expr.unary", "only 'not' and '-' are allowed as unary operators"))
        _check(node.operand)
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, (ast.Add, ast.Sub)):
            raise ExprError(msg("expr.binary", "only + and - are allowed"))
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.Compare):
        if not all(isinstance(op, _CMP) for op in node.ops):
            raise ExprError(msg("expr.compare", "comparison not allowed"))
        _check(node.left)
        for c in node.comparators:
            _check(c)
    elif isinstance(node, ast.Call):
        if not (isinstance(node.func, ast.Name) and node.func.id in FUNCS) or node.keywords:
            raise ExprError(msg("expr.functions", "only these functions are allowed: {names}", names=", ".join(sorted(FUNCS))))
        for a in node.args:
            _check(a)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for e in node.elts:
            _check(e)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (str, int, float, bool)) and node.value is not None:
            raise ExprError(msg("expr.constant", "only text, numbers, true/false and null are allowed"))
    elif isinstance(node, ast.Name) and node.id in _CONST_NAMES:
        return
    elif isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        if _ref(node) is None:
            raise ExprError(msg("expr.reference", "invalid reference"))
    else:
        raise ExprError(msg("expr.node", "{kind} is not allowed in a condition", kind=type(node).__name__))


def refs(text: str) -> list[tuple[str, ...]]:
    """References used, as (root, name[, …]) tuples: ("input", "x"), ("triage", "severity")."""
    out: list[tuple[str, ...]] = []

    def walk(node: ast.AST) -> None:
        if isinstance(node, ast.Name) and node.id in _CONST_NAMES:
            return
        if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
            r = _ref(node)
            if r is not None:
                out.append(tuple(str(x) for x in r))
                return
        if isinstance(node, ast.Call):
            for a in node.args:
                walk(a)
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(parse(text).body)
    return out


def _lookup(scope: Mapping[str, Any], path: list[str | int]) -> Any:
    cur: Any = scope
    for key in path:
        in_map = isinstance(cur, Mapping) and key in cur
        in_list = isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur)
        if not (in_map or in_list):
            return MISSING
        cur = cur[key]
    return cur


def _eval(node: ast.AST, scope: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_truthy(_eval(v, scope)) for v in node.values)
        return any(_truthy(_eval(v, scope)) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, scope)
        if isinstance(node.op, ast.Not):
            return not _truthy(v)
        return -v if isinstance(v, (int, float)) and not isinstance(v, bool) else MISSING
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, scope), _eval(node.right, scope)
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b)):
            return MISSING
        return a + b if isinstance(node.op, ast.Add) else a - b
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, scope)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval(a, scope) for a in node.args]
        return _call(node.func.id, args)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e, scope) for e in node.elts]
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONST_NAMES:
        return _CONST_NAMES[node.id]
    path = _ref(node)
    assert path is not None
    return _lookup(scope, path)


def _truthy(v: Any) -> bool:
    return False if v is MISSING else bool(v)


def _compare(op: ast.cmpop, a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return isinstance(op, ast.NotEq) and (a is MISSING) != (b is MISSING)
    try:
        if isinstance(op, ast.Eq):
            return a == b
        if isinstance(op, ast.NotEq):
            return a != b
        if isinstance(op, ast.In):
            return a in b
        if isinstance(op, ast.NotIn):
            return a not in b
        if isinstance(op, ast.Lt):
            return a < b
        if isinstance(op, ast.LtE):
            return a <= b
        if isinstance(op, ast.Gt):
            return a > b
        if isinstance(op, ast.GtE):
            return a >= b
    except TypeError:
        return False
    return False


def _call(name: str, args: list[Any]) -> Any:
    if name == "exists":
        return len(args) == 1 and args[0] is not MISSING and args[0] is not None
    if len(args) == 1 and args[0] is MISSING:
        return MISSING
    try:
        if name == "len" and len(args) == 1:
            return len(args[0])
        if name == "lower" and len(args) == 1:
            return str(args[0]).lower()
        if name == "upper" and len(args) == 1:
            return str(args[0]).upper()
        if name == "contains" and len(args) == 2:
            return MISSING not in args and args[1] in args[0]
    except TypeError:
        return MISSING
    raise ExprError(msg("expr.arity", "{name}() got the wrong number of arguments", name=name))


def evaluate(text: str, scope: Mapping[str, Any]) -> bool:
    """scope: {"input": {...}, "<agent>": {outputs}, "<gate>": {"decision", "comment"}}."""
    return _truthy(_eval(parse(text).body, scope))
