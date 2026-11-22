"""Per-test operand observations for isolated comparison edits on pinned sources."""

import ast
from dataclasses import dataclass
from enum import Enum, EnumType
import gzip
import hashlib
import json
import math
import operator

from setting_index import catalog_digest


OPERATORS = {
    ast.Eq: "eq", ast.NotEq: "ne", ast.Is: "is", ast.IsNot: "is_not",
    ast.Lt: "lt", ast.LtE: "le", ast.Gt: "gt", ast.GtE: "ge",
}
RELATIONS = {name: getattr(operator, name) for name in ("eq", "ne", "lt", "le", "gt", "ge")}
IDENTITY_OPS = {"is", "is_not"}
BUILTINS = {"none", "bool", "int", "float", "str", "tuple"}
RICH_METHODS = {f"__{name}__" for name in RELATIONS}
VALUE_ENUMS = {"intenum", "strenum"}


def source_digest(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def builtin_fact(value, depth=0):
    """Snapshot exact, immutable builtin values without calling custom methods."""
    cls = type(value)
    if value is None:
        return ("none",)
    if cls is bool:
        return "bool", value
    if cls is int and value.bit_length() <= 128:
        return "int", value
    if cls is float and math.isfinite(value):
        return "float", value
    if cls is str and len(value) <= 256:
        return "str", value
    if cls is tuple and depth < 3 and len(value) <= 8:
        items = tuple(builtin_fact(item, depth + 1) for item in value)
        if None not in items:
            return "tuple", items
    return None


def _method(mro, name):
    for cls in mro:
        attrs = type.__getattribute__(cls, "__dict__")
        if name in attrs:
            return attrs[name]
    return None


def identity_fact(value):
    cls = type(value)
    meta = type(cls)
    if meta is not type and meta is not EnumType:
        return None
    mro = type.__getattribute__(cls, "__mro__")
    if meta is EnumType and issubclass(cls, (int, str)):
        primitive = int if issubclass(cls, int) else str
        for base in mro:
            if base is primitive:
                break
            attrs = type.__getattribute__(base, "__dict__")
            if any(method in attrs for method in RICH_METHODS):
                return None
        # Use the builtin descriptor directly so inspection invokes no member code.
        fields = Enum.__dict__["__dict__"].__get__(value)
        name = fields.get("_name_")
        members = type.__getattribute__(cls, "__dict__").get("_member_map_")
        if type(name) is str and type(members) is dict and members.get(name) is value:
            if primitive is int:
                return "intenum", int.__int__(value), name
            return "strenum", str.__str__(value), name
        return None
    if _method(mro, "__eq__") is object.__eq__ and _method(mro, "__ne__") is object.__ne__:
        return ("identity",)
    return None


def operand_fact(value):
    return builtin_fact(value) or identity_fact(value)


def compare_site(node):
    return node.lineno, node.col_offset, node.end_lineno, node.end_col_offset


@dataclass(frozen=True)
class ComparisonEdit:
    site: tuple
    old_op: str
    new_op: str
    replacements: tuple


def _literal(node):
    try:
        return builtin_fact(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError):
        return None


def _single_comparison(old, new):
    if len(old.ops) != 1 or len(new.ops) != 1:
        return None
    old_op = OPERATORS.get(type(old.ops[0]))
    new_op = OPERATORS.get(type(new.ops[0]))
    if old_op is None or new_op is None:
        return None
    replacements = []
    for before, after in zip((old.left, *old.comparators), (new.left, *new.comparators)):
        if ast.dump(before) == ast.dump(after):
            if new_op in IDENTITY_OPS and old_op != new_op:
                literal = _literal(after)
                if literal is not None and literal[0] not in {"none", "bool"}:
                    return None
            replacements.append(None)
            continue
        a, b = _literal(before), _literal(after)
        if a is None or b is None:
            return None
        if (old_op in IDENTITY_OPS and a[0] not in {"none", "bool"}) or (
            new_op in IDENTITY_OPS and b[0] not in {"none", "bool"}
        ):
            return None
        replacements.append(b)
    if old_op == new_op and all(replacement is None for replacement in replacements):
        return None
    return ComparisonEdit(compare_site(old), old_op, new_op, tuple(replacements))


def comparison_edit(before, after):
    """Identify one operator or immutable literal edit within a single comparison."""
    edits = []

    def matches(old, new):
        if type(old) is not type(new):
            return False
        if isinstance(old, ast.Compare):
            edit = _single_comparison(old, new)
            if edit is not None:
                edits.append(edit)
                return len(edits) == 1
        if isinstance(old, ast.AST):
            return all(matches(value, getattr(new, field)) for field, value in ast.iter_fields(old))
        if isinstance(old, list):
            return len(old) == len(new) and all(matches(a, b) for a, b in zip(old, new))
        return old == new

    try:
        if matches(ast.parse(before, type_comments=True), ast.parse(after, type_comments=True)) and edits:
            return edits[0]
    except (RecursionError, SyntaxError, ValueError):
        pass
    return None


def _value(fact):
    kind = fact[0]
    if kind == "none":
        return None
    if kind == "tuple":
        return tuple(_value(item) for item in fact[1])
    return fact[1]


def _identity(left, right, same, same_type):
    a, b = left[0], right[0]
    if a != b:
        return False
    if a == "none":
        return True
    if a == "bool":
        return left[1] == right[1]
    if a == "identity":
        return same
    if a in VALUE_ENUMS:
        return same_type and left[2] == right[2]
    if _value(left) != _value(right):
        return False
    # Equal builtin values may come from distinct objects in a fresh test process.
    return None


def _outcome(op, left, right, same, same_type):
    if left is None or right is None:
        return None
    a, b = left[0], right[0]
    if op in IDENTITY_OPS:
        result = _identity(left, right, same, same_type)
        return None if result is None else result == (op == "is")
    if (a in BUILTINS and b in BUILTINS) or (a == b and a in VALUE_ENUMS and same_type):
        try:
            return RELATIONS[op](_value(left), _value(right))
        except TypeError:
            return None
    if op in {"eq", "ne"} and (
        a == b == "identity" or ("none" in {a, b} and ({a, b} & (VALUE_ENUMS | {"identity"})))
    ):
        return _identity(left, right, same, same_type) == (op == "eq")
    return None


def _equivalent(edit, facts):
    left, right, same, same_type = facts
    old = _outcome(edit.old_op, left, right, same, same_type)
    if old is None:
        return False
    new_left, new_right = edit.replacements
    if new_left is not None or new_right is not None:
        same = same_type = False
    new = _outcome(edit.new_op, new_left or left, new_right or right, same, same_type)
    return old is new


def _changed_line_range(before, after):
    left, right = before.splitlines(), after.splitlines()
    first = next((i for i, (a, b) in enumerate(zip(left, right)) if a != b), min(len(left), len(right)))
    tail = next((i for i, (a, b) in enumerate(zip(reversed(left[first:]), reversed(right[first:])))
                 if a != b), min(len(left), len(right)) - first)
    return first + 1, len(left) - tail


class ComparisonIndex:
    def __init__(self, numbers, source, files):
        self.numbers = numbers
        self.source = source
        self.files = files

    @classmethod
    def load(cls, path, tests):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        numbers = {test["id"]: test["number"] for test in tests}
        if data["version"] != 1 or data["catalog"] != catalog_digest(numbers):
            return None
        files = {file: {tuple(site): groups for site, groups in sites}
                 for file, sites in data["files"].items()}
        return cls([numbers[nodeid] for nodeid in sorted(numbers)], data["source"], files)

    def narrow(self, file, before, after, chosen):
        sites = self.files.get(file)
        if not sites or before is None or after is None or self.source.get(file) != source_digest(before):
            return None
        first, last = _changed_line_range(before, after)
        if last < first or not any(start <= first <= last <= end for start, _, end, _ in sites):
            return None
        edit = comparison_edit(before, after)
        if edit is None or edit.site not in sites:
            return None
        observed, unsafe = set(), set()
        try:
            for facts, indices in sites[edit.site]:
                if not all(type(index) is int and 0 <= index < len(self.numbers) for index in indices):
                    return None
                tests = {self.numbers[index] for index in indices} & chosen
                observed.update(tests)
                if tests and not _equivalent(edit, facts):
                    unsafe.update(tests)
        except (IndexError, KeyError, TypeError, ValueError):
            return None
        return chosen - (observed - unsafe)
