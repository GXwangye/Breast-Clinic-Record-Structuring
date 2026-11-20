# -*- coding: utf-8 -*-

import argparse
import hashlib
import json
import os
import sys
import tarfile

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"
FINAL = DATA + "/中间产物/_packed/assembled_v7_final.tar"

def members(path):
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith(".json") and not os.path.basename(m.name).startswith("._"):
                yield os.path.basename(m.name), tf.extractfile(m)

def norm(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default=FINAL)
    ap.add_argument("--b", default="/tmp/repro_v7/final.tar")
    ap.add_argument("--show", type=int, default=5)
    ap.add_argument("--json-out", default=DATA + "/qa_reports/全链复现比对.json")
    args = ap.parse_args()

    if not os.path.exists(args.b):
        print("复现产物不存在: " + args.b)
        raise SystemExit(1)

    n = same = 0
    only_a, only_b = [], []
    diffs = []
    with tarfile.open(args.a) as ta, tarfile.open(args.b) as tb:
        ma = {os.path.basename(m.name): m for m in ta.getmembers()
              if m.isfile() and m.name.endswith(".json") and not os.path.basename(m.name).startswith("._")}
        mb = {os.path.basename(m.name): m for m in tb.getmembers()
              if m.isfile() and m.name.endswith(".json") and not os.path.basename(m.name).startswith("._")}

        only_a = sorted(set(ma) - set(mb))
        only_b = sorted(set(mb) - set(ma))

        for name in sorted(set(ma) & set(mb)):
            n += 1
            oa = json.loads(ta.extractfile(ma[name]).read().decode("utf-8"))
            ob = json.loads(tb.extractfile(mb[name]).read().decode("utf-8"))
            if norm(oa) == norm(ob):
                same += 1
            elif len(diffs) < args.show:
                # 定位首个不同路径
                path = []

                def walk(x, y, p=""):
                    if len(path) >= 3:
                        return
                    if type(x) is not type(y):
                        path.append((p, type(x).__name__, type(y).__name__))
                        return
                    if isinstance(x, dict):
                        for k in set(x) | set(y):
                            if x.get(k) != y.get(k):
                                walk(x.get(k), y.get(k), p + "." + str(k))
                    elif isinstance(x, list):
                        if len(x) != len(y):
                            path.append((p + "[len]", len(x), len(y)))
                            return
                        for i, (u, v) in enumerate(zip(x, y)):
                            if u != v:
                                walk(u, v, "%s[%d]" % (p, i))
                    else:
                        path.append((p, repr(x)[:60], repr(y)[:60]))

                walk(oa, ob)
                diffs.append({"file": name, "差异路径": path})

    out = {"a": args.a, "b": args.b, "配对条数": n, "逐条一致": same,
           "不一致条数": n - same, "仅正式有": only_a[:20], "仅复现有": only_b[:20],
           "差异样本": diffs}
    os.makedirs(os.path.dirname(args.json_out), exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("配对 %d 条：逐条一致 %d，不一致 %d" % (n, same, n - same))
    print("仅正式有 %d，仅复现有 %d" % (len(only_a), len(only_b)))
    for d in diffs:
        print("  [%s]" % d["file"])
        for p in d["差异路径"]:
            print("     %s : %r -> %r" % p)
    print("wrote " + args.json_out)

if __name__ == "__main__":
    main()
