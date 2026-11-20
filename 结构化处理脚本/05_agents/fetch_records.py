
import argparse
import json
import os
import sys

ROOT = os.environ["BC_CLINIC_ROOT"]
DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
SRC = DATA + "/数据源/乳腺疾病门诊记录_20240915.xlsx"
OUTDIR = DATA + "/中间产物/_agents_input"
OUT = OUTDIR + "/input_records.jsonl"

# 列名 → 索引（与源表头严格对应，改源须同步）
ID_COLS = ["病历类型", "病历ID", "性别", "登记号", "就诊次数级别",
           "医生所属科室", "就诊日期", "就诊医生"]
FIELD_COLS = ["主诉", "现病史", "既往史", "辅助检查", "体格检查"]
DX_COLS = ["诊断%d" % i for i in range(1, 15)]
DX_EXTRA = ["诊断复杂度", "下一步处理", "住院"]
ORDER_COLS = ["医嘱"] + ["医嘱%d" % i for i in range(1, 61)]

ALL_COLS = ID_COLS + FIELD_COLS + DX_COLS + DX_EXTRA + ORDER_COLS

def _s(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()

def _richness(rec):
    score = 0
    score += len(rec["fields"].get("辅助检查", "")) * 1.0
    score += len(rec["fields"].get("现病史", "")) * 0.8
    score += len(rec["fields"].get("既往史", "")) * 0.3
    score += len(rec["fields"].get("体格检查", "")) * 0.3
    score += len(rec["diagnoses_text"]) * 0.5
    score += rec["orders_nonempty"] * 60
    return score

def read_rows(rows=None, scan=None, top=None):
    import openpyxl
    wb = openpyxl.load_workbook(SRC, read_only=True)
    ws = wb[wb.sheetnames[0]]
    it = ws.iter_rows(min_row=1, values_only=True)
    header = [_s(c) for c in next(it)]
    idx = {name: header.index(name) for name in ALL_COLS if name in header}
    missing = [c for c in ALL_COLS if c not in idx]
    if missing:
        print("⚠ 源表缺少列: %s" % missing, file=sys.stderr)

    out = []
    want = set(rows or [])
    for i, row in enumerate(it, start=1):  # i = 数据行号（1 起，不含表头）
        if rows is not None and i not in want:
            if scan is None:
                continue
            if want and max(want) < i:
                break
        if rows is not None and i not in want and scan is None:
            continue
        if scan is not None and i > scan and not want:
            break

        ids = {c: _s(row[idx[c]]) for c in ID_COLS if c in idx}
        fields = {c: _s(row[idx[c]]) for c in FIELD_COLS if c in idx}
        dxs = [(c, _s(row[idx[c]])) for c in DX_COLS if c in idx]
        dxs = [(c, v) for c, v in dxs if v]
        orders = [(c, _s(row[idx[c]])) for c in ORDER_COLS if c in idx]
        orders = [(c, v) for c, v in orders if v]
        ext = {c: _s(row[idx[c]]) for c in DX_EXTRA if c in idx}

        rec = {
            "row_index": i,
            "record_id": ids.get("病历ID", ""),
            "ids": ids,
            "fields": fields,
            "diagnoses": dxs,
            "diagnoses_text": "\n".join("%s: %s" % (c, v) for c, v in dxs),
            "orders": orders,
            "orders_nonempty": len(orders),
            "extra": ext,
        }
        out.append(rec)

        if scan is not None and rows is None and len(out) >= scan:
            break

    wb.close()

    if rows is not None:
        out = [r for r in out if r["row_index"] in want]
        out.sort(key=lambda r: r["row_index"])
    elif top:
        for r in out:
            r["richness"] = round(_richness(r))
        out.sort(key=lambda r: -r["richness"])
        out = out[:top]
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", help="逗号分隔的数据行号（1 起，不含表头）")
    ap.add_argument("--scan", type=int, default=5000, help="扫描前 N 行")
    ap.add_argument("--top", type=int, default=20, help="取富度最高的 K 条")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    rows = [int(x) for x in a.rows.split(",")] if a.rows else None
    recs = read_rows(rows=rows, scan=a.scan, top=a.top)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("写出 %d 条 → %s" % (len(recs), a.out))
    for r in recs:
        print("  row=%-6d id=%-8s 主诉%2d 现病史%4d 既往%3d 辅检%5d 体检%3d 诊断%d 医嘱%d %s"
              % (r["row_index"], r["record_id"],
                 len(r["fields"].get("主诉", "")), len(r["fields"].get("现病史", "")),
                 len(r["fields"].get("既往史", "")), len(r["fields"].get("辅助检查", "")),
                 len(r["fields"].get("体格检查", "")), len(r["diagnoses"]),
                 r["orders_nonempty"], r["extra"].get("就诊次数级别", "")))

if __name__ == "__main__":
    main()
