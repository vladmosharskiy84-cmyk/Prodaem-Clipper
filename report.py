"""Отчёты админу (Владу): что нашли, что смонтировали, какие ошибки — из МУРЧИКА у людей и из полной программы
(Влад и братики, все профили). Всё видно в одном месте: Telegram бота HQ и панель hq-mur.

Что уходит на сервер HQ: имя, которое человек ввёл сам, программа и профиль, версия, название клипа/момента, стиль,
длина, оценка критика, раскадровка готового ролика (маленькая картинка) и текст ошибки с последними строками журнала.
Ключи, токены и сами видео НЕ отправляются. Нет интернета — отчёты копятся в cache/report_queue.jsonl и уйдут позже.
Имя одно на всю программу (user.json рядом с программой), а не на каждую вкладку. webapp ставит ENABLED, APP, PROFILE.
"""
import base64
import json
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import montage as m

SERVER = "http://209.38.41.246:8092"             # сервис hq-mur на сервере HQ (hq/hq/mur.py)
APP_KEY = "mur-7Qx2LkV9cR4tW8pZ"                 # ключ только на запись отчётов: прочитать по нему ничего нельзя
USER_FILE = Path(__file__).resolve().parent / "user.json"     # одно имя на все вкладки программы
QUEUE = m.CACHE / "report_queue.jsonl"
KEEP = 500                                       # без связи долго — храним только последние 500 событий
ENABLED = False
APP = "clipper"                                  # "murchik" — массовое приложение, "clipper" — полная программа
PROFILE = ""                                     # вкладка: СНГ, DOTA, МУРЧИК, РЕАКТЫ…
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
    rec = dict(data, kind=kind, t=time.time(), app=APP, profile=PROFILE)
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
    rv = meta.get("review") or meta.get("agents") or {}
    ev("montage", title=str(title)[:120], style=meta.get("style", ""), dur=meta.get("duration"),
       hook=meta.get("hook", ""), score=rv.get("score"), decision=rv.get("decision", ""),
       reason=str(rv.get("reason") or "")[:200], secs=meta.get("secs"), font=meta.get("font", ""),
       head=meta.get("head", ""), music=bool(meta.get("music")), sheet=sheet(out))


def montage_bg(out, meta, title=""):
    """То же в фоне: раскадровка делается ffmpeg, очередь монтажа не ждёт."""
    if ENABLED:
        threading.Thread(target=montage_done, args=(out, meta, title), daemon=True).start()


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
    threading.Thread(target=_pulse, name="report-pulse", daemon=True).start()
    _usage_hook()


# ---------- пульс: программа открыта и что делает (раз в 3 минуты, без очереди — старый пульс не нужен) ----------
STATE = None                                     # webapp ставит функцию: {"doing", "queue", ...}
PULSE_SEC = 180


def _health():
    import shutil
    try:
        free = round(shutil.disk_usage(str(m.BASE)).free / 1e9, 1)
    except Exception:
        free = None
    return {"disk_gb": free, "ffmpeg": bool(shutil.which("ffmpeg"))}


def _pulse():
    while True:
        try:
            st = STATE() if STATE else {}
        except Exception:
            st = {}
        rec = dict(st, **_health(), kind="ping", t=time.time(), app=APP, profile=PROFILE, usd_today=usd_today())
        try:
            _send([rec])
        except Exception:
            pass
        time.sleep(PULSE_SEC)


# ---------- расходы Claude: считаем по ответам API (usage) — примерно, по ценам за 1M токенов ----------
PRICES = {"haiku-5-5": (0.10, 0.50), "sonnet-5-5": (2.0, 10.0), "opus-5-5": (4.0, 20.0), "fable-5-2": (8.0, 40.0),
          "haiku-4-5": (1.0, 5.0), "sonnet-5": (2.0, 10.0), "sonnet-4-6": (3.0, 15.0), "opus-5": (5.0, 25.0)}
SPEND_FILE = m.CACHE / "claude_spend.json"       # {"дата": {"usd", "in", "out", "calls"}}
_spend_lock = threading.Lock()


def _price(model):
    for k, v in PRICES.items():
        if k in str(model):
            return v
    return (2.0, 10.0)


def usd_today():
    try:
        return round(json.loads(SPEND_FILE.read_text(encoding="utf-8")).get(time.strftime("%Y-%m-%d"), {}).get("usd", 0.0), 4)
    except Exception:
        return 0.0


def _count(data):
    u = data.get("usage") or {}
    pin, pout = _price(data.get("model", ""))
    inp = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    cached = int(u.get("cache_read_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    usd = (inp * pin + cached * pin * 0.1 + out * pout) / 1e6
    day = time.strftime("%Y-%m-%d")
    with _spend_lock:
        try:
            s = json.loads(SPEND_FILE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        d = s.setdefault(day, {"usd": 0.0, "in": 0, "out": 0, "calls": 0})
        d["usd"] += usd
        d["in"] += inp + cached
        d["out"] += out
        d["calls"] += 1
        s = {k: v for k, v in s.items() if k >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 40 * 86400))}
        m.CACHE.mkdir(exist_ok=True)
        SPEND_FILE.write_text(json.dumps(s), encoding="utf-8")


def _usage_hook():
    """Все запросы к Claude в программе идут через requests: смотрим ответ и считаем токены. Сам запрос не меняем."""
    try:
        import requests
    except ImportError:
        return
    orig = requests.sessions.Session.request
    if getattr(orig, "_claude_count", False):
        return

    def request(self, method, url, *a, **k):
        r = orig(self, method, url, *a, **k)
        try:
            if "api.anthropic.com/v1/messages" in str(url) and r.status_code == 200:
                _count(r.json())
        except Exception:
            pass
        return r
    request._claude_count = True
    requests.sessions.Session.request = request
