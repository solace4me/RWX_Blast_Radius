"""Select readers of a changed expression argument requirement on pinned SQLGlot."""

import ast
from collections import Counter
import gzip
import hashlib
import json

from setting_index import catalog_digest


def arg_type_declarations(tree):
    """Find unambiguous class declarations with literal string-to-bool metadata."""
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    names = Counter(node.name for node in classes)
    declarations = {}
    for cls in classes:
        if names[cls.name] != 1 or cls.decorator_list or cls.keywords:
            continue
        assignments = [
            node for node in ast.walk(cls)
            if isinstance(node, ast.Name) and node.id == "arg_types"
        ]
        if len(assignments) != 1:
            continue
        for statement in cls.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
            elif isinstance(statement, ast.AnnAssign):
                target = statement.target
            else:
                continue
            value = statement.value
            if target is not assignments[0] or not isinstance(value, ast.Dict) or not value.keys:
                continue
            if not all(isinstance(key, ast.Constant) and type(key.value) is str for key in value.keys):
                continue
            if len({key.value for key in value.keys}) != len(value.keys):
                continue
            if all(isinstance(flag, ast.Constant) and type(flag.value) is bool for flag in value.values):
                declarations[cls.name] = value
    return declarations


def edited_arg_type(before, after):
    """Return the declaring class and key for a single boolean replacement."""
    try:
        old_tree = ast.parse(before, type_comments=True)
        new_tree = ast.parse(after, type_comments=True)
    except SyntaxError:
        return None
    old = arg_type_declarations(old_tree)
    new = arg_type_declarations(new_tree)
    change = None
    for name in old.keys() & new.keys():
        old_dict, new_dict = old[name], new[name]
        if [key.value for key in old_dict.keys] != [key.value for key in new_dict.keys]:
            continue
        for key, old_flag, new_flag in zip(old_dict.keys, old_dict.values, new_dict.values):
            if old_flag.value == new_flag.value:
                continue
            if change is not None:
                return None
            change = (name, key.value)
            new_flag.value = old_flag.value
    if change is not None and ast.dump(old_tree) == ast.dump(new_tree):
        return change
    return None


class ExprValidationIndex:
    def __init__(self, verified, files, source):
        self.verified = verified
        self.files = files
        self.source = source

    @classmethod
    def load(cls, path, tests):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            data = json.load(stream)
        numbers = {test["id"]: test["number"] for test in tests}
        if data["version"] != 1 or data["catalog"] != catalog_digest(numbers):
            return None

        def ids(nodeids):
            return {numbers[nodeid] for nodeid in nodeids}

        files = {
            file: {
                name: (ids(reads["whole"]), {
                    key: ids(readers) if readers is not None else None
                    for key, readers in reads["keys"].items()
                })
                for name, reads in owners.items()
            }
            for file, owners in data["files"].items()
        }
        return cls(ids(data["verified"]), files, data["source"])

    def narrow(self, file, before, after, chosen):
        owners = self.files.get(file)
        if owners is None or self.source.get(file) != hashlib.sha256(before.encode("utf-8")).hexdigest():
            return None
        change = edited_arg_type(before, after)
        if change is None or change[0] not in owners:
            return None
        owner, key = change
        whole, keys = owners[owner]
        readers = keys.get(key, ())
        if readers is None:
            return None
        return (chosen - self.verified) | whole.union(readers)
