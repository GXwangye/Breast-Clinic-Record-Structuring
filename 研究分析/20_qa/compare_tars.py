# -*- coding: utf-8 -*-
import os
import argparse
import collections
import hashlib
import json
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")
from tarstream import iter_records  # noqa: E402

def md5(b):
    return hashlib.md5(b).hexdigest()

def top_diff(a, b):
    out = []
    keys = set(a) | set(b)
    for k in sorted(keys):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, (dict, list)) or isinstance(vb, (dict, list)):
            if json.dumps(va, ensure_ascii=False, sort_keys=True) != json.dumps(vb, ensure_ascii=False, sort_keys=True):
                out.append(k)
        elif va != vb:
            out.append(k)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--show", type=int, default=2, help="打印前 N 条差异明细")
    a = ap.parse_args()

    n = same = diff = only_a = only_b = 0
    keys = collections.Counter()
    shown = 0
    ga, gb = iter_records(a.a), iter_records(a.b)
    while True:
        try:
            fn_a, rec_a = next(ga)
        except StopIteration:
            fn_a = None
        try:
            fn_b, rec_b = next(gb)
        except StopIteration:
            fn_b = None
        if fn_a is None and fn_b is None:
            break
        n += 1
        if fn_a is None:
            only_b += 1
            continue
        if fn_b is None:
            only_a += 1
            continue
        pa = json.dumps(rec_a, ensure_ascii=False).encode()
        pb = json.dumps(rec_b, ensure_ascii=False).encode()
        if md5(pa) == md5(pb):
            same += 1
            continue
        diff += 1
        ks = top_diff(rec_a, rec_b)
        for k in ks:
            keys[k] += 1
        if shown < a.show:
            shown += 1
            print("--- %s  差异顶层键：%s" % (fn_a.split("/")[-1], ks[:8]))
            for k in ks[:3]:
                sa = json.dumps(rec_a.get(k), ensure_ascii=False)[:220]
                sb = json.dumps(rec_b.get(k), ensure_ascii=False)[:220]
                print("    [%s]\n      A=%s\n      B=%s" % (k, sa, sb))

    print("\n记录 %d ／相同 %d ／不同 %d ／仅 A %d ／仅 B %d"
          % (n, same, diff, only_a, only_b))
    if keys:
        print("差异键分布：%s" % dict(keys.most_common(10)))
    print("结论：%s" % ("逐条一致" if diff == 0 and only_a == 0 and only_b == 0 else "存在差异"))

if __name__ == "__main__":
    main()
