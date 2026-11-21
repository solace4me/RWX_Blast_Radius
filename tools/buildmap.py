import json, os, sys, glob, coverage, collections
ROOT=os.path.expanduser('~/sqlglot'); KIT=os.path.expanduser('~/mnt/RWX_Blast_Radius')
tests=json.load(open(f'{KIT}/sample/tests.json'))['tests']
num={t['id']:t['number'] for t in tests}
lines=collections.defaultdict(lambda: collections.defaultdict(set))  # file->line->tests
unmatched=set(); opened=collections.defaultdict(set)
for d in glob.glob(os.path.expanduser(os.environ.get('BR_GLOB','~/brout_*'))):
    data=coverage.CoverageData(basename=os.path.join(d,'.coverage')); data.read()
    for f in data.measured_files():
        rel=os.path.relpath(f,ROOT)
        for ln,ctxs in data.contexts_by_lineno(f).items():
            for c in ctxs:
                if c in num: lines[rel][ln].add(num[c])
                elif c=='' : lines[rel][ln].add(-1)
                else: unmatched.add(c)
    op=os.path.join(d,'opened.json')
    if os.path.exists(op):
        for k,v in json.load(open(op)).items():
            if k in num:
                for p in v: opened[p].add(num[k])
            else: unmatched.add(k)
print('files',len(lines),'unmatched ctx',len(unmatched),list(unmatched)[:5])
covered={n for f in lines.values() for s in f.values() for n in s}
print('tests with coverage',len(covered-{-1}),'of',len(tests))
missing=[t['id'] for t in tests if t['number'] not in covered]
print('no-coverage tests',len(missing),missing[:10])
out={'lines':{f:{str(l):sorted(s) for l,s in d.items()} for f,d in lines.items()},
     'opened':{p:sorted(s) for p,s in opened.items()}}
json.dump(out,open(os.path.expanduser(os.environ.get('BR_MAPOUT','~/map_raw.json')),'w'))
print('opened files',len(opened)); print(sorted(opened)[:15])

# ---- dialect mention index & inheritance (for import-time changes) ----
import ast, re
names=sorted(p[:-3] for p in os.listdir(f'{ROOT}/sqlglot/dialects') if p.endswith('.py') and p not in ('__init__.py','dialect.py'))
fams=['dialects','parsers','generators','typing','tokenizers']
refs=collections.defaultdict(set)  # parent -> children dialect names
for fam in fams:
    d=f'{ROOT}/sqlglot/{fam}'
    if not os.path.isdir(d): continue
    for p in os.listdir(d):
        if not p.endswith('.py'): continue
        child=p[:-3]; src=open(os.path.join(d,p)).read()
        for m in re.finditer(r'sqlglot\.(?:%s)\.(\w+)'%'|'.join(fams), src):
            if m.group(1) in names and m.group(1)!=child: refs[m.group(1)].add(child)
mention=collections.defaultdict(set)
pats={n:re.compile(r'\b%s\b'%n, re.I) for n in names}
for t in tests:
    pass
src_cache={}
for t in tests:
    f=t['file']; parts=t['id'].split('::')
    if f not in src_cache:
        s=open(os.path.join(ROOT,f)).read(); tree=ast.parse(s); idx={}
        for node in ast.walk(tree):
            if isinstance(node,ast.ClassDef):
                for fn in node.body:
                    if isinstance(fn,(ast.FunctionDef,ast.AsyncFunctionDef)):
                        idx[(node.name,fn.name)]=ast.get_source_segment(s,fn) or ''
            elif isinstance(node,ast.FunctionDef):
                idx.setdefault((None,node.name),ast.get_source_segment(s,node) or '')
        src_cache[f]=idx
    idx=src_cache[f]
    key=(parts[1],parts[2].split('[')[0]) if len(parts)>=3 else (None,parts[-1].split('[')[0])
    body=idx.get(key,'')
    for n,p in pats.items():
        if p.search(body) or f.endswith(f'test_{n}.py'): mention[n].add(t['number'])
for fx,ts in out['opened'].items():
    try: body=open(os.path.join(ROOT,fx),errors='ignore').read()
    except Exception: continue
    for n,p in pats.items():
        if p.search(body): mention[n].update(ts)
out['mention']={n:sorted(s) for n,s in mention.items()}
out['children']={n:sorted(s) for n,s in refs.items()}
json.dump(out,open(os.path.expanduser(os.environ.get('BR_MAPOUT','~/map_raw.json')),'w'))
print('children',{k:v for k,v in out['children'].items()})
print('mention sizes',{n:len(s) for n,s in mention.items()})

# ---- function-body line ranges: lines outside them run at import time ----
bodies={}
for dp,dn,fn in os.walk(f'{ROOT}/sqlglot'):
    for p in fn:
        if not p.endswith('.py'): continue
        full=os.path.join(dp,p); rel=os.path.relpath(full,ROOT)
        try: tree=ast.parse(open(full).read())
        except SyntaxError: continue
        rs=[]
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.Lambda)) and not isinstance(node,ast.Lambda):
                rs.append([node.body[0].lineno, node.end_lineno])
        rs.sort(); merged=[]
        for a,b in rs:
            if merged and a<=merged[-1][1]+0: merged[-1][1]=max(merged[-1][1],b)
            else: merged.append([a,b])
        bodies[rel]=merged
out['bodies']=bodies
json.dump(out,open(os.path.expanduser(os.environ.get('BR_MAPOUT','~/map_raw.json')),'w'))
print('bodies files',len(bodies))
