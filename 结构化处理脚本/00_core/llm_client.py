# -*- coding: utf-8 -*-

import json
import re
import time
import urllib.error
import urllib.request

DEFAULT_MAX_TOKENS = 16000
RETRY = 4

class LLMError(RuntimeError):
    pass

class Client:
    def __init__(self, base_url, api_key, model, timeout=180,
                 max_tokens=DEFAULT_MAX_TOKENS, temperature=0.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.usage = {"prompt": 0, "completion": 0, "calls": 0}

    def chat(self, system, user):
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.key)
        last = None
        for attempt in range(RETRY):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode("utf-8"))
                u = d.get("usage") or {}
                self.usage["prompt"] += u.get("prompt_tokens", 0)
                self.usage["completion"] += u.get("completion_tokens", 0)
                self.usage["calls"] += 1
                return d["choices"][0]["message"]["content"]
            except Exception as e:                  # 连接类/5xx/429 一律退避重试
                last = e
                time.sleep(3 * (attempt + 1))
        raise LLMError("调用失败：%s" % last)

def parse_json_array(txt):
    if not txt:
        return None
    s = txt.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    i = s.find("[")
    if i < 0:
        return None
    body = s[i:]

    def _try(t):
        try:
            v = json.loads(t)
            return v if isinstance(v, list) else None
        except Exception:
            return None

    j = body.rfind("]")
    if j > 0:
        v = _try(body[:j + 1])
        if v is not None:
            return v
    for k in range(len(body) - 1, 0, -1):
        if body[k] != "}":
            continue
        v = _try(body[:k + 1] + "]")
        if v is not None:
            return v
    return None
