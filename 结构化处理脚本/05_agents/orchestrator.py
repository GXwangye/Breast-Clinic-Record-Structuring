# -*- coding: utf-8 -*-
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.environ["BC_CLINIC_ROOT"]
# 数据根：原始数据、中间产物、最终产物统一在此（工作区顶层已无 中间产物/）
DATA = ROOT + "/publish/02_结构化结果与过程"
HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

DEFAULT_API = os.environ.get("OPENAI_BASE_URL", "") + "/chat/completions"
DEFAULT_MODEL = "claude-sonnet-4-6"
ENV_KEY = "AUX_LLM_KEY"

STATE_FILE = os.path.join(DATA, "中间产物", "_orchestrator_state.json")

# 六步定义：name / 完成判据（产物路径，含 tar）/ 命令行构建器
STEPS = [
    {
        "name": "fetch",
        "desc": "① 取数：源 Excel → input_records.jsonl",
        "done_marker": "中间产物/_agents_input/input_records.jsonl",
        "script": "fetch_records.py",
    },
    {
        "name": "preprocess",
        "desc": "② 预处理：标准化 → standardized_v1",
        "done_marker": "中间产物/standardized_v1",
        "script": "standardize_raw.py",
    },
    {
        "name": "extract",
        "desc": "③ 抽取：run_batch 分板块 → agents_v7",
        "done_marker": "中间产物/agents_v7",
        "script": "run_batch.py",
    },
    {
        "name": "assemble",
        "desc": "④ 收口：assemble_all + validate_all → assembled_v7",
        "done_marker": "中间产物/assembled_v7",
        "script": "assemble_all.py",
    },
    {
        "name": "derive",
        "desc": "⑤ 派生：IHC call → TNM 回填 → 辅检形态/枚举归一 → BI-RADS 分级回填 → orders.kind 归一 → 契约字段补齐",
        "done_marker": "中间产物/_packed/assembled_v7_final.tar",
        "script": "derive_ihc_call.py",
    },
    {
        "name": "normalize",
        "desc": "⑥ 归一化：canon 小周期 → _norm",
        "done_marker": "中间产物/_norm",
        "script": "run_canon_cycle.py",
    },
]

def build_cmd(step, args):
    name = step["name"]
    script = os.path.join(HERE, step["script"])
    cmd = [PY, script]
    if name == "fetch":
        if args.fetch_rows:
            cmd += ["--rows", args.fetch_rows]
        cmd += ["--out", os.path.join(args.root, "中间产物", "_agents_input", "input_records.jsonl")]
    elif name == "preprocess":
        # standardize_raw 只支持 --limit/--rids/--workers/--dry-run/--verbose，
        # 根目录走 BC_CLINIC_ROOT 环境变量、key 走 AUX_LLM_KEY（均在 run_step 注入）。
        if args.limit:
            cmd += ["--limit", str(args.limit)]
    elif name == "extract":
        # run_batch 支持 --backend/--model/--api-key/--resume/--limit，根目录走 BC_CLINIC_ROOT。
        cmd += ["--backend", args.backend, "--model", args.model]
        if args.api_key:
            cmd += ["--api-key", args.api_key]
        if args.resume:
            cmd += ["--resume"]
        if args.limit:
            cmd += ["--limit", str(args.limit)]
    elif name == "assemble":
        # 收口 = assemble_all + validate_all，二者缺一不算完成；根目录走 BC_CLINIC_ROOT。
        cmd_validate = [PY, os.path.join(HERE, "validate_all.py")]
        return [cmd, cmd_validate]
    elif name == "derive":
        # 派生 = IHC call → TNM 原文确定性回填 → 辅检形态/枚举归一 → BI-RADS 分级确定性回填
        #       → orders.kind 七桶归一 → 契约字段补齐，六步串行、缺一不算完成。
        # 铁律：规则层不做语义抽取。TNM 回填只搬「原文显式三段式 + 归属唯一」的串；
        # 辅检归一只做形态改写与闭集映射（impressions 包对象、日期占位清空、
        # report_type/body_site/laterality 归闭集、labs 键统一），零推断、逐条留痕。
        # 而 final.tar 晚其 41 分钟）；重建时落 _kind.tar，避免读写同一文件。
        packed = os.path.join(args.root, "中间产物", "_packed")
        v_ihc = os.path.join(packed, "assembled_v7_final_ihc.tar")
        v_tnm = os.path.join(packed, "assembled_v7_final_tnm.tar")
        v_aux = os.path.join(packed, "assembled_v7_final_aux.tar")
        v_birads = os.path.join(packed, "assembled_v7_final_birads.tar")
        v_kind = os.path.join(packed, "assembled_v7_final_kind.tar")
        v_final = os.path.join(packed, "assembled_v7_final.tar")
        cmd += ["--out-tar", v_ihc]
        if args.force:
            cmd += ["--force"]
        cmd_bf = [PY, os.path.join(HERE, "backfill_tnm_from_text.py"),
                  "--src-tar", v_ihc, "--out-tar", v_tnm]
        cmd_norm = [PY, os.path.join(HERE, "normalize_aux_enum.py"),
                    "--src-tar", v_tnm, "--out-tar", v_aux]
        cmd_br = [PY, os.path.join(HERE, "backfill_birads_from_text.py"),
                  "--src-tar", v_aux, "--out-tar", v_birads]
        cmd_ok = [PY, os.path.join(HERE, "normalize_order_kind.py"),
                  "--src-tar", v_birads, "--out-tar", v_kind]
        cmd_cf = [PY, os.path.join(HERE, "derive_contract_fields.py"),
                  "--src-tar", v_kind, "--out-tar", v_final]
        for c in (cmd_bf, cmd_norm, cmd_br, cmd_ok, cmd_cf):
            if args.force:
                c += ["--force"]
        return [cmd, cmd_bf, cmd_norm, cmd_br, cmd_ok, cmd_cf]
    elif name == "normalize":
        cmd += ["--model", args.model]
        if args.api_key:
            cmd += ["--api-key", args.api_key]
    return cmd

def step_done(step, args):
    marker = os.path.join(args.root, step["done_marker"])
    return os.path.exists(marker)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_steps": [], "log": []}

def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def run_step(step, args):
    cmds = build_cmd(step, args)
    if not isinstance(cmds[0], list):
        cmds = [cmds]
    env = dict(os.environ)
    env["BC_CLINIC_ROOT"] = ROOT
    # key 统一注入两个子脚本惯用名（standardize_raw 读 AUX_LLM_KEY，
    # run_canon_cycle 读 OPENAI_API_KEY；单一 key，两处同值）
    key = args.api_key or os.environ.get(ENV_KEY, "")
    if key:
        env["AUX_LLM_KEY"] = key
        env["OPENAI_API_KEY"] = key
    t0 = time.time()
    ok = True
    tail = []
    rc = 0
    for cmd in cmds:
        print("\n▶ 执行步骤 [%s] %s" % (step["name"], step["desc"]))
        print("  $ %s" % " ".join(cmd))
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, env=env)
        lines = (r.stdout or "").strip().splitlines()[-5:]
        if r.returncode != 0:
            ok = False
            lines += (r.stderr or "").strip().splitlines()[-5:]
        tail.extend(lines)
        rc = r.returncode
        print("  → %s (rc=%d)" % ("OK" if r.returncode == 0 else "FAIL", r.returncode))
        for line in lines:
            print("    | " + line)
        if r.returncode != 0:
            break
    dt = time.time() - t0
    summary = {"ok": ok, "rc": rc, "elapsed_s": round(dt, 1), "tail": tail}
    return ok, summary

def llm_decision(key, model, api, context):
    if not key:
        return rule_fallback(context)
    user = json.dumps(context, ensure_ascii=False)
    sys_prompt = (
        "你是病历结构化流水线的顶层编排决策器。给定六步流水线的当前进度与上一步执行摘要，"
        "判断下一步动作。只输出 JSON：{\"action\": \"continue|skip|stop\", \"reason\": \"...\", "
        "\"attribution\": \"上一步失败的归因（若成功则空）\"}。\n"
        "规则：上一步成功→continue；上一步失败→stop 并归因（哪个子脚本/哪一层：取数/预处理/抽取/收口/派生/归一化）；"
        "产物已存在→skip。不臆测、不越权替子脚本做判断。"
    )
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}],
        "max_tokens": 500, "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        api, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8"))
    raw = d["choices"][0]["message"]["content"]
    raw = raw[raw.find("{"):raw.rfind("}") + 1]
    return json.loads(raw)

def rule_fallback(context):
    last = context.get("last_step")
    if last is None:
        return {"action": "continue", "reason": "无历史，从第一步开始", "attribution": ""}
    if last.get("ok"):
        return {"action": "continue", "reason": "上一步成功，继续", "attribution": ""}
    return {"action": "stop", "reason": "上一步失败，停止待人工",
            "attribution": "步骤 [%s] 执行失败（rc=%d）" % (last.get("name"), last.get("rc"))}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=str, default=DATA, help="数据根，默认 publish/02_结构化结果与过程")
    ap.add_argument("--dry-run", action="store_true", help="只规划六步，不执行")
    ap.add_argument("--resume", action="store_true", help="跳过已完成的步骤")
    ap.add_argument("--until", default="", help="只推进到指定步骤名（含）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="顶层决策 LLM（子脚本另有各自 --model）")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--api-key", default="", help="顶层决策 LLM key（默认取 AUX_LLM_KEY）")
    ap.add_argument("--backend", default="openai", choices=["mock", "ollama", "openai"],
                    help="抽取步骤后端（透传 run_batch）")
    ap.add_argument("--limit", type=int, default=0, help="预处理/抽取步骤只跑前 N 条（0=全部）")
    ap.add_argument("--fetch-rows", default="", help="取数步骤指定行号（逗号分隔）")
    ap.add_argument("--force", action="store_true", help="派生步骤允许覆盖已有产物")
    args = ap.parse_args()

    key = args.api_key or os.environ.get(ENV_KEY, "")
    state = load_state()

    print("=== 顶层编排智能体 === 根目录 %s" % args.root)
    print("六步流水线：")
    for s in STEPS:
        marker = os.path.join(args.root, s["done_marker"])
        done = "✓" if os.path.exists(marker) else "○"
        print("  %s %s" % (done, s["desc"]))

    until_idx = None
    if args.until:
        for i, s in enumerate(STEPS):
            if s["name"] == args.until:
                until_idx = i
                break
        if until_idx is None:
            print("[错误] 未知步骤名 --until=%s" % args.until)
            sys.exit(2)

    for i, step in enumerate(STEPS):
        if until_idx is not None and i > until_idx:
            break
        if args.dry_run:
            cmds = build_cmd(step, args)
            if not isinstance(cmds[0], list):
                cmds = [cmds]
            print("\n[dry-run] 将执行 [%s] %s" % (step["name"], step["desc"]))
            for c in cmds:
                print("  $ %s" % " ".join(c))
            continue

        if args.resume and step_done(step, args):
            print("\n⏭ 跳过已完成步骤 [%s]" % step["name"])
            continue

        ok, summary = run_step(step, args)
        context = {
            "step": step["name"],
            "last_step": {"name": step["name"], "ok": ok, "rc": summary["rc"],
                          "tail": summary["tail"]},
        }
        if ok:
            state["done_steps"].append(step["name"])
            state["log"].append({"step": step["name"], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "ok": True, "elapsed_s": summary["elapsed_s"]})
            save_state(state)
            continue

        # 失败 → 顶层 LLM 归因（不自动改文件、不自动重跑，停批待人工，遵循 HALT 铁律）
        state["log"].append({"step": step["name"], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "ok": False, "rc": summary["rc"], "tail": summary["tail"]})
        save_state(state)
        decision = llm_decision(key, args.model, args.api, context)
        print("\n■ 顶层决策：action=%s" % decision.get("action"))
        print("  归因：%s" % (decision.get("attribution") or "（无）"))
        print("  理由：%s" % decision.get("reason"))
        if decision.get("action") == "stop":
            print("[STOP] 停止流水线，等待人工介入修复后再 --resume 续跑。")
            sys.exit(1)
        # action=continue 则继续下一步（此处不出现，因失败默认 stop，除非 LLM 明确 continue）
        print("[继续] 按顶层决策继续下一步")

    print("\n=== 编排完成 ===")
    print("已完成步骤：%s" % (", ".join(state["done_steps"]) or "（无）"))
    print("状态文件：%s" % STATE_FILE)

if __name__ == "__main__":
    main()
