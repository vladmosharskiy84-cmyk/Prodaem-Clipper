"""Отчёты админу (Владу) из приложения МУРЧИК: что нашли, что смонтировали, какие ошибки.

Что уходит на сервер HQ: имя, которое человек ввёл сам, версия программы, название видео/момента, стиль, длина,
оценка критика, раскадровка готового ролика (маленькая картинка) и текст ошибки с последними строками журнала.
Ключи, токены и сами видео НЕ отправляются. Нет интернета — отчёты копятся в cache/report_queue.jsonl и уйдут позже.
Работает только в режиме монтажа блогера (Мурчик): webapp ставит ENABLED.
"""
import base64
import json
import subprocess
import threading
import time
import urllib.request
import uuid

import montage as m

SERVER = "http://209.38.41.246:8092"             # сервис hq-mur на сервере HQ (hq/hq/mur.py)
APP_KEY = "mur-7Qx2LkV9cR4tW8pZ"                 # ключ только на запись отчётов: прочитать по нему ничего нельзя
USER_FILE = m.BASE / "user.json"
QUEUE = m.CACHE / "report_queue.jsonl"
KEEP = 500                                       # без связи долго — храним только последние 500 событий
ENABLED = False
_lock = threading.Lock()
_wake = threading.Event()
_started = False


def user():
    """{"id", "name"}: id создаётся один раз, имя человек вводит при первом запуске."""
    try:
        u = json.loads(USER_FILE.read_text(encoding="utf-8"))
    except Exception:
        u = {}
    if not u.get("id"):
        u["id"] = uuid.uuid4().hex[:12]
        USER_FILE.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
    return u


def set_name(name):
    u = user()
    u["name"] = str(name or "").strip()[:40]
    USER_FILE.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")
    ev("hello")
    return u


def version():
    try:
        import updater
        return str(json.loads(updater.STATE_FILE.read_text(encoding="utf-8")).get("version", ""))
    except Exception:
        return ""


def ev(kind, **data):
    """Событие в очередь: start, hello, found, montage, error."""
    if not ENABLED:
        return
    rec = dict(data, kind=kind, t=time.time())
    try:
        m.CACHE.mkdir(exist_ok=True)
        with _lock, QUEUE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return
    start()
    _wake.set()


def sheet(video):
    """Раскадровка ролика: 6 кадров в ряд, ~20–40 КБ. Админ видит качество, не скачивая видео."""
    try:
        d = m.probe(video)["dur"]
        jpg = m.CACHE / "report_sheet.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
                        f"fps=6/{max(1.0, d):.2f},scale=150:-2,tile=6x1", "-frames:v", "1", "-q:v", "6", str(jpg)],
                       capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return base64.b64encode(jpg.read_bytes()).decode() if jpg.exists() else ""
    except Exception:
        return ""


def montage_done(out, meta, title=""):
    rv = meta.get("review") or {}
    ev("montage", title=str(title)[:120], style=meta.get("style", ""), dur=meta.get("duration"),
       hook=meta.get("hook", ""), score=rv.get("score"), decision=rv.get("decision", ""), sheet=sheet(out))


def _send(batch):
    u = user()
    body = json.dumps({"id": u["id"], "name": u.get("name", ""), "ver": version(), "events": batch},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{SERVER}/mur/ev?k={APP_KEY}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "murchik"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


def _read():
    return [x for x in QUEUE.read_text(encoding="utf-8").splitlines() if x.strip()] if QUEUE.exists() else []


def _loop():
    while True:
        _wake.wait(60)
        _wake.clear()
        try:
            with _lock:
                lines = _read()
                if len(lines) > KEEP:
                    lines = lines[-KEEP:]
                    QUEUE.write_text("".join(x + "\n" for x in lines), encoding="utf-8")
            if not lines:
                continue
            head = lines[:20]
            batch = []
            for ln in head:
                try:
                    batch.append(json.loads(ln))
                except Exception:
                    pass
            if batch and not _send(batch):
                continue
            with _lock:                                  # отправленные строки — всегда первые; новые дописаны в конец
                rest = _read()[len(head):]
                QUEUE.write_text("".join(x + "\n" for x in rest), encoding="utf-8")
            if rest:
                _wake.set()
        except Exception:
            time.sleep(30)                               # сервер недоступен — попробуем позже


def start():
    global _started
    if _started or not ENABLED:
        return
    _started = True
    threading.Thread(target=_loop, name="report", daemon=True).start()
