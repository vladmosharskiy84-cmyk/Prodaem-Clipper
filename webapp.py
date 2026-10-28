# -*- coding: utf-8 -*-
"""
Новый интерфейс программы: окно рисуется как современная страница (файл ui.html), а работает всё то же самое,
что в старом окне. Python поднимает маленький сервер только для этого компьютера (127.0.0.1) и открывает
Edge в режиме приложения: отдельное окно без адресной строки. Edge есть в любой Windows 10 и 11.

Запуск: python review.py (новое окно открывается само). Старое окно:  python review.py --old
"""
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import montage as m
import review as rv

BASE = rv.BASE                                   # данные этого профиля
CODE = rv.CODE                                   # сама программа (одна на все профили)
UI_FILE = CODE / "ui.html"
PORT_FILE = CODE / "cache" / "web_port.txt"
RENDER_LOCK_PORT = 47613                         # монтаж идёт по одному ролику на весь компьютер, во всех профилях сразу
LOG_FILE = BASE / "cache" / "app.log"
TOKEN = secrets.token_urlsafe(18)                # без него страница из интернета не сможет управлять программой

SHORT = {"talk": "Разговор", "talk_satis": "Разговор + залипалка", "react": "Реакция",
         "cs": "КС с камерой", "cs_nocam": "КС без камеры", "multi": "Несколько стримеров"}


# ---------- общее состояние ----------
class App:
    def __init__(self):
        self.cfg = json.loads((BASE / "montage.json").read_text(encoding="utf-8"))
        self.enc = None
        self.st = rv.load_state()
        self.lock = threading.RLock()
        self.events = []                         # события для страницы: журнал, всплывашки, обновления
        self.seq = 0
        self.jobs = queue.Queue()
        self.job_list = []                       # последние задачи монтажа для панели «Монтаж»
        self.busy = None
        self.done = 0
        self.failed = 0
        self.items = []
        self.queue_ver = 0
        self.last_poll = time.time()
        self.finding = False
        self.upd = {"info": None, "busy": False}
        self.pv = {"busy": False, "next": None, "ver": 0, "png": None}
        self.bpv = {"ver": 0, "png": None}
        self.pvv = {"ver": 0, "busy": False, "next": None, "file": None}
        self.dialogs = queue.Queue()             # окна выбора файлов открываются в главном потоке
        self.quit = threading.Event()
        self.restart_cmd = None

    # события
    def emit(self, kind, **data):
        with self.lock:
            self.seq += 1
            self.events.append(dict(data, seq=self.seq, kind=kind))
            if len(self.events) > 1500:
                self.events = self.events[-1000:]

    def log(self, text):
        for line in str(text).splitlines():
            if line.strip():
                self.emit("log", text=line.rstrip(), t=time.strftime("%H:%M"))
                with self.lock:
                    for j in self.job_list:
                        if j["state"] == "run":
                            j["stage"] = line.strip()[:120]
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(text).rstrip() + "\n")
        except Exception:
            pass

    def toast(self, text, ok=True):
        self.emit("toast", text=text, ok=ok)

    # очередь клипов
    def refresh(self):
        with self.lock:
            self.items = rv.load_queue(self.st)
            self.queue_ver += 1
        self.emit("queue", ver=self.queue_ver)
        try:
            prep_queue()                         # новые клипы сразу готовятся для просмотра
        except NameError:
            pass

    def item(self, cid):
        with self.lock:
            for p in self.items:
                if p["clip_id"] == cid:
                    return p
        raise KeyError("клип уже не в очереди, выбери другой")


A = App()


class Out:
    """Всё, что печатают монтаж и поиск, попадает в журнал окна."""
    def __init__(self):
        self.buf = ""

    def write(self, s):
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            A.log(line)

    def flush(self):
        pass


# ---------- профили ----------
LABELS = {"": "СНГ", "us": "US", "dota": "DOTA", "murchik": "МУРЧИК"}
ROLE = {"child": False, "parent": 0, "children": {}}     # главный процесс знает порты профилей


def profile_dirs():
    """Папки данных профилей: C:\\clipper (СНГ) и рядом C:\\clipper_us, C:\\clipper_dota и другие."""
    out = [("", CODE)]
    order = {"us": 0, "dota": 1, "murchik": 2}
    for d in sorted(CODE.parent.glob(CODE.name + "_*"), key=lambda x: (order.get(x.name.rsplit("_", 1)[-1].lower(), 9), x.name)):
        if d.is_dir() and (d / "profile.txt").exists() and (d / "montage.json").exists():
            out.append((d.name[len(CODE.name) + 1:].lower(), d))
    return out


def profile_label(key, d):
    try:
        return (d / "profile.txt").read_text(encoding="utf-8-sig").strip()[:12] or LABELS.get(key, key.upper())
    except Exception:
        return LABELS.get(key, key.upper())


def my_key():
    for k, d in profile_dirs():
        if d.resolve() == BASE.resolve():
            return k
    return (rv.PROFILE or "").lower()


def ports_map():
    """Порт каждого профиля: у главного свой, у остальных из списка, который главный разослал."""
    if ROLE["child"]:
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{ROLE['parent']}/ports?k={TOKEN}", timeout=2) as r:
                return {k: int(v) for k, v in json.loads(r.read().decode()).items()}
        except Exception:
            return {my_key(): SERVER["port"]}
    return dict(ROLE["children"], **{"": SERVER["port"]})


# ---------- что отдаём странице ----------
def init_data():
    fmts = [{"key": k, "label": SHORT[k], "full": lab, "hint": h} for k, lab, h in rv.FORMATS]
    gl = A.cfg.get("game_label")
    if gl:
        for f in fmts:
            f["label"] = f["label"].replace("КС", gl)
    ports, me = ports_map(), my_key()
    profiles = []
    for key, d in profile_dirs():
        if key in ports:
            profiles.append({"key": key, "label": profile_label(key, d), "current": key == me,
                             "url": f"http://127.0.0.1:{ports[key]}/?k={TOKEN}"})
    return {"app": rv.APP_NAME, "profile": (rv.PROFILE or "").lower(), "profiles": profiles, "formats": fmts,
            "styles": [["random", "Случайный"]] + [[k, v] for k, v in m.STYLES.items()], "geo": list(m.GEO_STYLES),
            "fonts": m.available_fonts(), "font_lib": {k: list(v) for k, v in m.FONT_LIB.items()}, "style": A.st.get("style_key") or _style_key(A.st.get("style")),
            "anims": [[k, v] for k, v in m.SUB_ANIMS.items()], "anim": A.st.get("sub_anim") or "karaoke",
            "zoom": A.st.get("zoom", True), "teaser": A.st.get("teaser", True),
            "font": A.st.get("font") if A.st.get("font") in m.available_fonts() else "",
            "banners": banners_short(), "banner_use": A.cfg.get("banner_use"),
            "needs_setup": rv.needs_setup() and A.cfg.get("mode") != "blogger", "game": gl or "КС",
            "video_broken": VB_FILE.exists(), "mode": A.cfg.get("mode") or "clips",
            "can_murchik": not ROLE["child"] and not any(k == "murchik" for k, _ in profile_dirs())}


def _style_key(label):
    for k, v in m.STYLES.items():
        if v == label:
            return k
    return "random" if label == "Случайный" else "classic"


def banners_short():
    out = []
    for name, b in m.banner_list(A.cfg).items():
        out.append({"name": name, "speed": float(b.get("speed") or 1), "fade": float(b.get("fade") or 0)})
    return out


def queue_data():
    with A.lock:
        items = list(A.items)
    out = []
    for p in items:
        out.append({"id": p["clip_id"], "streamer": "СВОЁ" if p.get("own") else p.get("streamer", ""),
                    "title": p.get("title", ""), "views": int(p.get("twitch_views") or 0),
                    "dur": int(p.get("duration") or 0), "own": bool(p.get("own")),
                    "score": p.get("score"), "reason": p.get("reason", ""), "vod": p.get("source") == "vod",
                    "hot": int(p.get("hot") or 0)})
    return {"items": out, "ver": A.queue_ver}


def open_clip(cid):
    p = A.item(cid)
    info = m.probe(p["file"])
    w, h = info["w"], info["h"]
    cap = None
    zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8")) if m.ZONES_FILE.exists() else {}
    z = zones.get(p.get("streamer", ""))
    if z and abs(w / h - 16 / 9) < 0.06:
        cap = [int(h * z[0] / 100), int(h * z[1] / 100)]
    guess = "cs" if p.get("format") == "cs" else "talk"
    rects, content, from_last = [], None, False
    last = A.st.get("last", {}).get(p.get("streamer", ""))
    if last and not p.get("own"):
        if last["fmt"] in [k for k, _, _ in rv.FORMATS]:
            guess = last["fmt"]
        kk = w / 1920
        rects = [[int(v * kk) for v in r] for r in last["rects"]]
        if last.get("content"):
            content = [int(v * kk) for v in last["content"]]
        from_last = bool(rects)
    ext = Path(p["file"]).suffix.lower()
    return {"id": cid, "w": w, "h": h, "dur": info["dur"], "streamer": p.get("streamer", ""),
            "title": p.get("title", ""), "views": int(p.get("twitch_views") or 0), "own": bool(p.get("own")),
            "fmt": guess, "rects": rects, "content": content, "cap": cap, "from_last": from_last,
            "sub_pos": A.st.get("sub_pos", {}), "video_ok": ext in (".mp4", ".m4v", ".webm", ".mov"),
            "score": p.get("score"), "reason": p.get("reason", ""),
            "vod_at": p.get("vod_at")}


def snapshot(since):
    with A.lock:
        ev = [e for e in A.events if e["seq"] > since]
        jobs = [dict(j) for j in A.job_list[-12:]]
    now = time.time()
    for j in jobs:
        j["sec"] = int((j.get("end") or now) - j["start"]) if j.get("start") else 0
    return {"events": ev, "seq": A.seq, "jobs": jobs, "busy": bool(A.busy) or VOD["busy"] or TT["busy"], "wait": A.jobs.qsize(),
            "done": A.done, "failed": A.failed, "queue_ver": A.queue_ver, "finding": A.finding,
            "render": bool(A.busy) or bool(A.jobs.qsize()) or VOD["busy"], "profile": rv.PROFILE or "",
            "upd": "ready" if A.upd["info"] else ("busy" if A.upd["busy"] else "")}


# ---------- действия ----------
def preview_req(d):
    """Пример ролика рисуется в фоне. Если пока рисовали, что-то поменяли, сразу рисуем ещё раз."""
    with A.lock:
        if A.pv["busy"]:
            A.pv["next"] = d
            return {"queued": True}
        A.pv["busy"] = True
    threading.Thread(target=_preview_work, args=(d,), daemon=True).start()
    return {"started": True}


def _preview_work(d):
    while d:
        res, err = None, None
        try:
            p = A.item(d["id"])
            rv.build_force(d["fmt"], d.get("rects") or [])
            png = m.CACHE / f"preview_web_{(A.pv['ver'] + 1) % 4}.png"
            m.CACHE.mkdir(exist_ok=True)
            png.unlink(missing_ok=True)
            dur = m.probe(p["file"])["dur"]
            t = max(0.0, min(float(d.get("t") or 0), dur - 0.8))     # на самом конце клипа кадра нет
            res = rv.make_preview(p["file"], d["fmt"], d.get("rects") or [], d.get("cap") or False, d.get("sub_y"),
                                  t, png, A.cfg, style=d.get("style"), font=d.get("font") or None,
                                  content=d.get("content"), sub_anim=d.get("anim"), zoom=d.get("zoom", True))
            if not res or not Path(res["png"]).exists():
                err = "пример не получился: перемотай клип на другой момент и нажми «Обновить пример»"
        except ValueError as e:
            err = str(e)
        except Exception as e:
            err = f"пример не получился: {e}"
        with A.lock:
            if res and Path(res["png"]).exists():
                A.pv["ver"] += 1
                A.pv["png"] = res["png"]
                want = d.get("style") or "classic"
                A.emit("preview", ver=A.pv["ver"], sub_auto=int(res.get("sub_auto") or 0),
                       note="" if want in ("random", res.get("style")) else
                       "Этот стиль работает, когда есть камера и игра. Здесь ролик соберётся в «Рамке».")
            elif err:
                A.emit("preview", ver=A.pv["ver"], error=err)
            d, A.pv["next"] = A.pv["next"], None
            if not d:
                A.pv["busy"] = False


def preview_video_req(d):
    """Пример в движении: 8 секунд с текущего места клипа, собранные как готовый ролик."""
    if A.pvv.get("busy"):
        A.pvv["next"] = d
        return {"queued": True}
    A.pvv["busy"] = True

    def work(d):
        while d:
            err, res = None, None
            try:
                p = A.item(d["id"])
                rv.build_force(d["fmt"], d.get("rects") or [])
                dur = m.probe(p["file"])["dur"]
                t = max(0.0, min(float(d.get("t") or 0), dur - 1.0))
                A.pvv["ver"] = A.pvv.get("ver", 0) + 1
                out = m.CACHE / f"pvideo_{A.pvv['ver'] % 3}.mp4"
                out.unlink(missing_ok=True)
                res = rv.make_preview(p["file"], d["fmt"], d.get("rects") or [], d.get("cap") or False, d.get("sub_y"),
                                      t, out, A.cfg, style=d.get("style"), font=d.get("font") or None,
                                      content=d.get("content"), video=min(8.0, max(1.0, dur - t)),
                                      sub_anim=d.get("anim"), zoom=d.get("zoom", True))
                if not res or not Path(res.get("video", "")).exists():
                    err = "пример в движении не собрался: попробуй с другого места клипа"
            except Exception as e:
                err = str(e)
            if res and not err:
                A.pvv["file"] = res["video"]
                A.emit("pvideo", ver=A.pvv["ver"], key=d.get("key"))
            else:
                A.emit("pvideo", error=err, key=d.get("key"))
            d, A.pvv["next"] = A.pvv.get("next"), None
        A.pvv["busy"] = False

    threading.Thread(target=work, args=(d,), daemon=True).start()
    return {"started": True}


def montage_req(d):
    p = A.item(d["id"])
    rv.build_force(d["fmt"], d.get("rects") or [])          # ошибка разметки сразу уходит в окно
    info = m.probe(p["file"])
    streamer = (d.get("streamer") or "").strip().lower()
    job = {"clip": p, "fmt_key": d["fmt"], "rects": d.get("rects") or [], "streamer": streamer,
           "title": d.get("title") or "", "captions": d.get("cap") or False, "sub_y": d.get("sub_y"),
           "style": d.get("style") or "classic", "font": d.get("font") or None,
           "banner": d.get("banner") if d.get("banner") in m.banner_list(A.cfg) else None,
           "content": d.get("content"), "sub_anim": d.get("anim"), "zoom": d.get("zoom", True),
           "teaser": d.get("teaser", True)}
    jid = secrets.token_hex(4)
    style_name = dict(m.STYLES).get(job["style"], "Случайный")
    with A.lock:
        A.job_list.append({"id": jid, "streamer": streamer or p.get("streamer") or "своё видео",
                           "title": p.get("title", ""), "fmt": SHORT.get(d["fmt"], d["fmt"]), "style": style_name,
                           "state": "wait", "stage": "ждёт своей очереди", "start": None, "end": None, "out": None})
        A.job_list = A.job_list[-30:]
        A.st["clips"][p["clip_id"]] = "queued"
        if streamer:
            kk = 1920 / info["w"]
            A.st.setdefault("last", {})[streamer] = {
                "fmt": d["fmt"], "rects": [[int(v * kk) for v in r] for r in job["rects"]],
                "content": [int(v * kk) for v in job["content"]] if job["content"] else None}
        rv.save_state(A.st)
    job["jid"] = jid
    A.jobs.put(job)
    A.refresh()
    return {"ok": True}


def render_lock():
    """Замок на весь компьютер: пока монтируется ролик одного профиля, другой ждёт.
    Это занятый сетевой порт: если программа упадёт, Windows сама его освободит."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", RENDER_LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


# ---------- монтаж блогера (стиль «Вальдемар») ----------
BLOG_CFG = BASE / "blogger.json"


def run_blog(job, j):
    import blogger

    def say(t):
        A.log("блогер: " + t)
        with A.lock:
            if j:
                j["stage"] = t[:140]
    out, meta = blogger.make(job["src"], A.cfg, A.enc, job["opts"], say=say)
    sent = False
    if job["opts"].get("tg", True):
        say("отправляю в Telegram…")
        cap = "🎬 Монтаж блогера\n" + (f"Крючок: {meta['hook']}\n" if meta.get("hook") else "") + \
              (f"Музыка: {Path(str(meta['music'])).name}\n" if meta.get("music") else "") + f"{meta['duration']:.0f} с"
        sent = blogger.tg_send(out, cap)
    Path(out).with_suffix(".json").write_text(json.dumps(dict(meta, tg_sent=sent), ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    return out


def blog_req(d):
    import blogger
    act = d.get("act")
    try:
        saved = json.loads(BLOG_CFG.read_text(encoding="utf-8"))
    except Exception:
        saved = {}
    if act == "init":
        hf = blogger.hook_font(download=False)
        threading.Thread(target=lambda: (blogger.hook_font(), blogger.sub_font()), daemon=True).start()
        return {"music": blogger.music_list(), "sounds": blogger.sound_list(), "saved": saved, "hook_font": hf[0],
                "corsiva": hf[0].lower().startswith("monotype"), "sub_font": blogger.sub_font(download=False)[0],
                "hook_fonts": blogger.user_hook_fonts()}
    if act == "add_font":
        name, cyr = blogger.add_hook_font(ask_files("font"))
        return {"fonts": blogger.user_hook_fonts(), "added": name, "cyr": cyr}
    if act == "add_sound":
        added = blogger.add_sound(ask_files("audio"))
        return {"sounds": blogger.sound_list(), "added": added}
    if act == "test_sound":                      # послушать щелчок: открываем файл в плеере Windows
        f = blogger.click_file(d.get("name"))
        if f:
            open_path(f)
        return {"ok": True}
    if act == "pick":
        return {"files": ask_files("video")}
    if act == "add_music":
        added = blogger.add_music(ask_files("audio"))
        return {"music": blogger.music_list(), "added": added}
    if act == "open_music":
        blogger.MUSIC_DIR.mkdir(parents=True, exist_ok=True)
        open_path(blogger.MUSIC_DIR)
        return {"ok": True}
    if act == "start":
        files = [f for f in d.get("files") or [] if Path(f).exists()]
        if not files:
            return {"error": "Выбери видео"}
        opts = {k: d[k] for k in ("music", "music_db", "hook_words", "hook_font", "hook_text", "hook_caps", "hook_case", "hook_palette", "cut_fillers", "click", "click_vol", "bw", "cut_pauses", "tg",
                                  "max_gap") if k in d}
        BLOG_CFG.write_text(json.dumps(opts, ensure_ascii=False), encoding="utf-8")
        for f in files:
            jid = secrets.token_hex(4)
            name = Path(f).stem[:40]
            with A.lock:
                A.job_list.append({"id": jid, "streamer": "Блогер", "title": name, "fmt": name, "style": "Вальдемар",
                                   "state": "wait", "stage": "ждёт своей очереди", "start": None, "end": None,
                                   "out": None, "blog": True})
                A.job_list = A.job_list[-30:]
            A.jobs.put({"blog": True, "jid": jid, "src": f, "opts": dict(opts),
                        "clip": {"clip_id": "blog_" + jid, "title": name, "streamer": "блогер"}, "streamer": "блогер"})
        A.refresh()
        return {"ok": True, "n": len(files)}
    if act == "make_murchik":
        return make_murchik()
    return {"error": "нет такой команды"}


def make_murchik():
    """Отдельный профиль «Мурчик»: только монтаж блогера в одном стиле. Ключи и Telegram — как в главном."""
    if ROLE["child"]:
        return {"error": "профиль создаётся из главного окна (СНГ)"}
    dst = CODE.parent / f"{CODE.name}_murchik"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("claude_key.txt", "telegram_token.txt", "telegram_chat.txt", "telegram_topics.json", "config.json"):
        if (CODE / name).exists() and not (dst / name).exists():
            shutil.copy2(CODE / name, dst / name)
    mj = json.loads((CODE / "montage.json").read_text(encoding="utf-8"))
    mj["mode"] = "blogger"
    if not (dst / "montage.json").exists():
        (dst / "montage.json").write_text(json.dumps(mj, ensure_ascii=False, indent=2), encoding="utf-8")
    (dst / "profile.txt").write_text("МУРЧИК", encoding="utf-8")
    if "murchik" not in ROLE["children"]:        # запускаем профиль сразу, без перезапуска программы
        port = free_port(SERVER["port"] + 9)
        ROLE["children"]["murchik"] = port
        env = dict(os.environ, CLIPPER_DATA=str(dst), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        subprocess.Popen([sys.executable, str(CODE / "review.py"), "--data", str(dst), "--port", str(port),
                          "--no-window", "--token", TOKEN, "--parent", str(SERVER["port"])],
                         cwd=str(dst), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        t0 = time.time()
        while time.time() - t0 < 20 and not _alive(port):
            time.sleep(0.3)
    port = ROLE["children"]["murchik"]
    A.log("создан профиль «Мурчик»: монтаж блогера")
    return {"ok": True, "url": f"http://127.0.0.1:{port}/?k={TOKEN}"}


UPLOADS = BASE / "uploads"


def save_upload(handler, name, size):
    """Видео, перетащенное в окно мышкой: сохраняем в папку uploads профиля и возвращаем путь."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", Path(name).name)[:120] or "video.mp4"
    out = UPLOADS / safe
    i = 1
    while out.exists():
        out = UPLOADS / f"{Path(safe).stem}_{i}{Path(safe).suffix}"
        i += 1
    left = size
    with open(out, "wb") as f:
        while left > 0:
            chunk = handler.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left > 0:
        out.unlink(missing_ok=True)
        return None
    return str(out)


# ---------- стоп монтажа ----------
class Cancelled(BaseException):
    """Монтаж остановлен кнопкой. BaseException, чтобы его не проглотили обработчики ошибок внутри монтажа."""


CANCEL = {"jid": None, "flag": False, "procs": set(), "gone": set()}
_Popen = subprocess.Popen


class _TrackPopen(_Popen):
    """ffmpeg и прочие процессы монтажа запоминаются, чтобы «Стоп» мог их сразу прервать."""
    def __init__(self, *a, **k):
        mine = threading.current_thread().name == "montage-worker"
        if mine and CANCEL["flag"]:
            self._child_created = False
            raise Cancelled()
        super().__init__(*a, **k)
        if mine:
            CANCEL["procs"].add(self)


subprocess.Popen = _TrackPopen


def _clip_back(job):
    """Остановленный клип возвращается в очередь, как будто его не трогали."""
    with A.lock:
        A.st["clips"].pop(job["clip"]["clip_id"], None)
        rv.save_state(A.st)


def cancel_req(d):
    """Убрать из пачки: один ролик (id) или всё сразу (all). Ждущие убираются сразу, текущий прерывается."""
    ids = None if d.get("all") else {d.get("id")}
    removed, stopping = 0, False
    with A.jobs.mutex:                           # ждущие: вынимаем из очереди
        keep = []
        for job in A.jobs.queue:
            if ids is None or job["jid"] in ids:
                CANCEL["gone"].add(job["jid"])
                removed += 1
                _clip_back(job)
            else:
                keep.append(job)
        A.jobs.queue.clear()
        A.jobs.queue.extend(keep)
    with A.lock:
        for j in A.job_list:
            if j["id"] in CANCEL["gone"] and j["state"] == "wait":
                j.update(state="cancel", stage="убран из пачки, клип вернулся в очередь", end=time.time())
    if CANCEL["jid"] and (ids is None or CANCEL["jid"] in ids):   # текущий: прерываем ffmpeg
        CANCEL["flag"] = True
        stopping = True
        for pr in list(CANCEL["procs"]):
            try:
                if pr.poll() is None:
                    pr.kill()
            except Exception:
                pass
        with A.lock:
            for j in A.job_list:
                if j["id"] == CANCEL["jid"]:
                    j["stage"] = "останавливаю…"
    if removed or stopping:
        A.log(f"монтаж: убрано из пачки {removed}" + (", текущий ролик останавливается" if stopping else ""))
    A.refresh()
    return {"ok": True, "removed": removed, "stopping": stopping}


def worker():
    while True:
        job = A.jobs.get()
        with A.lock:
            j = next((x for x in A.job_list if x["id"] == job["jid"]), None)
        if job["jid"] in CANCEL["gone"]:
            continue
        CANCEL.update(jid=job["jid"], flag=False, procs=set())
        lock = render_lock()
        while lock is None:                      # монтируется ролик другого профиля
            if j:
                j["stage"] = "ждёт: монтируется ролик другого профиля"
            if CANCEL["flag"]:
                break
            time.sleep(2)
            lock = render_lock()
        if CANCEL["flag"]:
            if lock:
                lock.close()
            CANCEL.update(jid=None, flag=False, procs=set())
            _clip_back(job)
            with A.lock:
                if j:
                    j.update(state="cancel", stage="остановлен, клип вернулся в очередь", end=time.time())
            A.refresh()
            continue
        with A.lock:
            if j:
                j.update(state="run", stage="начинаю", start=time.time())
        A.busy = job["clip"].get("title", "")[:28] or "клип"
        out, err = None, None
        t_start = time.time()
        try:
            if not m.ensure_space(2.0, A.log, skip_vods=VOD["busy"]):
                raise RuntimeError(f"нет места на диске (свободно {m.free_gb():.1f} ГБ): удали лишнее и запусти снова")
            if A.enc is None:
                A.enc = m.pick_encoder(A.cfg)
            out = run_blog(job, j) if job.get("blog") else rv.run_job(job, A.cfg, A.enc, log=A.log)
            if not out and CANCEL["flag"]:        # ffmpeg прерван, монтаж вернул пусто: это стоп, а не ошибка
                err = "cancel"
        except Cancelled:
            out, err = None, "cancel"
        except BaseException as e:               # любая ошибка: клип помечается, очередь монтажа идёт дальше
            if CANCEL["flag"]:
                out, err = None, "cancel"
            else:
                err = str(e) or e.__class__.__name__
                A.log(f"  ошибка: {err}")
        if err == "cancel":                      # остановлено кнопкой: недоделанный файл убираем, клип в очередь
            CANCEL.update(jid=None, flag=False, procs=set())
            try:
                for f in (BASE / "clips" / "ready").rglob("*.mp4"):
                    if f.stat().st_mtime >= t_start - 1 and not f.with_suffix(".json").exists():
                        f.unlink()
            except Exception:
                pass
            _clip_back(job)
            with A.lock:
                if j:
                    j.update(state="cancel", stage="остановлен, клип вернулся в очередь", end=time.time())
            A.busy = None
            lock.close()
            A.log("  монтаж остановлен")
            A.toast("Монтаж остановлен, клип вернулся в очередь")
            A.refresh()
            continue
        CANCEL.update(jid=None, flag=False, procs=set())
        ok = bool(out)
        dropped = not ok and (job.get("agents") or {}).get("decision") == "reject"   # судья: неактуально
        with A.lock:
            if not job.get("blog"):
                A.st["clips"][job["clip"]["clip_id"]] = "done" if ok else ("skipped" if dropped else "retry")
            rv.save_state(A.st)
            if ok:
                A.done += 1
            elif not dropped:
                A.failed += 1
            if j:
                sent = False
                try:
                    sent = bool(json.loads(Path(out).with_suffix(".json").read_text(encoding="utf-8")).get("tg_sent")) if ok else False
                except Exception:
                    pass
                stage = ("готово" + (" · отправлено в Telegram" if sent else "")) if ok else (err or "не получилось")[:140]
                ag = job.get("agents")
                if ag and (ok or dropped):       # что решили критик, судья и продюсер
                    pl = ag.get("plan") or {}
                    stage = (f"✓ {ag.get('score', '?')}/10 → @{pl['account']} {pl.get('when', '')}"
                             + (" · в Telegram" if sent else " · в Telegram не ушёл, см. журнал")
                             if ag.get("decision") == "publish" and pl.get("account")
                             else f"◌ в запасе: {ag.get('reason', '')}" if ag.get("decision") == "reserve"
                             else f"✕ в брак: {ag.get('reason', '')}")[:140]
                j.update(state="done" if (ok or dropped) else "error", end=time.time(), out=str(out) if out else None,
                         stage=stage)
        A.busy = None
        lock.close()
        name = job.get("streamer") or job["clip"].get("streamer") or "клип"
        if dropped:
            A.toast(f"{name}: судья не пропустил — {(job.get('agents') or {}).get('reason', '')}"[:120], ok=False)
        elif ok:
            A.toast(f"{name}: ролик готов" + (" и отправлен в Telegram" if j and "Telegram" in j["stage"] else ""))
        else:
            A.toast(f"{name}: не смонтировался, клип вернётся в очередь при перезапуске", ok=False)


VOD = {"busy": False, "stop": False}


def vod_req(d):
    """Нарезка стрима: ссылка или файл, сколько фрагментов. Идёт в фоне, ход виден в панели «Монтаж»."""
    if d.get("act") == "stop":
        VOD["stop"] = True
        return {"ok": True}
    if VOD["busy"]:
        return {"error": "Стрим уже нарезается: дождись конца или останови его в панели «Монтаж»."}
    src = (d.get("src") or "").strip()
    if d.get("act") == "file":
        got = ask_files("video")
        if not got:
            return {"cancel": True}
        src = got[0]
    if not src:
        return {"error": "Вставь ссылку на стрим или выбери файл."}
    n = max(1, min(30, int(d.get("n") or 10)))
    jid = "vod" + secrets.token_hex(3)
    name = Path(src).name if Path(src).exists() else re.sub(r"^https?://(www\.)?", "", src)[:40]
    with A.lock:
        A.job_list.append({"id": jid, "streamer": "Нарезка стрима", "title": name, "fmt": name, "style": f"{n} фрагм.",
                           "state": "run", "stage": "начинаю", "start": time.time(), "end": None, "out": None, "vod": True})
    VOD.update(busy=True, stop=False)

    def say(t):
        A.log("нарезка: " + t)
        with A.lock:
            for j in A.job_list:
                if j["id"] == jid:
                    j["stage"] = t[:140]

    def work():
        ok, res = False, None
        try:
            import highlights
        except ImportError:
            say("нет файла highlights.py в папке программы: скачай его и положи в C:\\clipper")
            with A.lock:
                for j in A.job_list:
                    if j["id"] == jid:
                        j.update(state="error", end=time.time())
            VOD["busy"] = False
            A.toast("Для нарезки стримов нужен файл highlights.py в папке программы", ok=False)
            return
        try:
            res = highlights.run(src, n, A.cfg, say=say, should_stop=lambda: VOD["stop"])
            ok = True
        except highlights.Stop as e:
            say(str(e))
        except BaseException as e:
            say(f"ошибка: {e}")
        with A.lock:
            for j in A.job_list:
                if j["id"] == jid:
                    j.update(state="done" if ok else "error", end=time.time(),
                             stage=f"готово: фрагментов в очереди {len(res)}" if ok else j["stage"])
        VOD["busy"] = False
        A.refresh()
        A.toast(f"Нарезка готова: {len(res)} фрагментов в очереди" if ok else "Нарезка не получилась, причина в журнале", ok=ok)

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


def skip_req(d):
    p = A.item(d["id"])
    with A.lock:
        A.st["clips"][p["clip_id"]] = "skipped"
        rv.save_state(A.st)
    A.refresh()
    return {"ok": True}


def find_clips():
    if A.finding:
        return {"busy": True}
    A.finding = True
    A.log("Ищу новые клипы…")

    def work():
        try:
            r = subprocess.run([sys.executable, str(CODE / "clipfinder.py")], cwd=str(BASE), capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", CLIPPER_DATA=str(BASE)),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            lines = [x for x in (r.stdout or "").strip().splitlines() if x.strip()]
            tail = lines[-1] if lines else "поиск завершён"
            if r.returncode and r.stderr:
                tail = "поиск не сработал: " + r.stderr.strip().splitlines()[-1][:200]
            A.log(tail)
            A.toast(tail[:90], ok=not r.returncode)
        except Exception as e:
            A.log(f"поиск не сработал: {e}")
        finally:
            A.finding = False
            A.refresh()

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}



def cleanup_loop():
    """Раз в 6 часов убирает временные файлы, чтобы диск не забивался (звук стримов, копии, исходники)."""
    time.sleep(60)
    while not A.quit.is_set():
        try:
            if not (A.busy or VOD["busy"] or LD["busy"] or TT["busy"]):
                freed, what = m.cleanup(skip_vods=VOD["busy"])
                if freed > 200e6:
                    A.log(f"очистка: освобождено {freed / 1e9:.1f} ГБ (" + ", ".join(what) + ")")
            if m.free_gb() < 3:
                A.toast(f"На диске осталось {m.free_gb():.1f} ГБ: программа почистила, что могла", ok=False)
        except Exception as e:
            A.log(f"очистка не сработала: {e}")
        A.quit.wait(6 * 3600)

def auto_find_loop():
    time.sleep(3)
    while not A.quit.is_set():
        if A.cfg.get("mode") == "blogger":       # профиль монтажа блогера: клипы с Twitch не ищем
            return
        if not rv.needs_setup():
            find_clips()
        A.quit.wait(int(A.cfg.get("interval_min", 120)) * 60)


def ask_files(kind):
    """Обычное окно Windows «Открыть файл». Его можно показать только из главного потока программы."""
    box = {"done": threading.Event(), "files": []}
    A.dialogs.put((kind, box))
    box["done"].wait(600)
    return box["files"]


def dialog_loop():
    """Главный поток: ждёт просьб показать окно выбора файлов и следит, не закрыли ли окно программы."""
    while not A.quit.is_set():
        try:
            kind, box = A.dialogs.get(timeout=1)
        except queue.Empty:
            busy = A.busy or A.jobs.qsize() or A.finding or VOD["busy"] or TT["busy"] or LD["busy"]
            if ROLE["child"]:
                if not parent_alive():
                    A.parent_dead = getattr(A, "parent_dead", 0) + 1
                    if A.parent_dead >= 3 and not busy:
                        A.quit.set()             # главная программа закрыта, свой монтаж доделан
                else:
                    A.parent_dead = 0
            elif time.time() - A.last_poll > 25 and not busy and not children_busy():
                A.quit.set()                     # окно закрыли и монтажа нет нигде: программа завершается
            continue
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            if kind == "font":
                box["files"] = list(filedialog.askopenfilenames(
                    parent=root, title="Выбери файл шрифта", filetypes=[("Шрифт", "*.ttf *.otf"), ("Все файлы", "*.*")]))
            elif kind == "audio":
                box["files"] = list(filedialog.askopenfilenames(
                    parent=root, title="Выбери музыку",
                    filetypes=[("Музыка", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"), ("Все файлы", "*.*")]))
            elif kind == "banner":
                f = filedialog.askopenfilename(parent=root, title="Файл баннера",
                                               filetypes=[("Видео", "*.mov *.mp4 *.webm *.mkv"), ("Все файлы", "*.*")])
                box["files"] = [f] if f else []
            else:
                box["files"] = list(filedialog.askopenfilenames(
                    parent=root, title="Выбери видео",
                    filetypes=[("Видео", "*.mp4 *.mov *.mkv *.webm *.avi"), ("Все файлы", "*.*")]))
            root.destroy()
        except Exception as e:
            A.log(f"окно выбора файла не открылось: {e}")
        box["done"].set()


def _get(port, path, timeout=2.0):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}{'&' if '?' in path else '?'}k={TOKEN}", timeout=timeout) as r:
        return r.read()


def _post(port, name, data, timeout=30.0):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/{name}", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def parent_alive():
    try:
        return _get(ROLE["parent"], "/ping", 1.5) == b"prodaem-clipper"
    except Exception:
        return False


def children_rendering():
    """Какие профили сейчас монтируют или режут стрим (поиск клипов и аналитика обновлению не мешают)."""
    out = []
    for key, port in ROLE["children"].items():
        try:
            d = json.loads(_get(port, "/poll?since=999999999&quiet=1"))
            if d.get("render", d.get("busy") or d.get("wait")):
                out.append(str(key).upper())
        except Exception:
            pass
    return out


def children_busy():
    for port in ROLE["children"].values():
        try:
            d = json.loads(_get(port, "/poll?since=999999999&quiet=1"))
            if d.get("busy") or d.get("wait") or d.get("finding"):
                return True
        except Exception:
            pass
    return False


def touch_parent():
    """Окно открыто на этом профиле: говорим главной программе, что закрываться не надо."""
    now = time.time()
    if ROLE["child"] and now - getattr(A, "touched", 0) > 5:
        A.touched = now
        def go():
            try:
                _get(ROLE["parent"], "/touch")
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()


def add_files():
    got = ask_files("video")
    for f in got:
        rv.add_own(A.st, f)
    if got:
        A.refresh()
        A.toast(f"добавлено файлов: {len(got)}")
    return {"added": len(got)}


def clear_caps(d):
    p = A.item(d["id"])
    if m.ZONES_FILE.exists():
        zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8"))
        if zones.pop(p.get("streamer", ""), None) is not None:
            m.ZONES_FILE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


def open_path(path):
    try:
        os.startfile(path)                       # только Windows
    except AttributeError:
        subprocess.Popen(["xdg-open", str(path)])


def quit_req(d):
    if A.busy or A.jobs.qsize():
        return {"busy": True}
    threading.Timer(0.3, A.quit.set).start()
    return {"ok": True}


# стримеры
def streamers(d):
    act, login = d.get("act"), rv.norm_login(d.get("login", "")) if d.get("login") else ""
    lst = rv.load_streamers()
    msg = ""
    if act == "add":
        if not login:
            return {"list": lst, "error": "Это не похоже на ник Twitch: только латиница, цифры и _, от 3 до 25 знаков."}
        if login in lst:
            return {"list": lst, "error": f"{login} уже в пуле"}
        lst.append(login)
        rv.save_streamers(lst)
        msg = f"{login} добавлен, ищу его клипы"
        find_clips()
    elif act == "remove" and d.get("login") in lst:
        lst.remove(d["login"])
        rv.save_streamers(lst)
        msg = f"{d['login']} убран из пула"
    return {"list": lst, "msg": msg}


# ключи
def settings(d):
    act = d.get("act")
    if act == "get":
        try:
            c = json.loads(rv.CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            c = {}
        clean = lambda v: "" if "ВСТАВЬ" in (v or "ВСТАВЬ") else v
        return {"cid": clean(c.get("client_id")), "sec": clean(c.get("client_secret")),
                "claude": m.KEY_FILE.read_text(encoding="utf-8-sig").strip() if m.KEY_FILE.exists() else "",
                "tg": m.TG_TOKEN.read_text(encoding="utf-8-sig").strip() if m.TG_TOKEN.exists() else ""}
    if act == "tg_test":                         # пробная отправка: сохраняет токен и шлёт сообщение
        tok = (d.get("tg") or "").strip()
        if not tok:
            return {"ok": False, "text": "сначала вставь токен бота"}
        m.TG_TOKEN.write_text(tok, encoding="ascii", errors="ignore")
        ok, text = m.tg_test(tok)
        return {"ok": ok, "text": text}
    if act == "tg":                              # токен бота сохраняется сразу, чтобы можно было привязать темы
        if d.get("tg"):
            m.TG_TOKEN.write_text(d["tg"].strip(), encoding="ascii", errors="ignore")
        return {"ok": True}
    if act == "check":
        k, v = d.get("kind"), d.get("values") or []
        ok, text = {"twitch": rv.check_twitch, "claude": rv.check_claude, "telegram": rv.check_telegram}[k](*v)
        return {"ok": ok, "text": text}
    if act == "save":
        if not d.get("cid") or not d.get("sec"):
            return {"error": "Без ключей Twitch клипы не ищутся: заполни оба поля."}
        rv.save_keys(d["cid"].strip(), d["sec"].strip(), (d.get("claude") or "").strip(), (d.get("tg") or "").strip())
        A.toast("Ключи сохранены")
        find_clips()
        return {"ok": True}
    return {}


def tg_topics(d):
    """Привязка тем группы Telegram без командной строки: то же, что python montage.py --tg-setup."""
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            m.tg_setup()
        return {"ok": True, "text": buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else "Готово"}
    except SystemExit as e:
        return {"ok": False, "text": str(e.code or "не получилось")}
    except Exception as e:
        return {"ok": False, "text": f"не получилось: {e}"}


# баннеры
def banners(d):
    act = d.get("act")
    if act == "get":
        return {"banners": m.banner_list(A.cfg), "use": A.cfg.get("banner_use")}
    if act == "add":
        got = ask_files("banner")
        if not got:
            return {"cancel": True}
        src = Path(got[0])
        dst = BASE / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)               # баннер должен лежать в папке программы
        auto = None
        try:
            auto = m.banner_autocrop(dst)           # сразу убираем лишние края вокруг карточки баннера
        except Exception:
            pass
        return {"file": dst.name, "name": src.stem.split(".")[0], "auto": auto}
    if act == "autocrop":
        try:
            auto = m.banner_autocrop(m.find_banner(d.get("file", "")))
        except Exception as e:
            return {"error": f"не получилось: {e}"}
        return {"auto": auto} if auto else {"error": "лишних краёв не нашёл: баннер и так занимает весь кадр"}
    if act == "preview":
        b = dict(d["banner"])
        m.CACHE.mkdir(exist_ok=True)
        A.bpv["ver"] += 1
        png = m.CACHE / f"banner_web_{A.bpv['ver'] % 3}.png"
        info = m.banner_preview(m.banner_for({"banners": {"x": b}}, "cs", "x"), A.cfg, png, 1.5)
        A.bpv["png"] = str(png)
        return {"ver": A.bpv["ver"], "sec": info["sec"], "dur": round(info["dur"], 1), "w": info["w"], "h": info["h"],
                "file": m.find_banner(b["file"]).name}
    if act == "save":
        rv.save_banners(A.cfg, d["banners"], d.get("use"))
        A.toast("Баннеры сохранены")
        return {"banners": banners_short(), "use": A.cfg.get("banner_use")}
    if act == "rename":
        old, new = d.get("old"), (d.get("new") or "").strip()[:40]
        lst = {k: dict(v) for k, v in m.banner_list(A.cfg).items()}
        if old not in lst:
            return {"error": "такого баннера нет"}
        if not new:
            return {"error": "впиши новое название"}
        if new != old and new in lst:
            return {"error": "баннер с таким названием уже есть"}
        lst = {(new if k == old else k): v for k, v in lst.items()}       # порядок в списке сохраняется
        use = A.cfg.get("banner_use")
        rv.save_banners(A.cfg, lst, new if use in (old, None) else use)
        return {"banners": banners_short(), "use": A.cfg.get("banner_use"), "name": new}
    if act == "use":
        if d.get("name") in m.banner_list(A.cfg):
            rv.save_banners(A.cfg, m.banner_list(A.cfg), d["name"])
        return {"ok": True}
    return {}



TT = {"busy": False, "stage": "", "error": "", "ver": 0}


def tt_req(d):
    """Анализ TikTok-аккаунтов: ники, цель, сбор в фоне и последний отчёт."""
    try:
        import accounts
    except ImportError:
        return {"error": "Для анализа аккаунтов нужен файл accounts.py: поставь обновление программы."}
    act = d.get("act")
    if act == "get":
        plan, roles = [], {}
        try:
            import agents
            plan = sorted(agents._load_schedule(), key=lambda x: (x["day"], x["time"]))
            roles = agents.settings()["roles"]
        except Exception:
            pass
        return {"settings": accounts.load_settings(), "report": accounts.last_report(), "plan": plan, "roles": roles,
                "busy": TT["busy"], "stage": TT["stage"], "error": TT["error"]}
    if act == "report_now":                      # прислать отчёт за день прямо сейчас (не чаще раза в сутки)
        return manual_report("report")
    if act == "reset_plan":                      # начать день с чистого листа: план продюсера пустой
        try:
            import agents
            agents.SCHEDULE.unlink(missing_ok=True)
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}
    if act == "roles":                           # роли аккаунтов для продюсера: «ник — роль» по строке
        st = accounts.load_settings()
        roles = {}
        for line in str(d.get("text") or "").splitlines():
            mt = re.match(r"\s*@?([\w.\-]+)\s*[—\-:]+\s*(.+)", line)
            if mt:
                roles[mt.group(1).lower()] = mt.group(2).strip()[:160]
        st["roles"] = roles
        if roles and not st.get("accounts"):
            st["accounts"] = list(roles)
        accounts.save_settings(st)
        return {"ok": True, "roles": roles}
    if act == "status":
        return {"busy": TT["busy"], "stage": TT["stage"], "error": TT["error"], "ver": TT["ver"],
                "report": None if TT["busy"] else accounts.last_report()}
    if act == "open":
        url = str(d.get("url") or "")
        if url.startswith("https://www.tiktok.com/"):
            import webbrowser
            webbrowser.open(url)
        return {"ok": True}
    if act == "run":
        if TT["busy"]:
            return {"error": "Анализ уже идёт"}
        handles = [h for h in re.split(r"[\s,;]+", d.get("handles") or "") if h.strip()]
        goal = int(d.get("goal") or 100000)
        TT.update(busy=True, stage="начинаю", error="")

        def say(t):
            TT["stage"] = str(t)[:160]
            A.log("аналитика: " + str(t))

        def work():
            try:
                accounts.analyze(handles, goal, A.cfg, say, deep=bool(d.get("deep", True)))
                A.toast("Анализ аккаунтов готов")
            except accounts.Stop as e:
                TT["error"] = str(e)
                A.log("аналитика: " + str(e))
            except Exception as e:
                TT["error"] = f"анализ не получился: {e}"
                A.log("аналитика: " + TT["error"])
            finally:
                TT["busy"] = False
                TT["ver"] += 1

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}
    return {}


LD = {"busy": False, "stage": "", "error": "", "ver": 0}


def leaders_req(d):
    """Учиться у лидеров: ссылки на популярные ролики и профили TikTok → книга правил для критика и судьи."""
    try:
        import leaders
    except ImportError:
        return {"error": "Нужен файл leaders.py: поставь обновление программы."}
    act = d.get("act")
    if act in ("get", "status"):
        return {"busy": LD["busy"], "stage": LD["stage"], "error": LD["error"],
                "book": None if LD["busy"] else leaders.load()}
    if act == "run":
        if LD["busy"]:
            return {"error": "Разбор уже идёт"}
        text = d.get("links") or ""
        if not leaders.parse_links(text):
            return {"error": "Вставь ссылки на ролики или профили из TikTok"}
        LD.update(busy=True, stage="начинаю", error="")

        def say(t):
            LD["stage"] = str(t)[:160]
            A.log("лидеры: " + str(t))

        def work():
            try:
                leaders.analyze(text, A.cfg, say)
                A.toast("Книга правил готова: критик и судья теперь сверяются с ней")
            except (leaders.Stop, Exception) as e:
                LD["error"] = str(e)[:300]
                A.log("лидеры: " + LD["error"])
            finally:
                LD["busy"] = False
                LD["ver"] += 1

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}
    return {}




# ---------- редактор готового ролика ----------
MEDIA = {}                                       # номер → файл, который окно может проиграть


def media_url(path):
    mid = secrets.token_hex(6)
    MEDIA[mid] = str(path)
    if len(MEDIA) > 200:
        for k in list(MEDIA)[:100]:
            MEDIA.pop(k, None)
    return f"/media?id={mid}&k={TOKEN}"


def editor_req(d):
    act = d.get("act")
    if act == "list":                            # последние готовые ролики, которые можно поправить
        rows = []
        for js in sorted((BASE / "clips" / "ready").glob("*/*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:60]:
            mp4 = js.with_suffix(".mp4")
            if not mp4.exists():
                continue
            try:
                p = json.loads(js.read_text(encoding="utf-8"))
            except Exception:
                continue
            ag = p.get("agents") or {}
            pl = ag.get("plan") or {}
            rows.append({"out": str(mp4), "streamer": p.get("streamer", ""), "title": p.get("final_title", ""),
                         "when": time.strftime("%d.%m %H:%M", time.localtime(mp4.stat().st_mtime)), "dur": p.get("duration"),
                         "score": ag.get("score"), "plan": f"@{pl['account']} {pl.get('when', '')}" if pl.get("account") else "",
                         "editable": bool(p.get("job")), "edited": bool(p.get("edited"))})
        return {"rows": rows}
    if act == "open":
        out = Path(d.get("out") or "")
        js = out.with_suffix(".json")
        if not js.exists():
            return {"error": "у этого ролика нет данных для редактора (смонтирован старой версией программы)"}
        p = json.loads(js.read_text(encoding="utf-8"))
        src = Path(p.get("file", ""))
        src = src if src.is_absolute() else BASE / src
        if not src.exists():
            return {"error": "исходный клип уже удалён очисткой: переделать этот ролик нельзя"}
        if not p.get("job"):
            return {"error": "ролик смонтирован старой версией программы: открой его в редакторе после следующего монтажа"}
        ed = p.get("edit") or {}
        words = ed.get("words")
        if words is None:
            words = m.ai_fix_words(m.merge_hyphens(m.transcribe(src, A.cfg)), src, A.cfg)
        info = m.probe(src)
        play = safe_copy("ed_" + src.stem, str(src)) if d.get("safe") else src   # на компьютерах, где видео чёрное
        return {"out": str(out), "src": media_url(play), "ready": media_url(out), "dur": info["dur"],
                "w": info["w"], "h": info["h"], "words": words, "edit": ed, "title": p.get("final_title", ""),
                "streamer": p.get("streamer", ""), "banner_at_out": p.get("banner_at"), "score": (p.get("agents") or {}).get("score")}
    if act == "save":
        out = Path(d.get("out") or "")
        p = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
        job = dict(p["job"])
        clip = dict(p)
        clip["file"] = p.get("file")
        src = Path(clip["file"])
        clip["file"] = str(src if src.is_absolute() else BASE / src)
        job["clip"] = clip
        e = d.get("edit") or {}
        dur = m.probe(clip["file"])["dur"]
        edit = {"start_at": max(0.0, float(e.get("start_at") or 0)) or None,
                "end_at": (min(dur, float(e["end_at"])) if e.get("end_at") and float(e["end_at"]) < dur - 0.05 else None),
                "cuts": [[float(a), float(b)] for a, b in (e.get("cuts") or []) if float(b) - float(a) > 0.1],
                "banner_at": float(e["banner_at"]) if e.get("banner_at") is not None else None,
                "teaser": bool(e.get("teaser", True)),
                "words": [{"s": float(w["s"]), "e": float(w["e"]), "t": str(w["t"])} for w in (e.get("words") or [])]
                if e.get("words") is not None else None}
        job["edit"] = edit
        jid = secrets.token_hex(4)
        job["jid"] = jid
        with A.lock:
            A.job_list.append({"id": jid, "streamer": job.get("streamer") or p.get("streamer", ""), "title": p.get("final_title", ""),
                               "fmt": "правка", "style": dict(m.STYLES).get(job.get("style"), ""), "state": "wait",
                               "stage": "ждёт своей очереди", "start": None, "end": None, "out": None})
            A.job_list = A.job_list[-30:]
        A.jobs.put(job)
        A.refresh()
        return {"ok": True}
    return {}


def daily_report(weekly=False, fetch=True):
    """Отчёт в группу «Отчёты». fetch=True (только по кнопке или команде): заново собрать цифры с TikTok,
    это открывает браузер. Сам по себе (22:00, понедельник) отчёт берёт цифры последней аналитики и TikTok не трогает."""
    import accounts
    st = accounts.load_settings()
    if not st.get("accounts"):
        return {"error": "в аналитике не вписаны аккаунты"}
    if TT["busy"]:
        return {"error": "аналитика уже собирается"}
    if not fetch:
        rep = accounts.last_report()
        if not rep:
            m.tg_message("📊 Отчёт: свежих цифр нет. Открой «Аналитика TikTok» и нажми «Собрать анализ», "
                         "тогда отчёты будут с цифрами.", "report")
            return {"error": "нет данных аналитики"}
        text = (accounts.weekly_text(rep) if weekly else accounts.report_text(rep)) + \
            f"\n\n(цифры TikTok из аналитики от {time.strftime('%d.%m %H:%M', time.localtime(rep.get('time', 0)))}; " \
            "свежие — кнопка «Собрать анализ»)"
        ok = m.tg_message(text, "report")
        A.log(("разбор недели " if weekly else "отчёт за день ") + ("отправлен в Telegram" if ok else "не ушёл в Telegram"))
        return {"ok": True, "sent": ok}
    TT.update(busy=True, stage="отчёт: собираю цифры", error="")
    try:
        rep = accounts.analyze(st["accounts"], st.get("goal", 100000), A.cfg,
                               lambda t: TT.update(stage=str(t)[:160]), deep=False, ai=weekly)
        text = accounts.weekly_text(rep) if weekly else accounts.report_text(rep)
        ok = m.tg_message(text, "report")
        A.log(("разбор недели " if weekly else "отчёт за день ") + ("отправлен в Telegram" if ok else "собран, но в Telegram не ушёл"))
        return {"ok": True, "text": text, "sent": ok}
    except Exception as e:
        TT["error"] = str(e)[:300]
        return {"error": str(e)[:300]}
    finally:
        TT["busy"] = False
        TT["ver"] += 1


def daily_loop():
    """Проверяет раз в минуту, не пора ли прислать отчёт (каждый день) или разбор недели (по понедельникам)."""
    import accounts
    mark = BASE / "analytics" / "report_sent.txt"
    while not A.quit.wait(60):
        try:
            st = accounts.load_settings()
            if not st.get("report", True) or not st.get("accounts"):
                continue
            hour = int(st.get("report_hour", 22))
            today = time.strftime("%Y-%m-%d")
            sent = mark.read_text().strip() if mark.exists() else ""
            if time.localtime().tm_hour >= hour and sent != today and not (A.busy or VOD["busy"]):
                mark.parent.mkdir(parents=True, exist_ok=True)
                mark.write_text(today)
                daily_report(weekly=time.localtime().tm_wday == int(st.get("weekly_day", 0)) and st.get("weekly", True),
                             fetch=False)       # сам по себе TikTok не открываем: только цифры последней аналитики
        except Exception as e:
            A.log(f"отчёт не получился: {e}")


MANUAL = BASE / "analytics" / "report_manual.json"


def manual_report(kind):
    """Отчёт или разбор недели по кнопке или команде: не чаще раза в 24 часа (каждый вид отдельно).
    Отчёт в 22:00 и разбор по понедельникам приходят сами, это ограничение их не трогает."""
    import accounts
    if not accounts.load_settings().get("accounts"):
        return {"error": "no_accounts"}
    if TT["busy"]:
        return {"error": "аналитика уже собирается"}
    try:
        last = json.loads(MANUAL.read_text(encoding="utf-8"))
    except Exception:
        last = {}
    t = float(last.get(kind, 0))
    if time.time() - t < 24 * 3600:
        nxt = time.strftime("%d.%m в %H:%M", time.localtime(t + 24 * 3600))
        what = "Разбор недели" if kind == "week" else "Отчёт"
        return {"error": f"{what} уже присылался {time.strftime('%d.%m в %H:%M', time.localtime(t))}. "
                         f"Следующий по кнопке можно {nxt} (не чаще раза в сутки)", "limit": True}
    last[kind] = time.time()
    MANUAL.parent.mkdir(parents=True, exist_ok=True)
    MANUAL.write_text(json.dumps(last), encoding="utf-8")
    threading.Thread(target=daily_report, kwargs={"weekly": kind == "week"}, daemon=True).start()
    return {"ok": True}


# ---------- очередь стримов (старые видео Стрея и т.п.) ----------
VODQ = BASE / "analytics" / "vod_queue.json"
VODQ_LOCK = threading.Lock()


def vodq_load():
    try:
        return json.loads(VODQ.read_text(encoding="utf-8"))
    except Exception:
        return []


def vodq_save(q):
    VODQ.parent.mkdir(parents=True, exist_ok=True)
    VODQ.write_text(json.dumps(q[-300:], ensure_ascii=False, indent=1), encoding="utf-8")


def _vod_key(u):
    u = u.strip()
    mt = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", u)
    if mt:
        return "yt:" + mt.group(1)
    mt = re.search(r"twitch\.tv/videos/(\d+)", u)
    return ("tw:" + mt.group(1)) if mt else u.split("&")[0]


def vodq_add(urls, n=None):
    """Добавляет ссылки в очередь нарезки. Повторы (уже нарезанные или стоящие в очереди) пропускает."""
    added, dup = [], 0
    with VODQ_LOCK:
        q = vodq_load()
        have = {_vod_key(x["url"]) for x in q}
        for u in urls:
            k = _vod_key(u)
            if k in have:
                dup += 1
                continue
            have.add(k)
            q.append({"url": u, "added": time.strftime("%Y-%m-%d %H:%M"), "status": "wait",
                      "n": int(n or A.cfg.get("vod_queue_n", 10))})
            added.append(u)
        vodq_save(q)
    wait = sum(1 for x in vodq_load() if x["status"] == "wait")
    if added:
        A.log(f"в очередь нарезки добавлено видео: {len(added)}, всего ждут {wait}")
    return {"added": len(added), "dup": dup, "wait": wait}


def vodq_loop():
    """Когда программа простаивает (нет монтажа, поиска и нарезки), берёт следующее видео из очереди и нарезает."""
    time.sleep(90)
    while not A.quit.wait(60):
        try:
            if A.busy or A.jobs.qsize() or VOD["busy"] or A.finding or TT["busy"] or LD["busy"]:
                continue
            with VODQ_LOCK:
                q = vodq_load()
                nxt = next((x for x in q if x["status"] == "wait"), None)
                if not nxt:
                    continue
                nxt["status"] = "run"
                nxt["started"] = time.strftime("%Y-%m-%d %H:%M")
                vodq_save(q)
            A.log("очередь нарезки: беру " + nxt["url"][:80])
            r = vod_req({"src": nxt["url"], "n": nxt.get("n", 10)})
            while VOD["busy"] and not A.quit.is_set():
                time.sleep(5)
            with VODQ_LOCK:
                q = vodq_load()
                for x in q:
                    if x["url"] == nxt["url"] and x["status"] == "run":
                        x["status"] = "error" if r.get("error") else "done"
                        x["finished"] = time.strftime("%Y-%m-%d %H:%M")
                vodq_save(q)
        except Exception as e:
            A.log(f"очередь нарезки: {e}")


# ---------- управление с телефона (через Telegram-бота) ----------
LIVE_DIR = CODE / "cache" / "live"


def live_register():
    """Запись «этот профиль запущен, вот его порт»: по ней бот находит все профили на компьютере."""
    try:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        key = my_key()
        (LIVE_DIR / f"{key or 'main'}.json").write_text(json.dumps(
            {"key": key, "label": profile_label(key, BASE), "port": SERVER["port"], "token": TOKEN,
             "pid": os.getpid(), "base": str(BASE), "t": time.time()}), encoding="utf-8")
    except Exception as e:
        A.log(f"запись профиля для бота не сохранилась: {e}")


def remote_req(d):
    act = d.get("act")
    if act == "status":
        job = None
        with A.lock:
            for j in A.job_list:
                if j.get("state") == "run":
                    job = f"{j.get('streamer', '')}: {j.get('stage', '')}"[:120]
            n_items = len(A.items)
        q = vodq_load()
        return {"label": profile_label(my_key(), BASE), "job": job, "jobs": A.jobs.qsize(), "queue": n_items,
                "finding": A.finding, "vod": VOD["busy"], "done": A.done, "failed": A.failed,
                "vod_wait": sum(1 for x in q if x["status"] == "wait"), "free_gb": round(m.free_gb(), 1),
                "up": round(time.time() - START_T)}
    if act == "find":
        return find_clips()
    if act == "stray_add":
        return vodq_add(d.get("urls") or [], d.get("n"))
    if act == "vod_queue":
        return {"items": vodq_load()[-15:]}
    if act in ("report", "week"):
        return manual_report(act)
    return {"error": "нет такой команды"}


# ---------- «я снова работаю» ----------
START_T = time.time()
BEAT = CODE / "cache" / "heartbeat.json"


def _dur(sec):
    h, mm = int(sec // 3600), int(sec % 3600 // 60)
    return (f"{h} ч {mm} мин" if h else f"{mm} мин")


def heartbeat_loop():
    """Главная программа раз в минуту отмечается. Если отметок долго не было (компьютер был выключен, спал
    или программа падала), после запуска пишет в группу «Отчёты», сколько длился простой."""
    try:
        old = json.loads(BEAT.read_text(encoding="utf-8")) if BEAT.exists() else None
    except Exception:
        old = None
    now = time.time()
    if old and now - old.get("t", now) > 10 * 60:
        why = "программу закрыли" if old.get("clean") else "компьютер был выключен, спал или программа закрылась сама"
        t0 = time.strftime("%d.%m %H:%M", time.localtime(old["t"]))
        msg = (f"⚡ Программа снова работает.\nПростой: с {t0} до {time.strftime('%H:%M')} ({_dur(now - old['t'])}).\n"
               f"Причина: {why}.\nИщу новые клипы, очередь нарезки продолжится сама.")
        threading.Thread(target=m.tg_message, args=(msg, "report"), daemon=True).start()
    last = now
    while True:
        now = time.time()
        if now - last > 5 * 60:                  # компьютер спал, а программа была открыта
            msg = (f"😴 Компьютер спал с {time.strftime('%H:%M', time.localtime(last))} до {time.strftime('%H:%M')} "
                   f"({_dur(now - last)}), всё это время клипы не резались.\n"
                   "Чтобы не засыпал: Параметры → Система → Питание → Сон → Никогда.")
            threading.Thread(target=m.tg_message, args=(msg, "report"), daemon=True).start()
        last = now
        try:
            BEAT.write_text(json.dumps({"t": now, "clean": False}), encoding="utf-8")
        except Exception:
            pass
        live_register()
        if A.quit.wait(60):
            break
    try:
        BEAT.write_text(json.dumps({"t": time.time(), "clean": True}), encoding="utf-8")
    except Exception:
        pass


# обновления
def update(d):
    import updater
    if d.get("act") == "check":
        if A.upd["busy"]:
            return {"busy": True}
        A.upd["busy"] = True
        try:
            res = updater.check()
        except Exception as e:
            res = {"error": f"проверка обновлений не сработала: {e}"}
        A.upd["busy"] = False
        if res.get("available"):
            A.upd["info"] = res
            return {"state": "ready", "text": f"Есть обновление {res['version']}" + (f": {res['notes']}" if res["notes"] else "")}
        return {"state": "error" if res.get("error") else "off" if res.get("off") else "latest",
                "text": res.get("error") or ("Обновления не настроены: нет файла update_url.txt" if res.get("off")
                                             else "У тебя последняя версия")}
    if d.get("act") == "apply":
        if ROLE["child"]:                        # обновление ставит главная программа: она перезапускает все профили
            try:
                return _post(ROLE["parent"], "update", {"act": "apply"}, timeout=180)
            except Exception as e:
                return {"error": f"не получилось: {e}"}
        if not A.upd["info"]:
            res = updater.check()
            A.upd["info"] = res if res.get("available") else None
            if not A.upd["info"]:
                return {"error": "сначала проверь обновления"}
        busy = (["этом профиле"] if (A.busy or A.jobs.qsize() or VOD["busy"]) else []) + children_rendering()
        if busy:
            return {"error": "Сейчас идёт монтаж или нарезка стрима (" + ", ".join(busy)
                             + "). Дождись конца, потом ставь обновление."}
        done = updater.apply(A.upd["info"])
        A.log("Обновлено: " + ", ".join(done) + ". Перезапускаю программу…")
        for port in ROLE["children"].values():
            try:
                _post(port, "quit", {}, timeout=5)
            except Exception:
                pass
        A.restart_cmd = [sys.executable, str(CODE / "review.py"), "--port", str(SERVER["port"]), "--no-window",
                         "--token", TOKEN]
        threading.Timer(2.0, A.quit.set).start()
        return {"restart": True}
    return {}


def look(d):
    with A.lock:
        if "style" in d:
            A.st["style_key"] = d["style"]
            A.st["style"] = dict(m.STYLES).get(d["style"], "Случайный")
        if "font" in d:
            A.st["font"] = d["font"] or "Как в настройках"
        if "anim" in d:
            A.st["sub_anim"] = d["anim"]
        if "zoom" in d:
            A.st["zoom"] = bool(d["zoom"])
        if "teaser" in d:
            A.st["teaser"] = bool(d["teaser"])
        if "sub" in d:
            if d["sub"] is None:
                A.st.setdefault("sub_pos", {}).pop(d["fmt"], None)
            else:
                A.st.setdefault("sub_pos", {})[d["fmt"]] = int(d["sub"])
        rv.save_state(A.st)
    return {"ok": True}


ACTIONS = {
    "init": lambda d: init_data(),
    "queue": lambda d: queue_data(),
    "open": lambda d: open_clip(d["id"]),
    "preview": preview_req,
    "preview_video": preview_video_req,
    "montage": montage_req,
    "skip": skip_req,
    "vod": vod_req,
    "find": lambda d: find_clips(),
    "add_files": lambda d: add_files(),
    "clear_caps": clear_caps,
    "open_player": lambda d: (open_path(A.item(d["id"])["file"]), {"ok": True})[1],
    "open_out": lambda d: (open_path(Path(d["path"]).parent), {"ok": True})[1],
    "quit": quit_req,
    "prefetch": lambda d: (threading.Thread(target=lambda: [safe_copy(i, A.item(i)["file"]) for i in d.get("ids", [])
                                                         if _has(i)], daemon=True).start(), {"ok": True})[1],
    "streamers": streamers,
    "settings": settings,
    "tg_topics": tg_topics,
    "banners": banners,
    "tt": tt_req,
    "leaders": leaders_req,
    "editor": editor_req,
    "update": update,
    "look": look,
    "remote": remote_req,
    "cancel": cancel_req,
    "blog": blog_req,
}


# ---------- сервер ----------
SERVER = {"port": 0}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _qs(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def _authed(self):
        return self._qs().get("k") == TOKEN or self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ping":
            return self._send(200, b"prodaem-clipper", "text/plain")
        if not self._authed():
            return self._send(403, "Открой программу ярлыком на рабочем столе.".encode("utf-8"), "text/plain; charset=utf-8")
        q = self._qs()
        try:
            if path == "/":
                html = UI_FILE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/poll":
                if not q.get("quiet"):
                    A.last_poll = time.time()
                    touch_parent()
                return self._json(snapshot(int(q.get("since", 0))))
            if path == "/touch":
                A.last_poll = time.time()
                return self._json({"ok": True})
            if path == "/ports":
                return self._json(ports_map())
            if path == "/logo.png":
                ico, png = rv.make_assets()
                big = m.CACHE / f"logo_128_v5_{(rv.PROFILE or 'cis').lower()}.png"
                if not big.exists():
                    rv.draw_logo(rv.PROFILE, 128).save(big)
                return self._send(200, big.read_bytes(), "image/png", {"Cache-Control": "no-cache"})
            if path == "/wordmark.png":
                f = m.CACHE / f"wordmark_v5_{(rv.PROFILE or 'cis').lower()}.png"
                if not f.exists():
                    wm = rv.wordmark(rv.PROFILE)
                    if wm is None:
                        return self._send(404)
                    wm.save(f)
                return self._send(200, f.read_bytes(), "image/png", {"Cache-Control": "no-cache"})
            if path == "/preview.png":
                f = A.pv["png"]
                return self._send(200, Path(f).read_bytes(), "image/png") if f and Path(f).exists() else self._send(404)
            if path == "/banner.png":
                f = A.bpv["png"]
                return self._send(200, Path(f).read_bytes(), "image/png") if f and Path(f).exists() else self._send(404)
            if path == "/frame.jpg":
                p = A.item(q["id"])
                r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{float(q.get('t', 0)):.2f}", "-i", p["file"],
                                    "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "3", "-f", "image2pipe",
                                    "-vcodec", "mjpeg", "pipe:1"], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return self._send(200 if r.stdout else 404, r.stdout, "image/jpeg")
            if path == "/pvideo.mp4":
                f = A.pvv.get("file")
                return self._video(f) if f and Path(f).exists() else self._send(404)
            if path == "/media":
                f = MEDIA.get(q.get("id", ""))
                return self._video(f) if f and Path(f).exists() else self._send(404)
            if path == "/video":
                f = A.item(q["id"])["file"]
                return self._video(safe_copy(q["id"], f) if q.get("safe") else f)
        except KeyError as e:
            return self._json({"error": str(e).strip("'")}, 404)
        except Exception as e:
            return self._json({"error": str(e)}, 500)
        return self._send(404)

    def _video(self, file):
        """Видео клипа с поддержкой перемотки (Range), чтобы смотреть его прямо в окне."""
        size = os.path.getsize(file)
        ctype = {".webm": "video/webm", ".mov": "video/quicktime"}.get(Path(file).suffix.lower(), "video/mp4")
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:
                start = size - int(b)
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(file, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass

    def do_POST(self):
        if not self._authed():
            return self._send(403)
        name = urllib.parse.urlparse(self.path).path.strip("/").replace("api/", "")
        n = int(self.headers.get("Content-Length") or 0)
        if name == "upload":                     # файл перетащили в окно
            got = save_upload(self, self._qs().get("name") or "video.mp4", n)
            return self._json({"path": got} if got else {"error": "файл не загрузился"})
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            fn = ACTIONS.get(name)
            if not fn:
                return self._json({"error": "нет такой команды"}, 404)
            return self._json(fn(d) or {})
        except ValueError as e:
            return self._json({"error": str(e)})
        except KeyError as e:
            return self._json({"error": str(e).strip("'")})
        except Exception as e:
            A.log(f"ошибка окна ({name}): {e}")
            return self._json({"error": str(e)})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                                     # окно закрыли посреди загрузки видео: это не ошибка


SAFE_LOCK = threading.Lock()
SAFE_LOCKS = {}
VB_FILE = CODE / "cache" / "video_broken.txt"    # окно на этом компьютере не показывает оригиналы клипов
PREP = {"q": queue.Queue(), "seen": set()}


def prep_worker():
    """Заранее готовит копии клипов из очереди, чтобы видео в окне появлялось сразу."""
    while True:
        cid = PREP["q"].get()
        try:
            safe_copy(cid, A.item(cid)["file"])
        except Exception:
            pass


def prep_queue(n=25):
    if not VB_FILE.exists():
        return
    with A.lock:
        ids = [p["clip_id"] for p in A.items[:n]]
    for cid in ids:
        if cid not in PREP["seen"]:
            PREP["seen"].add(cid)
            PREP["q"].put(cid)


def video_broken(d):
    VB_FILE.parent.mkdir(exist_ok=True)
    VB_FILE.write_text("1", encoding="utf-8")
    if d.get("id"):
        PREP["seen"].add(d["id"])
    prep_queue()
    return {"ok": True}


ACTIONS["video_broken"] = video_broken


def video_log(d):
    """Почему окно не показало видео клипа: пишем в журнал, чтобы было видно причину."""
    try:
        name = Path(A.item(d.get("id"))["file"]).name
    except Exception:
        name = str(d.get("id"))[:40]
    A.log(f"видео в окне не показалось ({'копия' if d.get('safe') else 'оригинал'}): {str(d.get('why'))[:120]} · {name}")
    return {"ok": True}


ACTIONS["video_log"] = video_log


def _has(cid):
    try:
        A.item(cid)
        return True
    except KeyError:
        return False


def safe_copy(cid, src):
    """Копия клипа, которую окно проигрывает на любом компьютере: H.264 720p, обычный цвет, звук AAC.
    Делается один раз, если оригинал окно показывает чёрным."""
    out = m.CACHE / "play" / ("".join(ch for ch in cid if ch.isalnum() or ch in "_-")[:60] + ".mp4")
    with SAFE_LOCK:
        lk = SAFE_LOCKS.setdefault(cid, threading.Lock())
    with lk:                                     # один клип не готовится дважды, а разные готовятся параллельно
        if not out.exists() or out.stat().st_size < 1000:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".tmp.mp4")
            err = ""
            for hw in (["-hwaccel", "auto"], []):    # видеокарта иногда не справляется: тогда без неё
                try:
                    tmp.unlink()
                except OSError:
                    pass
                r = subprocess.run(["ffmpeg", "-v", "error", "-y", *hw, "-i", str(src), "-vf", "scale=854:-2,fps=30,format=yuv420p",
                                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-profile:v", "main",
                                    "-threads", "4", "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart", str(tmp)],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
                    tmp.replace(out)
                    break
                err = ((r.stderr or "").strip().splitlines() or ["?"])[-1][:160]
            else:
                A.log(f"копия клипа для окна не получилась: {err}")
    return str(out) if out.exists() else src


def free_port(want=0):
    s = socket.socket()
    if sys.platform != "win32":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", want))
    except OSError:
        s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_browser():
    """Edge (есть в любой Windows 10/11), иначе Chrome. Окно открывается без адресной строки."""
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if root:
            cands += [Path(root) / "Microsoft/Edge/Application/msedge.exe", Path(root) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


def open_window(url):
    br = find_browser()
    if not br:
        return False
    prof = CODE / "cache" / "webui"
    subprocess.Popen([br, f"--app={url}", f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check",
                      "--disable-features=Translate", "--start-maximized", "--window-size=1500,950",
                      "--disable-accelerated-video-decode"],       # иначе на части видеокарт AMD видео чёрное
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def already_running(start=""):
    """Если программа уже открыта, просто показываем её окно ещё раз (на нужном профиле)."""
    try:
        port, tok = PORT_FILE.read_text(encoding="utf-8").split()[:2]
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1.5) as r:
            if r.read() == b"prodaem-clipper":
                open_window(f"http://127.0.0.1:{port}/?k={tok}" + (f"&start={start}" if start else ""))
                return True
    except Exception:
        pass
    return False


def start_children():
    """Главная программа (СНГ) запускает остальные профили в фоне: у каждого свой пул, очередь и настройки."""
    for i, (key, d) in enumerate([x for x in profile_dirs() if x[0]]):
        port = free_port(SERVER["port"] + 1 + i)
        ROLE["children"][key] = port
        env = dict(os.environ, CLIPPER_DATA=str(d), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        subprocess.Popen([sys.executable, str(CODE / "review.py"), "--data", str(d), "--port", str(port),
                          "--no-window", "--token", TOKEN, "--parent", str(SERVER["port"])],
                         cwd=str(d), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.time()                             # ждём, пока профили поднимутся, чтобы вкладки сразу открывались
    while time.time() - t0 < 15:
        if all(_alive(p) for p in ROLE["children"].values()):
            break
        time.sleep(0.3)


def _alive(port):
    try:
        return _get(port, "/ping", 1) == b"prodaem-clipper"
    except Exception:
        return False


def hide_console():
    """Чёрное окно консоли прячем: всё, что туда писалось, видно в журнале программы и в cache/app.log."""
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            ids = (ctypes.c_uint * 8)()
            alone = ctypes.windll.kernel32.GetConsoleProcessList(ids, 8) <= 1
            if hwnd and alone:                   # окно консоли наше (запуск ярлыком), а не открытый PowerShell
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass


def run(port=0, window=True, token=None, parent=0, start=""):
    """Запускает новое окно. False, если открыть его негде (нет Edge и Chrome): тогда откроется старое.
    parent: этот процесс обслуживает один профиль, а окно и остальные профили держит главная программа."""
    global TOKEN
    if token:
        TOKEN = token                            # после обновления окно продолжает работать с тем же ключом
    ROLE["child"], ROLE["parent"] = bool(parent), parent
    m.CACHE.mkdir(exist_ok=True)
    if not UI_FILE.exists():
        print("Нет файла ui.html рядом с программой: открываю старое окно.")
        return False
    if window and not port and already_running(start):
        return True
    if window and not find_browser():
        print("Не найден Edge или Chrome: открываю старое окно.")
        return False
    SERVER["port"] = free_port(port)
    srv = Server(("127.0.0.1", SERVER["port"]), Handler)
    if not ROLE["child"]:
        PORT_FILE.write_text(f"{SERVER['port']} {TOKEN}", encoding="utf-8")
    url = f"http://127.0.0.1:{SERVER['port']}/?k={TOKEN}" + (f"&start={start}" if start else "")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real_out = sys.__stdout__
    sys.stdout = sys.stderr = Out()              # с этого момента всё, что печатает монтаж, идёт в журнал
    A.refresh()
    if not ROLE["child"]:
        ico, _ = rv.make_assets()
        msg = rv.make_shortcut(ico) if ico else None
        if msg:
            A.log(msg)
        start_children()
    threading.Thread(target=worker, name="montage-worker", daemon=True).start()
    threading.Thread(target=auto_find_loop, daemon=True).start()
    threading.Thread(target=cleanup_loop, daemon=True).start()
    threading.Thread(target=daily_loop, daemon=True).start()
    threading.Thread(target=vodq_loop, daemon=True).start()
    live_register()
    if ROLE["child"]:
        def live_loop():
            while not A.quit.wait(60):
                live_register()
        threading.Thread(target=live_loop, daemon=True).start()
    else:
        threading.Thread(target=heartbeat_loop, daemon=True).start()
    try:                                         # кнопки «Залил / Не буду», /plan и управление с телефона
        import tgbot
        tgbot.LIVE_DIR = LIVE_DIR
        tgbot.start(A.log)
    except Exception as e:
        A.log(f"телеграм-бот не запустился: {e}")

    threading.Thread(target=prep_worker, daemon=True).start()

    def fonts_job():                             # шрифты для субтитров скачиваются один раз
        missing = [k for k in m.FONT_LIB if k not in m._font_map()]
        if not missing:
            return
        bad = []
        for k in missing:
            if not m.get_font(k):
                bad.append(k)
        if len(bad) < len(missing):
            A.log(f"Шрифты для субтитров готовы: {len(missing) - len(bad)}" + (f", без русских букв или не скачались: {', '.join(bad)}" if bad else ""))
        else:
            A.log("Шрифты для субтитров не скачались: нет связи с Google Fonts. Попробую при следующем запуске.")
    threading.Thread(target=fonts_job, daemon=True).start()
    prep_queue()
    if window:
        open_window(url)
    A.last_poll = time.time() + 30               # даём окну время открыться
    hide_console()
    try:
        dialog_loop()
    finally:
        srv.shutdown()
        if not ROLE["child"]:
            try:
                BEAT.write_text(json.dumps({"t": time.time(), "clean": True}), encoding="utf-8")
            except Exception:
                pass
            try:
                PORT_FILE.unlink()
            except Exception:
                pass
        sys.stdout = real_out
        if A.restart_cmd:
            subprocess.Popen(A.restart_cmd, cwd=str(BASE))
    return True
