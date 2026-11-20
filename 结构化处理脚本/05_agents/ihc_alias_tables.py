# -*- coding: utf-8 -*-

import re

# ---------------------------------------------------------------------------
# 一、标准名注册表（诊断鉴别型全收，只记原值，不出 call）
# ---------------------------------------------------------------------------
CANON_BASE = [
    # 治疗决策型
    "ER", "PR", "HER2", "HER2(FISH)", "Ki-67",
    # 上皮 / 肌上皮
    "CK", "CK5", "CK5/6", "CK6", "CK7", "CK8", "CK8/18", "CK14", "CK17", "CK18",
    "CK19", "CK20", "AE1/AE3", "CAM5.2", "Pan-CK", "HMW", "EMA", "E-cadherin",
    "P120", "β-catenin", "Calponin", "SMA", "SMMHC", "MSA", "Caldesmon",
    "Myosin", "Actin", "Desmin", "Vimentin", "Myogenin", "MyoD1",
    # 乳腺 / 腺上皮鉴别
    "GATA-3", "Mammaglobin", "GCDFP15", "ERBB2", "SOX10", "TRPS1", "FOXA1", "FOXC1",
    "BCSG1", "S100P", "S-100", "HMB-45", "Melan-A", "WT1", "PAX5", "PAX-8", "TTF-1",
    "Napsin A", "HepPar-1", "Hepatocyte", "Glypican-3", "Arginase-1", "CDX-2",
    "Villin", "MUC1", "MUC2", "MUC4", "MUC5AC", "MUC6", "CA15-3", "CA125", "CA19-9",
    # 神经内分泌 / 神经
    "Syn", "CgA", "INSM1", "Calcitonin", "TG", "CEA", "CD56", "GFAP", "SSTR2",
    # 淋巴 / 造血
    "CD1", "CD3", "CD4", "CD5", "CD7", "CD8", "CD10", "CD15", "CD20", "CD21",
    "CD24", "CD30", "CD31", "CD34", "CD38", "CD44", "CD45", "CD68", "CD79a",
    "CD99", "CD117", "CD138", "CD163", "MPO", "TdT", "Kappa", "Lambda", "IgG4",
    "BCL-2", "BCL-6", "MUM1", "PD-1", "PD-L1", "CTLA-4", "Foxp3",
    # 增殖 / 抑癌 / 分子
    "P16", "P40", "P53", "P63", "PTEN", "CyclinD1", "C-myc", "MDM2", "CDK4",
    "STAT6", "ALK", "ROS1", "MLH1", "MSH2", "MSH6", "PMS2", "BRG1", "INI1", "ATRX",
    "TOP2A", "TopoIIα", "PHH3", "BRCA1", "BRCA2", "ESR1", "PGR", "HER1",
    "SATB2", "Calretinin", "RB1", "Inhibin", "LCA", "CT", "CD45", "ALDH1", "INI1",
    # 血管 / 淋巴管 / 间质
    "D2-40", "Podoplanin", "F8", "ERG", "VEGF", "ACTA2", "CNN1", "COL4",
    "CD34/D2-40", "SMA/Calponin", "CK5/6、P63、Calponin",
    # 其他实质标志物
    "AR", "Ber-EP4", "c-KIT", "DOG1", "EBER", "EBERs", "Nectin-4", "IgG",
    "ACTH", "PTHrP", "HBsAg", "HBcAg", "FSH", "LH", "PRL", "PROG", "TESTO", "ESTRDL",
    "MHC", "TRDS1",
]

# 机械变体索引：去掉所有非字母数字后比较（CD79A↔CD79a、SOX-10↔SOX10、WT-1↔WT1、HMB45↔HMB-45）
IDX = {}
for _c in CANON_BASE:
    IDX[re.sub(r"[^A-Z0-9]", "", _c.upper())] = _c

# ---------------------------------------------------------------------------
# 二、显式别名（机械索引覆盖不到的：中文、拼写错、缩写、带后缀）
# ---------------------------------------------------------------------------
ALIAS = {
    # ---- HER2 家族（治疗决策型，必须归一，别名缺口会造成 HER2 判读落空）
    "CERBB2": "HER2", "CERBB-2": "HER2", "C-ERBB-2": "HER2", "CERBB2(HER2)": "HER2",
    "HER2/NEU": "HER2", "HER-2/NEU": "HER2", "HER2/CERBB-2": "HER2",
    "CERBB-2/HER-2": "HER2", "HER2/HER-2": "HER2", "HER-2/HER2": "HER2",
    "C-ERBB2": "HER2", "C-ERB-2": "HER2", "CERBB-2/HER2": "HER2",
    "HER2_IHC": "HER2", "HER-2/IHC": "HER2", "HER2IHC": "HER2", "HER2-IHC": "HER2",
    "HER2基因": "HER2(FISH)", "HER-2基因": "HER2(FISH)",
    # ---- ER / PR / AR 带修饰后缀
    "ESTROGENRECEPTOR": "ER", "PROGESTERONERECEPTOR": "PR",
    "HER2PERCENT": "HER2", "ERPCT": "ER", "ERP": "ER", "PRP": "PR",
    # ---- Ki-67 变体
    "K-67": "Ki-67", "KI67": "Ki-67", "KI-67": "Ki-67", "MKI67": "Ki-67",
    # ---- 细胞角蛋白
    "CYTOKERATIN": "CK", "CK-PAN": "Pan-CK", "PAN-CK": "Pan-CK", "CKPAN": "Pan-CK",
    "CK-H": "HMW", "CKH": "HMW", "CK-L": "CK", "34ΒE12": "HMW", "CK34BE12": "HMW",
    "CK34ΒE12": "HMW", "KC5/6": "CK5/6", "K5/6": "CK5/6", "CK8/18": "CK8/18",
    "CYTOKERATIN5/6": "CK5/6",
    # ---- 上皮膜抗原 / 钙黏蛋白
    "EPITHELIALMEMBRANEANTIGEN": "EMA", "E-CAD": "E-cadherin", "ECAD": "E-cadherin",
    "CADHERIN": "E-cadherin", "P120CATENIN": "P120", "P120CTN": "P120",
    "CATENIN-Β": "β-catenin", "CATENIN-B": "β-catenin", "B-CATENIN": "β-catenin",
    "BETACATENIN": "β-catenin", "BETA-CATENIN": "β-catenin", "Β-CATENIN": "β-catenin",
    "CATENIN": "β-catenin",
    # ---- 腺上皮 / 肌上皮
    "CALPOLIN": "Calponin", "CALPONI": "Calponin", "CALPONIN": "Calponin",
    "SMOOTHMUSCLEMYOSINHEAVYCHAIN": "SMMHC", "SM-MHC": "SMMHC", "SMM-HC": "SMMHC",
    "MYOSIN-HC": "SMMHC", "HMWCK": "HMW",
    # ---- 标志性鉴别标志物
    "HEPATOCYTE": "HepPar-1", "HEP-1": "HepPar-1", "HEPPAR1": "HepPar-1",
    "GCDFP": "GCDFP15", "GCDPF": "GCDFP15", "GCPDF-15": "GCDFP15",
    "GCDFP-15": "GCDFP15", "MAAAGLOBIN": "Mammaglobin", "MAMMAGLOBIN": "Mammaglobin",
    "CHROMOGRANINA": "CgA", "CHROMOGRANIN": "CgA", "SYNAPTOPHYSIN": "Syn",
    "SNY": "Syn", "NAPSINA": "Napsin A", "NAPSIN-A": "Napsin A",
    "GLYPICAN3": "Glypican-3", "GLYPICAN-3": "Glypican-3",
    "ARGINASE-1": "Arginase-1", "TTF1": "TTF-1", "PAX8": "PAX-8", "PAX-5": "PAX5",
    "DOG-1": "DOG1", "C-KIT": "c-KIT", "CD-40": "CD40", "CDX2": "CDX-2",
    "HER-2/HER2（IHC）": "HER2",
    # ---- 拓扑异构酶 / 波形蛋白 / 钙调蛋白
    "TOPOII": "TopoIIα", "TOPOIIA": "TopoIIα", "TOPOLL": "TopoIIα",
    "TOPOⅡ": "TopoIIα", "TOPOⅡΑ": "TopoIIα", "TOPII": "TopoIIα",
    "TPOIIA": "TopoIIα", "TOP2A": "TOP2A", "MASSAGLOBIN": "Mammaglobin",
    "VMENTIN": "Vimentin", "VIMENTIN": "Vimentin",
    # ---- 中文（仅教科书等价；其余中文名留 unresolved，不强行合并）
    "细胞角蛋白": "CK", "细胞角蛋白(CK)": "CK", "细胞角蛋白(广谱)": "CK",
    "细胞角蛋白（广谱）": "CK", "细胞角蛋白(广谱/pan-CK)": "CK",
    "广谱细胞角蛋白": "CK", "细胞角蛋白19": "CK19", "细胞角蛋白19(CK19)": "CK19",
    "细胞角蛋白7": "CK7", "细胞角蛋白20": "CK20", "细胞角蛋白8/18": "CK8/18",
    "细胞角蛋白5/6": "CK5/6",
    "上皮膜抗原": "EMA", "上皮膜抗原(EMA)": "EMA",
    "表皮生长因子受体": "EGFR", "波形蛋白": "Vimentin",
    "波形蛋白(Vimentin)": "Vimentin", "P63蛋白": "P63", "P53蛋白": "P53",
    "肿瘤蛋白p53": "P53", "p53抑癌蛋白": "P53", "P16蛋白": "P16", "P16抑癌蛋白": "P16",
    "E-ca膜": "E-cadherin", "E-Ca膜": "E-cadherin", "E钙黏蛋白": "E-cadherin",
    "E-钙粘蛋白": "E-cadherin", "E-钙粘附蛋白": "E-cadherin",
    "E-钙黏附蛋白": "E-cadherin", "E-ca膜（E-cadherin）": "E-cadherin",
    "P120连环蛋白": "P120", "P120连蛋白": "P120", "P120浆": "P120",
    "P120膜蛋白": "P120", "P120连环素": "P120", "P120蛋白": "P120",
    "P120钙黏蛋白": "P120", "P120胞膜": "P120", "P120膜浆": "P120",
    "P120连接蛋白": "P120", "雄激素受体": "AR", "雄激素受体(AR)": "AR",
    "CD34抗原": "CD34", "CD34血管标志": "CD34", "CD10抗原": "CD10",
    "GATA结合蛋白3": "GATA-3", "平滑肌肌球蛋白重链": "SMMHC",
    "平滑肌肌动蛋白": "SMA", "钙蛋白": "Calponin", "平滑肌钙蛋白": "Calponin",
    "钙调宁蛋白": "Calponin", "Calponin蛋白": "Calponin", "Calponin肌上皮": "Calponin",
    "S-100蛋白": "S-100", "S100P（S100钙结合蛋白P）": "S100P",
    "D2-40蛋白": "D2-40", "D2-40抗原": "D2-40", "D2-40淋巴管标志": "D2-40",
    "降钙素": "Calcitonin", "癌胚抗原": "CEA", "突触素": "Syn", "结蛋白": "Desmin",
    "β-连环蛋白": "β-catenin", "甲状腺转录因子-1": "TTF-1",
    "雌激素受体ER": "ER", "ER雌激素受体": "ER", "孕激素受体PR": "PR",
    "PR孕激素受体": "PR", "人表皮生长因子受体2HER2": "HER2",
    "增殖指数Ki-67": "Ki-67", "Ki-67标记指数": "Ki-67", "IV胶原": "COL4",
    # 疑似截断：全量普查里 63 与 63/P63、P63-2 共现，判为 P63
    "63": "P63",
    "LCA": "CD45", "P120-CATENIN": "P120", "E-CAHERIN": "E-cadherin",
    "EBFR": "EGFR", "EFR": "EGFR", "D-40": "D2-40", "D2-40PODOPLANIN": "D2-40",
    "CERBB-2(HER2)": "HER2", "C-ERB-2(HER-2)": "HER2", "CERBB-2/HER-2": "HER2",
    "RB": "RB1", "CT": "Calcitonin", "INHIBIN": "Inhibin",
    "INI": "INI1", "D2-40/PODOPLANIN": "D2-40",
    "巨囊性疾病液体蛋白-15": "GCDFP15", "GCDFP-15（巨囊病液体蛋白15）": "GCDFP15",
}

# 中文名归一（去括号/空格后再次比对，处理「X（Y）」写法）
CN2EN = {k: v for k, v in ALIAS.items() if re.search(r"[\u4e00-\u9fff]", k)}

# ---------------------------------------------------------------------------
# 三、非标志物桶
# ---------------------------------------------------------------------------
STAIN_CANON = {
    "PAS": "PAS染色", "PAS特殊染色": "PAS染色", "D-PAS": "D-PAS染色",
    "DPAS": "D-PAS染色", "AG": "Ag嗜银染色", "银染": "Ag嗜银染色",
    "嗜银染色": "Ag嗜银染色", "抗酸": "抗酸染色", "奥辛蓝": "阿辛蓝染色",
    "阿辛蓝": "阿辛蓝染色", "MASSON": "Masson三色染色", "铁染": "铁染色",
    "铜染": "铜染色", "弹力纤维染色": "弹力纤维染色",
}
STAIN_RE = re.compile(r"(染色|特染|银染|抗酸|奥辛蓝|阿辛蓝|MASSON|PAS|(^|[^A-Z])AG([^A-Z]|$))", re.I)

def _fk(s):
    return re.sub(r"[\s/·、]", "", str(s or "").upper())

FISH_CANON = {
    # key 必须与 _fk() 的输出一致，否则查表恒不命中
    _fk(k): v for k, v in {
        "HER2/CEP17比值": "HER2/CEP17比值", "HER2/CEP17": "HER2/CEP17比值",
        "HER2CEP17RATIO": "HER2/CEP17比值", "HER2/CEP17RATIO": "HER2/CEP17比值",
        "CEP17拷贝数": "CEP17拷贝数", "CEP17均值": "CEP17拷贝数",
        "CEP17状态": "CEP17状态", "CEP17倍体性": "CEP17倍体性", "二倍体性": "CEP17倍体性",
        "平均HER2拷贝数": "HER2平均拷贝数", "HER2平均拷贝数": "HER2平均拷贝数",
        "HER2拷贝数": "HER2平均拷贝数", "平均HER2拷贝数/细胞": "HER2平均拷贝数",
        "17号染色体(CEP17)": "CEP17拷贝数", "HER2基因拷贝数/细胞": "HER2平均拷贝数",
        "HER2扩增状态": "HER2扩增状态", "HER-2评分": "HER2扩增状态",
    }.items()
}
FISH_RE = re.compile(r"(CEP|/CEP|比值|拷贝数|均值|扩增状态|倍体|基因检测)", re.I)
# 度量词：出现即说明这个字面量是"某项检测的读数"，不是标志物本身
METRIC_RE = re.compile(r"(比值|拷贝数|均值|倍体|扩增状态|信号数|拷贝/细胞)")

# 字段错位 / 非标志物内容（进 unresolved，出统计）
NON_MARKER_RE = re.compile(
    r"(切缘|淋巴结|脉管|癌栓|瘤栓|前哨|病理诊断|病理结果|诊断结论|组织学|分级|分型|"
    r"备注|原值|未提及|待定|参见|未补充|免疫组化|增生指数|分子分型|周围乳腺|余乳腺|"
    r"乳头|基底|皮肤|癌累及|肌上皮|肌酸激酶|肌动蛋白结合蛋白|21基因|ONCOTYPE|"
    r"RECURRENCESCORE|MILLER|BI-RADS|原位癌|浸润灶|导管内癌|^LN\d|^GYN$|^TMB$|"
    r"^PI3K$|^TNBC$|^IHC$|^CK?IHC$|复发风险评分|冰冻病理|病理类型|待后|待定|"
    r"未出|部位|未行IHC|左乳|标本|钙蛋白酶抑制蛋白|钙调蛋白结合蛋白)", re.I)
# 无信息量的短串 / 序号 / 实验号
JUNK_RE = re.compile(r"^(R|E|MC|EP|PER|CR|HR|D10|LN\d|\d+|\d+\+|H\d{5,})$")
# 模态后缀（英文 `_percent` 与中文「染色强度/百分比」同义），剥掉后主体仍是同一标志物
# 注意：英文后缀必须带分隔符，否则 "PR" 会被当成 "PR_percent" 的后缀整串吃掉（已踩过）
SUFFIX_RE = re.compile(
    r"(?:[_\-\s](?:PERCENT|PCT|PR|RE|REPEAT\d?|CALL|NOTE|REF|INTENSITY|IHC|NST|IMP|"
    r"INVASIVE|DCIS|LN|BASAL|RPT\d?)|"
    r"(?:染色强度|强度|百分比|阳性细胞比例|阳性比例|弱阳比例|表达率|比例|"
    r"标记指数|指数|原位癌|浸润灶|导管内癌))+$", re.I)
ISH_RE = re.compile(r"(FISH|ISH|原位杂交|荧光原位杂交)", re.I)

def clean(lit):
    s = str(lit or "").replace("\u200b", "").replace("\u3000", " ")
    s = s.replace("＋", "+").replace("＊", "*").replace("#", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s

def _strip_gloss(s):
    return re.sub(r"[（(][^（()）]{0,30}[)）]\s*$", "", s).strip()

def _hit(s):
    if not s:
        return None
    if s in ALIAS:
        return ALIAS[s]
    up = s.upper()
    if up in ALIAS:
        return ALIAS[up]
    k = re.sub(r"[^A-Z0-9]", "", up)
    if k in IDX:
        return IDX[k]
    g = _strip_gloss(s)
    if g and g != s:
        if g in ALIAS:
            return ALIAS[g]
        kg = re.sub(r"[^A-Z0-9]", "", g.upper())
        if kg in IDX:
            return IDX[kg]
        if g in CN2EN:
            return CN2EN[g]
    return None

def _fish(s):
    k = _fk(s)
    if k in FISH_CANON:
        return FISH_CANON[k]
    # 比值优先于 CEP：HER2/CEP17 比值里也含 CEP
    if "比值" in s or "RATIO" in s.upper():
        return "HER2/CEP17比值"
    if "CEP" in s.upper():
        return "CEP17拷贝数"
    if "拷贝数" in s:
        return "HER2平均拷贝数"
    return "HER2扩增状态"

def classify(lit):
    s = clean(lit)
    if not s:
        return "", "empty"

    # 1) 原位杂交类（必须在去括号之前，否则 HER2(FISH) 会褪成 HER2）
    #    bucket 用 ish 而不是 special：原位杂交是 IHC 面板里的正式检测项，
    #    留在 markers[] 里（原值 + status 都有意义），不能当特殊染色移走。
    if ISH_RE.search(s):
        return "HER2(FISH)", "ish"

    # 2) 标志物直接命中
    h = _hit(s)
    if h:
        # 例外：带度量词的（平均HER2拷贝数 / HER2/CEP17比值）是度量值不是标志物，
        # 否则会被 "HER2" 顺带命中，把 FISH 指标塞进 marker
        if METRIC_RE.search(s):
            return _fish(s), "fish"
        return h, "marker"

    # 3) 剥模态后缀后命中（ER_percent / ER染色强度 / Ki-67_原位癌 / CK_repeat1）
    base = SUFFIX_RE.sub("", s).strip("_- ")
    if base and base != s:
        h = _hit(base)
        if h:
            return h, "marker"

    # 4) FISH 度量值（比值 / 拷贝数 / CEP / 扩增状态）——不是标志物
    if FISH_RE.search(s):
        k = _fk(s)
        if k in FISH_CANON:
            return FISH_CANON[k], "fish"
        # 比值优先于 CEP：HER2/CEP17 比值里也含 CEP，先判 CEP 会被吞成拷贝数
        if "比值" in s or "RATIO" in s.upper():
            return "HER2/CEP17比值", "fish"
        if "CEP" in s.upper():
            return "CEP17拷贝数", "fish"
        if "拷贝数" in s:
            return "HER2平均拷贝数", "fish"
        return "HER2扩增状态", "fish"

    # 5) 特殊染色（PAS / Ag 嗜银 / 抗酸 / 阿辛蓝 / Masson）
    if STAIN_RE.search(s):
        cut = re.sub(r"[（(][^）)]*[)）]", "", s).replace("特殊染色", "").replace("特染", "").strip()
        cu = cut.upper()
        if cu in STAIN_CANON:
            return STAIN_CANON[cu], "stain"
        if cu.startswith("AG"):
            return "Ag嗜银染色", "stain"
        if cu.startswith("PAS") or cu.startswith("D-PAS") or cu.startswith("DPAS"):
            return "PAS染色", "stain"
        return cut or s, "stain"

    # 6) 非标志物（切缘 / 淋巴结 / 分级 / 占位词）
    if NON_MARKER_RE.search(s):
        return s, "non_marker"

    # 7) 噪声短串
    if JUNK_RE.match(s.upper()):
        return s, "non_marker"

    return s, "unmapped"
