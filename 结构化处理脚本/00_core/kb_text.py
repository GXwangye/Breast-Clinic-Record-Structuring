# -*- coding: utf-8 -*-
import re

CJK = re.compile(r"[\u4e00-\u9fff]")
PUNCT = re.compile(r"[\s\u3000\u200b\"'“”‘’()（）\[\]【】<>《》,，、;；:：/\\|+*#\-_]+")
_ASCII_SEG = re.compile(r"[A-Za-z][A-Za-z0-9\-]*")

def units(s):
    if not s:
        return []
    out = []
    for seg in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9\-]*", str(s)):
        if CJK.match(seg):
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            low = seg.lower()
            out.append(low)
            flat = low.replace("-", "")
            if flat != low and len(flat) >= 2:
                out.append(flat)
            head = re.match(r"[a-z]+", low)
            if head and len(head.group()) >= 2 and head.group() != low:
                out.append(head.group())
            # 数字段单独成元：15-3 → 153 / 15 / 3 之外的稳定形式
            digits = re.findall(r"\d+", low)
            if digits and len(low) <= 12:
                joined = "".join(digits)
                if len(joined) >= 2:
                    out.append(joined)
    return out

def squash(s):
    s = str(s or "").replace("＋", "+").replace("＊", "*").replace("＃", "#")
    s = re.sub(r"[\u3000\u200b]", "", s)
    return PUNCT.sub("", s).lower()
