# -*- coding: utf-8 -*-

import argparse
import collections
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
NORM = DATA + "/中间产物/_norm"
sys.path.insert(0, ROOT + "/结构化处理脚本/00_core")

import search_tools as kb                                        # noqa: E402
from llm_client import Client, parse_json_array, LLMError       # noqa: E402

DOMAINS = ["icd10_dx", "icd10_comorb", "icd9_surgery", "loinc_lab", "drug"]
KB_DOMAIN = {"icd10_dx": "icd10", "icd10_comorb": "icd10", "icd9_surgery": "icd9",
             "loinc_lab": "loinc", "drug": "drug"}
BATCH = 20
VAR_PER_BLOCK = 5          # 每块给模型看的代表写法上限（控 token）
CAND_PER_BLOCK = 3         # 字典参考候选上限
KB_LOCK = threading.Lock()  # search_tools 共用一条 sqlite 连接，多线程查询要串行

# 空转词：给了等于没给
JUNK = {"", "无", "其他", "未知", "不详", "待定", "同上", "无法确定", "无法判断",
        "未分类", "未能确定", "无法映射", "不确定", "待确认"}

# 疾病核心词：canonical 出现而原写法没有 → 说明在改临床含义。
#
#   `左乳癌术` → `乳腺癌手术`：原写法写「术」，canonical 写「手术」，字面比会判「凭空多了手术」；
#   `…左腋窝SLNB术` → `…前哨淋巴结活检术`：缩写展开被当成新增「活检」。
# 故先把写法与 canonical 都压成同一套核心概念 token，再比集合差。
CORE_MAP = [
    # 缩写与简写先展开（必须在「术→OP」之前，否则 SLNB术 会被切碎）
    ("slnb", ["BX"]), ("alnd", ["LND"]), ("cancer", ["MALIG"]), ("ca", ["MALIG"]),
    ("恶性肿瘤", ["MALIG"]), ("癌", ["MALIG"]),
    ("肉瘤", ["SARC"]),
    ("肿物", ["MASS"]), ("包块", ["MASS"]), ("肿块", ["MASS"]),
    ("肿瘤", ["TUMOR"]), ("瘤", ["TUMOR"]),
    ("囊肿", ["CYST"]), ("增生", ["HYPER"]), ("结节", ["NODE"]), ("息肉", ["POLYP"]),
    ("炎症", ["INFLAM"]), ("炎", ["INFLAM"]),
    ("转移", ["MET"]), ("结石", ["STONE"]),
    # 「已除/已切/缺如」是病历里对「已切除」的简写，拆成术后+切除两个概念
    ("已除", ["POSTOP", "EXCI"]), ("已切", ["POSTOP", "EXCI"]), ("缺如", ["ABSENT"]),
    # 状态词必须早于「手术 / 术」，否则「术后」会被拆成「手术」
    ("手术后", ["POSTOP"]), ("术后", ["POSTOP"]),
    ("化疗", ["CHEMO"]), ("放疗", ["RADIO"]),
    ("手术", ["OP"]), ("术", ["OP"]),
    ("切除", ["EXCI"]), ("根治", ["RADIC"]), ("活检", ["BX"]),
    ("清扫", ["LND"]), ("重建", ["RECON"]), ("成形", ["PLASTY"]),
    ("穿刺", ["PUNCT"]), ("引流", ["DRAIN"]), ("闭合", ["CLOSURE"]), ("内固定", ["FIX"]),
    # 定性词：canonical 凭空说「正常/阴性/阳性」也是改含义
    ("正常", ["NORMAL"]), ("阴性", ["NEG"]), ("阳性", ["POS"]),
]
TOKEN_CN = {"MALIG": "癌/恶性肿瘤", "SARC": "肉瘤", "MASS": "肿物", "TUMOR": "瘤",
            "CYST": "囊肿", "HYPER": "增生", "NODE": "结节", "POLYP": "息肉",
            "INFLAM": "炎", "MET": "转移", "STONE": "结石", "POSTOP": "术后",
            "CHEMO": "化疗", "RADIO": "放疗", "OP": "手术/术", "EXCI": "切除",
            "RADIC": "根治", "BX": "活检", "LND": "清扫", "RECON": "重建",
            "PLASTY": "成形", "PUNCT": "穿刺", "DRAIN": "引流",
            "CLOSURE": "闭合", "FIX": "内固定", "ABSENT": "缺如",
            "NORMAL": "正常", "NEG": "阴性", "POS": "阳性"}

# 硬拦的词类：
#   ① 恶性化（MALIG/SARC）—— 把良性或非肿瘤写成癌，唯一会造成临床误读的方向；
#   ② 定性翻转（NORMAL/NEG/POS）—— 原写法说异常、canonical 说"正常/阴性"。
# （乙肝→乙型肝炎、粉瘤→皮脂腺囊肿、全切→切除术、再造→重建），硬拦会把对的判死，
# 故只写进 `core_added` 供复核，不作废。
CROSS_HARD = {"MALIG", "SARC", "NORMAL", "NEG", "POS"}

# 术式/操作域（kd == "icd9"）只认"动作词"作为必留核心概念。
#
#   `前哨淋巴结6枚，均未见癌（0/6）`、`右腋窝前哨淋巴结活检，10枚淋巴结均未见癌`
# 其中「癌/转移/阳性」是**结果描述**，而规范术式名按书写规范本来就不带这些字。
# 用疾病域的核心词集去要求术式名保留，会把 1,295 条正确归一全判死（占全量作废 81%）：
#   前哨淋巴结活检、乳腺癌根治术、腔镜下乳全切+假体置入、乳腺病损微创旋切术 ……
# 术式域真正的"换手术方式"（保乳→根治）靠动作词判不出来，那是步 5 归并与抽检的职责，
# 不是本闸门能解决的，硬判只会误伤。
ACT_TOKENS = {"OP", "EXCI", "RADIC", "BX", "LND", "RECON", "PLASTY",
              "PUNCT", "DRAIN", "CLOSURE", "FIX"}

# 原文本身就是这些占位字面：没有可归的东西，不算归一失败（"其他"/":"/"无"）
NOISE_LIT = {"其他", "无", "空", "", ":", "-", "—", "/", "；", ";"}

SIDE_IN = re.compile(r"(双侧|左侧|右侧|单侧|左|右|双)")
DUR_IN = re.compile(r"\d+\s*(?:余)?\s*(?:天|日|周|月|年|小时)\S*$")
PUNCT = re.compile(r"[\s（）()\[\]【】、,，.。;；:：\"'“”‘’·\-—_/\\|+]+")
CJK = re.compile(r"[\u4e00-\u9fff]")

def latin_only(s):
    return bool(s) and not CJK.search(s)

def squash(s):
    return PUNCT.sub("", unicodedata.normalize("NFKC", s or "").lower())

def lcs_len(a, b):
    if not a or not b:
        return 0
    if a in b or b in a:
        return min(len(a), len(b))
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best

def core_hit(s):
    t = squash(s)
    for a, toks in CORE_MAP:         # 顺序即优先级，见 CORE_MAP 注释
        if a in t:
            t = t.replace(a, "".join("§%s§" % x for x in toks))
    return {m for m in TOKEN_CN if "§%s§" % m in t}

def is_subseq(a, b):
    it = iter(b)
    return all(ch in it for ch in a)

def anchored(sc, sl, sl0=None):
    if not sc or not sl:
        return False, "空"
    if sc in sl or sl in sc:
        return True, "子串"
    if lcs_len(sc, sl) >= 2:
        return True, "LCS≥2"
    if sc[:1] == sl[:1] and len(sc) > len(sl):
        return True, "首字相同且展开"
    if sl0 and is_subseq(sl0, sc):      # 原写法的字全部按序出现在 canonical 里
        return True, "按序包含"
    if core_hit(sc) & core_hit(sl):
        return True, "核心概念交集"
    return False, "无字面共享"

SYS = """你是乳腺专科病历的术语标准化专家。任务：把一批中文病历写法归并为**规范标签**。

规则：
1. `canonical` 必填，中文标准术语，2–40 字，不得留空、不得写「无法确定/其他/未知/同上」。
2. 归并的是同一个概念的多种写法：同义、简写、错别字、侧别差异、时长限定、括号注释，
   都归到同一个 canonical。不同概念绝不合并。
3. canonical 的粒度：
   - **不带侧别**（左/右/双），侧别另有字段；
   - **不带时长**（「术后20余天」是术后状态，不是「20余天」）；
   - **诊断到疾病名级别**；`XX术后` 是合法的诊断名（随访患者常见），保留为「XX术后」；
   - **术式到术式名级别**：`经腋窝腔镜下右乳切除加假体重建术` → `腔镜下乳房切除+假体重建术`；
   - **药品、检验到通用名级别**（去掉厂家、规格、商品名）。
3.5 **同义替换只许做这些**：乳房↔乳腺↔乳、术↔手术、术后↔手术后、
   癌↔恶性肿瘤、ca↔癌、SLNB↔前哨淋巴结活检、ALND↔腋窝淋巴结清扫。
   **不得引入原写法没有的疾病层级词**：原写「左乳改良根治术」应作「乳腺改良根治术」，
   **不能**写成「乳腺癌改良根治术」（凭空多了「癌」）。缩写一律展开成中文全称。
4. **不得改变临床含义**：良性不能归成恶性、癌不能归成瘤、增生不能归成癌。
   若 canonical 里出现了原写法没有的疾病核心词（癌/恶性肿瘤/瘤/炎/增生/囊肿/术后/
   手术/根治/活检…），系统会判你改写了含义并作废该条，请严格按原写法定。
5. `candidates` 是字典检索的参考，能对上就给 `code`；对不上 `code` 留空即可 ——
   **字典里没有不影响归一，canonical 照样要给**。
6. payload 里同一批若有多条其实同义，请给出**逐字相同**的 canonical 字符串，系统靠它合并。
7. 只输出 JSON 数组，不要任何解释文字。
8. **每条都必须带 `i` 字段，值与输入的 i 完全一致**（`{"i":3,"canonical":"…","code":""}`）。
   系统按 `i` 回填结果；漏写 `i` 会导致整批对不上位。"""

USER_TPL = """本域：{dom}（字典：{kbm}）

下面是 {n} 个折叠块，每块给出 key、代表写法 variants、出现次数 n、字典参考候选 candidates。
请对每块给出 canonical（必填）、code（可空）、reason（≤12 字）。

{items}"""

def build_items(dom, blocks, fetch_cands=True):
    kd = KB_DOMAIN[dom]
    items = []
    for i, (key, b) in enumerate(blocks):
        vs = sorted(b["variants"].items(), key=lambda kv: -kv[1])[:VAR_PER_BLOCK]
        cands = []
        if fetch_cands and vs:
            # 按需取候选：只为**这一批**的代表写法查，不为全部 3 万块预取。
            seen = set()
            with KB_LOCK:
                hits = kb.search(kd, vs[0][0], k=6)
            for h in hits:
                if h["name"] in seen:
                    continue
                seen.add(h["name"])
                cands.append({"code": h["code"], "name": h["name"]})
                if len(cands) >= CAND_PER_BLOCK:
                    break
        items.append({
            "i": i, "key": key[:30], "n": b["n"],
            "variants": [v for v, _ in vs],
            "candidates": cands,
        })
    return items

def gate_row(dom, key, block, ans):
    kd = KB_DOMAIN[dom]
    raw = str(ans.get("canonical") or "").strip()
    code = str(ans.get("code") or "").strip()
    reason = str(ans.get("reason") or "").strip()[:24]
    reps = [v for v, _ in sorted(block["variants"].items(), key=lambda kv: -kv[1])]
    lit_all = " ".join(reps)
    dom0 = reps[0].strip() if reps else ""      # 块内最高频写法＝代表写法
    sq0 = squash(dom0)
    abbr = latin_only(sq0)

    row = {"key": key, "kb_domain": kd, "canonical": None, "code": None,
           "kb_name": None, "display": reps[0], "n": block["n"],
           "n_variants": len(reps), "variants": reps[:10],
           "match_status": "rejected", "reason": reason,
           "raw_canonical": raw, "raw_code": code}

    # ① 非空 / 长度 / 空转词
    if ans.get("__missing__"):
        row["gate"] = "模型未返回该条（非判定为空，重跑即可）"
        return row, False
    # 原文本身就是噪声占位（"其他"/":"/"无"）→ 没有可归的东西，不算归一失败
    if sq0 in NOISE_LIT:
        row["noise"] = True
        row["gate"] = "原文为噪声占位，不做归一"
        return row, False
    c = raw.replace(" ", "")
    # c 与原写法完全相同 → 是"已经规范"；纯拉丁写法的单字标签（Ca→钙）也放行
    if c in JUNK or len(c) > 40 or (len(c) < 2 and not (abbr or c == dom0)):
        row["gate"] = "canonical 空/过短/过长/空转词"
        return row, False

    # ⑤ 侧别与时长剥离（不参与标签）
    side = SIDE_IN.search(c)
    if side and side.group(1) in ("双侧", "左侧", "右侧", "单侧", "左", "右", "双"):
        c2 = SIDE_IN.sub("", c, count=1)
        if len(c2) >= 2:
            row["side_stripped"] = side.group(1)
            c = c2
    c3 = DUR_IN.sub("", c)
    if c3 != c and len(c3) >= 2:
        row["dur_stripped"] = True
        c = c3

    # ② 锚定 / ③ 跨类 —— 纯拉丁写法（AST/ALT/WBC/MMT）例外
    # 判据用**块内最高频写法**，不是全部变体拼起来的串：
    # `mmt` 块里混着 `mmt术后1周`（折叠把时长剥了），拼起来就有汉字，
    # 会让 `MMT → 乳房微创旋切术` 被误判成"未锚定"；`双乳假体植入` 块里混着「术后」
    # 变体，会让 cl 多出 POSTOP，把正确的 `乳房假体植入术` 判成"核心概念消失"。
    sc, sl = squash(c), squash(lit_all)
    if abbr:
        row["abbr"] = True
        row["anchor"] = "纯拉丁写法，跳过字面锚定（须复核）"
    else:
        aok, ahow = anchored(sc, sl, sq0)
        if not aok:
            row["gate"] = "未锚定：canonical 与原写法无字面共享"
            return row, False
        row["anchor"] = ahow
        # 只对疾病/术式域做跨类检查
        if kd in ("icd10", "icd9"):
            cl, cc = core_hit(sq0), core_hit(c)
            if kd == "icd9":
                # 术式域：只看动作词。结果描述（癌/转移/阳性/肿物）丢掉不算改含义。
                extra = (cc - cl) & ACT_TOKENS
                # 「肉瘤化」仍硬拦（良性写成肉瘤是明确的临床误读）
                if "SARC" in (cc - cl):
                    row["gate"] = "跨类改写：canonical 新增核心词 肉瘤"
                    row["core_extra"] = ["肉瘤"]
                    return row, False
                # 术式名带「癌」是常规书写（复发根治术 → 乳腺癌根治术），只记账不拦；
                # 但**既加了癌、又把动作换了**就是真改含义：`乳腺活检术 → 乳腺癌根治术`
                # （诊断性活检被写成根治性切除，病人按此理解会以为乳房已切除）。
                # `术` 映射的 OP 无区分度（两个名字都带"术"），比对动作时剔除。
                la, ca = cl & ACT_TOKENS, cc & ACT_TOKENS
                act_swap = bool(la - {"OP"}) and bool(ca - {"OP"}) and not ((la - {"OP"}) & (ca - {"OP"}))
                if (cc - cl) & {"MALIG"} and act_swap:
                    row["gate"] = "跨类改写：canonical 新增核心词 癌/恶性肿瘤 且手术方式被替换"
                    row["core_extra"] = ["癌/恶性肿瘤"]
                    return row, False
                # 术式名带「癌」是常规书写（复发根治术 → 乳腺癌根治术），只记账不拦
                if (cc - cl) & {"MALIG", "SARC", "NORMAL", "NEG", "POS"}:
                    row["core_added"] = sorted(
                        TOKEN_CN.get(x, x) for x in ((cc - cl) & {"MALIG", "NORMAL", "NEG", "POS"}))
                if la and not (la & ca):
                    # 动作词被整体换掉且无交集：`切除→活检` 这类同义（前哨淋巴结切除/活检）
                    row["act_changed"] = sorted(TOKEN_CN.get(x, x) for x in la)
                if extra:
                    row["core_added"] = sorted(
                        set(row.get("core_added") or []) | {TOKEN_CN.get(x, x) for x in extra})
            else:
                extra = cc - cl
                # ① 把良性/非肿瘤写成恶性 —— 唯一会造成临床误读的方向，硬拦
                hard = {x for x in extra if x in CROSS_HARD}
                if hard:
                    cn = sorted(TOKEN_CN.get(x, x) for x in hard)
                    row["gate"] = "跨类改写：canonical 新增核心词 %s" % "、".join(cn)
                    row["core_extra"] = cn
                    return row, False
                # ② 原写法的核心概念被整体抹掉（囊肿→炎、增生→瘤）—— 这是换了疾病类别，硬拦
                if cl and not (cl & cc):
                    row["gate"] = "跨类改写：原写法核心概念（%s）在 canonical 中全部消失" \
                                  % "、".join(sorted(TOKEN_CN.get(x, x) for x in cl))
                    return row, False
                if extra:
                    # ③ 其余新增词（术/切除/炎/囊肿/术后…）**不作废，只记账**：
                    # 硬拦会把对的判死。落进错误记录 `core_added` 供复核。
                    row["core_added"] = sorted(TOKEN_CN.get(x, x) for x in extra)

    # ④ 码存在
    row["canonical"] = c
    row["match_status"] = "canonical_only"
    if code:
        if kb.exists(kd, code):
            info = kb.get(kd, code)
            row["code"] = code
            row["kb_name"] = info[0]["name"] if info else None
            row["match_status"] = "canonical_with_code"
        else:
            row["code_rejected"] = code
            row["gate"] = "码在字典中不存在，已置空（canonical 保留）"
    return row, True

def prep_blocks(dom):
    p = os.path.join(NORM, "fold_%s.jsonl" % dom)
    b = collections.defaultdict(lambda: {"variants": collections.Counter(), "n": 0})
    with open(p, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            blk = b[r["key"]]
            blk["variants"][r["literal"]] += r["count"]
            blk["n"] += r["count"]
    return sorted(b.items(), key=lambda kv: -kv[1]["n"])

def load_done(p):
    done = {}
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            done[r["key"]] = r
    return done

def run_domain(dom, a, cli):
    blocks = prep_blocks(dom)
    out_p = os.path.join(NORM, "canon_%s.jsonl" % dom)
    keep = a.resume or a.only_rejected or a.retry_rejected
    done = load_done(out_p) if keep else {}
    if not keep and os.path.exists(out_p):
        os.remove(out_p)
    # 待判队列：未跑的 + （可选）闸门作废的。
    # PDCA 里「改错后迭代重跑错误样本」就靠 --only-rejected：
    # 只重判 canonical 为空的行，新行按 key 追加，load_done 取最后一次即覆盖旧结论。
    pend = []
    for k, b in blocks:
        r = done.get(k)
        if r is None:
            if not a.only_rejected:
                pend.append((k, b))
        elif not r.get("canonical"):
            if a.only_rejected or a.retry_rejected:
                pend.append((k, b))
    if a.limit:
        pend = pend[:a.limit]
    batches = [pend[i:i + BATCH] for i in range(0, len(pend), BATCH)]

    print("[%s] 块 %d，已完成 %d，本轮待判 %d，%d 批"
          % (dom, len(blocks), len(done), len(pend), len(batches)), flush=True)

    if a.dry_run:
        it = build_items(dom, batches[0]) if batches else []
        print("\n--- system ---\n%s" % SYS)
        print("--- user（第 1 批，%d 块）---" % len(it))
        print(USER_TPL.format(dom=dom, kbm=KB_DOMAIN[dom], n=len(it),
                              items=json.dumps(it, ensure_ascii=False, indent=1)))
        return

    lock = threading.Lock()
    fh = open(out_p, "a", encoding="utf-8")
    stat = collections.Counter()
    t0 = time.time()

    def work(batch):
        items = build_items(dom, batch)
        txt = cli.chat(SYS, USER_TPL.format(dom=dom, kbm=KB_DOMAIN[dom],
                                            n=len(items),
                                            items=json.dumps(items, ensure_ascii=False)))
        arr = parse_json_array(txt)
        if not arr:
            return None, batch
        # 此时按数组顺序回填：只有**条数相等**才敢对位，条数不等就整批算失败重跑，
        # 绝不做"猜哪条对哪条"。
        lack = [x for x in arr if isinstance(x, dict) and x.get("i") is None]
        if lack:
            if len(arr) == len(batch) and len(lack) == len(arr):
                for idx, x in enumerate(arr):
                    x["i"] = idx
            else:
                return None, batch
        got = {x.get("i"): x for x in arr if isinstance(x, dict)}
        rows = []
        for i, (k, b) in enumerate(batch):
            ans = got.get(i)
            if ans is None:
                # 与"模型判定为空"区分开：这是没返回该条，重跑即可，不是判断结果
                ans = {"__missing__": True}
            row, ok = gate_row(dom, k, b, ans)
            rows.append((row, ok))
        return rows, batch

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, b): b for b in batches}
        for k, fu in enumerate(as_completed(futs), 1):
            try:
                rows, batch = fu.result()
            except LLMError as e:
                rows, batch = None, None
                with lock:
                    stat["err"] += 1
                print("  !! 批失败：%s" % str(e)[:100], flush=True)
            if rows:
                with lock:
                    for row, ok in rows:
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stat["ok" if ok else "reject"] += 1
                        if row.get("match_status") == "canonical_with_code":
                            stat["code"] += 1
                    fh.flush()
            if k % 10 == 0 or k == len(batches):
                print("  …%d/%d 批  通过 %d / 作废 %d / 带码 %d / 异常批 %d  %.0fs"
                      % (k, len(batches), stat["ok"], stat["reject"], stat["code"],
                         stat["err"], time.time() - t0), flush=True)
    fh.close()
    print("[%s] 完成：通过 %d，作废 %d，其中带码 %d；tokens 入 %d / 出 %d"
          % (dom, stat["ok"], stat["reject"], stat["code"],
             cli.usage["prompt"], cli.usage["completion"]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多判多少个折叠块")
    ap.add_argument("--retry-rejected", action="store_true",
                    help="把闸门作废的块一并放回待判（与未跑的混跑）")
    ap.add_argument("--only-rejected", action="store_true",
                    help="只重判闸门作废的块（PDCA 迭代用）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", ""))
    a = ap.parse_args()

    doms = DOMAINS if a.all else ([a.domain] if a.domain else [])
    if not doms:
        raise SystemExit("须指定 --domain 或 --all")
    for d in doms:
        if d not in DOMAINS:
            raise SystemExit("未知域：%s" % d)

    cli = None
    if not a.dry_run:
        if not (a.base_url and a.api_key and a.model):
            raise SystemExit("缺 --base-url / --api-key / --model")
        cli = Client(a.base_url, a.api_key, a.model)

    for d in doms:
        run_domain(d, a, cli)

if __name__ == "__main__":
    main()
