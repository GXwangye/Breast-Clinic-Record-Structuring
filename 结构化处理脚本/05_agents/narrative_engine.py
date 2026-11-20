# -*- coding: utf-8 -*-

import json
import os
import re

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
STD_DIR = os.path.join(DATA, "中间产物/standardized_v1")

# 字段映射：standardized 中文字段 → assembled_v7 英文板块
FIELD_TO_SECTION = {
    "主诉": "chief_complaint",
    "现病史": "present_illness",
    "既往史": "past_history",
    "体格检查": "physical_exam",
}
SEC_FIELD = {v: k for k, v in FIELD_TO_SECTION.items()}
NARRATIVE_FIELDS = ("主诉", "现病史", "既往史", "体格检查")

SIDES = {"左", "右", "双", "未指明"}
CARE_KINDS = {"检查", "诊断", "治疗"}

# ---------------------------------------------------------------------------
# 细颗粒抽取 system prompt（与 restore_narrative.py 的 SYS 完全一致）
# ---------------------------------------------------------------------------
NARRATIVE_SYS = """你是病历叙述板块结构化子智能体。输入是 4 个字段（主诉/现病史/既往史/体格检查）的分段列表，每段已标注 [字段名 #起止] 和段首原文。你的任务是把每段的语义展开成结构化条目。

输出一个 JSON 对象（四个键，键名固定）：
{"chief_complaint": {...}, "present_illness": {...}, "past_history": {...}, "physical_exam": {...}}

各键结构（严格按此）：
chief_complaint = {"symptoms": [{text, std, side, duration, onset, anchor}]}
present_illness = {"symptoms": [{text, std, side, duration, character, trend, neg, anchor}],
                   "care_history": [{time, kind, detail, place, anchor}],
                   "medications": [{name, dose, duration, anchor}]}
past_history = {"care_history": [{time, kind, detail, place, anchor}],   ← 既往治疗史（化疗/放疗/靶向/内分泌/疗程）
                "medications": [{name, dose, duration, anchor}],          ← 既往用药史
                "comorbidities": [{name, std, icd10, status, anchor}],
                "surgeries": [{name, date, anchor}],
                "menstrual": {menarche, lmp, menopause_age, gravida, para},
                "family": {text, cancer_history}}
physical_exam = {"breast": [{side, site, finding, size, texture, border, mobility, skin, nipple, anchor}],
                 "lymph_nodes": [{region, side, size, nature, anchor}],
                 "general": [{finding, anchor}],
                 "vitals": {T, P, R, BP}}

anchor = {"field": "中文段字段名(主诉/现病史/既往史/体格检查)", "seg_start": 整数, "quote": "该条目对应的段内原文逐字片段(6~80字)"}

硬规则：
1) 每条结构化条目必须给 anchor，且 quote 必须在 anchor.field 所指字段、seg_start 所指的那一段原文中逐字命中（段列表里 [字段 #起止] 后的原文即该段）。禁止跨段拼接、禁止改写一字。
2) side 只允许 左/右/未指明（主诉、现病史、体格检查用）；双侧各出一条或一条标 未指明 均可，但不许造 双 以外的值。既往史 comorbidities 无 side。
3) std 是标准词（如「疼痛」之于「疼」），写不出留空字符串，不臆造。
4) icd10 只在确定时填（如 高血压→I10、2型糖尿病→E11.9），不确定留空。
5) neg=true 仅当原文明确否定（如「无疼痛」）；「没写」一律 neg=false 且不杜撰条目。三态显式：未提及就不出该条目。阴性症状也是信息：「无疼痛/无发热/无咳嗽」等明确否定表述必须抽取为对应症状条目并标 neg=true，禁止丢弃。
6) care_history.kind 只允许 检查/诊断/治疗；月经婚育信息无论出现在哪个字段都要提取填入 menstrual（见规则14），四字段均无对应文字时才给空对象 {}；vitals 只在体格检查有 T/P/R/BP 数值时填，否则 {}。
6a) 否认病史防翻转（最高优先级）：「否认高血压史」「无糖尿病史」这类否定表述，若抽取则 status 必须为 否认，且 quote 必须包含「否认/无」字样；禁止剥掉否定词当成阳性合并症。comorbidities.status 只允许 现症/既往/否认。
6b) 体格检查的一般情况（神志清楚、对答切题、一般情况可、皮肤巩膜等乳房与淋巴结以外的查体所见）入 general 数组，finding 写原文要点；禁止因无处安放而丢弃。
7) 空字段输出空数组/空对象。只输出一个合法 JSON 对象，禁止解释文字，禁止 ``` 围栏，字符串内引号换行必须转义。
8) 条目要细：「头晕、头痛，伴恶心」应拆成 头晕/头痛/恶心 三条，各自带 anchor；持续时间/大小/部位等修饰语归入对应条目的 duration/size/site，不单独成条。
9) 完整性硬规则：某字段原文有实质内容（「无/无特殊/未见异常」等空值除外）时，该字段至少产出 1 条条目，禁止因"不像典型症状"而输出空数组。常见易漏形态：主诉「左乳腺癌术后9个月」是就诊状态，入 symptoms（std 可留空）；现病史的化疗方案/周期/药物剂量（如 PCb-EC、第6周期、表柔比星125mg）入 care_history 与 medications；既往史的手术史（术式+时间）入 surgeries，合并症入 comorbidities；体格检查的术后改变、疤痕、红肿、触诊肿物入 breast。
10) 逐段覆盖：输入的每一段原文，只要含临床信息，就必须被至少 1 条条目覆盖（anchor 指向该段）；禁止只处理前几段、跳过后面的段。
11) detail 完整：检查/病理/手术类条目的 detail 须覆盖对应原文的全部要点，阴性所见（如「未见脉管及神经侵犯」「切缘未见癌」「大汗腺化生」）同样是要点，禁止只摘主诊断；一段含多个独立要点时拆成多条，各自带 anchor。
12) 三态显式（禁止默认值）：原文未提及家族史时 family 必须给空对象 {}，禁止输出 cancer_history=false 这类默认值；原文无月经婚育史文字时 menstrual 给 {}；原文无 T/P/R/BP 数值时 vitals 给 {}。「没写」不得落成阴性结论。
13) 类别归位：非症状内容禁止入 symptoms。检查需求/拟行检查/建议或要求做某检查 → care_history(kind=检查)；术后状态（如「乳腺癌术后2年余」）在主诉入 symptoms 的同时，必须在现病史/既往史有对应文字处产出 care_history（诊断或治疗）或 surgeries 条目，禁止只在主诉留一条、其余板块留空。
14) 跨字段落点：月经婚育信息（初潮/周期/经期/经量/LMP/绝经/孕产）无论出现在哪个字段，一律填入 past_history.menstrual；就诊目的句（「今日为取药及复查来诊」「要求复查」「咨询来诊」等）入 care_history（取药/复查/咨询按 kind=检查或治疗归最接近者），禁止当连接语丢弃。
15) 侧别无依据不补全：原文未指明侧别的条目（如「免疫组化未回」「双乳对称」）side 必须为 未指明；「双乳/双侧」表述的查体所见 side 必须为 双，禁止标单侧。
16) 属性就地取材：条目的 onset/duration/time/dose 等属性只能填该条目 anchor.quote 中有明确文字支撑的内容；quote 中无对应文字一律留空字符串，禁止从邻近条目、其他段或其他板块借值抄值（实测缺陷：主诉仅「发现乳房结节2年余」时 onset 填「2年前体检」属借值；内分泌/护骨治疗被抄上化疗的「第5周期」属抄值，均禁止）。计划性内容（「需行/拟行/计划」）time 一律留空。
17) 板块归属：条目必须放在 anchor.field 所指字段对应的板块下（anchor.field=体格检查 的条目只能进 physical_exam，禁止塞进 present_illness/chief_complaint 的数组）。唯一例外是规则18 的 past_history 落点区。
18) 手术史跨字段落点：手术史信息（「…术术后改变」「行…术」「术后X年」等）无论出现在哪个字段（最常见于体格检查），都要在 past_history.surgeries 产出条目（name=术式、date 无则留空、anchor 指实际出现该文字的字段与段）；既往史写「无/无特殊」不构成阻碍。同理，合并症（高血压/糖尿病等）无论写在哪个字段都进 past_history.comorbidities，anchor 指实际字段。
19) 空壳禁令：条目内容键全空（如 lymph_nodes 的 region/size/nature 皆空、breast 的 finding 空）时禁止输出该条目；anchor 不是摆设，无内容不成条。
20) 内容不出 quote：条目的 text/detail/name/finding 语义必须落在 anchor.quote 范围内，quote 未提及的内容禁止写入（实测：quote「现为靶向治疗来诊」detail 写成「取药及复查」属编造；就诊目的句 detail 照写原文如「为进一步就诊」，禁止润色改写）。
21) 化疗经历必成条：「N周期X方案」「行新辅助/辅助化疗」「化疗方案：XX」这类化疗经历必须产出 care_history（kind=治疗，time 填周期/日期）与 medications 条目，禁止只写进某条 detail 的长文里不单列。
22) 处置与就诊目的归位：「未予处理/未治疗/未特殊处理」属处置经过，入 care_history(kind=治疗)；「复查/复诊/取药/要求检查」入 care_history(kind=检查或治疗取最接近者)；二者均禁止入 symptoms。纯「来诊/为进一步就诊/今日来院」无具体检查或治疗行为者不产生条目（45919016/46179482/46182290 教训）。
23) 症状不出 quote：症状 text 的原词必须能在 anchor.quote 中找得到（可去掉「无/未见/伴」等否定与连接词做归一化，但不得造 quote 里没有的表述）；quote 中不存在的症状禁止输出。
24) std 不升级（禁止推断）：std 只做同义术语归一（「疼」→「疼痛」、IDC→浸润性导管癌），禁止把原文未明确的量化描述推断成结论性标准词。实测禁例：「距基底切缘距离小于1mm」std 写「切缘阳性」——原文未下切缘阳性结论，std 应留空或写「切缘距离<1mm」的原文要点；「淋巴结7枚未见癌转移0/7」不得推断为 N0 分期。
25) 日期该填必填：anchor.quote 中含明确日期的诊疗事件（手术、换药、化疗周期、复查），care_history.time 必须填该日期（照原文写法，如「2020年09月04日」「2025-4-8」），medications 的换药/用药起点同理填 duration；禁止段内有日期却 time 留空（45007844 教训：手术日期与三次换药日期全部未落成时间化记录）。
26) 乳房所见归 breast：体格检查中提及乳房/乳腺/乳头/乳晕/术区/疤痕/术后改变的所见一律入 breast 数组（side 按原文），general 只收乳房与淋巴结以外的全身状态（神志、对答、一般情况、皮肤巩膜、心肺腹等）。实测禁例：体格检查仅「左乳癌保乳术后改变」却整条进 general、breast 为空（45025784 教训）。
27) 既往治疗史落 past_history：既往已完成的治疗（化疗/放疗/靶向/内分泌治疗/免疫治疗）与其周期数、方案、既往用药，无论这些文字出现在哪个字段（现病史、既往史、体格检查），条目一律落 past_history 的 care_history（kind=治疗）或 medications，anchor 指该段实际所在字段。禁止放进 present_illness——present_illness 只收本次就诊的经过。判据：已发生/已完成的治疗（「已完成放化疗」「做了左乳全切除术」「术后拟行TCH共6周期化疗」）归 past_history；本次正在进行的处置与本次来诊目的归 present_illness。
28) 淋巴结计数与转移状态必成条：原文出现「（前哨）淋巴结N枚」「淋巴结N枚，均未见肿瘤」「其中M枚见癌转移」「M/N」等枚数与转移描述时必须成条——体格检查字段进 physical_exam.lymph_nodes（region/side/size/nature，nature 照录「未见肿瘤」等原文结论）；现病史/既往史字段进 care_history（kind=检查或诊断）或 surgeries，detail 须照录枚数与转移结论。禁止只抽术式而丢掉淋巴结计数（45004416 教训：「（前哨）淋巴结6枚，均未见肿瘤」整块漏抽）；也禁止把「未见肿瘤」推断为 N 分期结论（见规则24）。
29) 复合术式完整、就诊目的不抢位：一段含「行A术+B术+C术」「…切除术伴…活检术」等复合手术时，name/detail 必须逐字完整包含全部术式与其侧别括号（如「保留乳房的单侧乳腺癌切除术伴前哨淋巴结活检术（左侧）+腋窝淋巴结清扫术（左侧）+颈内静脉输液港置入术(Port)（右侧）」），time 填段内日期。同段末尾的就诊目的句（「现为换药来诊」）须单独成条，禁止用就诊目的覆盖或吞并同段的手术信息（45050967 教训：只抽「现为换药来诊」，手术日期 2025-05-30 与完整术名全丢）。
30) 治疗相关不良反应与并发症必成条：原文出现「XX治疗后出现…」「换用XX后仍…」「…内分泌治疗，子宫内膜增厚」这类治疗后的结局事件，必须逐条抽为 present_illness.symptoms（text=原词，std 可留空，quote 须含该表述），禁止只抽 medications 而丢弃结局事件（45012508 教训：他莫西芬→子宫内膜增厚 未成条）。
31) 药物不耐受与换药的药名成条：原文出现「不能耐受XX」「停用XX」「XX效果不佳，改为YY」时，被停用/不耐受的药名（XX）与换药结果（YY）各出一条 medications 条目；present_illness.symptoms 同步出「不能耐受XX」症状条目。禁止只保留换药结果而丢掉被停用的药名（45223539 教训：「不能耐受阿贝西利及瑞波西利片，改为达尔西利片」只抽了达尔西利片）。
32) medications.name 照原文药名：name 填原文出现的药名，商品名保留原样（如「砝码新」），禁止自行归一为通用名（「砝码新」不得写成「表柔比星」，45173155 教训）；通用名归一属下游映射，不在本层做。
33) 笼统治疗史与放疗计划必成条：原文「曾行化疗」「行放疗」「术后化疗」等无药名的笼统治疗表述，也须各出一条 care_history（kind=治疗，detail 照原文）；放疗剂量计划（如「PCTV1 5000cGy/26f」）逐字进 detail，多个靶区剂量各成条或并入同条 detail，不得整句丢弃（44956095 教训：「曾行化疗，行放疗，计划：PCTV1 5000cGy/26f…」全句漏抽）。
34) 既往治疗句的时间与术式是抽取主体，不是背景：凡出现「于X年X月…行…手术/切除术」「术后行/完成…化疗/放疗/靶向/内分泌治疗」「原使用…治疗，已使用X年」句式，手术时间（time 填句内日期）、术式（逐字完整）、化疗/放疗/靶向/内分泌与原用药方案，每一项都必须成条（44936133/45055140 教训：只抽了句尾就诊目的，句首的手术时间+术式+辅助化疗+双靶向+原用药全丢）。
35) 体格检查条目只用体格检查原文：physical_exam 的 region/finding/side 必须出自体格检查字段的文字；现病史里的超声/影像所见（如「未见明显肿大」）不得搬进体格检查条目，影像所见归现病史 care_history（kind=检查）或留待辅助检查板块（44965993 教训：原文体检仅「双乳未扪及肿物」，却多出 region=腋窝条目）。
36) 检查无结论/治疗结束/分期表述/方案字母剂量四类边角：①原文仅写「XX提示.」而无结论（如「ct提示.」）时，detail 照录「XX提示（结论未记录）」，禁止把就诊主诉或症状填成检查结果（45995031 教训）；②「分期较晚/晚期/早期」等分期表述须进 care_history（kind=诊断）detail，不得因词短而丢；③治疗结束/完成状态（「现放疗结束」「已完成X周期」）各成条或并入对应治疗条 detail（46018250 教训）；④方案字母+连续剂量无分隔时按方案字母拆（「PHP方案…P410mg,H348mg,P420mg」→ P 410mg/H 348mg/P 420mg），dose 取字母后完整数字，禁止把字母+首数字拼成药名（46040032 教训）。
37) 就诊目的句归「治疗」不归「检查」：care_history.kind 取值只有 检查/诊断/治疗，「为进一步诊治来我院」「今日来诊」「定期复查」这类就诊目的说明（无检查、无治疗动作）统一标 kind=治疗，禁止标成「检查」（46217726 教训：就诊目的被标为检查，属取值域就近误选）。
38) 单句现病史不得整句当背景丢弃：「患者于2024年5月因右乳癌在我院行腔镜下右乳癌改良根治+背阔肌肌皮瓣I期乳房重建术，术后恢复可」这类一气呵成的短句，须至少拆出手术（time=句内日期、术式详情逐字完整）与诊断（右乳癌）各一条，「术后恢复可」等术后状态进对应条目 detail 或 present_illness.symptoms（46268650 教训：整句零条目，LLM 误当背景）。
39) time/duration 字段语义：care_history.time 只填日期或时间短语（如「2022-6-2」「1月前」），周期数（「8周期」）属治疗量而非时间，须写进 detail；medications.duration 填疗程长度（如「1年」），用药日期不填 duration（46261143 教训：time=8周期、duration=2020-10-26 两处语义错）。
40) 「X癌术后X天/年」主诉必出手术条目：主诉写「右乳癌术后14天」「左乳癌术后1年余」时，手术事实已由原文明示，past_history.surgeries 必须成条；name 取保守描述（如「乳腺癌手术」，禁止臆造术式），date 照录原文相对时间（「14天前」「1年余」），anchor.quote 逐字取自主诉（46578940/46595175 教训：主诉有「术后」字样却无手术条目）。
41) 既往方案与现行方案分条：「已行/已用/已完成X方案N周期」是既往疗程，「现改为/现用Y方案」是现行方案，两者必须各成一条，既往方案落 past_history（46594847 教训：「已行EC+唑来磷酸方案治疗1周期」被「现改为」新方案盖过而整句丢弃）。
42) 长叙述不得漏抽既往术式/化疗：现病史若同时含大段病理/免疫组化与既往手术（「行…改良根治术/保乳术/切除术」）、既往化疗/放疗/靶向/内分泌（带既往时间或「曾/已/于X年」），须逐条抽出术式进 past_history.surgeries、治疗进 past_history.care_history(kind=治疗) 与 medications，禁止被前后病理文字淹没而只抽诊断/检查（46616639 教训：改良根治术与 AC序贯TH 化疗整段漏抽；46615592 教训：主诉「术后1月余」未出 surgeries）。
43) 跨器官症状分条且侧别只按自身修饰词：同一句中并列的不同器官症状（如「双侧乳腺疼痛，伴有甲状腺疼痛不适」）必须各自独立成条；每条 side 仅依据该症状自身文字的左右/双侧修饰判定，禁止因同句其他症状带「双侧/双乳」而扩散到本条（46682280 教训：甲状腺疼痛被同句「双侧乳腺」误带成 side=双）。"""

# ---------------------------------------------------------------------------
# 输入构造
# ---------------------------------------------------------------------------
def load_std(rid):
    p = os.path.join(STD_DIR, rid + ".json")
    if not os.path.exists(p):
        return None
    return json.load(open(p, encoding="utf-8"))

def build_segments_text(std_doc, fields, with_kind=True, with_source=True):
    seg_index = {}
    seg_list = []
    lines = []
    for fld in fields:
        segs = ((std_doc.get("fields") or {}).get(fld) or {}).get("segments") or []
        segs = sorted(segs, key=lambda s: (s.get("anchor") or {}).get("start", 0))
        if not segs:
            lines.append("【%s】（空）" % fld)
            continue
        lines.append("【%s】" % fld)
        for sg in segs:
            a = sg.get("anchor") or {}
            st = a.get("start")
            seg_index[(fld, st)] = sg
            txt = str(sg.get("text", "")).replace("\n", " ")
            seg_list.append((fld, st, txt))
            tag = ""
            if with_kind and sg.get("kind"):
                tag += "kind=%s" % sg.get("kind")
            if with_source and a.get("field") and a.get("field") != fld:
                tag += (" | " if tag else "") + "来自字段[%s]" % a.get("field")
            if tag:
                lines.append("[%s #%s](%s) %s" % (fld, st, tag, txt))
            else:
                lines.append("[%s #%s] %s" % (fld, st, txt))
    return "\n".join(lines), seg_index, seg_list

def build_input(std_doc):
    raws = {}
    seg_index = {}   # (field, seg_start) -> seg dict
    seg_list = []
    lines = []
    for fld in NARRATIVE_FIELDS:
        segs = ((std_doc.get("fields") or {}).get(fld) or {}).get("segments") or []
        segs = sorted(segs, key=lambda s: (s.get("anchor") or {}).get("start", 0))
        raw = "".join(s.get("text", "") for s in segs)
        raws[fld] = raw
        if not segs:
            lines.append("【%s】（空）" % fld)
            continue
        lines.append("【%s】" % fld)
        for sg in segs:
            a = sg.get("anchor") or {}
            st = a.get("start")
            seg_index[(fld, st)] = sg
            txt = str(sg.get("text", "")).replace("\n", " ")
            seg_list.append((fld, st, txt))
            lines.append("[%s #%s] %s" % (fld, st, txt))
    return "\n".join(lines), raws, seg_index, seg_list

# ---------------------------------------------------------------------------
# anchor 校验（空白/标点归一化命中后回填真实原文）
# ---------------------------------------------------------------------------
_WS_RE = re.compile(r"\s+")
_PUNCT_MAP = str.maketrans({
    "（": "(", "）": ")", "【": "[", "】": "]", "，": ",", "。": ".",
    "：": ":", "；": ";", "！": "!", "？": "?", "＋": "+", "、": ",", "　": " ",
})

def _canon(s):
    return _WS_RE.sub("", str(s)).translate(_PUNCT_MAP)

_TRAIL_PUNCT = ".,;:!?)]}"

def _real_quote(txt, quote):
    nq = _canon(quote)
    if not nq:
        return None
    idxs = [i for i, ch in enumerate(txt) if not _WS_RE.match(ch)]
    nt = _canon("".join(txt[i] for i in idxs))
    p = nt.find(nq)
    if p >= 0:
        return txt[idxs[p]: idxs[p + len(nq) - 1] + 1]
    nq2 = nq.rstrip(_TRAIL_PUNCT)
    if nq2 and nq2 != nq:
        p = nt.find(nq2)
        if p >= 0:
            return txt[idxs[p]: idxs[p + len(nq2) - 1] + 1]
    return None

def validate_anchor(quote, field, seg_start, seg_index):
    sg = seg_index.get((field, seg_start))
    if sg is None:
        return False
    txt = str(sg.get("text", ""))
    if not quote:
        return False
    if quote in txt:
        return True
    return bool(_real_quote(txt, quote))

def norm_anchor(an, seg_index):
    if not isinstance(an, dict):
        return None
    fld = an.get("field", "")
    q = (an.get("quote", "") or "").strip()
    ss = an.get("seg_start")
    if isinstance(ss, str) and ss.isdigit():
        ss = int(ss)
    if fld not in FIELD_TO_SECTION or not isinstance(ss, int) or not q:
        return None
    qt = re.sub(r"[\s　。，,、；;：:！!？?]+", "", q)
    if len(qt) < 3 or qt in _SKIP_SEG:
        return None
    if validate_anchor(q, fld, ss, seg_index):
        return (fld, ss, q)
    sg0 = seg_index.get((fld, ss))
    if sg0 is not None:
        rq = _real_quote(str(sg0.get("text", "")), q)
        if rq:
            return (fld, ss, rq)
    for (f2, s2), sg in seg_index.items():
        if f2 == fld:
            txt2 = str(sg.get("text", ""))
            if q in txt2:
                return (fld, s2, q)
            rq = _real_quote(txt2, q)
            if rq:
                return (fld, s2, rq)
    return None

def clean_enum(v, allowed, default=""):
    return v if v in allowed else default

# ---------------------------------------------------------------------------
# 闸门常量
# ---------------------------------------------------------------------------
_ATTR_GATE = {"symptom": ("onset", "duration"), "care": ("time",), "med": ("dose", "duration")}
_SHELL_KEYS = {"symptom": ("text",), "care": ("detail",), "med": ("name",),
               "comorbidity": ("name",), "surgery": ("name",),
               "breast": ("finding",), "node": ("region", "size", "nature"),
               "general": ("finding",)}
_RELOC_ARRS = {"past_history": ("care_history", "medications", "surgeries", "comorbidities")}

_BREAST_RE = re.compile(r"乳房|乳腺|乳癌|乳晕|乳头|双乳|左乳|右乳|乳术后|乳腺术后|术区")
_TRIVIAL_RE = re.compile(r"^(?:无|无特殊|无明显异常|未见异常|未见明显异常|未查|正常|否认|—|-|/|\.|，|,)+$")
_SKIP_SEG = {"无", "无特殊", "无明显异常", "未见异常", "未见明显异常", "未查", "正常",
             "余无不适", "无不适", "无其他不适", "无其他特殊", "否认"}

_BILATERAL_RE = re.compile(r"双侧|双乳|两侧|两乳|双肺|双侧乳腺")
_SIDE_L = re.compile(r"左(?:乳|侧|腋|锁|胸|乳腺)")
_SIDE_R = re.compile(r"右(?:乳|侧|腋|锁|胸|乳腺)")
_CJK = re.compile(r"[一-鿿0-9]")

_NON_BREAST_ORGANS = re.compile(
    r"甲状腺|肺|肝|肾|淋巴结|卵巢|子宫|头颅|颅脑|腹部|胃|肠|骨骼|前列腺|睾丸|皮肤|肌肉|心脏")
_BILAT_NEAR = re.compile(r"双侧|双乳|双[侧乳]|两[侧乳]")

def _norm_relocated(entry, arr, an):
    e = dict(entry)
    e["anchor"] = {"field": an[0], "seg_start": an[1], "quote": an[2]}
    e["source_field"] = an[0]
    q = an[2]
    if arr == "care_history":
        e["kind"] = clean_enum(e.get("kind", ""), CARE_KINDS, "")
        if not e["kind"]:
            return None
        for k in ("time", "detail", "place"):
            e.setdefault(k, "")
        if str(e.get("time") or "").strip() and e["time"] not in q:
            e["time"] = ""
        if not str(e.get("detail") or "").strip():
            return None
    elif arr == "medications":
        for k in ("name", "dose", "duration"):
            e.setdefault(k, "")
        for k in ("dose", "duration"):
            v = str(e.get(k) or "").strip()
            if v and v not in q:
                e[k] = ""
        if not str(e.get("name") or "").strip():
            return None
    elif arr == "surgeries":
        for k in ("name", "date"):
            e.setdefault(k, "")
        nm = str(e.get("name") or "").strip()
        core = re.sub(r"^[左右双]侧?", "", nm)
        if nm and nm not in q and not (core and core in q):
            return None
    elif arr == "comorbidities":
        e["status"] = clean_enum(e.get("status", ""), {"现症", "既往", "否认"}, "既往")
        for k in ("name", "std", "icd10"):
            e.setdefault(k, "")
        if not str(e.get("name") or "").strip():
            return None
    return e

def _dedup_exact(arr):
    if not isinstance(arr, list):
        return arr
    seen, out = set(), []
    for e in arr:
        if isinstance(e, dict):
            k = json.dumps({kk: e.get(kk) for kk in sorted(e) if kk != "anchor"},
                           ensure_ascii=False, sort_keys=True) + "|" + \
                json.dumps(e.get("anchor") or {}, ensure_ascii=False, sort_keys=True)
        else:
            k = json.dumps(e, ensure_ascii=False, sort_keys=True)
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out

def _text_supported(text, q):
    a = _CJK.findall(str(text or ""))
    if not a:
        return False
    b = set(_CJK.findall(str(q or "")))
    return sum(1 for c in a if c in b) / len(a) >= 0.5

def _side_blob(r):
    q = (r.get("anchor") or {}).get("quote", "") or ""
    return q + "".join(str(r.get(k) or "") for k in
                       ("finding", "site", "text", "region", "name", "detail"))

def norm_side(r):
    side = clean_enum(r.get("side", ""), SIDES, "未指明")
    blob = _side_blob(r)
    has_l, has_r = bool(_SIDE_L.search(blob)), bool(_SIDE_R.search(blob))
    bilat = bool(_BILATERAL_RE.search(blob))
    if bilat and not (has_l or has_r):
        return "双"
    if side == "左" and not has_l and not bilat:
        return "未指明"
    if side == "右" and not has_r and not bilat:
        return "未指明"
    if side == "未指明" and not bilat:
        if has_l and not has_r:
            return "左"
        if has_r and not has_l:
            return "右"
    return side

def build_section_out(section, items, seg_index, dropped, relocated=None):
    hit = total = 0
    out = {}

    def proc(entry, kind_tag, arr=""):
        nonlocal hit, total
        if not isinstance(entry, dict):
            return None
        total += 1
        an = norm_anchor(entry.get("anchor"), seg_index)
        if an is None:
            dropped.append({"section": section, "err": "anchor未命中",
                            "item": json.dumps(entry, ensure_ascii=False)[:150]})
            return None
        if section != "past_history" and an[0] != SEC_FIELD.get(section):
            tgt = FIELD_TO_SECTION.get(an[0])
            if relocated is not None and tgt and arr in _RELOC_ARRS.get(tgt, ()):
                e2 = _norm_relocated(entry, arr, an)
                if e2 is not None:
                    relocated.append((tgt, arr, e2))
                    hit += 1
                    return None
            dropped.append({"section": section, "err": "跨板块搬入",
                            "item": json.dumps(entry, ensure_ascii=False)[:150]})
            return None
        hit += 1
        e = dict(entry)
        e["anchor"] = {"field": an[0], "seg_start": an[1], "quote": an[2]}
        q = an[2]
        for k in _ATTR_GATE.get(kind_tag, ()):
            v = str(e.get(k) or "").strip()
            if v and v not in q:
                dropped.append({"section": section, "err": "属性无quote支撑已清空:" + k,
                                "item": json.dumps(entry, ensure_ascii=False)[:150]})
                e[k] = ""
        sk = _SHELL_KEYS.get(kind_tag)
        if sk and all(not str(e.get(k) or "").strip() for k in sk):
            dropped.append({"section": section, "err": "空壳条目",
                            "item": json.dumps(entry, ensure_ascii=False)[:150]})
            return None
        if kind_tag == "symptom" and not _text_supported(e.get("text"), q):
            dropped.append({"section": section, "err": "症状text无quote支撑",
                            "item": json.dumps(entry, ensure_ascii=False)[:150]})
            return None
        return e

    if section == "chief_complaint":
        syms = []
        for e in (items.get("symptoms") or []):
            r = proc(e, "symptom")
            if r:
                r["side"] = norm_side(r)
                for k in ("text", "std", "duration", "onset"):
                    r.setdefault(k, "")
                syms.append(r)
        out["symptoms"] = syms

    elif section == "present_illness":
        syms, care, meds = [], [], []
        for e in (items.get("symptoms") or []):
            r = proc(e, "symptom", "symptoms")
            if r:
                r["side"] = norm_side(r)
                r["neg"] = bool(r.get("neg", False))
                for k in ("text", "std", "duration", "character", "trend"):
                    r.setdefault(k, "")
                syms.append(r)
        for e in (items.get("care_history") or []):
            r = proc(e, "care", "care_history")
            if r:
                r["kind"] = clean_enum(r.get("kind", ""), CARE_KINDS, "")
                if not r["kind"]:
                    dropped.append({"section": section, "err": "kind越界",
                                    "item": json.dumps(e, ensure_ascii=False)[:150]})
                    continue
                for k in ("time", "detail", "place"):
                    r.setdefault(k, "")
                care.append(r)
        for e in (items.get("medications") or []):
            r = proc(e, "med", "medications")
            if r:
                for k in ("name", "dose", "duration"):
                    r.setdefault(k, "")
                meds.append(r)
        out["symptoms"] = syms
        out["care_history"] = care
        out["medications"] = meds

    elif section == "past_history":
        com, surg, care, meds = [], [], [], []
        for e in (items.get("care_history") or []):
            r = proc(e, "care", "care_history")
            if r:
                r["kind"] = clean_enum(r.get("kind", ""), CARE_KINDS, "")
                if not r["kind"]:
                    dropped.append({"section": section, "err": "kind越界",
                                    "item": json.dumps(e, ensure_ascii=False)[:150]})
                    continue
                for k in ("time", "detail", "place"):
                    r.setdefault(k, "")
                care.append(r)
        for e in (items.get("medications") or []):
            r = proc(e, "med", "medications")
            if r:
                for k in ("name", "dose", "duration"):
                    r.setdefault(k, "")
                meds.append(r)
        for e in (items.get("comorbidities") or []):
            r = proc(e, "comorbidity")
            if r:
                r["status"] = clean_enum(r.get("status", ""), {"现症", "既往", "否认"}, "既往")
                q = (r.get("anchor") or {}).get("quote", "") or ""
                nm = str(r.get("name", "") or "")
                if nm and r["status"] != "否认":
                    if re.search(r"(?:否认|无)[^。；;，,]{0,8}%s" % re.escape(nm), q):
                        r["status"] = "否认"
                elif nm and r["status"] == "否认":
                    if not re.search(r"(?:否认|无)[^。；;，,]{0,8}%s" % re.escape(nm), q):
                        r["status"] = "既往"
                for k in ("name", "std", "icd10"):
                    r.setdefault(k, "")
                com.append(r)
        for e in (items.get("surgeries") or []):
            r = proc(e, "surgery")
            if r:
                nm = str(r.get("name", "") or "").strip()
                q = r["anchor"]["quote"]
                core = re.sub(r"^[左右双]侧?", "", nm)
                if nm and nm not in q and not (core and core in q):
                    dropped.append({"section": section, "err": "术名无quote支撑",
                                    "item": json.dumps(e, ensure_ascii=False)[:150]})
                    continue
                for k in ("name", "date"):
                    r.setdefault(k, "")
                surg.append(r)
        mens = items.get("menstrual") or {}
        fam = items.get("family") or {}
        out["care_history"] = care
        out["medications"] = meds
        out["comorbidities"] = com
        out["surgeries"] = surg
        out["menstrual"] = {k: str(mens.get(k, "")) for k in
                            ("menarche", "lmp", "menopause_age", "gravida", "para")} if mens else {}
        out["family"] = {"text": str(fam.get("text", "")),
                         "cancer_history": str(fam.get("cancer_history", ""))} if fam else {}

    elif section == "physical_exam":
        breast, nodes, gen = [], [], []
        for e in (items.get("breast") or []):
            r = proc(e, "breast")
            if r:
                r["side"] = norm_side(r)
                for k in ("site", "finding", "size", "texture", "border", "mobility", "skin", "nipple"):
                    r.setdefault(k, "")
                breast.append(r)
        for e in (items.get("lymph_nodes") or []):
            r = proc(e, "node")
            if r:
                r["side"] = norm_side(r)
                for k in ("region", "size", "nature"):
                    r.setdefault(k, "")
                nodes.append(r)
        for e in (items.get("general") or []):
            r = proc(e, "general")
            if r:
                r.setdefault("finding", "")
                if _BREAST_RE.search(str(r.get("finding") or "")):
                    b = {"side": norm_side(r), "site": "", "finding": r.get("finding", ""),
                         "size": "", "texture": "", "border": "", "mobility": "",
                         "skin": "", "nipple": "", "anchor": r.get("anchor")}
                    breast.append(b)
                    dropped.append({"section": section, "err": "乳房所见由general归位breast",
                                    "item": json.dumps(r, ensure_ascii=False)[:150]})
                    continue
                gen.append(r)
        vit = items.get("vitals") or {}
        out["breast"] = breast
        out["lymph_nodes"] = nodes
        out["general"] = gen
        out["vitals"] = {k: str(vit.get(k, "")) for k in ("T", "P", "R", "BP")} if vit else {}

    for k, v in list(out.items()):
        if isinstance(v, list):
            out[k] = _dedup_exact(v)
    return out, hit, total

# ---------------------------------------------------------------------------
# 确定性兜底网（既往手术/化疗）、侧别修正、剪枝 —— 跨板块级闸门
# ---------------------------------------------------------------------------
_DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日?)?|\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?|\d{4}\s*年")
_REL_RE = re.compile(r"[\d一二三四五六七八九十百千零]+?\s*(?:天|日|周|月|年)\s*前?")
_SURG_NOUN = r"(?:改良根治术|保乳术|根治术|乳腺切除术|乳房切除术|乳房重建术|重建术|旋切术|活检术|切除术|乳腺手术)"
_SURG_NOUN_RE = re.compile(_SURG_NOUN)
_PAST_MARK_RE = re.compile(r"术后|曾行|已行|已完成|已做|既往|已用|行了|行过|\d{4}\s*年|\d{1,2}\s*月|\d{1,3}\s*[天日]|年前|月前|天前|周前")
_DO_RE = re.compile(r"(?:行|施|做|进行|接受|予|给予|于)")
_NOW_TX_RE = re.compile(r"现为|目前|本次|今日|需行|拟行|计划|待行|准备|新辅助|复查|取药|来诊|就诊|入院")
_TX_RE = re.compile(r"化疗|放疗|靶向治疗|靶向|内分泌治疗|免疫治疗|内分泌")
_DRUG_DOSE_RE = re.compile(r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]*?)\s*(\d+(?:\.\d+)?\s*(?:mg|g|ml|IU|单位|片)?)")
_RELO_PAST_RE = re.compile(r"曾行|已行|已完成|术后行|术后辅助|行[^，。；]{0,15}术|(?:19|20)\d{2}\s*年|(?:19|20)\d{2}[-./]")
_VISIT_NOW_RE = re.compile(r"来诊|就诊|复查|取药|入院|现为|本次|今日")
_MENSES_RE = re.compile(r"月经|初潮|经期|经量|LMP|末次月经|绝经|孕\d|产\d|周期\d")
_FAMILY_RE = re.compile(r"家族|父|母|姐妹|兄弟|遗传|亲属|近亲")
_MENSTRUAL_RE = re.compile(r"月经|初潮|绝经|末次|LMP|妊娠|孕产|孕\d|产\d|生育|哺乳")
_VITALS_RE = re.compile(r"体温|脉搏|呼吸|血压|mmHg|BP|次/分")

def _near_date(raw, pos, before=90):
    seg = raw[max(0, pos - before):pos]
    m = list(_DATE_RE.finditer(seg))
    if m:
        return m[-1].group().replace(" ", "")
    m2 = list(_REL_RE.finditer(seg))
    if m2:
        return m2[-1].group().replace(" ", "")
    return ""

def _span_covered(lst, span):
    if not span:
        return False
    for e in (lst or []):
        if not isinstance(e, dict):
            continue
        q = (e.get("anchor") or {}).get("quote") or ""
        if span in q or q in span:
            return True
    return False

def _inject_ph(sections_out, arr, entry):
    ph = sections_out.get("past_history") or {}
    st = ph.get("structured") or {}
    if not isinstance(st.get(arr), list):
        st[arr] = []
    st[arr].append(entry)
    ph["structured"] = st
    sections_out["past_history"] = ph

def _backfill_source_field(sections_out):
    ph = (sections_out.get("past_history") or {}).get("structured") or {}
    for arr in ("surgeries", "care_history", "medications", "comorbidities"):
        for e in (ph.get(arr) or []):
            if not isinstance(e, dict):
                continue
            an = e.get("anchor") or {}
            fld = an.get("field")
            if fld and fld != "既往史" and not e.get("source_field"):
                e["source_field"] = fld
    return sections_out

def _surgery_chemo_net(sections_out, raws):
    for fld in ("主诉", "现病史", "体格检查"):
        raw = str(raws.get(fld) or "")
        if not raw:
            continue
        for m in _SURG_NOUN_RE.finditer(raw):
            s, e = m.start(), m.end()
            ctx = raw[max(0, s - 30):e]
            if (not (_PAST_MARK_RE.search(ctx) or _DO_RE.search(ctx))) or _NOW_TX_RE.search(ctx):
                continue
            back = raw[max(0, s - 25):s]
            vb = -1
            for ch in "行施做经接受予给予于":
                p = back.rfind(ch)
                if p > vb:
                    vb = p
            proc_start = (s - 25) + vb + 1 if vb >= 0 else max(0, s - 20)
            proc = re.sub(r"^[行施做经接受予给予于]+\s*", "", raw[proc_start:e]).strip()
            if len(proc) < 2 or len(proc) > 40:
                continue
            quote = proc
            seg_start = raw.find(quote)
            if seg_start < 0:
                seg_start = s
            covered = _span_covered((sections_out.get("past_history") or {}).get("structured", {}).get("surgeries"), quote)
            if not covered:
                for sec in ("chief_complaint", "present_illness", "past_history"):
                    if _span_covered((sections_out.get(sec) or {}).get("structured", {}).get("surgeries"), quote) or \
                       _span_covered((sections_out.get(sec) or {}).get("structured", {}).get("care_history"), quote):
                        covered = True
                        break
            if covered:
                continue
            _inject_ph(sections_out, "surgeries", {
                "name": quote, "date": _near_date(raw, s),
                "anchor": {"field": fld, "seg_start": seg_start, "quote": quote},
                "source_field": fld})
        for m in re.finditer(r"(?:左|右|双)?\s*侧?\s*乳腺?癌?\s*术后\s*([\d一二三四五六七八九十百千零]+\s*(?:余|多|半)?\s*(?:天|日|周|月|年)(?:余|整|多)?)", raw):
            if _NOW_TX_RE.search(m.group(0)):
                continue
            quote = m.group(0)
            date = m.group(1)
            seg_start = m.start()
            if _span_covered((sections_out.get("past_history") or {}).get("structured", {}).get("surgeries"), quote):
                continue
            _inject_ph(sections_out, "surgeries", {
                "name": "乳腺癌手术", "date": date,
                "anchor": {"field": fld, "seg_start": seg_start, "quote": quote},
                "source_field": fld})
        for m in _TX_RE.finditer(raw):
            s, e = m.start(), m.end()
            ctx = raw[max(0, s - 40):s]
            if (not (_PAST_MARK_RE.search(ctx) or _DATE_RE.search(ctx))) or _NOW_TX_RE.search(ctx):
                continue
            pre = raw[max(0, s - 30):s]
            vb = -1
            for ch in "行施做予给予于接受":
                p = pre.rfind(ch)
                if p > vb:
                    vb = p
            pre2 = pre[vb + 1:] if vb >= 0 else pre
            tx_phrase = (pre2 + raw[s:e]).lstrip("。，、 ").strip()
            dose_txt = ""
            if e < len(raw) and raw[e] in "（(":
                close = raw.find("）", e)
                if close < 0:
                    close = raw.find(")", e)
                if close > e:
                    dose_txt = raw[e + 1:close]
                    tx_phrase += raw[e:close + 1]
            quote = tx_phrase
            seg_start = raw.find(quote) if quote in raw else s
            cov = False
            for sec in ("chief_complaint", "present_illness", "past_history"):
                if _span_covered((sections_out.get(sec) or {}).get("structured", {}).get("care_history"), quote) or \
                   _span_covered((sections_out.get(sec) or {}).get("structured", {}).get("medications"), quote):
                    cov = True
                    break
            if cov:
                continue
            _inject_ph(sections_out, "care_history", {
                "time": _near_date(raw, s), "kind": "治疗", "detail": tx_phrase,
                "anchor": {"field": fld, "seg_start": seg_start, "quote": quote},
                "source_field": fld})
            if dose_txt:
                for dm in re.split(r"[+、，,]", dose_txt):
                    mm = _DRUG_DOSE_RE.search(dm)
                    if mm:
                        ds = raw.find(dm.strip()) if dm.strip() in raw else seg_start
                        _inject_ph(sections_out, "medications", {
                            "name": mm.group(1), "dose": mm.group(2).strip(), "duration": "",
                            "anchor": {"field": fld, "seg_start": ds, "quote": dm.strip()},
                            "source_field": fld})
    return _backfill_source_field(sections_out)

def _fix_symptom_sides(sections_out):
    for sec in ("chief_complaint", "present_illness"):
        st = (sections_out.get(sec) or {}).get("structured") or {}
        for e in (st.get("symptoms") or []):
            if e.get("side") != "双":
                continue
            text = (e.get("text") or e.get("std") or "")
            if not _NON_BREAST_ORGANS.search(text):
                continue
            quote = (e.get("anchor") or {}).get("quote", "")
            idx = quote.find(text)
            if idx <= 0:
                continue
            last_bilat = None
            for mm in _BILAT_NEAR.finditer(quote[:idx]):
                last_bilat = mm
            if last_bilat is None:
                e["side"] = "未指明"
                continue
            between = quote[last_bilat.end():idx]
            if _NON_BREAST_ORGANS.search(between) or re.search(r"乳腺|乳房", between):
                e["side"] = "未指明"
    return sections_out

def _substantive(raw):
    t = re.sub(r"\s+|\u3000+", "", str(raw or ""))
    return len(t) > 2 and not _TRIVIAL_RE.match(t)

def _prune_unsupported(sections_out, raws):
    joined = " ".join(str(raws.get(f) or "") for f in NARRATIVE_FIELDS)
    keep_mens = bool(_MENSES_RE.search(joined))
    keep_fam = bool(_FAMILY_RE.search(joined))
    for fld, sec in FIELD_TO_SECTION.items():
        if not _substantive(raws.get(fld)):
            st = (sections_out.get(sec) or {}).get("structured") or {}
            for k, v in list(st.items()):
                if sec == "past_history" and k in ("surgeries", "care_history", "medications", "comorbidities"):
                    continue
                if sec == "past_history" and k == "menstrual" and keep_mens:
                    continue
                if sec == "past_history" and k == "family" and keep_fam:
                    continue
                st[k] = [] if isinstance(v, list) else {}
    ph = (sections_out.get("past_history") or {}).get("structured") or {}
    if not keep_fam:
        ph["family"] = {}
    if not keep_mens:
        ph["menstrual"] = {}
    pe = (sections_out.get("physical_exam") or {}).get("structured") or {}
    if not _VITALS_RE.search(joined):
        pe["vitals"] = {}
    return sections_out

# ---------------------------------------------------------------------------
# 漏抽嫌疑清单
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(r"Luminal\s*[AB]|三阴性|HER-?2阳性型|分子分型|手术顺利|切缘|脉管|神经侵犯|"
                     r"骨转移|肝转移|肺转移|脑转移|复发|BI-?RADS|新辅助|姑息|化疗|放疗|内分泌治疗|靶向治疗|"
                     r"第?\d+\s?周期|[A-Za-z]{1,8}\s?方案|"
                     r"复查|来诊|就诊|取药|随诊|复诊|"
                     r"免疫组化|病理结果|术后病理|病理诊断|前哨淋巴结|"
                     r"淋巴结\s*\d+\s*枚|未见癌转移|未见肿瘤|癌转移\s*\d+/\d+|浸润性癌|原位癌|"
                     r"送手术室|(?:全麻|局麻|腰麻)下|治疗后出现|术后出现")
_TREAT_GAP_RE = re.compile(
    r"行[^，。；]{0,15}术|切除术|根治术|化疗|放疗|靶向|内分泌|抗HER?2|OFS|"
    r"托瑞米芬|他莫昔芬|阿那曲唑|来曲唑|依西美坦|亮丙瑞林|戈舍瑞林|曲妥珠|帕妥珠|cGy")
_DATE_RE2 = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}\s*年\s*\d{1,2}\s*月|"
                       r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{4}[-/.]\d{1,2}")

def _seg_needs_cover(text):
    t = re.sub(r"[\s　。，,、；;：:！!？?]+", "", str(text or ""))
    return len(t) > 2 and t not in _SKIP_SEG

def _covered_pairs(sections_out):
    cov = set()
    for sec in sections_out.values():
        st = (sec or {}).get("structured") or {}
        for v in st.values():
            if not isinstance(v, list):
                continue
            for it in v:
                if isinstance(it, dict):
                    an = it.get("anchor") or {}
                    fld, ss = an.get("field"), an.get("seg_start")
                    if isinstance(fld, str) and isinstance(ss, int):
                        cov.add((fld, ss))
    return cov

def _uncovered_segs(sections_out, seg_list):
    cov = _covered_pairs(sections_out)
    return [(fld, st, txt[:60]) for fld, st, txt in seg_list
            if _seg_needs_cover(txt) and (fld, st) not in cov]

def collect_gaps(sections_out, raws, seg_list):
    gaps = [(fld, "段", txt) for fld, _, txt in _uncovered_segs(sections_out, seg_list)]
    mens = (((sections_out.get("past_history") or {}).get("structured") or {}).get("menstrual")) or {}
    mens_filled = any(str(v).strip() for v in mens.values())
    if mens_filled:
        gaps = [g for g in gaps if not _MENSES_RE.search(g[2])]
    else:
        joined = " ".join(str(raws.get(f) or "") for f in NARRATIVE_FIELDS)
        m = re.search(r"LMP|lmp|末次月经|初潮|绝经|月经(?:规律|不调|稀发|频发)|周期\d+/\d+|孕\d产\d", joined)
        if m:
            gaps.append(("既往史", "要点", "月经婚育信息未入menstrual: " + joined[max(0, m.start() - 10):m.end() + 15]))
    for fld, sec in FIELD_TO_SECTION.items():
        raw = str(raws.get(fld) or "")
        dumped = json.dumps((sections_out.get(sec) or {}).get("structured") or {},
                            ensure_ascii=False)
        for m in _KEY_RE.finditer(raw):
            core = m.group(0).strip()
            if core and core not in dumped:
                gaps.append((fld, "要点", raw[max(0, m.start() - 12):m.end() + 12]))
        for e in (((sections_out.get(sec) or {}).get("structured") or {}).get("care_history") or []):
            if not isinstance(e, dict) or str(e.get("time") or "").strip():
                continue
            q = str((e.get("anchor") or {}).get("quote") or "")
            m = _DATE_RE2.search(q)
            if m:
                gaps.append((fld, "时间缺失", "quote有日期未落time: " + q[:60]))
    seen, uniq = set(), []
    for g in gaps:
        k = (g[0], g[2][:40])
        if k not in seen:
            seen.add(k)
            uniq.append(g)
    return uniq
