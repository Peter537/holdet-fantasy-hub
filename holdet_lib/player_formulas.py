"""A small, explicit formula language for calculated player columns.

The module intentionally never calls ``eval`` or ``compile``.  Expressions are
parsed with :mod:`ast`, validated against a closed grammar and interpreted by a
private tree walker.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from math import isfinite
from typing import Mapping


MAX_FORMULA_LENGTH = 500
MAX_FORMULA_NODES = 100
MAX_FORMULA_DEPTH = 12
PLAYER_FORMULA_METRICS = frozenset(
    {
        "value",
        "total_growth",
        "round_growth",
        "popularity",
        "popularity_change",
        "trend",
        "index",
        "form_3",
        "form_5",
        "stability",
        "growth_per_million",
        "potential",
        "risk",
        "is_active",
        "is_disabled",
        "is_injured",
        "has_suspension",
    }
)
_FUNCTIONS = frozenset(
    {"abs", "min", "max", "round", "coalesce", "clamp", "ifelse"}
)
_MISSING = object()


class FormulaError(ValueError):
    """Raised for an unsafe, invalid or non-evaluable player formula."""


@dataclass(frozen=True, slots=True)
class ComputedPlayerColumn:
    game_locale: str
    game_slug: str
    column_id: str
    name: str
    expression: str
    decimals: int = 2

    def __post_init__(self) -> None:
        name = " ".join(self.name.split())
        if not self.game_locale.strip() or not self.game_slug.strip():
            raise ValueError("En beregnet kolonne kræver spilidentitet")
        if not self.column_id.strip() or not name:
            raise ValueError("En beregnet kolonne kræver id og navn")
        if len(self.column_id) > 64 or len(name) > 80:
            raise ValueError("Kolonne-id eller -navn er for langt")
        if not 0 <= self.decimals <= 8:
            raise ValueError("Antal decimaler skal være mellem 0 og 8")
        object.__setattr__(self, "game_locale", self.game_locale.casefold())
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "expression", self.expression.strip())
        validate_player_formula(self.expression)


@dataclass(frozen=True, slots=True)
class FormulaResult:
    value: float | bool | None
    error: str | None = None


def _tree_depth(node: ast.AST) -> int:
    children = tuple(ast.iter_child_nodes(node))
    return 1 if not children else 1 + max(_tree_depth(child) for child in children)


def _parse(expression: str) -> ast.Expression:
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("Formlen skal være udfyldt")
    if len(expression) > MAX_FORMULA_LENGTH:
        raise FormulaError("Formlen må højst være 500 tegn")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise FormulaError("Formlen har ugyldig syntaks") from exc
    nodes = tuple(ast.walk(tree))
    if len(nodes) > MAX_FORMULA_NODES:
        raise FormulaError("Formlen må højst indeholde 100 led")
    if _tree_depth(tree) > MAX_FORMULA_DEPTH:
        raise FormulaError("Formlen må højst have dybde 12")
    return tree


def validate_player_formula(
    expression: str,
    *,
    allowed_names: frozenset[str] = PLAYER_FORMULA_METRICS,
) -> None:
    """Validate an expression against the closed player-formula grammar."""

    tree = _parse(expression)
    allowed_nodes = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.Call,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.UAdd,
        ast.USub,
        ast.Not,
        ast.And,
        ast.Or,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise FormulaError(
                f"Ikke-tilladt formelled: {type(node).__name__}"
            )
        if isinstance(node, ast.Constant) and (
            not isinstance(node.value, (int, float))
            or isinstance(node.value, bool)
        ):
            raise FormulaError("Kun numeriske konstanter er tilladt")
        if isinstance(node, ast.Name) and node.id not in allowed_names | _FUNCTIONS:
            raise FormulaError(f"Ukendt spillerfelt: {node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
                raise FormulaError("Kun de dokumenterede formelfunktioner er tilladt")
            if node.keywords:
                raise FormulaError("Navngivne funktionsargumenter er ikke tilladt")


def _number(value: object) -> float:
    if value is _MISSING or value is None:
        raise FormulaError("Et nødvendigt input mangler")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FormulaError("Et input er ikke numerisk")
    result = float(value)
    if not isfinite(result):
        raise FormulaError("Et input eller resultat er ikke endeligt")
    return result


class _Evaluator:
    def __init__(self, values: Mapping[str, float | int | bool | None]) -> None:
        self.values = values

    def visit(self, node: ast.AST) -> object:
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise FormulaError(f"Ikke-tilladt formelled: {type(node).__name__}")
        return method(node)

    def visit_Expression(self, node: ast.Expression) -> object:
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant) -> object:
        return _number(node.value)

    def visit_Name(self, node: ast.Name) -> object:
        return self.values.get(node.id, _MISSING)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> object:
        if isinstance(node.op, ast.Not):
            return not bool(self._required(self.visit(node.operand)))
        value = _number(self.visit(node.operand))
        return value if isinstance(node.op, ast.UAdd) else -value

    def visit_BinOp(self, node: ast.BinOp) -> float:
        left = _number(self.visit(node.left))
        right = _number(self.visit(node.right))
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        elif isinstance(node.op, ast.Div):
            if right == 0:
                raise FormulaError("Division med nul")
            result = left / right
        elif isinstance(node.op, ast.Mod):
            if right == 0:
                raise FormulaError("Modulo med nul")
            result = left % right
        else:
            raise FormulaError("Operatoren er ikke tilladt")
        if not isfinite(result):
            raise FormulaError("Resultatet er ikke endeligt")
        return result

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        values = [bool(self._required(self.visit(item))) for item in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self._required(self.visit(node.left))
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = self._required(self.visit(comparator))
            if isinstance(operator, ast.Eq):
                matched = left == right
            elif isinstance(operator, ast.NotEq):
                matched = left != right
            else:
                left_number, right_number = _number(left), _number(right)
                if isinstance(operator, ast.Lt):
                    matched = left_number < right_number
                elif isinstance(operator, ast.LtE):
                    matched = left_number <= right_number
                elif isinstance(operator, ast.Gt):
                    matched = left_number > right_number
                else:
                    matched = left_number >= right_number
            if not matched:
                return False
            left = right
        return True

    def visit_Call(self, node: ast.Call) -> object:
        assert isinstance(node.func, ast.Name)
        name = node.func.id
        if name == "ifelse":
            if len(node.args) != 3:
                raise FormulaError("ifelse kræver tre argumenter")
            condition = bool(self._required(self.visit(node.args[0])))
            return self.visit(node.args[1] if condition else node.args[2])
        if name == "coalesce":
            if not node.args:
                raise FormulaError("coalesce kræver mindst ét argument")
            for argument in node.args:
                value = self.visit(argument)
                if value is not _MISSING and value is not None:
                    return value
            raise FormulaError("Alle coalesce-input mangler")
        arguments = [self.visit(argument) for argument in node.args]
        if name == "abs" and len(arguments) == 1:
            return abs(_number(arguments[0]))
        if name in {"min", "max"} and arguments:
            numbers = [_number(value) for value in arguments]
            return min(numbers) if name == "min" else max(numbers)
        if name == "round" and len(arguments) in {1, 2}:
            digits = 0 if len(arguments) == 1 else int(_number(arguments[1]))
            if not -8 <= digits <= 8:
                raise FormulaError("round understøtter -8 til 8 decimaler")
            return float(round(_number(arguments[0]), digits))
        if name == "clamp" and len(arguments) == 3:
            value, low, high = (_number(item) for item in arguments)
            if low > high:
                raise FormulaError("clamp kræver minimum før maksimum")
            return min(max(value, low), high)
        raise FormulaError(f"Ugyldige argumenter til {name}")

    @staticmethod
    def _required(value: object) -> object:
        if value is _MISSING or value is None:
            raise FormulaError("Et nødvendigt input mangler")
        return value


def evaluate_player_formula(
    expression: str,
    values: Mapping[str, float | int | bool | None],
    *,
    allowed_names: frozenset[str] | None = None,
) -> FormulaResult:
    """Evaluate safely; data errors become a blank-cell result with a reason."""

    names = allowed_names or PLAYER_FORMULA_METRICS
    try:
        validate_player_formula(expression, allowed_names=names)
        value = _Evaluator(values).visit(_parse(expression))
        if isinstance(value, bool):
            return FormulaResult(value)
        return FormulaResult(_number(value))
    except FormulaError as exc:
        return FormulaResult(None, str(exc))
