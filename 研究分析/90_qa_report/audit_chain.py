# -*- coding: utf-8 -*-

import argparse
import ast
import collections
import hashlib
import io
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile

ROOT = os.environ["BC_CLINIC_ROOT"]
SRC = ROOT + "/结构化处理脚本"
PUB = ROOT + "/结构化处理脚本"  # 源层与发布层已合一（01_流水线脚本 已删除），自审计
FINAL_TAR = ROOT + "/publish/02_结构化结果与过程/中间产物/_packed/assembled_v7_final.tar"

RE_STR = re.compile(r'"(/[^"\n]*?)"')

def _ev(node, env):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, r = _ev(node.left, env), _ev(node.right, env)
        if isinstance(l, str) and isinstance(r, str):
            return l + r
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "join":
        parts = [_ev(x, env) for x in node.args]
        if parts and all(isinstance(p, str) for p in parts):
            return os.path.join(*parts)
    return None

def path_consts(tree):
    env, changed = {}, True
    while changed:
        changed = False
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                v = _ev(node.value, env)
                if v is not None and env.get(node.targets[0].id) != v:
                    env[node.targets[0].id] = v
                    changed = True
    return {k: v for k, v in env.items() if v.startswith(ROOT)}

def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()

def list_py(base):
    out = []
    for d, _, fs in os.walk(base):
        for f in fs:
            if f.endswith(".py"):
                out.append(os.path.join(d, f))
    return sorted(out)

def rel(base, p):
    return os.path.relpath(p, base)

def _cls(path):
    if "/_packed/" in path and path.endswith(".tar"):
        return "可重建中间件"
    if "/中间产物/" in path:
        return "已删目录(tar化遗留)"
    return "其他"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", default=ROOT + "/publish/02_结构化结果与过程/qa_reports/链条审计.json")
    a = ap.parse_args()

    src, pub = list_py(SRC), list_py(PUB)
    src_names = {rel(SRC, p): p for p in src}
    pub_names = {rel(PUB, p): p for p in pub}

    # ---------- ① 完整性 ----------
    src_base = {os.path.basename(k): v for k, v in src_names.items()}
    pub_base = {os.path.basename(k): v for k, v in pub_names.items()}
    only_src = sorted(set(src_base) - set(pub_base))
    only_pub = sorted(set(pub_base) - set(src_base))

    # ---------- ② 镜像一致性 ----------
    mirror = []
    for name in sorted(set(src_base) & set(pub_base)):
        a1, b1 = md5(src_base[name]), md5(pub_base[name])
        mirror.append({"name": name, "src": rel(SRC, src_base[name]),
                       "pub": rel(PUB, pub_base[name]), "md5_same": a1 == b1, "md5": a1})

    # ---------- ③ 编译 + 断链 ----------
    tmp = tempfile.mkdtemp()
    compile_fail, broken, const_table = [], [], {}
    for p in src:
        try:
            py_compile.compile(p, cfile=os.path.join(tmp, "x.pyc"), doraise=True)
        except py_compile.PyCompileError as e:
            compile_fail.append({"file": rel(SRC, p), "err": str(e).splitlines()[-1][:200]})

        txt = io.open(p, encoding="utf-8", errors="replace").read()
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        env = path_consts(tree)
        # 只收录「输入类」常量：默认输入、源目录。输出类（新建产物）缺失属正常
        keep = {}
        for k, v in env.items():
            if any(t in k for t in ("SRC", "INPUT", "IN_", "_IN", "DEFAULT", "ROOT", "DATA", "PACKED", "SRC_DIR")):
                keep[k] = {"path": v.replace(ROOT, "."), "exists": os.path.exists(v)}
        if keep:
            const_table[rel(SRC, p)] = keep
            for k, d in keep.items():
                if not d["exists"] and k not in ("ROOT",):
                    broken.append({"file": rel(SRC, p), "const": k, "path": d["path"],
                                   "cls": _cls(d["path"])})

    # ---------- ④ 时序 ----------
    tar_mtime = os.path.getmtime(FINAL_TAR) if os.path.exists(FINAL_TAR) else 0
    newer_than_tar = []
    for p in src:
        m = os.path.getmtime(p)
        if tar_mtime and m > tar_mtime:
            newer_than_tar.append({"file": rel(SRC, p), "mtime": __import__("datetime").datetime.fromtimestamp(m).strftime("%m-%d %H:%M")})

    out = {
        "_meta": {
            "src_py": len(src), "pub_py": len(pub),
            "final_tar_mtime": __import__("datetime").datetime.fromtimestamp(tar_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        },
        "①完整性": {"源层独有": only_src, "发布层独有": only_pub},
        "②镜像": {"不一致": [m for m in mirror if not m["md5_same"]], "总数": len(mirror)},
        "③编译": {"失败": compile_fail},
        "③断链": {"缺失的输入类常量": broken},
        "④时序": {"晚于终态tar的脚本": newer_than_tar},
    }
    os.makedirs(os.path.dirname(a.json_out), exist_ok=True)
    with open(a.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("源层 %d / 发布层 %d 个 py" % (len(src), len(pub)))
    print("\n① 完整性  源层独有 %d %s | 发布层独有 %d %s"
          % (len(only_src), only_src, len(only_pub), only_pub))
    bad = [m["name"] for m in mirror if not m["md5_same"]]
    print("② 镜像    %d 对，md5 不一致 %d %s" % (len(mirror), len(bad), bad))
    print("③ 编译    失败 %d %s" % (len(compile_fail), [(x["file"], x["err"]) for x in compile_fail]))
    print("③ 断链    缺失的输入类常量 %d，按类 %s"
          % (len(broken), dict(collections.Counter(x["cls"] for x in broken))))
    for x in broken:
        print("     [%s] %-34s %-16s %s" % (x["cls"], x["file"], x["const"], x["path"]))
    print("④ 时序    晚于终态 tar(%s) 的脚本 %d"
          % (out["_meta"]["final_tar_mtime"], len(newer_than_tar)))
    print("     （mtime 仅作线索：批量 touch/cp 也会触发；内容是否变更须以全链复现比对为准）")
    for x in newer_than_tar:
        print("     %-40s %s" % (x["file"], x["mtime"]))
    print("\nwrote " + a.json_out)

if __name__ == "__main__":
    main()
