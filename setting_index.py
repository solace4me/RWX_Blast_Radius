"""Narrow literal dialect setting edits with reads recorded on the pinned source."""

import ast
import gzip
import hashlib
import json


def catalog_digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def _immutable(value):
    return type(value) in (bool, int, str, float, type(None)) or (
        type(value) is tuple and all(_immutable(item) for item in value)
    )


def setting_declarations(tree):
    """Find literal settings defined once in an ordinary, top-level class body."""
    declarations = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.decorator_list or cls.keywords:
            continue
        local_names = {}
        for node in ast.walk(cls):
            if isinstance(node, ast.Name):
                local_names[node.id] = local_names.get(node.id, 0) + 1
        for statement in cls.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
            elif isinstance(statement, ast.AnnAssign):
                target = statement.target
            else:
                continue
            if (
                not isinstance(target, ast.Name)
                or not target.id.isupper()
                or target.id.startswith("_")
                or local_names[target.id] != 1
                or statement.value is None
            ):
                continue
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(statement.value, ast.Dict):
                if not (
                    type(value) is dict
                    and len(value) == len(statement.value.keys)
                    and value
                    and all(type(k) is str and _immutable(v) for k, v in value.items())
                ):
                    continue
            elif not _immutable(value):
                continue
            identity = (cls.name, target.id)
            if identity in declarations:
                # A repeated class definition makes the binding ambiguous.
                declarations[identity] = None
            else:
                declarations[identity] = (statement, value)
    return {identity: declaration for identity, declaration in declarations.items() if declaration}


def edited_setting(before, after):
    """Return the class, setting and optional key for one literal replacement."""
    try:
        old = ast.parse(before, type_comments=True)
        new = ast.parse(after, type_comments=True)
    except SyntaxError:
        return None
    previous = setting_declarations(old)
    revised = setting_declarations(new)
    changed = []
    for identity, (statement, old_value) in previous.items():
        if identity not in revised:
            continue
        other, new_value = revised[identity]
        if ast.dump(statement.value) != ast.dump(other.value):
            changed.append((identity, statement, old_value, other, new_value))
    if len(changed) != 1:
        return None
    (cls, attr), statement, old_value, other, new_value = changed[0]
    if type(old_value) is not type(new_value):
        return None
    key = None
    if type(old_value) is dict:
        if list(old_value) != list(new_value):
            return None
        old_nodes = statement.value.values
        new_nodes = other.value.values
        keys = [
            name for name, a, b in zip(old_value, old_nodes, new_nodes)
            if ast.dump(a) != ast.dump(b)
        ]
        if len(keys) != 1 or type(old_value[keys[0]]) is not type(new_value[keys[0]]):
            return None
        key = keys[0]
    statement.value = other.value = ast.Constant(None)
    if ast.dump(old) != ast.dump(new):
        return None
    return cls, attr, key


class SettingIndex:
    def __init__(self, verified, settings, source_hashes=None):
        self.verified = verified
        self.settings = settings
        self.source_hashes = source_hashes or {}

    @classmethod
    def load(cls, path, tests):
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            data = json.load(handle)
        numbers = {test["id"]: test["number"] for test in tests}
        if data["version"] != 1 or data["catalog"] != catalog_digest(numbers):
            return None

        def ids(nodeids):
            return {numbers[nodeid] for nodeid in nodeids}

        verified = ids(data["verified"])
        settings = {}
        for file, classes in data["files"].items():
            settings[file] = {
                (name, attr): (ids(reads["whole"]), {key: ids(v) for key, v in reads["keys"].items()})
                for name, attrs in classes.items()
                for attr, reads in attrs.items()
            }
        return cls(verified, settings, data["source"])

    def matches_source(self, file, text):
        return (
            file in self.settings
            and self.source_hashes.get(file) == hashlib.sha256(text.encode("utf-8")).hexdigest()
        )

    def narrow(self, file, change, chosen):
        if change is None:
            return None
        cls, attr, key = change
        reads = self.settings.get(file, {}).get((cls, attr))
        if reads is None:
            return None
        whole, keys = reads
        if key is None:
            affected = whole.union(*keys.values())
        else:
            affected = whole | keys.get(key, set())
        if not affected <= chosen:
            return None
        return (chosen - self.verified) | affected
