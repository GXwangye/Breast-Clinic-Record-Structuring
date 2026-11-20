# -*- coding: utf-8 -*-
import re
from collections import OrderedDict

__all__ = ["parse_ihc", "detect_markers", "marker_kind", "QC_FRAGMENTS"]

# ---------------------------------------------------------------- 标记物分类
KIND_MAP = {
    "HER2": "her2", "CERBB2": "her2", "ERBB2": "her2", "HER2NEU": "her2",
    "ER": "hormone", "PR": "hormone",
    "KI67": "prolif", "MIB1": "prolif",
}
KIND_MAP = {re.sub(r"[^A-Z0-9]", "", k): v for k, v in KIND_MAP.items()}

def marker_kind(marker):
    k = re.sub(r"[^A-Z0-9]", "", str(marker or "").upper())
    return KIND_MAP.get(k, "general")

# ---------------------------------------------------------------- 词表与正则
QC_FRAGMENTS = [
    # 长片段优先（sort by len desc 后替换），避免短片段先替换造成截断
    "阳性对照显示结果可靠", "阴性对照显示结果可靠", "对照显示结果可靠",
    "内对照及外对照表达正常", "内、外对照表达正常", "内外对照表达正常",
    "阴性对照表达正常", "阳性对照表达正常", "内对照表达正常", "外对照表达正常",
    "外对照表达准确", "外对照正常", "外对照准确",
    "对照组正常表达", "对照正常表达", "对照表达准确", "对照表达正常",
    "对照染色正常", "对照染色准确", "阴性对照准确", "阳性对照准确",
    "阴阳对照准确", "阴阳性对照准确", "对照准确", "对照正常",
    "内对照阳性", "内对照存在", "内设对照",
]
# 未出结果 / 不可判读 → 缺失
PENDING_RE = re.compile(
    r"(待后报|待重然后报|待报|待补充|待二次|等待实验|待重做|稍后报|未出结果|无法判读|"
    r"未见组织|未见有效组织|未见细胞|肿瘤细胞少|细胞太少|无组织|无片|未做|未查|未检|未行|"
    r"不满意|NS|未见标本|脱片|深切|病灶消失|病变消失|染色不佳|表达不理想|重做后发|"
    r"蜡块均做|对照表达良好|表达未见特殊|无法评估)")
# 字段错位：IHC 字段里写的是诊断结论或技术说明，不是染色结果
MISPLACED_RE = re.compile(
    r"(浸润性癌|原位癌|导管上皮来源|小叶癌|导管内癌成分|未累及|横纹肌|肝S8|达CR|达pCR|"
    r"支持[^，,;；]{0,6}(来源|诊断)|提示浸润|考虑[^，,;；]{0,8}癌)")
# 抗体克隆号 / 试剂名：只有试剂标识，没有结果
CLONE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[\s\-/][A-Za-z0-9]+)*$")

# 定量词
_PCT_RE = re.compile(r"(\d{1,3})\s*(?:[-~—]\s*(\d{1,3})\s*)?%")
_PLUS_NUM_RE = re.compile(r"[+＋]\s*(\d{1,3})(?![0-9])")          # +95 形式（省略 %）
_BARE_NUM_RE = re.compile(r"(?<![0-9.])(\d{1,3})(?![0-9.])")
_SCORE_RE = re.compile(r"(?<![0-9])([0123])\s*[+＋]")           # 3+ / 2+ / 1+ / 0+
_SCORE_REV_RE = re.compile(r"[+＋]\s*([0123])(?![0-9])")        # +3 / +2（书写倒序）
_BARE_DIGIT_RE = re.compile(r"(?<![0-9.])([0-3])(?![0-9.%±])")
_BARE_ZERO_RE = re.compile(r"(?<![0-9])0(?![0-9%])")
_BARE_PLUS_RE = re.compile(r"(?<![\d+＋])[+＋](?![+\d])")
_PLUSPLUS_RE = re.compile(r"\+\+")
_TRAIL_STRIP = " ，,;；.。、—\t\r\n()（）"
_FULLWIDTH = str.maketrans("０１２３４５６７８９＋－", "0123456789+-")

_NEG_RE = re.compile(r"(阴性|不表达|未表达|未见表达|无表达|无着色|不着色|未着色|无染色|未见染色|阴性表达|未提示|不提示)")
_NEGATED_POS_RE = re.compile(r"(无任何阳性|无阳性|未见阳性|未见明确阳性|阴性信号|无强阳性|无膜阳)")
_POS_RE = re.compile(r"(阳性|强阳|弱阳|表达阳性|阳性表达|阳)")
_FISH_RE = re.compile(r"(FISH|ISH|基因扩增|荧光原位杂交)")

# 语义专项
_LVI_RE = re.compile(r"(脉管|血管|癌栓|瘤栓|淋巴管)")
_NERVE_RE = re.compile(r"(神经侵犯|神经束|嗜神经|周围神经)")
_MYOEP_RE = re.compile(r"(肌上皮|肌纤维母细胞|肌上皮细胞)")
_MYOEP_NEG_RE = re.compile(r"(缺失|消失|减少|无肌上皮|未见|大部分消失|不完整)")
_MYOEP_POS_RE = re.compile(r"(存在|保留|完整|可见|阳性|尚存|完好)")
_RANGE_MOD_RE = re.compile(r"(灶|部分|局部|散在|个别|片内|不均)")
_P53_MUT_RE = re.compile(r"(突变型|突变)")
_P53_WT_RE = re.compile(r"(野生型|野生)")

# ---------------------------------------------------------------- 基础工具
def _strip_qc(s):
    for q in sorted(QC_FRAGMENTS, key=len, reverse=True):
        s = s.replace(q, "")
    # 兜底：未穷举的「X对照…」短语
    s = re.sub(r"[,，、;；]?\s*[内外阴阳]?性?\s*对照[^，,;；。]{0,10}", "", s)
    s = re.sub(r"[（(]\s*[)）]", "", s)
    return s.strip(_TRAIL_STRIP)

def _extract_pct(s, kind):
    m = _PCT_RE.search(s)
    if m:
        hi = m.group(2)
        if hi:
            return (int(m.group(1)) + int(hi)) / 2.0, "百分比区间取值(中位)"
        return float(m.group(1)), ""
    m1 = _PLUS_NUM_RE.search(s)
    if m1:
        v = int(m1.group(1))
        if 1 <= v <= 100:
            return float(v), "省略%号的百分比（+NN 形式）"
    if kind == "prolif":
        m2 = _BARE_NUM_RE.search(s)
        if m2:
            v = int(m2.group(1))
            if 1 <= v <= 100:
                return float(v), "无%号，按 Ki67 惯例视为百分数"
    return None, ""

def _extract_intensity(s):
    if "强" in s:
        return "强"
    if "中" in s:
        return "中"
    if "弱" in s:
        return "弱"
    return ""

def _mk(raw, status, value=None, intensity="", note=""):
    return {"raw": raw, "status": status, "value": value,
            "intensity": intensity, "note": note}

def _finalize(raw, status, value, intensity, notes):
    # 百分比数值保留，不做四舍五入；notes 去重保序
    seen, out = set(), []
    for x in notes:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return {"raw": raw, "status": status, "value": value,
            "intensity": intensity, "note": "；".join(out)}

# ---------------------------------------------------------------- 语义专项
def _semantic(raw, s, kind):
    # 1) 未出结果 / 不可判读
    if PENDING_RE.search(s):
        return _mk(raw, "缺失", None, "", "未出结果或不可判读")

    # 2) 字段错位：IHC 字段里写的是诊断结论或技术说明（带明确评分/阴阳符号者除外）
    if MISPLACED_RE.search(s) and not re.search(r"[+＋]|阴性|阳性|[-－–—]\s*$", s):
        return _mk(raw, "缺失", None, "", "字段错位:非IHC染色结果")

    # 3) 抗体克隆号 / 试剂名（如 4B5 Ventana、SP1、1E2、EP38Y、22C3）
    t = s.strip()
    if (t and len(t) <= 16 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9]*(?:[\s\-/][A-Za-z0-9]+)*", t)
            and re.search(r"[A-Za-z]", t) and not re.search(r"[阴阳]", t)
            and t.lower() not in ("neg", "negative", "pos", "positive")):
        return _mk(raw, "缺失", None, "", "仅抗体/试剂名称，无测定结果")

    # 4) 脉管 / 血管侵犯评估（D2-40、CD31、CD34 等承载）
    if _LVI_RE.search(s):
        if re.search(r"(未见|未查见|未提示|不提示|提示无|显示无|均无|片内无|无脉管|无血管|阴性)", s):
            return _mk(raw, "阴", None, "", "脉管侵犯评估:未见")
        if re.search(r"(可见|示|显示)\s*[，,]?\s*(脉管|血管|癌栓|瘤栓)", s) or \
           re.search(r"[+＋].{0,3}(脉管|血管|癌栓|瘤栓)", s):
            return _mk(raw, "阳", None, "", "脉管侵犯评估:可见")
        return _mk(raw, "不确定", None, "", "脉管侵犯评估:表述不明确")

    # 5) 神经侵犯评估
    if _NERVE_RE.search(s):
        if re.search(r"(未见|未查见|未提示|提示无|显示无|无神经)", s):
            return _mk(raw, "阴", None, "", "神经侵犯评估:未见")
        if re.search(r"(可见|有|示|显示)\s*[，,]?\s*(神经侵犯|嗜神经)", s):
            return _mk(raw, "阳", None, "", "神经侵犯评估:可见")
        return _mk(raw, "不确定", None, "", "神经侵犯评估:表述不明确")

    # 6) 肌上皮评估（CK5/6、Calponin、P63、SMMHC、SMA、P120、CD10、S-100 等承载）
    if _MYOEP_RE.search(s):
        neg = bool(_MYOEP_NEG_RE.search(s))
        pos = bool(_MYOEP_POS_RE.search(s))
        if neg and not pos:
            tag = "部分性" if _RANGE_MOD_RE.search(s) else "整体"
            return _mk(raw, "阴", None, "", "肌上皮评估:%s缺失" % tag)
        if pos and not neg:
            return _mk(raw, "阳", None, "", "肌上皮评估:存在/保留")
        if neg and pos:
            return _mk(raw, "不确定", None, "", "肌上皮评估:表述矛盾")
        return _mk(raw, "不确定", None, "", "肌上皮评估:表述不明确")

    # 7) P53 突变型 / 野生型
    if _P53_MUT_RE.search(s) and not _P53_WT_RE.search(s):
        return _mk(raw, "阳", None, "", "P53 突变型表述")
    if _P53_WT_RE.search(s) and not _P53_MUT_RE.search(s):
        return _mk(raw, "阴", None, "", "P53 野生型表述")

    return None

def _fish_conclusion(s):
    if re.search(r"(无扩增|未扩增|未见扩增|无基因扩增|未见基因扩增)", s):
        return "阴"
    if "扩增" in s:
        return "阳"
    if not _FISH_RE.search(s):
        return None
    if re.search(r"(FISH|ISH|荧光原位杂交)[^，,;；。]{0,10}(阴性|阳性)", s):
        return "阴" if "阴性" in s else "阳"
    return None

# ---------------------------------------------------------------- 主判读
def parse_ihc(raw, marker=""):
    try:
        return _parse(raw, marker)
    except Exception as e:                       # 防御：不因单条脏值中断全量跑批
        return _mk(str(raw), "未解析", None, "", "判读异常:%s" % e)

def _parse(raw, marker):
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "":
        return None
    kind = marker_kind(marker)

    s = _strip_qc(raw.translate(_FULLWIDTH))
    if s == "":
        return _mk(raw, "缺失", None, "", "质控语无有效值")
    if "对照" in s:
        # 残留未收录的质控片段：若仍含有效测定信息则继续判读，否则判缺失
        if not re.search(r"[0-9]|[+＋]|[-－]|阳|阴", s):
            return _mk(raw, "缺失", None, "", "质控语无有效值(未收录短语)")

    notes = []
    if s != raw:
        notes.append("已剥离质控语")
        if "对照" in s:
            notes.append("残留未收录质控片段")

    # 语义专项优先于数值判读（脉管/神经/肌上皮/P53/未出结果）
    sem = _semantic(raw, s, kind)
    if sem is not None:
        return sem

    pct, pct_note = _extract_pct(s, kind)
    inten = _extract_intensity(s)
    if pct_note:
        notes.append(pct_note)

    # 部分性表述（灶-/部分-/局部-）：不做整体阴阳判定
    if re.search(r"[-－—=]\s*$", s) and _RANGE_MOD_RE.search(s):
        return _mk(raw, "不确定", pct, inten, "部分性表述（%s），不做整体阴阳判定" %
                   _RANGE_MOD_RE.search(s).group(1))

    if kind == "her2":
        return _parse_her2(raw, s, pct, inten, notes)
    if kind == "prolif":
        return _parse_prolif(raw, s, pct, inten, notes)
    return _parse_general(raw, s, pct, inten, notes, kind)

def _score_tokens(s):
    sc = set(_SCORE_RE.findall(s))
    for x in _SCORE_REV_RE.findall(s):
        sc.add(x)
    if not sc:
        # 无带号评分时，HER2 值域内的裸数字视为评分（如 "3"、"2"）
        if re.fullmatch(r"\s*[0-3]\s*", s):
            sc.add(_BARE_DIGIT_RE.search(s).group(1))
    return sc, bool(_PLUSPLUS_RE.search(s)), bool(re.search(r"\+\+\+", s))

def _parse_her2(raw, s, pct, inten, notes):
    sc, has_pp, has_ppp = _score_tokens(s)
    has_zero = bool(_BARE_ZERO_RE.search(s))
    has_bare_plus = bool(_BARE_PLUS_RE.search(s))

    # 1) ISH/FISH 实际结论优先
    fish = _fish_conclusion(s)
    if fish:
        notes.append("ISH/FISH 结论直接采用")
        return _finalize(raw, fish, pct, inten, notes)

    # 2) +++ / ++ 简写
    if has_ppp:
        notes.append("ASCO/CAP 3+（+++）")
        return _finalize(raw, "阳", pct, inten, notes)
    if has_pp and not sc and not has_zero:
        notes.append("ASCO/CAP 2+（++）")
        return _finalize(raw, "不确定", pct, inten, notes)

    # 3) 数字评分
    if len(sc) >= 2:
        notes.append("同一字段出现多个评分 %s" % sorted(sc))
        return _finalize(raw, "不确定", pct, inten, notes)
    if len(sc) == 1:
        v = sc.pop()
        if v == "3":
            notes.append("ASCO/CAP 3+")
            return _finalize(raw, "阳", pct, inten, notes)
        if v == "2":
            notes.append("ASCO/CAP 2+（须 ISH 复核）")
            return _finalize(raw, "不确定", pct, inten, notes)
        if has_bare_plus or re.search(r"(强阳|阳性)", s):
            notes.append("评分 %s+ 与游离阳性表述并存" % v)
            return _finalize(raw, "不确定", pct, inten, notes)
        notes.append("ASCO/CAP %s+（视为阴性）" % v)
        return _finalize(raw, "阴", pct, inten, notes)

    # 4) 裸 0 分
    if has_zero:
        if has_bare_plus or (re.search(r"(强阳|阳性)", s) and not _NEGATED_POS_RE.search(s)):
            notes.append("0 分与阳性/游离+表述并存")
            return _finalize(raw, "不确定", pct, inten, notes)
        notes.append("ASCO/CAP 0 分（无着色）")
        return _finalize(raw, "阴", pct, inten, notes)

    # 5) 无评分，退回阴阳表述
    if _NEGATED_POS_RE.search(s):
        notes.append("否定阳性表述")
        return _finalize(raw, "阴", pct, inten, notes)
    if _NEG_RE.search(s) and not _POS_RE.search(s):
        notes.append("阴性表述")
        return _finalize(raw, "阴", pct, inten, notes)
    if re.search(r"(强阳|阳性)", s):
        notes.append("阳性表述（无评分）")
        return _finalize(raw, "阳", pct, inten, notes)
    if has_bare_plus or re.fullmatch(r"[+＋]", s):
        notes.append("游离+（未定量），HER2 按 1+ 处理")
        return _finalize(raw, "阴", pct, inten, notes)
    if s in ("阴", "阴性", "-", "－") or re.search(r"[-－–—]\s*$", s):
        notes.append("阴性表述")
        return _finalize(raw, "阴", pct, inten, notes)
    if pct is not None:
        notes.append("含百分比但无评分，无法归入 ASCO/CAP 评分")
        return _finalize(raw, "不确定", pct, inten, notes)
    notes.append("表述无法识别")
    return _finalize(raw, "未解析", pct, inten, notes)

def _parse_prolif(raw, s, pct, inten, notes):
    if pct is not None:
        lvl = "高表达" if pct >= 20 else "低表达"
        notes.append("St Gallen 阈值 20%")
        return _finalize(raw, lvl, pct, inten, notes)
    if re.search(r"未见异常表达|表达正常|未见异常", s):
        notes.append("未见异常表达（无百分比）")
        return _finalize(raw, "低表达", None, inten, notes)
    if _NEG_RE.search(s) and not _POS_RE.search(s):
        notes.append("阴性表述（无百分比）")
        return _finalize(raw, "低表达", None, inten, notes)
    if _POS_RE.search(s) or _BARE_PLUS_RE.search(s) or _SCORE_RE.search(s):
        notes.append("阳性表述但无百分比，无法定量")
        return _finalize(raw, "不确定", None, inten, notes)
    notes.append("表述无法识别")
    return _finalize(raw, "未解析", None, inten, notes)

def _parse_general(raw, s, pct, inten, notes, kind):
    # 明确阴性
    if re.fullmatch(r"[-－–—一]+", s) or s in ("阴", "阴性", "neg", "negative", "(-)", "(－)"):
        notes.append("明确阴性")
        return _finalize(raw, "阴", None, inten, notes)
    if re.fullmatch(r"\s*0\s*[+＋]?\s*", s):
        notes.append("0 分（无表达）")
        return _finalize(raw, "阴", None, inten, notes)
    if re.fullmatch(r"\s*[均两]\s*项?\s*[-－—]\s*", s):
        notes.append("表述为『均/两项阴性』")
        return _finalize(raw, "阴", None, inten, notes)
    # 矛盾 / 不可判定表述
    if s in ("±", "＋－", "+-", "=", "==") or re.search(r"(强弱不一|强弱不等|不一致|无法判定)", s):
        notes.append("表述不明确（%s）" % s[:6])
        return _finalize(raw, "不确定", pct, inten, notes)
    if s in ("两项", "两项-", "3项", "三项", "部分缺失", "少数灶性", "少数灶", "膜"):
        notes.append("表述不明确（%s）" % s)
        return _finalize(raw, "不确定", pct, inten, notes)
    if s == "膜阳":
        notes.append("膜着色阳性")
        return _finalize(raw, "阳", pct, inten, notes)
    if s == "膜-":
        notes.append("膜着色阴性")
        return _finalize(raw, "阴", pct, inten, notes)
    # 否定阳性表述优先于阳性词
    if _NEGATED_POS_RE.search(s):
        notes.append("否定阳性表述")
        return _finalize(raw, "阴", pct, inten, notes)

    neg = bool(_NEG_RE.search(s)) or bool(re.search(r"[-－]\s*$", s))
    pos = (bool(_POS_RE.search(s)) or bool(_SCORE_RE.search(s)) or bool(_SCORE_REV_RE.search(s))
           or bool(_BARE_PLUS_RE.search(s)) or bool(_PLUSPLUS_RE.search(s)))
    # 前导阴性符号 + 强度词（如「-强」）→ 表述矛盾
    # 注意「中-强」中的连字符是强度区间，不是阴性符号，不可误判
    if re.match(r"^[-－–—]\s*(强|中|弱)", s):
        notes.append("前导阴性符号与染色强度并存，表述矛盾")
        return _finalize(raw, "不确定", pct, inten, notes)
    if neg and pos:
        notes.append("矛盾表述（同时含阴阳）")
        return _finalize(raw, "不确定", pct, inten, notes)
    if neg:
        notes.append("阴性表述")
        return _finalize(raw, "阴", pct, inten, notes)
    if pos:
        if "建议" in s and ("FISH" in s.upper() or "ISH" in s.upper()):
            notes.append("建议进一步检测")
            return _finalize(raw, "不确定", pct, inten, notes)
        if kind == "hormone" and pct is not None:
            if pct < 1:
                notes.append("ASCO/CAP <1% 判阴")
                return _finalize(raw, "阴", pct, inten, notes)
            notes.append("ASCO/CAP ≥1% 判阳")
            return _finalize(raw, "阳", pct, inten, notes)
        notes.append("阳性表述" if pct is None else "阳性表述(含百分比)")
        return _finalize(raw, "阳", pct, inten, notes)
    if re.search(r"(少量|散在表达|局灶表达|弱表达)", s):
        notes.append("低量表达表述")
        return _finalize(raw, "阳", pct, inten, notes)
    if pct is not None:
        if kind == "hormone":
            if pct < 1:
                notes.append("仅百分比且 <1% → 阴")
                return _finalize(raw, "阴", pct, inten, notes)
            notes.append("仅百分比且 ≥1% → 阳（无强度描述）")
            return _finalize(raw, "阳", pct, inten, notes)
        notes.append("仅百分比，无阴阳表述")
        return _finalize(raw, "阳", pct, inten, notes)
    notes.append("表述无法识别")
    return _finalize(raw, "未解析", None, inten, notes)

def detect_markers(raw_panel):
    out = OrderedDict()
    if not isinstance(raw_panel, dict):
        return out
    for mk, mv in raw_panel.items():
        res = parse_ihc(mv, mk)
        if res is None:
            continue
        out[str(mk)] = res
    return out

# ---------------------------------------------------------------- 状态投影
# 归一状态 → 分析用二/三值投影。**全工作空间唯一映射表**，各分析脚本只允许调用
# status_bin()，不得自行维护映射（历史上 6 份副本互相漂移，是「同一指标两套数字」的成因之一）。
BIN_MAP = {
    "阳": "阳", "高表达": "阳",
    "阴": "阴", "低表达": "阴",
    "不确定": "不确定",
    "缺失": "未知", "未解析": "未知",
}

_ABSENT_LITERALS = ("无", "未做", "无片", "不详", "无片可读")

def status_bin(v, marker="", keep_unknown=True):
    miss = "未知" if keep_unknown else None
    if v is None:
        return miss
    s = str(v).strip()
    if s == "" or s in _ABSENT_LITERALS:
        return miss
    direct = BIN_MAP.get(s)
    if direct is not None:
        return direct
    try:
        r = parse_ihc(s, marker)
    except Exception:
        return miss
    if r is None:
        return miss
    out = BIN_MAP.get(r["status"], "未知")
    return miss if (out == "未知" and not keep_unknown) else out

# ---------------------------------------------------------------- 自检
CASES = [
    # ---- HER2：ASCO/CAP 判读点 ----
    ("HER2", "3+", "阳", "ASCO/CAP 3+"),
    ("HER2", "1+", "阴", "1+ 视为阴"),
    ("HER2", "2+", "不确定", "2+ 须 ISH"),
    ("HER2", "0", "阴", "无着色"),
    ("HER2", "阴阳对照准确", "缺失", "纯质控语"),
    ("HER2", "2+，阴性、阳性对照准确", "不确定", "剥质控语后仍为 2+"),
    ("HER2", "3+，阴性、阳性对照准确", "阳", "剥质控语后 3+"),
    ("HER2", "3+，90%有阳性对照", "阳", "残留未收录质控片段但值有效"),
    ("HER2", "导管内癌呈3+，浸润癌呈2+，建议来我科行FISH检查", "不确定", "双评分"),
    ("HER2", "导管内癌3+", "阳", "单评分 3+"),
    ("HER2", "原位癌成分1+", "阴", "单评分 1+"),
    ("HER2", "0，无任何阳性信号", "阴", "0 分且否定阳性"),
    ("HER2", "0，个别癌细胞+", "不确定", "0 与 + 冲突"),
    ("HER2", "无扩增", "阴", "FISH 无扩增"),
    ("HER2", "FISH检测显示基因扩增", "阳", "FISH 扩增"),
    ("HER2", "3+，建议FISH检测", "阳", "3+ 已确定，仅建议检测不算结论"),
    ("HER2", "1-2 +", "不确定", "区间评分"),
    ("HER2", "+2", "不确定", "倒序书写 +2"),
    ("HER2", "+3", "阳", "倒序书写 +3"),
    ("HER2", "3", "阳", "裸数字评分 3"),
    ("HER2", "2", "不确定", "裸数字评分 2"),
    # ---- ER / PR ----
    ("ER", "强+，约90%", "阳", "ER 90%"),
    ("ER", "弱+，约5%", "阳", "ER 5% ≥1%"),
    ("ER", "阴性", "阴", "明确阴性"),
    ("ER", "-", "阴", "短横线"),
    ("ER", "0%", "阴", "0% <1%"),
    ("ER", "0", "阴", "裸 0"),
    ("ER", "阳性对照准确", "缺失", "纯质控语"),
    ("ER", "-,内设对照", "阴", "残留质控但值有效"),
    ("PR", "约1%", "阳", "边界 1%"),
    ("PR", "--", "阴", "双横线"),
    ("PR", "强弱不一", "不确定", "表述不明确"),
    # ---- Ki67：不得压成阳/阴 ----
    ("Ki67", "约40%+", "高表达", "保留数值"),
    ("Ki67", "约5%", "低表达", "低表达"),
    ("Ki67", "+，约30%", "高表达", "含+但以数值为准"),
    ("Ki67", "约40+", "高表达", "无%号按百分数处理"),
    ("Ki67", "未见异常表达", "低表达", "无百分比的正常表达"),
    # ---- 语义专项 ----
    ("D2-40", "片内均未见脉管内癌栓", "阴", "脉管侵犯阴性"),
    ("D2-40", "提示无脉管内癌栓", "阴", "脉管侵犯阴性"),
    ("CD34", "+，可见脉管癌栓", "阳", "脉管侵犯阳性"),
    ("CD34", "显示片内未见脉管瘤栓", "阴", "脉管侵犯阴性"),
    ("P63", "-，提示未见神经侵犯", "阴", "神经侵犯阴性"),
    ("P63", "示肌上皮缺失", "阴", "肌上皮缺失"),
    ("Calponin", "示肌上皮存在", "阳", "肌上皮存在"),
    ("CK5/6", "肌上皮完整", "阳", "肌上皮完整"),
    ("SMA", "部分区肌上皮消失", "阴", "部分区肌上皮消失"),
    ("P53", "突变型", "阳", "P53 突变型"),
    ("P53", "野生型", "阴", "P53 野生型"),
    ("P53", "强+，约90%", "阳", "其他标记物常规判读"),
    ("P63", "部分+", "阳", "部分着色"),
    ("Calponin", "小灶-", "不确定", "部分性表述不整体定性"),
    ("AR", "阴阳对照准确", "缺失", "质控语"),
    ("Ki67", "待重然后报", "缺失", "未出结果"),
    ("ER", "肿瘤细胞少，无法判读", "缺失", "不可判读"),
    # ---- 补充规则 ----
    ("CK", "+++", "阳", "通用标记物极强阳性"),
    ("EMA", "++", "阳", "通用标记物强阳性"),
    ("HER2", "３＋", "阳", "全角字符归一后 3+"),
    ("HER2", "4B5 Ventana", "缺失", "仅抗体克隆号"),
    ("ER", "SP1", "缺失", "仅抗体克隆号"),
    ("E-cadherin", "癌细胞-、内对照及外对照表达正常", "阴", "剥离内外对照语"),
    ("P120", "示浸润性癌", "缺失", "字段错位:诊断结论"),
    ("P63", "支持导管上皮来源", "缺失", "字段错位:诊断结论"),
    ("CK5/6", "-部分脱片", "缺失", "技术原因不可判读"),
    ("D2-40", "提示无血管侵犯", "阴", "血管侵犯阴性"),
    ("AR", "-强", "不确定", "阴性符号与强度并存"),
    ("P53", "+95，强", "阳", "省略%号百分比"),
    ("CK7", "膜++", "阳", "膜着色强阳"),
    ("CD10", "膜", "不确定", "仅记录着色部位"),
    ("SMA", "3项", "不确定", "表述不明确"),
    ("AR", "一,外对照表达正常", "阴", "全角一字表阴性"),
    ("S-100", "NOS", "缺失", "仅试剂/分类标识"),
    ("ER", "+90%，中-强", "阳", "连字符是强度区间，不是阴性符号"),
    ("ER", "中-强+，约80%", "阳", "强度区间表述"),
    ("ER", "80%，中-强阳", "阳", "强度区间+阳性词"),
    ("ER", "阳性对照准确", "缺失", "纯质控语（回归）"),
    ("HER2", "阴阳对照准确", "缺失", "纯质控语（回归）"),
    ("ER", "-,内设对照", "阴", "残留质控但值有效（回归）"),
]

# status_bin 投影自检：(marker, 输入, 期望投影)
BIN_CASES = [
    ("ER", "阳", "阳"), ("ER", "阴", "阴"),
    ("Ki67", "高表达", "阳"), ("Ki67", "低表达", "阴"),
    ("ER", "不确定", "不确定"),
    ("ER", "缺失", "未知"), ("ER", "未解析", "未知"),
    ("ER", "", "未知"), ("ER", None, "未知"), ("ER", "未做", "未知"),
    ("ER", "-", "阴"),            # 裸负号是真实阴性（曾因归 other 被剔出分母）
    ("ER", "er-", "阴"),
    ("ER", "1+", "阳"),           # ER/PR：≥1% 着色即为阳
    ("ER", "2+", "阳"),           # 激素受体无「2+ 须 ISH」规则
    ("HER2", "1+", "阴"),         # HER2：1+ 为阴
    ("HER2", "2+", "不确定"),     # HER2：2+ 须 ISH
    ("HER2", "3+", "阳"),
    ("Ki67", "约30%+", "阳"),     # ≥20% 高表达
    ("Ki67", "约10%+", "阴"),     # <20% 低表达
]

def selftest(verbose=False):
    ok = bad = 0
    for mk, raw, exp, why in CASES:
        r = parse_ihc(raw, mk)
        got = r["status"] if r else None
        if got == exp:
            ok += 1
        else:
            bad += 1
            print("FAIL  %-8s %-42r 期望=%-4s 实得=%-4s  (%s)" % (mk, raw, exp, got, why))
        if verbose:
            print("     %-8s %-40r -> %-4s | %s" % (mk, raw[:40], got, r["note"]))
    print("\n自检：%d 通过 / %d 失败（共 %d 例）" % (ok, bad, len(CASES)))
    bok = bbad = 0
    for mk, raw, exp in BIN_CASES:
        got = status_bin(raw, mk)
        if got == exp:
            bok += 1
        else:
            bbad += 1
            print("FAIL  status_bin(%r, %s) 期望=%s 实得=%s" % (raw, mk, exp, got))
    print("status_bin 投影自检：%d 通过 / %d 失败（共 %d 例）" % (bok, bbad, len(BIN_CASES)))
    return bad + bbad

if __name__ == "__main__":
    raise SystemExit(1 if selftest() else 0)
