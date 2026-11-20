import sys, os, io, contextlib, signal

ROOT = os.environ["BC_CLINIC_ROOT"]
AGENTS = os.path.join(ROOT, "结构化处理脚本/05_agents")
sys.path.insert(0, AGENTS)
import validate as V

IDX = V.DATA + "/中间产物/agents_v7/_done_index.txt"
with open(IDX, encoding="utf-8") as f:
    rids = [ln.strip() for ln in f if ln.strip()]

RID_TIMEOUT = 30

class _RidTimeout(Exception):
    pass

def _handler(signum, frame):
    raise _RidTimeout()

signal.signal(signal.SIGALRM, _handler)

n_pass = n_issue = n_err = n_timeout = 0
issue_sample = []
err_sample = []
total = len(rids)
for rid in rids:
    signal.alarm(RID_TIMEOUT)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = V.validate(rid, rewrite=False)
    except _RidTimeout:
        signal.alarm(0)
        n_timeout += 1
        if len(err_sample) < 50:
            err_sample.append(rid)
        continue
    except Exception as e:
        signal.alarm(0)
        n_err += 1
        if len(err_sample) < 50:
            err_sample.append("%s:%s" % (rid, repr(e)))
        continue
    signal.alarm(0)
    if rc == 0:
        n_pass += 1
    else:
        n_issue += 1
        if len(issue_sample) < 200:
            issue_sample.append(rid)
    done = n_pass + n_issue + n_err + n_timeout
    if done % 2000 == 0:
        print("validate %d/%d" % (done, total), flush=True)

print("=== validate_all: pass=%d issue=%d err=%d timeout=%d total=%d ==="
      % (n_pass, n_issue, n_err, n_timeout, total), flush=True)
print("ISSUE_SAMPLE: " + " ".join(issue_sample), flush=True)
if err_sample:
    print("ERR_SAMPLE: " + " ".join(err_sample), flush=True)
