# -*- coding: utf-8 -*-

import argparse
import glob
import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import section_spec  # 同目录：八板块 SECTIONS 定义（脚本运行时 05_agents 已在 sys.path[0]）
import narrative_engine as ne  # 叙述四板块细颗粒抽取引擎

# 容错 JSON 兜底：demjson3 能恢复结构性坏 JSON（内部括号失衡、尾随逗号、裸值等），
# 这是 LLM 长文本结构化输出的高频故障（尤其辅助检查多报告组）。标准解析失败才启用。
try:
    import demjson3
    _HAVE_DEMJSON = True
except Exception:
    _HAVE_DEMJSON = False

QUOTA = threading.Event()  # 额度耗尽信号：chat 收到 403 时置位，main 循环据此提前终止
TRIP = threading.Event()   # 鉴权/连接持续失败信号：累计 HTTP 401 达阈值置位，main 据此快速终止，避免空转死循环
_AUTH_FAIL = {"n": 0}
_AUTH_LOCK = threading.Lock()
AUTH_FAIL_LIMIT = 20      # 累计 401 达此数判定鉴权不可用，熔断整批（成功时回退，避免偶发抖动误杀）

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
AGENT_ROOT = DATA + "/中间产物/agents_v7"
# 叙述四板块细抽的输入源：标准化产物 segments（子智能体吃 segments 而非原文）
STD_DIR = DATA + "/中间产物/standardized_v1"
# 完成索引：记录 9 板块全部落盘的 rid，一行一个。--resume 时与合并产物 <rid>.json
# 取并集跳过——因为合并( assemble )只在跑批后统一执行，仅靠合并产物做断点判断，
# 任何中途停车都会把已跑完但未合并的记录整批重跑（token 浪费可达上亿）。
DONE_INDEX = AGENT_ROOT + "/_done_index.txt"
INPUT = DATA + "/中间产物/_agents_input/input_records.jsonl"

# 板块名 -> 输出顶层键
TOP_KEY = {
    "chief_complaint": "chief_complaint",
    "present_illness": "present_illness",
    "past_history": "past_history",
    "physical_exam": "physical_exam",
    "auxiliary_exam": "auxiliary_exam",
    "diagnoses": "diagnoses",
    "treatment": "treatment",
    "orders": "orders",
}

PY_PROMPT_HEAD = (
    "你是结构化子智能体。只输出一个合法 JSON 对象，不要任何解释文字、不要用 ``` 围栏。\n"
    "硬规则：\n"
    "1) 只输出结构化字段，禁止输出 raw 原文、禁止输出 anchor 引文、禁止输出任何 *_raw 原文片段。\n"
    "2) 字符串里的双引号、单引号、换行必须用 \\\" 转义；遇到原文里的引号/换行必须转义。\n"
    "3) 严格只输出一个 JSON 对象，禁止用 ```json 围栏，禁止前后加任何文字。\n"
    "4) 值域严格遵守：laterality 只允许 右/左/双/未指明（没有「未知」、没有「单侧」）；拿不准一律「未指明」。\n"
)

def _repair_json(raw):
    out = []
    in_str = False
    esc = False
    for ch in raw:
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == '\\':
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            out.append(ch)
            continue
        if in_str:
            if ch == '\n':
                out.append('\\n')
                continue
            if ch == '\r':
                out.append('\\r')
                continue
            if ch == '\t':
                out.append('\\t')
                continue
        out.append(ch)
    return ''.join(out)

def _try_parse(text):
    i, j = text.find("{"), text.rfind("}")
    candidates = [text, _repair_json(text)]
    if 0 <= i < j:
        sub = text[i:j + 1]
        candidates.append(sub)
        candidates.append(_repair_json(sub))
    for c in candidates:
        try:
            return json.loads(c)
        except Exception:
            continue
    return None

def _parse_json(content):
    text = content.strip()
    # 防御：部分部署的 Claude 默认输出 <think>…</think> 块，必须剥离，否则会污染 JSON 解析
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    obj = _try_parse(text)
    if obj is not None:
        return obj
    # 最后兜底：demjson3 容错解码，专治内部括号失衡等标准解析器无法恢复的坏 JSON
    if _HAVE_DEMJSON:
        try:
            rec = demjson3.decode(text, strict=False)
            if isinstance(rec, dict):
                return rec
        except Exception:
            pass
    try:
        with open("/tmp/last_llm_response.txt", "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass
    raise ValueError("json parse failed; saved to /tmp/last_llm_response.txt (first 200: %s)" % content[:200])

# ---------------------------------------------------------------------------
# 后端
# ---------------------------------------------------------------------------

class OpenAIBackend:

    def __init__(self, base_url, api_key, model, is_deepseek=False, is_claude=False, disable_thinking=True):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.api_key = api_key or "ollama"
        self.model = model
        self.is_deepseek = is_deepseek
        self.is_claude = is_claude
        self.disable_thinking = disable_thinking
        # 真实 token 用量累加器（DeepSeek 响应里带 usage）
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._usage_lock = threading.Lock()

    def chat(self, system, user, schema):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        if self.is_deepseek:
            # DeepSeek V4-Pro：不带 thinking 字段即为 non-thinking 模式（官方默认）；
            # 显式传 thinking: {type: disabled} 在部分 API 路由会触发 405。
            payload["response_format"] = {"type": "json_object"}
            # 强引导："请直接输出 JSON，不要先思考"，让模型默认走 non-thinking 输出路径
            if "messages" in payload and payload["messages"]:
                payload["messages"][0]["content"] += (
                    "\n\n（不要输出任何思考或推理，直接以 JSON 对象形式给出最终答案。"
                    "你的回复只包含一个 JSON，不要 ``` 围栏、不要前缀文字。）"
                )
        elif self.is_claude:
            # Claude 走 OpenAI 兼容层，一般只支持 json_object、不支持 json_schema 结构化输出。
            # 板块 prompt 已内嵌完整结构说明，靠 json_object + 强 prompt + _parse_json 容错即可。
            payload["response_format"] = {"type": "json_object"}
            # _parse_json 剥离；若 content 变空，由 chat 兜底取 reasoning 字段。
            payload["thinking"] = {"type": "disabled"}
            # 默认输出上限较小，auxiliary_exam（多份病理报告）会被截断成不完整 JSON。
            # 显式放大 max_tokens，避免大板块被砍断导致解析失败、填空中。
            payload["max_tokens"] = 32000
        elif schema is None:
            # 叙述板块（细颗粒抽取）：结构高度嵌套（含 anchor），不适合 json_schema 严格约束，
            # 走 json_object + 强 prompt（NARRATIVE_SYS 已内嵌完整结构说明）。
            payload["response_format"] = {"type": "json_object"}
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "struct", "schema": schema, "strict": False},
            }
        if TRIP.is_set():
            raise RuntimeError("backend tripped (auth/connection unusable); fast-fail pending requests")
        data = json.dumps(payload).encode("utf-8")
        url = self.url
        redirects = 0
        # 重试策略：
        #  - 301/302/307/308：跟随并重新 POST（不消耗重试预算）
        #  - 429（限流）：指数退避重试
        #  - 5xx：指数退避重试
        #  - 403（额度耗尽）：致命，立即抛出，避免空耗重试
        #  - 其他 4xx：致命，立即抛出
        max_attempts = 7
        backoff = 2.0
        for attempt in range(max_attempts):
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", "Bearer %s" % self.api_key)
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    obj = json.loads(resp.read().decode("utf-8"))
                # 采集真实 token 用量（DeepSeek / OpenAI 兼容都返回 usage）
                u = obj.get("usage") or {}
                if u:
                    with self._usage_lock:
                        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                            if k in u:
                                self.usage[k] = self.usage.get(k, 0) + int(u[k])
                    with _AUTH_LOCK:  # 成功一次则回退失败计数，避免偶发抖动误熔断
                        if _AUTH_FAIL["n"] > 0:
                            _AUTH_FAIL["n"] -= 1
                content = obj["choices"][0]["message"].get("content") or ""
                if not content:
                    # 服务端偶尔把答案放进 reasoning 字段而 content 为空，
                    # 兜底取 reasoning 再解析；都没有才判失败。
                    content = obj["choices"][0]["message"].get("reasoning") or ""
                if not content:
                    raise RuntimeError("empty content (thinking on?) response=%s" % json.dumps(obj)[:300])
                return _parse_json(content)
            except urllib.error.HTTPError as e:
                code = e.code
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")[:400]
                except Exception:
                    pass
                if code in (301, 302, 307, 308):
                    loc = e.headers.get("Location")
                    if loc and redirects < 5:
                        url = urljoin(url, loc)
                        redirects += 1
                        print("  [backend redirect %d] -> %s" % (code, url))
                        continue
                    raise RuntimeError("too many redirects")
                if code == 403:
                    # 额度耗尽：致命，立即失败，交给上层决定（通常意味着需要充值）
                    QUOTA.set()
                    print("  [backend HTTP 403 quota] %s" % (body or e.reason))
                    raise RuntimeError("quota exhausted (HTTP 403): %s" % (body or e.reason))
                if code in (401, 429) or 500 <= code < 600:
                    if code == 401:
                        with _AUTH_LOCK:
                            _AUTH_FAIL["n"] += 1
                            if _AUTH_FAIL["n"] >= AUTH_FAIL_LIMIT:
                                TRIP.set()
                        if TRIP.is_set():
                            raise RuntimeError(
                                "auth unusable after %d x HTTP 401 (tripped)" % _AUTH_FAIL["n"])
                    wait = backoff * (2 ** min(attempt, 3))  # 单请求退避封顶 8s，避免空耗
                    tag = "401 jitter" if code == 401 else ("429 rate" if code == 429 else "5xx")
                    print("  [backend HTTP %d %s] retry %.1fs (att %d, authfail=%d)"
                          % (code, tag, wait, attempt + 1, _AUTH_FAIL["n"]))
                    time.sleep(wait)
                    continue
                # 其他 4xx：致命
                print("  [backend HTTP %d fatal] %s" % (code, body or e.reason))
                raise RuntimeError("backend HTTP %d: %s" % (code, body or e.reason))
            except Exception as e:
                wait = backoff * (2 ** attempt)
                print("  [backend retry] %s (att %d, wait %.1fs)" % (e, attempt + 1, wait))
                time.sleep(wait)
        raise RuntimeError("backend failed after retries")

class MockBackend:

    def __init__(self):
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def chat(self, system, user, schema):
        # 从 user 里提取 raw 文本（inputs 拼接）
        return None  # 由调用方按 TOP_KEY 构造骨架

# ---------------------------------------------------------------------------
# 输入与板块组合
# ---------------------------------------------------------------------------

def load_input(input_path=INPUT):
    recs = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            recs.append(json.loads(line))
    return recs

def composite_text(rec, names, rid=None):
    parts = []
    for n in names:
        if n == "__narrative_segments__":
            # 叙述板块：吃 segments。rid 由调用方传入。
            ni = load_narrative_input(rid)
            txt = ni[0] if ni is not None else "（标准化产物缺失）"
        elif n == "__aux_segments__":
            ai = load_aux_input(rid)
            txt = ai[0] if ai is not None else "（标准化产物缺失）"
        elif n == "__treatment_segments__":
            ti = load_treatment_input(rid)
            txt = ti[0] if ti is not None else "（标准化产物缺失）"
        elif n == "__diagnoses__":
            txt = "【诊断列】\n" + "\n".join("%s: %s" % (c, v) for c, v in rec.get("diagnoses", []))
        elif n == "__treatment__":
            txt = ("【现病史】\n%s\n\n【辅助检查】\n%s\n\n【诊断列】\n%s"
                   % (rec["fields"].get("现病史", ""), rec["fields"].get("辅助检查", ""),
                      "\n".join("%s: %s" % (c, v) for c, v in rec.get("diagnoses", []))))
        elif n == "__orders__":
            txt = "【医嘱列】\n" + "\n".join("%s: %s" % (c, v) for c, v in rec.get("orders", []))
        else:
            txt = "【%s】\n%s" % (n, rec["fields"].get(n, ""))
        parts.append(txt)
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# 叙述四板块：segments 输入 + 单板块收窄 prompt + 细颗粒后处理
# ---------------------------------------------------------------------------

_NARRATIVE_SECS = ("chief_complaint", "present_illness", "past_history", "physical_exam")

# 单板块收窄指令：让每个叙述子智能体只负责输出自己那个键，其余键给空对象。
# 跨板块路由（手术史/合并症/月经婚育归 past_history）仍靠 NARRATIVE_SYS 规则 14/18/27，
# 输入是完整四字段 segments，故路由信息齐全。
_NARROW = {
    "chief_complaint": (
        "\n\n【本次任务范围】你只负责输出 chief_complaint 这一个键；"
        "present_illness / past_history / physical_exam 三个键输出空对象 {}。"
        "但跨字段路由仍照做：若主诉里有手术史/合并症等，仍按规则 18/27 把它们放进 past_history（而非留空）。"),
    "present_illness": (
        "\n\n【本次任务范围】你只负责输出 present_illness 这一个键；"
        "chief_complaint / past_history / physical_exam 三个键输出空对象 {}。"
        "但跨字段路由仍照做：若现病史里的既往治疗史按规则 27 应落 past_history，仍照落。"),
    "past_history": (
        "\n\n【本次任务范围】你只负责输出 past_history 这一个键；"
        "chief_complaint / present_illness / physical_exam 三个键输出空对象 {}。"
        "past_history 是跨字段落点区（规则 14/18/27）：手术史/合并症/治疗史/月经婚育，"
        "无论写在哪个字段都要落进本键。"),
    "physical_exam": (
        "\n\n【本次任务范围】你只负责输出 physical_exam 这一个键；"
        "chief_complaint / present_illness / past_history 三个键输出空对象 {}。"
        "但跨字段路由仍照做：若体格检查里的手术史按规则 18 应落 past_history，仍照落。"),
}

def load_narrative_input(rid):
    std_doc = ne.load_std(rid)
    if std_doc is None:
        return None
    return ne.build_input(std_doc)

def load_aux_input(rid):
    std_doc = ne.load_std(rid)
    if std_doc is None:
        return None
    return ne.build_segments_text(std_doc, ["辅助检查"], with_kind=True, with_source=True)

def load_treatment_input(rid):
    std_doc = ne.load_std(rid)
    if std_doc is None:
        return None
    return ne.build_segments_text(std_doc, ["主诉", "现病史", "既往史", "辅助检查", "体格检查"],
                                  with_kind=True, with_source=True)

def narrative_system(section):
    return ne.NARRATIVE_SYS + _NARROW.get(section, "")

def postprocess_narrative(rid, section, payload, dropped_log):
    std_doc = ne.load_std(rid)
    if std_doc is None:
        raise RuntimeError("标准化产物缺失，无法校验 anchor")
    _, _, seg_index, _ = ne.build_input(std_doc)
    items = (payload or {}).get(section) or {}
    dropped = []
    relocated = []
    out, hit, total = ne.build_section_out(section, items, seg_index, dropped, relocated)
    for d in dropped:
        d["rid"] = rid
        d["section"] = section
        dropped_log.append(d)
    # 单板块跨板块搬迁：条目 anchor 指向别的板块且目标板块有承载数组 → 迁到对应板块。
    # 但 run_batch 是每板块独立落盘，跨板块搬迁需在四板块聚合后统一处理（见 aggregate_narrative）。
    return out, hit, total, relocated

def aggregate_narrative(rid, section_files, dropped_log):
    std_doc = ne.load_std(rid)
    if std_doc is None:
        return
    _, raws, _, _ = ne.build_input(std_doc)

    # 读回四板块 structured
    sections_out = {}
    for sec in _NARRATIVE_SECS:
        p = section_files.get(sec)
        val = {}
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    val = json.load(f)
            except Exception:
                val = {}
        # 板块文件是 {<top>: {structured}} 或 {<top>: {...}} 形态；统一取结构化内容
        top = TOP_KEY[sec]
        body = val.get(top, {})
        if isinstance(body, dict) and "structured" in body:
            st = body.get("structured") or {}
        else:
            st = body if isinstance(body, dict) else {}
        sections_out[sec] = {"structured": st, "raw": raws.get(ne.SEC_FIELD[sec], "")}

    # 跨板块闸门
    sections_out = ne._surgery_chemo_net(sections_out, raws)
    sections_out = ne._fix_symptom_sides(sections_out)
    sections_out = ne._prune_unsupported(sections_out, raws)

    # 写回四板块文件（{top: {raw, structured, source_patch}} 形态，与 restore 一致）
    for sec in _NARRATIVE_SECS:
        p = section_files.get(sec)
        if not p:
            continue
        fld = ne.SEC_FIELD[sec]
        out = {TOP_KEY[sec]: {
            "raw": raws.get(fld, ""),
            "structured": (sections_out.get(sec) or {}).get("structured", {}),
            "source_patch": "narrative_fine_extract_20261105",
        }}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)

def build_program_sections(rec):
    ids = rec.get("ids", {})
    patient = {
        "登记号": ids.get("登记号", ""),
        "性别": ids.get("性别", ""),
        "病历类型": ids.get("病历类型", ""),
    }
    visit = {
        "病历ID": ids.get("病历ID", ""),
        "就诊日期": ids.get("就诊日期", ""),
        "就诊医生": ids.get("就诊医生", ""),
        "医生所属科室": ids.get("医生所属科室", ""),
        "就诊次数级别": ids.get("就诊次数级别", ""),
    }
    qc = {"missing": [], "low_conf": [], "conflicts": [], "review_status": "未核"}
    return {"patient": patient, "visit": visit, "qc": qc}

# ---------------------------------------------------------------------------
# 单条记录处理
# ---------------------------------------------------------------------------

def process_record(rec, backend, dry_run, section_workers=8, only_sections=None):
    rid = rec["record_id"]
    d = os.path.join(AGENT_ROOT, rid)
    os.makedirs(d, exist_ok=True)

    # 程序段（本地生成，不调 LLM）
    prog = build_program_sections(rec)
    if dry_run:
        return rid, []

    # 准备 8 个板块任务（composite_text 是本地计算，快）；only_sections 指定时只取子集
    tasks = []
    done_files = []
    narr_files = {}   # 叙述板块 section -> 文件路径（聚合用）
    for sec in section_spec.SECTIONS:
        if only_sections and sec["name"] not in only_sections:
            continue
        top = TOP_KEY[sec["name"]]
        out_path = os.path.join(d, sec["file"])
        is_narrative = bool(sec.get("narrative_section"))
        user = composite_text(rec, sec["inputs"], rid=rid)
        tasks.append((sec, top, out_path, user))
        if is_narrative:
            narr_files[sec["name"]] = out_path

    dropped_log = []

    # 单条内板块级并发
    if backend is None:  # mock：只产出空结构化骨架
        for sec, top, out_path, user in tasks:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({top: {}}, f, ensure_ascii=False, indent=2)
    else:
        sw = max(1, min(section_workers, len(tasks)))
        with ThreadPoolExecutor(max_workers=sw) as ex:
            futs = {}
            for sec, top, out_path, user in tasks:
                sys_prompt = narrative_system(sec["name"]) if sec.get("narrative_section") else sec["system"]
                sch = None if sec.get("narrative_section") else sec["schema"]
                futs[ex.submit(backend.chat, sys_prompt, PY_PROMPT_HEAD + user, sch)] = \
                    (sec, top, out_path)
            for fut in as_completed(futs):
                sec, top, out_path = futs[fut]
                try:
                    payload = fut.result()
                except Exception as e:
                    # 单板块失败（解析/超时/重定向耗尽/额度耗尽）不应拖垮整条记录：
                    # 写 _failed 标记骨架，便于修复调度器精确区分「真正失败（需重跑）」
                    # 与「模型判定为空（真稀疏，合法）」—— 二者在合并/值域校验中行为不同。
                    print("  [section FAIL] %s: %s" % (top, e))
                    payload = {top: {"_failed": True}}
                if not isinstance(payload, dict) or top not in payload:
                    # 模型返回合法 JSON 但缺顶层键 → 同样记为失败，交修复调度器重跑
                    payload = {top: {"_failed": True}}
                if sec.get("narrative_section") and not payload.get(top, {}).get("_failed"):
                    # 叙述板块：经规则层后处理（anchor 校验 + 侧别归一 + 跨板块搬迁），
                    # 产出 {top: {raw, structured}} 形态；raw 在聚合阶段统一回填。
                    try:
                        out, hit, total, relocated = postprocess_narrative(
                            rid, sec["name"], payload, dropped_log)
                        # 单板块视角先落 structured，raw 由 aggregate_narrative 回填
                        payload = {top: {"structured": out, "_n_hit": hit, "_n_total": total}}
                    except Exception as e:
                        print("  [narrative postprocess FAIL] %s: %s" % (top, e))
                        payload = {top: {"_failed": True}}
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

    # 叙述四板块聚合后的跨板块确定性闸门（仅当本记录含叙述板块时）
    if narr_files:
        try:
            aggregate_narrative(rid, narr_files, dropped_log)
        except Exception as e:
            print("  [aggregate_narrative FAIL] %s: %s" % (rid, e))

    # 程序段落盘
    for k, v in prog.items():
        with open(os.path.join(d, "01_%s.json" % k), "w", encoding="utf-8") as f:
            json.dump({k: v}, f, ensure_ascii=False, indent=2)

    return rid, done_files + [sec["file"] for sec, _, _, _ in tasks]

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=INPUT)
    ap.add_argument("--backend", default="mock", choices=["mock", "ollama", "openai"])
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", ""))
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="qwen3.5:27b")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4, help="外层并发记录数")
    ap.add_argument("--section-workers", type=int, default=8, help="单条内并发板块数（板块级并发，默认 8）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    ap.add_argument("--resume", action="store_true", help="跳过已存在且校验通过的记录")
    ap.add_argument("--sections", default="",
                    help="只跑指定板块（逗号分隔，如 diagnoses,treatment）；用于定向补跑，空=全部8板块")
    ap.add_argument("--force", action="store_true",
                    help="强制重跑（忽略已存在产物）；与 --sections 配合定向补空板块")
    ap.add_argument("--dry-run", action="store_true", help="只规划批次，不调 LLM")
    a = ap.parse_args()

    recs = load_input(a.input)
    if a.limit:
        recs = recs[: a.limit]
    total = len(recs)

    # 定向补板块：--sections 指定只跑哪些板块（如 diagnoses）；与 --force 配合强制覆盖旧产物
    only_secs = None
    if a.sections:
        only_secs = set(s.strip() for s in a.sections.split(",") if s.strip())
        # 校验板块名合法
        valid = set(TOP_KEY.keys())
        bad = only_secs - valid
        if bad:
            print("[错误] 未知板块名: %s｜合法板块: %s" % (sorted(bad), sorted(valid)))
            sys.exit(2)

    # 估算 token（粗算：中文字 ~1.6 token/字）
    est_chars = sum(len("".join(str(v) for v in r["fields"].values())) for r in recs)
    est_tokens = int(est_chars * 1.6)
    n_batches = (total + a.batch_size - 1) // a.batch_size

    print("=== 分批计划 ===")
    print("记录数: %d" % total)
    print("每批: %d ｜ 批次数: %d" % (a.batch_size, n_batches))
    print("输入原文总字数(估): %d ｜ 总 token(估): %d" % (est_chars, est_tokens))
    print("后端: %s (model=%s)" % (a.backend, a.model))
    if a.resume:
        done = sum(1 for r in recs if os.path.exists(os.path.join(AGENT_ROOT, r["record_id"], "%s.json" % r["record_id"])))
        print("断点续跑: 已存在产物 %d 条将被跳过" % done)
    if only_secs:
        print("定向补板块: %s（%s 模式，覆盖旧产物）" % (",".join(sorted(only_secs)),
                                                    "强制重跑" if a.force else "仅补缺失"))
    if a.dry_run:
        print("[dry-run] 不调用 LLM，结束。")
        return

    backend = None
    if a.backend == "mock":
        backend = MockBackend()
        print("[mock] 仅产出 raw 骨架，不调 LLM")
    else:
        is_deepseek = "deepseek" in a.base_url.lower()
        is_claude = "claude" in a.model.lower()
        backend = OpenAIBackend(a.base_url, a.api_key, a.model,
                                is_deepseek=is_deepseek, is_claude=is_claude,
                                disable_thinking=True)
        if is_deepseek:
            print("[deepseek] 模式: thinking=disabled, response_format=json_object")

    def todo(r):
        # 定向补板块：始终处理该记录（补指定板块），不做跳过
        if only_secs:
            return r
        if r["record_id"] in done_index:
            return None
        if a.resume and os.path.exists(os.path.join(AGENT_ROOT, r["record_id"], "%s.json" % r["record_id"])):
            return None
        return r

    # 加载完成索引（含合并产物记录的补登记，二者语义一致：9 板块已齐）
    done_index = set()
    if a.resume and os.path.exists(DONE_INDEX):
        with open(DONE_INDEX, encoding="utf-8") as f:
            done_index = {ln.strip() for ln in f if ln.strip()}
        print("完成索引: %d 条" % len(done_index))

    recs_todo = [r for r in recs if todo(r) is not None]
    print("待处理: %d 条" % len(recs_todo))

    ok = 0
    t0 = time.time()
    n_todo = len(recs_todo)
    # 真分批提交：每块最多 batch_size 条，逐块 submit→收集，避免一次性把 8 万条 future 压进内存。
    # QUOTA 早停跨块生效；--resume 已在 recs_todo 层跳过已完成记录。
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for b0 in range(0, n_todo, a.batch_size):
            batch = recs_todo[b0:b0 + a.batch_size]
            futs = {ex.submit(process_record, r, backend, False, a.section_workers, only_secs): r
                    for r in batch}
            for i, fut in enumerate(as_completed(futs), 1):
                rid, files = fut.result()
                ok += 1
                # 完成即登记：该记录 9 板块（含 _failed 标记骨架）已全部落盘，
                # 任何时刻停车重启都无需重跑本条（_failed 板块走 --sections 定向补）。
                with open(DONE_INDEX, "a", encoding="utf-8") as fidx:
                    fidx.write(rid + "\n")
                if QUOTA.is_set() or TRIP.is_set():
                    print("  [STOP] 额度耗尽或鉴权熔断，提前终止；已完成记录已落盘，修复后重跑同组即可续接")
                    break
                if i % 20 == 0 or b0 + i == n_todo:
                    u = backend.usage
                    print("  进度 %d/%d  (%.1fs) ｜ 累计 token 提示=%d 补完=%d 总=%d"
                          % (b0 + i, n_todo, time.time() - t0,
                             u["prompt_tokens"], u["completion_tokens"], u["total_tokens"]))
            if QUOTA.is_set():
                break
    if QUOTA.is_set() or TRIP.is_set():
        why = "额度耗尽(403)" if QUOTA.is_set() else "鉴权/连接持续失败(401熔断)"
        print("[STOP] %s：本批提前终止（EXIT=3）。已完成记录已落盘，修复鉴权后重跑同组即可续接。" % why)
        sys.exit(3)
    u = backend.usage
    print("产出完成: %d 条 → 运行 assemble.py --all && validate.py --all 合并校验" % ok)
    print("==== 真实 token 用量（累计）====")
    print("  prompt_tokens     (输入): %d" % u["prompt_tokens"])
    print("  completion_tokens (输出): %d" % u["completion_tokens"])
    print("  total_tokens           : %d" % u["total_tokens"])

if __name__ == "__main__":
    main()
