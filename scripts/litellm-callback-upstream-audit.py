#!/usr/bin/env python3
"""看 LiteLLM 升级会不会打坏我们的 callbacks：逐个 callback 找它引用 / monkey-patch 的上游符号，比两个 tag。

在 198 root 上跑（要 /root/litellm-fork 里已 fetch 两个 tag，读 tag 不 checkout）：

    python3 litellm-callback-upstream-audit.py --old v1.105.1 --new v1.106.0 --cb-dir <放 callbacks 的目录>

callbacks 目录一般直接从 live prod 的 callbacks CM 落出来（见 skill litellm-198-version-upgrade §0）。

输出每行一个符号：
  GONE      新版没有这个模块/符号（callback 会 ImportError 或 AttributeError，多半静默 skip）
  CHANGED   被 monkey-patch 的函数体在新版变了（我们的替换版会盖掉上游新改动，要逐个看 diff）
  same      两版函数体 AST 相同
只报 litellm.* 下的符号；补丁写法是 `Cls.meth = ...` / `setattr(Cls, "meth", ...)` / `module.func = ...`。
"""
import argparse
import ast
import hashlib
import os
import subprocess
import sys

FORK = "/root/litellm-fork"
_cache = {}


def show(tag, mod):
    key = (tag, mod)
    if key not in _cache:
        p = mod.replace(".", "/")
        src = None
        for cand in (p + ".py", p + "/__init__.py"):
            r = subprocess.run(["git", "-C", FORK, "show", f"{tag}:{cand}"], capture_output=True, text=True)
            if r.returncode == 0:
                src = r.stdout
                break
        _cache[key] = src
    return _cache[key]


def resolve(tag, mod, name):
    """返回 (模块源码, 名字在模块里的 AST 节点或 None)；模块不存在返回 (None, None)。"""
    src = show(tag, mod)
    if src is None:
        sub = show(tag, f"{mod}.{name}")  # from pkg import submodule
        return (sub, "MODULE") if sub is not None else (None, None)
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return src, n
        if isinstance(n, (ast.Assign, ast.AnnAssign)):
            tgts = n.targets if isinstance(n, ast.Assign) else [n.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in tgts):
                return src, n
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if any((a.asname or a.name.split(".")[-1]) == name for a in n.names):
                return src, n
    # 兜底：任意深度定义（条件定义 / try 里 import）
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name:
            return src, n
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[-1]) == name for a in n.names):
            return src, n
    return src, None


def body_hash(node):
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()[:10]


def member(cls_node, attr):
    for n in cls_node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == attr:
            return n
    return None


def scan_callback(path):
    """返回 imports: {本地名: (模块, 名字|None)} 和 patches: [(本地名, 属性)]。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    imports, patches = {}, set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("litellm") and n.level == 0:
            for a in n.names:
                if a.name != "*":
                    imports[a.asname or a.name] = (n.module, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("litellm"):
                    imports[a.asname or a.name.split(".")[0]] = (a.name, None)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    patches.add((t.value.id, t.attr))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr" and len(n.args) >= 2:
            o, a = n.args[0], n.args[1]
            if isinstance(o, ast.Name) and isinstance(a, ast.Constant) and isinstance(a.value, str):
                patches.add((o.id, a.value))
    return imports, sorted(patches)


def audit(cb_dir, old, new):
    rows = []
    for fn in sorted(os.listdir(cb_dir)):
        if not fn.endswith(".py"):
            continue
        imports, patches = scan_callback(os.path.join(cb_dir, fn))
        for local, (mod, name) in sorted(imports.items()):
            if name is None:
                if show(new, mod) is None and show(old, mod) is not None:
                    rows.append(("GONE", fn, mod))
                continue
            _, n_old = resolve(old, mod, name)
            _, n_new = resolve(new, mod, name)
            if n_old is not None and n_new is None:
                rows.append(("GONE", fn, f"{mod}:{name}"))
        for local, attr in patches:
            if local not in imports:
                continue
            mod, name = imports[local]
            if name is None:  # module.func = ...
                mod_o, mod_n = show(old, mod), show(new, mod)
                if mod_o is None or mod_n is None:
                    continue
                f_o = next((x for x in ast.parse(mod_o).body if getattr(x, "name", None) == attr), None)
                f_n = next((x for x in ast.parse(mod_n).body if getattr(x, "name", None) == attr), None)
            else:
                src_o, c_o = resolve(old, mod, name)
                src_n, c_n = resolve(new, mod, name)
                if c_o == "MODULE" or c_n == "MODULE":
                    sub = f"{mod}.{name}"
                    f_o = next((x for x in ast.parse(show(old, sub) or "").body if getattr(x, "name", None) == attr), None)
                    f_n = next((x for x in ast.parse(show(new, sub) or "").body if getattr(x, "name", None) == attr), None)
                elif isinstance(c_o, ast.ClassDef) and isinstance(c_n, ast.ClassDef):
                    f_o, f_n = member(c_o, attr), member(c_n, attr)
                else:
                    continue
            label = f"{name + '.' if name else mod + '.'}{attr}"
            if f_o is None and f_n is None:
                continue  # 我们新加的属性，不是替换上游
            if f_n is None:
                rows.append(("GONE", fn, label))
            elif f_o is None:
                rows.append(("NEW-UPSTREAM", fn, label))
            elif body_hash(f_o) != body_hash(f_n):
                rows.append(("CHANGED", fn, label))
            else:
                rows.append(("same", fn, label))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--cb-dir", required=True)
    a = ap.parse_args()
    rows = audit(a.cb_dir, a.old, a.new)
    order = {"GONE": 0, "CHANGED": 1, "NEW-UPSTREAM": 2, "same": 3}
    for st, fn, sym in sorted(rows, key=lambda r: (order[r[0]], r[1], r[2])):
        print(f"{st:13} {fn:40} {sym}")
    c = {k: sum(1 for r in rows if r[0] == k) for k in order}
    print(f"\nGONE={c['GONE']} CHANGED={c['CHANGED']} NEW-UPSTREAM={c['NEW-UPSTREAM']} same={c['same']}")
    return 1 if c["GONE"] else 0


if __name__ == "__main__":
    sys.exit(main())
