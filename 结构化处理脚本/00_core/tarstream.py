# -*- coding: utf-8 -*-
import json
import os
import subprocess
import tarfile

def _is_meta(name):
    base = os.path.basename(name)
    return base.startswith("._") or "__MACOSX" in name

def iter_records(path, suffix=".json", limit=0):
    n = 0
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(suffix) or _is_meta(m.name):
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            yield os.path.basename(m.name), json.loads(fh.read().decode("utf-8"))
            n += 1
            if limit and n >= limit:
                return

def iter_raw(path, suffix=".json"):
    with tarfile.open(path) as tf:
        for m in tf.getmembers():
            if not m.isfile() or not m.name.endswith(suffix) or _is_meta(m.name):
                continue
            fh = tf.extractfile(m)
            if fh is None:
                continue
            yield os.path.basename(m.name), fh.read()

def pack(src_dir, tar_path, force=False):
    if os.path.exists(tar_path) and not force:
        return tar_path
    os.makedirs(os.path.dirname(tar_path), exist_ok=True)
    env = dict(os.environ, COPYFILE_DISABLE="1")
    subprocess.run(["tar", "-cf", tar_path, "-C", src_dir, "."],
                   check=True, env=env)
    return tar_path

def count(tar_path, suffix=".json"):
    with tarfile.open(tar_path) as tf:
        return sum(1 for m in tf.getmembers()
                   if m.isfile() and m.name.endswith(suffix) and not _is_meta(m.name))

_MADE = set()

def write_json(obj, path, make_dir=True):
    d = os.path.dirname(path) or "."
    if make_dir and d not in _MADE:
        os.makedirs(d, exist_ok=True)
        _MADE.add(d)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
