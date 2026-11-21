"""Narrow a single value edit under one key of a literal dict setting in a
dialect class (nested classes included), using reads recorded on the pinned
suite by tools/record_dictkeys.py."""

import ast
import gzip
import hashlib
import json

FAMILIES = ("dialects", "parsers", "generators", "tokenizers")


def _immutable(v):
    return type(v) in (bool, int, str, float, type(None)) or (
        type(v) is tuple and all(_immutable(x) for x in v))


def _literal_dict(node):
    if not isinstance(node, ast.Dict) or not node.keys or None in node.keys:
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return None
    if type(value) is dict and len(value) == len(node.keys) and all(
            type(k) is str and _immutable(v) for k, v in value.items()):
        return value
    return None


def _settings(tree):
    """(qualname, attr) -> (assign statement, value) for class-level literal dicts;
    the same rule the recorder uses to decide what it wraps."""
    found = {}

    def walk(cls, stack):
        stack = stack + [cls.name]
        for st in cls.body:
            if isinstance(st, ast.ClassDef):
                walk(st, stack)
            elif (isinstance(st, ast.Assign) and len(st.targets) == 1
                  and isinstance(st.targets[0], ast.Name) and st.targets[0].id.isupper()):
                value = _literal_dict(st.value)
                if value is not None:
                    key = (".".join(stack), st.targets[0].id)
                    found[key] = None if key in found else (st, value)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            walk(node, [])
    return {k: v for k, v in found.items() if v}


def edited_dict_value(before, after):
    """(qualname, attr, key) if the only change is one value under one existing key."""
    try:
        old, new = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return None
    a, b = _settings(old), _settings(new)
    if a.keys() != b.keys():
        return None
    changed = [k for k in a if ast.dump(a[k][0].value) != ast.dump(b[k][0].value)]
    if len(changed) != 1:
        return None
    ident = changed[0]
    (sa, va), (sb, vb) = a[ident], b[ident]
    if list(va) != list(vb):
        return None
    keys = [k for k, na, nb in zip(va, sa.value.values, sb.value.values) if ast.dump(na) != ast.dump(nb)]
    if len(keys) != 1:
        return None
    sa.value = sb.value = ast.Constant(None)
    if ast.dump(old) != ast.dump(new):
        return None
    return ident[0], ident[1], keys[0]


class DictKeyIndex:
    def __init__(self, data, numbers):
        self.verified = {numbers[t] for t in data["verified"] if t in numbers}
        self.source = data["source"]
        self.settings = {}
        for ident, e in data["settings"].items():
            self.settings[ident] = (
                {k: {numbers[t] for t in v if t in numbers} for k, v in e["keys"].items()},
                {numbers[t] for t in e["whole"] if t in numbers},
                set(e["blocked"]),
            )

    @classmethod
    def load(cls, path, tests):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
        numbers = {t["id"]: t["number"] for t in tests}
        catalog = hashlib.sha256("\n".join(sorted(numbers)).encode()).hexdigest()
        if data.get("version") != 1 or data.get("catalog") != catalog:
            return None
        return cls(data, numbers)

    def narrow(self, file, before, after, chosen):
        """Tests to keep, or None when the edit isn't one we have evidence for."""
        if self.source.get(file) != hashlib.sha256(before.encode("utf-8")).hexdigest():
            return None
        edit = edited_dict_value(before, after)
        if edit is None:
            return None
        qualname, attr, key = edit
        entry = self.settings.get(f"{file}:{qualname}:{attr}")
        if entry is None:
            return None
        keys, whole, blocked = entry
        if "*" in blocked or repr(key) in blocked:
            return None
        affected = whole | keys.get(repr(key), set())
        if not affected <= chosen:
            return None
        return (chosen - self.verified) | affected
