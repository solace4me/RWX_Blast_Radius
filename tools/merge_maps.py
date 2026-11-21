"""Union the coverage maps (py3.10 + py3.13) and add the extra tables -> map.json.gz.
usage: merge_maps.py OUT map_raw.json map_raw13.json importlines.json importlines13.json
                     dialect_uses_dir tpcds_exec_lines.json tests.json [instantiations_dir]"""
import collections, glob, gzip, json, os, sys

out, a, b, il1, il2, usesdir, tpc, tests_json = sys.argv[1:9]
instdir = sys.argv[9] if len(sys.argv) > 9 else None
A, B = json.load(open(a)), json.load(open(b))
num = {t["id"]: t["number"] for t in json.load(open(tests_json))["tests"]}


def union(key):
    d = collections.defaultdict(set)
    for m in (A, B):
        for k, v in m[key].items():
            d[k].update(v)
    return d


lines = collections.defaultdict(lambda: collections.defaultdict(set))
for m in (A, B):
    for f, d in m["lines"].items():
        for l, ts in d.items():
            lines[f][int(l)].update(ts)


def ranges(ls):
    r = []
    for l in sorted(ls):
        if r and l == r[-1][1] + 1:
            r[-1][1] = l
        else:
            r.append([l, l])
    return r


il = collections.defaultdict(set)
for p in (il1, il2):
    for f, v in json.load(open(p)).items():
        il[f].update(v)
uses = collections.defaultdict(set)
for f in glob.glob(os.path.join(usesdir, "*.json")):
    for k, v in json.load(open(f)).items():
        if k in num:
            for n in v:
                uses[n].add(num[k])


def ctx_tests(c):
    """A test's own context -> itself; its setup/teardown -> its class; a
    module's collection -> every test in it."""
    if c in num:
        return {num[c]}
    if c.endswith("|setup") or c.endswith("|teardown"):
        parts = c.rsplit("|", 1)[0].split("::")
        prefix = "::".join(parts[:-1]) + "::" if len(parts) > 2 else parts[0] + "::"
        return {n for i, n in num.items() if i.startswith(prefix)}
    if c.startswith("collect:"):
        node = c[len("collect:"):]
        return {n for i, n in num.items() if i == node or i.startswith(node + "::")}
    return set()


inst = collections.defaultdict(set)  # class qualname -> tests that instantiated it (or a subclass)
if instdir:
    for f in glob.glob(os.path.join(instdir, "*.json")):
        for k, v in json.load(open(f)).items():
            ts = ctx_tests(k)
            for q in v:
                inst[q].update(ts)
cl = {}
for f, d in lines.items():
    g = {}
    for l, ts in d.items():
        g.setdefault(tuple(sorted(ts)), []).append(l)
    cl[f] = [[sorted(v), list(k)] for k, v in g.items()]
m = {
    "lines": cl,
    "opened": {k: sorted(v) for k, v in union("opened").items()},
    "mention": {k: sorted(v) for k, v in union("mention").items()},
    "children": B["children"],
    "bodies": B["bodies"],
    "importlines": {f: ranges(v) for f, v in il.items()},
    "uses": {k: sorted(v) for k, v in uses.items() if k != "dialect"},
    "tpcds_exec": {f: ranges(v) for f, v in json.load(open(tpc)).items() if v},
    "inst": {k: sorted(v) for k, v in inst.items()},
}
open(out, "wb").write(gzip.compress(json.dumps(m, separators=(",", ":")).encode(), 9))
print(os.path.getsize(out) // 1024, "KB")
