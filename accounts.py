"""Анализ TikTok-аккаунтов: что заходит, что нет и что выкладывать дальше.

Собирает с открытых профилей все ролики (просмотры, лайки, репосты, сохранения, длину, время выхода),
находит прострелы и провалы, дубли между аккаунтами, сравнивает начало роликов по звуку и движению,
связывает ролики с настройками клиппера (стример, стиль, формат) и просит Claude написать разбор.
Каждый запуск сохраняет снимок просмотров, так что видно рост за день и за неделю и прогресс к цели.
"""
import difflib
import json
import re
import statistics as st
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import montage as m

BASE = m.BASE
DIR = BASE / "analytics"
SETTINGS = DIR / "settings.json"
HISTORY = DIR / "history.json"
REPORT = DIR / "report.json"
VIDS = DIR / "videos"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PROBE_VIEWS = 700          # примерно столько TikTok даёт любому ролику на пробу
HIT = 10000                # с этого ролик считается прострелом


class Stop(Exception):
    pass


# ---------- настройки ----------
def load_settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        s = {}
    s.setdefault("accounts", [])
    s.setdefault("goal", 100000)
    s.setdefault("rate", 1.0)          # $ за 1000 просмотров
    s.setdefault("min_views", 3000)    # в выплату идут ролики от стольких просмотров
    s.setdefault("money_goal", 150)    # цель на неделю в $
    return s


def save_settings(s):
    DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def clean_handle(h):
    h = str(h).strip()
    mt = re.search(r"tiktok\.com/@([\w.\-]+)", h)
    if mt:
        h = mt.group(1)
    return h.lstrip("@").strip().strip("/").lower()


# ---------- сбор роликов ----------
def _ytdlp(args, timeout=600):
    return subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"] + args,
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN, timeout=timeout)


_UPDATED = {"done": False}


def _update_ytdlp(say):
    """TikTok часто меняет сайт; свежий yt-dlp с curl_cffi обычно снова его открывает."""
    if _UPDATED["done"]:
        return
    _UPDATED["done"] = True
    say("обновляю загрузчик yt-dlp (TikTok часто меняет сайт)…")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default,curl-cffi]"],
                   capture_output=True, creationflags=NOWIN, timeout=600)


def fetch_account(handle, say, limit=120):
    """Все ролики профиля: [{id, url, desc, views, likes, comments, shares, saves, dur, ts}]."""
    url = f"https://www.tiktok.com/@{handle}"
    err = ""
    for attempt in range(2):
        try:
            r = _ytdlp(["-J", "--flat-playlist", "--playlist-end", str(limit), url])
        except subprocess.TimeoutExpired:
            err = "TikTok не ответил за 10 минут"
            continue
        if r.returncode == 0 and r.stdout.strip():
            break
        if "No module named yt_dlp" in (r.stderr or ""):
            err = "не установлен yt-dlp"
        else:
            err = ((r.stderr or "").strip().splitlines() or ["неизвестная ошибка"])[-1][:240]
        if attempt == 0:
            _update_ytdlp(say)
    else:
        raise Stop(f"@{handle}: не получилось открыть профиль ({err}). Проверь ник и что профиль открытый.")
    j = json.loads(r.stdout)
    out = []
    for e in j.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({"id": str(e["id"]), "acc": handle,
                    "url": e.get("url") or e.get("webpage_url") or f"https://www.tiktok.com/@{handle}/video/{e['id']}",
                    "desc": (e.get("description") or e.get("title") or "").strip(),
                    "views": int(e.get("view_count") or 0), "likes": int(e.get("like_count") or 0),
                    "comments": int(e.get("comment_count") or 0), "shares": int(e.get("repost_count") or 0),
                    "saves": int(e.get("save_count") or 0), "dur": float(e.get("duration") or 0),
                    "ts": int(e.get("timestamp") or 0)})
    if not out:
        raise Stop(f"@{handle}: в профиле не видно роликов (профиль закрытый или пустой)")
    return out


# ---------- история просмотров ----------
def save_snapshot(videos):
    """Запоминает просмотры каждого ролика на сегодня: так видно рост за сутки и за неделю."""
    try:
        hist = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:
        hist = {}
    day = datetime.now().strftime("%Y-%m-%d")
    hist[day] = {v["id"]: v["views"] for v in videos}
    days = sorted(hist)[-60:]
    hist = {d: hist[d] for d in days}
    DIR.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(hist), encoding="utf-8")
    return hist


def growth(videos, hist, days):
    """Сколько просмотров набрали ролики за последние days дней (по снимкам; новые ролики целиком)."""
    now = datetime.now()
    old_day = None
    for d in sorted(hist):
        if (now - datetime.strptime(d, "%Y-%m-%d")).days >= days:
            old_day = d
    old = hist.get(old_day, {}) if old_day else {}
    tot, per = 0, {}
    cutoff = time.time() - days * 86400
    for v in videos:
        if v["id"] in old:
            g = max(0, v["views"] - old[v["id"]])
        elif v["ts"] >= cutoff or not old_day:
            g = v["views"] if v["ts"] >= cutoff else 0
        else:
            g = 0
        tot += g
        per[v["acc"]] = per.get(v["acc"], 0) + g
    return tot, per, bool(old_day)


# ---------- связь с роликами клиппера ----------
def local_outputs():
    """Готовые ролики клиппера с их настройками и длиной."""
    out = []
    for j in (BASE / "clips" / "ready").glob("*/*.json"):
        mp4 = j.with_suffix(".mp4")
        if not mp4.exists():
            continue
        try:
            p = json.loads(j.read_text(encoding="utf-8"))
            d = m.probe(mp4)["dur"]
        except Exception:
            continue
        out.append({"file": str(mp4), "dur": d, "mtime": mp4.stat().st_mtime, "streamer": p.get("streamer", ""),
                    "style": p.get("style", ""), "format": p.get("format", ""), "clip_id": p.get("clip_id", ""),
                    "title": p.get("final_title") or p.get("title", ""), "source": p.get("source", "")})
    return out


def match_local(videos, outs):
    """Какой готовый ролик клиппера стал этим TikTok: длина совпадает, сделан до публикации,
    и если есть название, оно похоже на описание."""
    for v in videos:
        if not v["dur"]:
            continue
        best, bs = None, 0
        for o in outs:
            if abs(o["dur"] - v["dur"]) > 1.05 or (v["ts"] and o["mtime"] > v["ts"] + 600):
                continue
            sc = 1.0 - abs(o["dur"] - v["dur"]) / 2
            if o["title"] and v["desc"]:
                sc += difflib.SequenceMatcher(None, o["title"].lower(), v["desc"].lower()[:len(o["title"]) + 20]).ratio()
            if v["ts"]:
                sc += max(0, 1 - (v["ts"] - o["mtime"]) / (3 * 86400)) * 0.5   # выложен вскоре после монтажа
            if sc > bs:
                best, bs = o, sc
        if best and bs >= 0.9:
            v["local"] = best


def _plain(desc):
    """Описание без хэштегов, упоминаний и эмодзи: одинаковые #dota2 не делают ролики дублями."""
    t = re.sub(r"[#@][\w.]+", " ", desc.lower())
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def duplicates(videos):
    """Один и тот же клип на нескольких аккаунтах (или дважды на одном): тот же клип клиппера,
    либо одинаковая длина и почти одинаковое описание (без хэштегов, не короче 12 букв)."""
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(a, b):
        parent[find(a)] = find(b)

    by_clip = {}
    for v in videos:
        key = (v.get("local") or {}).get("clip_id")
        if key:
            if key in by_clip:
                join(v["id"], by_clip[key])
            by_clip[key] = v["id"]
    txt = [(v, _plain(v["desc"])) for v in videos]
    txt = [(v, t) for v, t in txt if len(t) >= 12 and v["dur"]]
    for i, (a, ta) in enumerate(txt):
        for b, tb in txt[i + 1:]:
            if a["id"] != b["id"] and abs(a["dur"] - b["dur"]) <= 1 and \
                    difflib.SequenceMatcher(None, ta, tb).ratio() > 0.92:
                join(a["id"], b["id"])
    groups = {}
    for v in videos:
        if v["id"] in parent:
            groups.setdefault(find(v["id"]), {})[v["id"]] = v
    return [sorted(g.values(), key=lambda v: v["ts"]) for g in groups.values() if len(g) > 1]


# ---------- как начинается ролик ----------
def hook_metrics(path):
    """Звук и движение по времени: насколько сильно цепляют первые 2 секунды, есть ли провалы."""
    import numpy as np
    path = str(path)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True, creationflags=NOWIN).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    res = {}
    if a.size >= 8000:
        n = a.size // 800
        rms = np.sqrt(np.mean(a[:n * 800].reshape(n, 800) ** 2, axis=1) + 1e-10)
        db = 20 * np.log10(rms)
        med = float(np.median(db))
        res["dur"] = round(n / 10, 1)
        res["hook_loud"] = round(float(np.mean(db[:20])) - med, 1)          # первые 2 с громче обычного?
        quiet = db < max(med - 14, -50)
        runs, cur = [], 0
        for q in quiet:
            cur = cur + 1 if q else 0
            if cur == 1:
                runs.append(1)
            elif cur > 1:
                runs[-1] += 1
        res["dead_air"] = round(sum(r for r in runs if r >= 10) / 10, 1)     # тихие куски дольше секунды
        w = np.convolve(db, np.ones(10) / 10, mode="valid")
        res["peak_at"] = round(float(np.argmax(w)) / 10, 1)                  # где самый громкий момент
        on = np.where(db > med - 3)[0]
        res["sound_start"] = round(float(on[0]) / 10, 1) if on.size else None
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-an", "-vf", "fps=5,scale=48:84,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, creationflags=NOWIN).stdout
    f = np.frombuffer(raw, dtype=np.uint8)
    if f.size >= 48 * 84 * 4:
        fr = f[:f.size // (48 * 84) * 48 * 84].reshape(-1, 84, 48).astype(np.float32)
        d = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
        medm = float(np.median(d)) + 0.5
        res["hook_motion"] = round(float(np.mean(d[:10])) / medm, 2)          # первые 2 с динамичнее обычного?
        res["cuts"] = int(np.sum(d > max(18, medm * 4)))
    return res


def get_video(v, say):
    """Файл ролика для разбора: свой из клиппера, иначе скачать лёгкую версию с TikTok."""
    if v.get("local") and Path(v["local"]["file"]).exists():
        return Path(v["local"]["file"])
    VIDS.mkdir(parents=True, exist_ok=True)
    have = list(VIDS.glob(f"{v['id']}.*"))
    if have:
        return have[0]
    say(f"скачиваю ролик @{v['acc']} на {fmt_n(v['views'])} просмотров…")
    r = _ytdlp(["-S", "res:540,+size", "-o", str(VIDS / f"{v['id']}.%(ext)s"), v["url"]], timeout=300)
    have = list(VIDS.glob(f"{v['id']}.*"))
    return have[0] if r.returncode == 0 and have else None


# ---------- цифры ----------
def fmt_n(n):
    n = int(n)
    return f"{n / 1e6:.1f}М" if n >= 1e6 else f"{n / 1e3:.1f}к".replace(".0к", "к") if n >= 1000 else str(n)


def _med(xs):
    return int(st.median(xs)) if xs else 0


def account_stats(vs):
    views = [v["views"] for v in vs]
    eng = [(v["likes"] + v["comments"] * 2 + v["shares"] * 3 + v["saves"] * 2) / v["views"] * 100
           for v in vs if v["views"] >= 100]
    last = [v for v in vs if v["ts"] >= time.time() - 7 * 86400]
    hours = {}
    for v in vs:
        if v["ts"]:
            hours.setdefault(datetime.fromtimestamp(v["ts"]).hour, []).append(v["views"])
    return {"videos": len(vs), "total": sum(views), "median": _med(views), "max": max(views or [0]),
            "hits": sum(1 for x in views if x >= HIT), "stuck": round(sum(1 for x in views if x <= PROBE_VIEWS) / max(1, len(views)) * 100),
            "eng": round(st.median(eng), 1) if eng else 0, "last7": len(last),
            "dur_med": round(st.median([v["dur"] for v in vs if v["dur"]]) if any(v["dur"] for v in vs) else 0)}


def buckets(videos, key, labels):
    """Медиана просмотров по группам (длина, час выхода и т.п.)."""
    g = {}
    for v in videos:
        k = key(v)
        if k is not None:
            g.setdefault(k, []).append(v["views"])
    return [{"label": lab, "n": len(g.get(k, [])), "median": _med(g.get(k, []))} for k, lab in labels if g.get(k)]


def dur_bucket(v):
    d = v["dur"]
    if not d:
        return None
    return 0 if d < 15 else 1 if d < 25 else 2 if d < 40 else 3 if d < 60 else 4


DUR_LABELS = [(0, "до 15 с"), (1, "15–25 с"), (2, "25–40 с"), (3, "40–60 с"), (4, "больше минуты")]


def hour_bucket(v):
    if not v["ts"]:
        return None
    h = datetime.fromtimestamp(v["ts"]).hour
    return 0 if h < 6 else 1 if h < 12 else 2 if h < 17 else 3 if h < 21 else 4


HOUR_LABELS = [(0, "ночь 0–6"), (1, "утро 6–12"), (2, "день 12–17"), (3, "вечер 17–21"), (4, "поздно 21–24")]


# ---------- разбор от Claude ----------
REPORT_PROMPT = """Ты продюсер коротких видео в TikTok. Перед тобой статистика аккаунтов клиппера: он режет клипы
со стримов по Dota 2 (и иногда другим играм) и выкладывает их в TikTok. Ролики делает программа: вертикальный
формат, камера стримера, субтитры, баннер партнёрки в середине ролика. Обычно ролики застревают на 300-700
просмотрах (TikTok дал пробный показ и не пошёл дальше). Цель автора: {goal} просмотров на одном ролике за неделю.
Деньги: партнёрка платит ${rate} за 1000 просмотров из СНГ, но только за ролики, набравшие от {min_views} просмотров.
Ролики ниже порога не приносят ничего, поэтому важна не частота, а доля роликов, перешедших порог. Цель на неделю: ${money_goal}.

Данные (JSON):
{data}

Пояснения к полям: views просмотры, eng вовлечённость в % (лайки + комментарии x2 + репосты x3 + сохранения x2
к просмотрам), stuck % роликов, застрявших на пробном показе, hook_loud насколько громче обычного первые 2 секунды
(дБ), hook_motion насколько динамичнее обычного первые 2 секунды (1 = как весь ролик), dead_air сколько секунд
тишины дольше секунды, peak_at на какой секунде самый громкий момент, cuts количество резких смен кадра,
settings: стример, стиль и формат монтажа в программе.

Найди, что реально отличает ролики-прострелы от провалов, на основе цифр, а не общих советов. Будь конкретным:
называй ролики, секунды, проценты. Если данных мало, так и скажи. Советуй только честные способы: что брать
в клипы, как начинать ролик, длину, подачу, текст, время, разделение аккаунтов по темам. Не советуй накрутку,
уникализацию дублей, обход проверок TikTok.

Ответь ТОЛЬКО JSON без markdown:
{{"main": "главная причина, почему ролики застревают, 1-2 предложения",
 "works": ["что работает, с цифрами", ...],
 "kills": ["что убивает охват, с цифрами", ...],
 "accounts": [{{"acc": "ник", "verdict": "коротко что с ним", "fix": ["что поменять", ...]}}],
 "plan": ["план на 7 дней к цели, по шагам", ...],
 "next_clips": ["какие моменты искать и выкладывать в первую очередь", ...]}}"""


def claude_report(data, cfg, say):
    if not m.KEY_FILE.exists():
        return None
    key = m.KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        return None
    import requests
    say("Claude разбирает статистику…")
    prompt = REPORT_PROMPT.format(goal=fmt_n(data.get("goal", 100000)), rate=data.get("rate", 1),
                                  min_views=data.get("min_views", 3000), money_goal=data.get("money_goal", 150),
                                  data=json.dumps(data, ensure_ascii=False)[:60000])
    models = [cfg.get("report_model", "claude-sonnet-5-5"), cfg.get("claude_model", "claude-haiku-4-5")]
    last = ""
    for model in dict.fromkeys(models):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": model, "max_tokens": 3000,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=180)
        except Exception as e:
            last = str(e)
            continue
        if r.status_code != 200:
            last = f"{r.status_code}: {r.text[:200]}"
            continue
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        out = re.sub(r"```(?:json)?|```", "", out).strip()
        try:
            return json.loads(out[out.index("{"):out.rindex("}") + 1])
        except Exception as e:
            last = f"ответ не разобрался: {e}"
    say(f"разбор от Claude не получился: {last}")
    return None


# ---------- главный запуск ----------
def short(v):
    o = {"acc": v["acc"], "views": v["views"], "eng": round((v["likes"] + v["comments"] * 2 + v["shares"] * 3 + v["saves"] * 2)
                                                         / max(1, v["views"]) * 100, 1),
         "likes": v["likes"], "shares": v["shares"], "saves": v["saves"], "comments": v["comments"],
         "dur": v["dur"], "posted": datetime.fromtimestamp(v["ts"]).strftime("%d.%m %H:%M") if v["ts"] else "",
         "desc": v["desc"][:110]}
    if v.get("hook"):
        o["hook"] = v["hook"]
    if v.get("local"):
        o["settings"] = {k: v["local"][k] for k in ("streamer", "style", "format")}
    return o


def analyze(handles, goal, cfg, say=print, deep=True):
    handles = [clean_handle(h) for h in handles if clean_handle(h)]
    if not handles:
        raise Stop("впиши хотя бы один ник аккаунта")
    s = load_settings()
    s.update(accounts=handles, goal=int(goal or 100000))
    save_settings(s)
    videos, errors = [], []
    for k, h in enumerate(handles, 1):
        say(f"собираю ролики @{h} ({k} из {len(handles)})…")
        try:
            videos += fetch_account(h, say)
        except Stop as e:
            errors.append(str(e))
            say(str(e))
    if not videos:
        raise Stop("ни один профиль не открылся. " + " ".join(errors))
    hist = save_snapshot(videos)
    g7, g7_acc, has_hist = growth(videos, hist, 7)
    g1, _, has1 = growth(videos, hist, 1)
    say("связываю ролики с монтажом клиппера…")
    try:
        match_local(videos, local_outputs())
    except Exception as e:
        say(f"связать с клиппером не получилось: {e}")
    dups = duplicates(videos)

    # разбор начала роликов: лучшие и худшие из свежих
    if deep:
        pool = sorted([v for v in videos if v["ts"] >= time.time() - 45 * 86400] or videos, key=lambda v: -v["views"])
        pick = pool[:5] + [v for v in pool[-6:] if v not in pool[:5]]
        for i, v in enumerate(pick, 1):
            say(f"смотрю начало роликов: {i} из {len(pick)}…")
            try:
                f = get_video(v, say)
                if f:
                    v["hook"] = hook_metrics(f)
            except Exception as e:
                say(f"ролик {v['id']} не разобрался: {e}")

    accs = {h: account_stats([v for v in videos if v["acc"] == h]) for h in handles if any(v["acc"] == h for v in videos)}
    best = max(videos, key=lambda v: v["views"])
    top = sorted(videos, key=lambda v: -v["views"])[:8]
    recent = [v for v in videos if v["ts"] >= time.time() - 30 * 86400] or videos
    flops = sorted(recent, key=lambda v: v["views"])[:8]
    settings_perf = {}
    for v in videos:
        if v.get("local"):
            for k in ("streamer", "style", "format"):
                settings_perf.setdefault(k, {}).setdefault(v["local"][k] or "—", []).append(v["views"])
    settings_perf = {k: sorted([{"value": a, "n": len(b), "median": _med(b), "max": max(b)} for a, b in d.items()],
                               key=lambda x: -x["median"]) for k, d in settings_perf.items()}
    data = {"goal": s["goal"], "rate": s["rate"], "min_views": s["min_views"], "money_goal": s["money_goal"],
            "money_week": money(videos, s), "accounts": accs, "best": short(best),
            "growth_7d": g7, "growth_7d_by_account": g7_acc,
            "by_duration": buckets(videos, dur_bucket, DUR_LABELS), "by_hour": buckets(videos, hour_bucket, HOUR_LABELS),
            "settings": settings_perf, "duplicates": [[short(v) for v in g] for g in dups[:10]],
            "top": [short(v) for v in top], "flops": [short(v) for v in flops]}
    ai = claude_report(data, cfg, say)
    rep = {"time": time.time(), "handles": handles, "goal": s["goal"], "errors": errors,
           "accounts": accs, "best": {**short(best), "url": best["url"]},
           "growth_7d": g7, "growth_1d": g1 if has1 else None, "has_history": has_hist,
           "by_duration": data["by_duration"], "by_hour": data["by_hour"], "settings": settings_perf,
           "duplicates": [[{**short(v), "url": v["url"]} for v in g] for g in dups],
           "top": [{**short(v), "url": v["url"]} for v in top],
           "flops": [{**short(v), "url": v["url"]} for v in flops],
           "total_videos": len(videos), "ai": ai, "money": money(videos, s)}
    DIR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    say("готово")
    return rep


def money(videos, s):
    """Сколько принесли ролики недели: в выплату идут только набравшие min_views."""
    week = [v for v in videos if v["ts"] >= time.time() - 7 * 86400]
    paid = [v for v in week if v["views"] >= s["min_views"]]
    near = [v for v in week if s["min_views"] * 0.4 <= v["views"] < s["min_views"]]
    usd = sum(v["views"] for v in paid) / 1000 * float(s["rate"])
    return {"usd": round(usd, 2), "goal": s["money_goal"], "paid": len(paid), "week": len(week),
            "near": [{"acc": v["acc"], "views": v["views"], "url": v["url"], "desc": v["desc"][:80]} for v in
                     sorted(near, key=lambda v: -v["views"])[:6]],
            "min_views": s["min_views"], "rate": s["rate"],
            "lost": sum(v["views"] for v in week if v["views"] < s["min_views"])}


def last_report():
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("handles", nargs="+")
    ap.add_argument("--goal", type=int, default=100000)
    ap.add_argument("--fast", action="store_true")
    a = ap.parse_args()
    cfg = json.loads((BASE / "montage.json").read_text(encoding="utf-8")) if (BASE / "montage.json").exists() else {}
    r = analyze(a.handles, a.goal, cfg, deep=not a.fast)
    print(json.dumps(r.get("ai") or {}, ensure_ascii=False, indent=1))
