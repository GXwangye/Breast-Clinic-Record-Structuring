import sys, os, json, signal, time

ROOT = os.environ["BC_CLINIC_ROOT"]
AGENTS = os.path.join(ROOT, "结构化处理脚本/05_agents")
sys.path.insert(0, AGENTS)
from assemble import assemble, AGENT_ROOT

DATA = ROOT + "/publish/02_结构化结果与过程"  # 数据根：原始数据+中间产物+最终产物统一在此
ASSEMBLED_ROOT = DATA + "/中间产物/assembled_v7"
os.makedirs(ASSEMBLED_ROOT, exist_ok=True)

IDX = DATA + "/中间产物/agents_v7/_done_index.txt"
with open(IDX, encoding="utf-8") as f:
    rids = [ln.strip() for ln in f if ln.strip()]

RID_TIMEOUT = 30
PACING = 0.04  # 每条节流 40ms

class _RidTimeout(Exception):
    pass

def _handler(signum, frame):
    raise _RidTimeout()

signal.signal(signal.SIGALRM, _handler)

def _valid(path):
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        return isinstance(rec, dict) and "record_id" in rec and "meta" in rec \
            and isinstance(rec.get("meta", {}).get("assembled_from"), list)
    except Exception:
        return False

def already_done(rid):
    # 新位置优先，旧位置（前次已合并的 ~4 万条）也认
    if _valid(os.path.join(ASSEMBLED_ROOT, "%s.json" % rid)):
        return True
    return _valid(os.path.join(AGENT_ROOT, rid, "%s.json" % rid))

n_ok = n_skip = n_err = n_timeout = 0
timeouts = []
t0 = time.time()
total = len(rids)
for i, rid in enumerate(rids):
    if already_done(rid):
        n_skip += 1
        if n_skip % 5000 == 0:
            print("skip %d ..." % n_skip, flush=True)
        continue
    signal.alarm(RID_TIMEOUT)
    try:
        rec = assemble(rid)
    except _RidTimeout:
        signal.alarm(0)
        n_timeout += 1
        timeouts.append(rid)
        if len(timeouts) <= 50:
            print("TIMEOUT %s" % rid, flush=True)
        continue
    except Exception as e:
        signal.alarm(0)
        n_err += 1
        if n_err <= 50:
            print("ERR %s %s" % (rid, repr(e)), flush=True)
        continue
    signal.alarm(0)
    out = os.path.join(ASSEMBLED_ROOT, "%s.json" % rid)
    try:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, separators=(",", ":"))
    except Exception as e:
        n_err += 1
        if n_err <= 50:
            print("WRITE_ERR %s %s" % (rid, repr(e)), flush=True)
        continue
    n_ok += 1
    if n_ok % 1000 == 0:
        dt = time.time() - t0
        print("assemble %d/%d (skip=%d err=%d timeout=%d %.1fs)"
              % (n_ok, total, n_skip, n_err, n_timeout, dt), flush=True)
    time.sleep(PACING)

print("=== assemble_all v3: ok=%d skip=%d err=%d timeout=%d total=%d ==="
      % (n_ok, n_skip, n_err, n_timeout, total), flush=True)
if timeouts:
    with open("/tmp/assemble_timeouts.txt", "w") as fh:
        fh.write("\n".join(timeouts))
    print("timeouts -> /tmp/assemble_timeouts.txt (%d)" % len(timeouts), flush=True)
