# -*- coding: utf-8 -*-
import os
import re
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
sys.path.insert(0, ROOT + "/结构化处理脚本/05_agents")

import section_spec  # noqa: E402
import normalize_enum as NE  # noqa: E402

CONTRACT = ROOT + "/文档/门诊结构化病历结构_20260923.md"
FIELDS = ["report_type", "body_site", "laterality"]

fails = []
ok = 0

def _spec_enum(field):
    for sec in section_spec.SECTIONS:
        if sec["name"] != "auxiliary_exam":
            continue
        props = sec["schema"]["properties"]["auxiliary_exam"]["properties"]
        reps = props["reports"]["items"]["properties"]
        return reps.get(field, {}).get("enum")
    return None

def _contract_vocab(field):
    if not os.path.exists(CONTRACT):
        return None
    lines = open(CONTRACT, encoding="utf-8").read().splitlines()
    hit = []
    for ln in lines:
        if field in ln and "`" in ln:
            hit.extend(re.findall(r"`([^`]+)`", ln))
    return hit

print("=" * 62)
print("section_spec ≡ normalize_enum.CLOSED ≡ 契约 三方枚举一致性")
print("=" * 62)

for f in FIELDS:
    spec = _spec_enum(f)
    closed = NE.CLOSED.get(f)
    if spec is None:
        fails.append("%s: section_spec 里没有 enum（schema 未定义该闭集）" % f)
        continue
    if closed is None:
        fails.append("%s: normalize_enum.CLOSED 里没有该域" % f)
        continue

    s_set = {x for x in spec if x != ""}       # 空串是「标本留空」的合法态，不算值域成员
    c_set = set(closed)
    if s_set == c_set:
        ok += 1
        print("  [%d] %-14s 两处一致，%d 值" % (ok, f, len(s_set)))
    else:
        only_s = sorted(s_set - c_set)
        only_c = sorted(c_set - s_set)
        fails.append("%s: 两处不一致  仅 spec=%s  仅 CLOSED=%s" % (f, only_s, only_c))
        print("  [✗] %-14s 不一致" % f)
        if only_s:
            print("        仅 spec  : %s" % only_s)
        if only_c:
            print("        仅 CLOSED: %s" % only_c)

# 契约文档反查：每个闭集值都得在契约里出现过，否则说明契约没写全
print("\n" + "-" * 62)
print("契约文档反查（值必须能在 md 里找到）")
print("-" * 62)
for f in FIELDS:
    closed = NE.CLOSED.get(f) or []
    txt = open(CONTRACT, encoding="utf-8").read()
    missing = [v for v in closed if v not in txt]
    if missing:
        fails.append("%s: 契约文档未收录 %s" % (f, missing))
        print("  [✗] %-14s 契约缺 %s" % (f, missing))
    else:
        print("  [✓] %-14s 契约全覆盖（%d 值）" % (f, len(closed)))

# body_site 空串必须在 spec 里显式声明，否则「标本留空」会被 enum 拦下
bs = _spec_enum("body_site") or []
if "" in bs:
    print("\n  [✓] body_site 含空串（标本类留空合法）")
else:
    fails.append("body_site: spec enum 缺空串「」，标本类无法留空")

print("\n" + "=" * 62)
if fails:
    print("✗ 失败 %d 项：" % len(fails))
    for x in fails:
        print("   -", x)
    sys.exit(1)
print("✓ 全部通过（%d 域三方一致 + 空态声明）" % ok)
