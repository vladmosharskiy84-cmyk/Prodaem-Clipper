# -*- coding: utf-8 -*-
"""
Обновления программы через интернет.

У всех:      программа сама проверяет, есть ли новая версия, и ставит её по кнопке.
У автора:    python updater.py release      собирает папку release, которую нужно загрузить в хранилище.

Адрес хранилища лежит в файле update_url.txt (одна строка, папка с файлами и version.json).
Обновляются только файлы программы из списка FILES. Ключи, настройки и список стримеров не трогаются.
"""
import hashlib
import json
import py_compile
import shutil
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
URL_FILE = BASE / "update_url.txt"
STATE_FILE = BASE / "update_state.json"
FILES = ["montage.py", "review.py", "top5.py", "fixcam.py", "clipfinder.py", "updater.py"]


def base_url():
    if not URL_FILE.exists():
        return ""
    lines = URL_FILE.read_text(encoding="utf-8-sig").strip().splitlines()
    url = lines[0].strip() if lines else ""
    # ссылку на страницу GitHub превращаем в адрес самих файлов
    if url.startswith("https://github.com/"):
        parts = url[len("https://github.com/"):].strip("/").split("/")
        if len(parts) >= 2:
            branch = parts[3] if len(parts) >= 4 and parts[2] == "tree" else "main"
            url = f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}/{branch}/"
    if not (url.startswith("https://") or url.startswith("http://127.0.0.1")):
        return ""
    return url if url.endswith("/") else url + "/"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _get(url):
    req = urllib.request.Request(url + ("&" if "?" in url else "?") + f"t={int(time.time())}",
                                 headers={"User-Agent": "prodaem-clipper", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def applied_time():
    try:
        return float(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("time", 0))
    except Exception:
        return 0.0


def check():
    """Есть ли обновление. Возвращает {"available": bool, "version", "notes", "changed": [...]}
    или {"error": "..."}; {"off": True}, если адрес обновлений не задан."""
    url = base_url()
    if not url:
        return {"off": True}
    try:
        info = json.loads(_get(url + "version.json").decode("utf-8-sig"))
    except Exception as e:
        return {"error": f"не получилось проверить обновления: {e}"}
    files = {k: v for k, v in (info.get("files") or {}).items() if k in FILES}
    changed = [n for n, h in files.items() if not (BASE / n).exists() or sha(BASE / n) != h]
    newer = float(info.get("time", 0)) > applied_time()
    return {"available": bool(changed) and newer, "version": str(info.get("version", "")),
            "notes": str(info.get("notes", "")), "changed": changed, "files": files, "time": info.get("time", 0)}


def apply(info):
    """Скачивает и ставит обновление. Сначала проверяет все файлы целиком и только потом заменяет:
    если хоть один файл битый или с ошибкой, программа остаётся как была."""
    url = base_url()
    tmp = BASE / "cache" / "update_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for name in info["changed"]:
        data = _get(url + name)
        if hashlib.sha256(data).hexdigest() != info["files"][name]:
            raise RuntimeError(f"файл {name} скачался не полностью, попробуй ещё раз через пару минут")
        (tmp / name).write_bytes(data)
        if name.endswith(".py"):
            try:
                py_compile.compile(str(tmp / name), cfile=str(tmp / (name + "c")), doraise=True)
            except py_compile.PyCompileError:
                raise RuntimeError(f"в обновлении ошибка в файле {name}, оно не установлено")
    backup = BASE / "backup" / time.strftime("%Y-%m-%d_%H-%M-%S")
    backup.mkdir(parents=True, exist_ok=True)
    for name in info["changed"]:
        if (BASE / name).exists():
            shutil.copy2(BASE / name, backup / name)          # старую версию можно вернуть из папки backup
        shutil.copy2(tmp / name, BASE / name)
    STATE_FILE.write_text(json.dumps({"time": info.get("time", 0), "version": info.get("version", "")}), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return info["changed"]


def make_release(notes=""):
    """Для автора: собирает папку release с файлами программы и version.json."""
    rel = BASE / "release"
    shutil.rmtree(rel, ignore_errors=True)
    rel.mkdir()
    files = {}
    for name in FILES:
        if (BASE / name).exists():
            py_compile.compile(str(BASE / name), cfile=str(BASE / "cache" / (name + "c")), doraise=True)
            shutil.copy2(BASE / name, rel / name)
            files[name] = sha(BASE / name)
    now = time.time()
    info = {"version": time.strftime("%Y.%m.%d %H:%M"), "time": now, "notes": notes, "files": files}
    (rel / "version.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE_FILE.write_text(json.dumps({"time": now, "version": info["version"]}), encoding="utf-8")
    return rel, info


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "release":
        (BASE / "cache").mkdir(exist_ok=True)
        try:
            notes = " ".join(sys.argv[2:]) or input("Что нового в этой версии (одной строкой): ").strip()
        except EOFError:
            notes = ""
        folder, inf = make_release(notes)
        print(f"\nГотово: версия {inf['version']}, файлов: {len(inf['files'])}")
        print(f"Папка: {folder}")
        print("Загрузи ВСЕ файлы из этой папки в своё хранилище на GitHub (Add file -> Upload files -> Commit changes).")
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["explorer", str(folder)])
    else:
        res = check()
        print(res if not res.get("available") else f"Есть обновление {res['version']}: {res['notes']} ({', '.join(res['changed'])})")
