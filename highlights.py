# -*- coding: utf-8 -*-
"""
Нарезка из длинного видео: запись стрима на Twitch, видео на YouTube или файл с компьютера.
Программа сама находит лучшие моменты и кладёт их в очередь, как обычные клипы.

Где ищем моменты (всё, что доступно для этого видео):
  громкость    крики, смех, резкие реакции
  чат          всплески сообщений (запись Twitch, повтор чата YouTube)
  клипы        моменты, которые уже нарезали зрители Twitch
  YouTube      «самые пересматриваемые» места
Потом речь распознаётся только вокруг найденных мест, и Claude выбирает лучшие фрагменты
с началом и концом по фразам. Без ключа Claude берутся самые сильные места по сигналам.

Запуск без окна:  python highlights.py <ссылка или файл> [сколько фрагментов]
"""
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import montage as m

BASE = m.BASE
VODS = BASE / "clips" / "vods"
PASSPORTS = BASE / "passports.jsonl"
CONFIG_FILE = BASE / "config.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Stop(Exception):
    pass


def _ytdlp(args, **kw):
    return subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"] + args,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NOWIN, **kw)


def source_info(src):
    """Что за видео: файл или ссылка, длина, канал, id."""
    p = Path(src.strip().strip('"'))
    if p.exists():
        return {"kind": "file", "path": str(p.resolve()), "id": re.sub(r"\W+", "_", p.stem)[:40],
                "title": p.stem, "channel": "", "dur": m.probe(p)["dur"]}
    url = src.strip()
    r = _ytdlp(["-J", "--skip-download", url])
    if r.returncode != 0:
        raise Stop("не получилось открыть ссылку: " + ((r.stderr or "").strip().splitlines() or ["неизвестная ошибка"])[-1][:200])
    j = json.loads(r.stdout)
    site = "twitch" if "twitch" in (j.get("extractor") or "").lower() else \
        "youtube" if "youtube" in (j.get("extractor") or "").lower() else "web"
    vid = str(j.get("id") or "").lstrip("v")
    return {"kind": site, "url": url, "id": re.sub(r"\W+", "_", f"{site}_{vid}")[:40], "vod": vid,
            "title": j.get("title") or "", "channel": (j.get("uploader_id") or j.get("channel") or j.get("uploader") or "").lower(),
            "dur": float(j.get("duration") or 0), "heatmap": j.get("heatmap") or [],
            "game": " ".join(j.get("categories") or []) or j.get("game") or ""}


def get_audio(info, say):
    """Звук всего видео в WAV 16 кГц моно: по нему считаем громкость и распознаём речь."""
    VODS.mkdir(parents=True, exist_ok=True)
    wav = VODS / f"{info['id']}.wav"
    if wav.exists() and wav.stat().st_size > 100000:
        return wav
    src = info.get("path")
    if not src:
        say("скачиваю звук стрима…")
        tmpl = str(VODS / f"{info['id']}_audio.%(ext)s")
        r = _ytdlp(["-f", "bestaudio/Audio_Only/audio_only/worst", "-o", tmpl, info["url"]])
        got = sorted(VODS.glob(f"{info['id']}_audio.*"))
        if r.returncode != 0 or not got:
            raise Stop("звук стрима не скачался: " + ((r.stderr or "").strip().splitlines() or [""])[-1][:200])
        src = str(got[0])
    say("готовлю звук для анализа…")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        str(wav)], capture_output=True, text=True, creationflags=NOWIN)
    if r.returncode != 0 or not wav.exists():
        raise Stop("звук не получилось подготовить: " + (r.stderr or "")[-200:])
    if not info.get("path"):
        for f in VODS.glob(f"{info['id']}_audio.*"):
            f.unlink(missing_ok=True)
    return wav


def loudness_curve(wav):
    """Громкость каждой секунды в дБ."""
    import numpy as np
    with wave.open(str(wav), "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        out = []
        while True:
            buf = w.readframes(sr * 60)
            if not buf:
                break
            a = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
            k = len(a) // sr
            if k:
                rms = np.sqrt((a[:k * sr].reshape(k, sr) ** 2).mean(axis=1) + 1e-10)
                out.extend((20 * np.log10(rms)).tolist())
    return np.array(out, dtype=np.float32)


def twitch_chat(vod, dur, say):
    """Время каждого сообщения чата записи Twitch (секунды от начала). Пусто, если Twitch не отдал чат."""
    import requests
    times, cursor, off = [], None, 0
    hdr = {"Client-Id": "kimne78kx3ncx6brgo4mv6wki5h1ko"}
    q = {"operationName": "VideoCommentsByOffsetOrCursor",
         "extensions": {"persistedQuery": {"version": 1,
                                           "sha256Hash": "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"}}}
    last_say = 0
    for _ in range(3000):
        q["variables"] = {"videoID": vod, "cursor": cursor} if cursor else {"videoID": vod, "contentOffsetSeconds": off}
        try:
            r = requests.post("https://gql.twitch.tv/gql", json=q, headers=hdr, timeout=20)
            d = r.json()
            edges = (((d.get("data") or {}).get("video") or {}).get("comments") or {}).get("edges") or []
        except Exception:
            break
        if not edges:
            break
        for e in edges:
            t = (e.get("node") or {}).get("contentOffsetSeconds")
            if t is not None:
                times.append(float(t))
        info = (((d.get("data") or {}).get("video") or {}).get("comments") or {}).get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = edges[-1].get("cursor")
        if not cursor:
            break
        if times and time.time() - last_say > 8 and dur:
            last_say = time.time()
            say(f"читаю чат: {int(100 * min(1, times[-1] / dur))}%")
    return times


def youtube_chat(info, say):
    """Время сообщений повтора чата YouTube, если он есть у записи стрима."""
    say("читаю чат YouTube…")
    tmpl = str(VODS / f"{info['id']}_chat")
    _ytdlp(["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", tmpl, info["url"]], timeout=900)
    times = []
    for f in VODS.glob(f"{info['id']}_chat*.json"):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            mm = re.search(r'"videoOffsetTimeMsec"\s*:\s*"?(\d+)', line)
            if mm:
                times.append(int(mm.group(1)) / 1000.0)
        f.unlink(missing_ok=True)
    return times


def twitch_clips(info):
    """Клипы, которые зрители нарезали с этой записи: [(секунда записи, просмотры)]."""
    import requests
    try:
        c = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        tok = requests.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": c["client_id"], "client_secret": c["client_secret"], "grant_type": "client_credentials"},
            timeout=20).json()["access_token"]
        h = {"Client-Id": c["client_id"], "Authorization": f"Bearer {tok}"}
        v = requests.get("https://api.twitch.tv/helix/videos", params={"id": info["vod"]}, headers=h, timeout=20).json()["data"][0]
        start = dt.datetime.fromisoformat(v["created_at"].replace("Z", "+00:00"))
        end = start + dt.timedelta(seconds=info["dur"] + 3600)
        out, cur = [], None
        for _ in range(20):
            p = {"broadcaster_id": v["user_id"], "first": 100, "started_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "ended_at": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
            if cur:
                p["after"] = cur
            j = requests.get("https://api.twitch.tv/helix/clips", params=p, headers=h, timeout=20).json()
            for cl in j.get("data", []):
                if str(cl.get("video_id")) == str(info["vod"]) and cl.get("vod_offset") is not None:
                    out.append((float(cl["vod_offset"]), int(cl.get("view_count") or 0)))
            cur = (j.get("pagination") or {}).get("cursor")
            if not cur:
                break
        return out
    except Exception:
        return []


def _z(x):
    import numpy as np
    s = x.std()
    return (x - x.mean()) / s if s > 1e-6 else x * 0


def _smooth(x, k):
    import numpy as np
    if k <= 1:
        return x
    return np.convolve(x, np.ones(k) / k, mode="same")


def score_curve(loud, chat, clips, heat, n):
    """Сила момента на каждой секунде из всех сигналов. Возвращает (кривая, описание сигналов по секундам)."""
    import numpy as np
    loud = np.asarray(loud[:n], dtype=np.float32)
    if len(loud) < n:
        loud = np.pad(loud, (0, n - len(loud)), constant_values=float(loud.min()) if len(loud) else -60)
    # громкость относительно обычной для этого куска стрима (окно 5 минут)
    base = np.array([np.median(loud[max(0, i - 150):i + 150]) for i in range(0, n, 30)])
    base = np.repeat(base, 30)[:n]
    excess = _smooth(np.clip(loud - base, 0, None), 4)
    score = 1.0 * _z(excess)
    parts = {"loud": excess}
    if chat:
        c = np.zeros(n, dtype=np.float32)
        for t in chat:
            i = int(t) - 6                       # чат реагирует на момент секунд через 5–8
            if 0 <= i < n:
                c[i] += 1
        rate = _smooth(c, 10)
        cbase = np.array([np.median(rate[max(0, i - 300):i + 300]) for i in range(0, n, 60)])
        cbase = np.repeat(cbase, 60)[:n]
        rel = rate / (cbase + 0.05)
        score += 1.4 * _z(np.log1p(rel))
        parts["chat"] = rel
    if heat:
        hm = np.zeros(n, dtype=np.float32)
        for h in heat:
            a, b = int(h.get("start_time", 0)), int(h.get("end_time", 0))
            hm[max(0, a):min(n, b)] = float(h.get("value", 0))
        score += 2.5 * _z(hm)
        parts["heat"] = hm
    if clips:
        cl = np.zeros(n, dtype=np.float32)
        for off, views in clips:
            mid = int(off) + 18                  # клип зрителя обычно заканчивается сразу после момента
            for i in range(max(0, mid - 20), min(n, mid + 20)):
                cl[i] += math.log1p(views) * math.exp(-((i - mid) / 10) ** 2)
        score += 1.2 * cl / (cl.max() + 1e-6) * 4
        parts["clips"] = cl
    return _smooth(score, 5), parts


def pick_peaks(score, k, gap=80, edge=20):
    import numpy as np
    s = score.copy()
    s[:edge] = -1e9
    s[-edge:] = -1e9
    out = []
    for _ in range(k):
        i = int(np.argmax(s))
        if s[i] <= -1e8:
            break
        out.append(i)
        s[max(0, i - gap):i + gap] = -1e9
    return sorted(out)


def describe(i, parts):
    why = []
    if parts.get("loud") is not None and parts["loud"][i] > 4:
        why.append(f"громче обычного на {parts['loud'][i]:.0f} дБ")
    if parts.get("chat") is not None and parts["chat"][max(0, i - 3):i + 12].max() > 2:
        why.append(f"чат в {parts['chat'][max(0, i - 3):i + 12].max():.1f} раза активнее")
    if parts.get("clips") is not None and parts["clips"][max(0, i - 25):i + 25].max() > 0.5:
        why.append("зрители нарезали клип")
    if parts.get("heat") is not None and parts["heat"][i] > 0.6:
        why.append("часто пересматривают")
    return ", ".join(why) or "заметный момент"


def cut_wav(wav, a, b, out):
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        w.setpos(int(a * sr))
        data = w.readframes(int((b - a) * sr))
        with wave.open(str(out), "wb") as o:
            o.setnchannels(1)
            o.setsampwidth(2)
            o.setframerate(sr)
            o.writeframes(data)


PROMPT = """Ты монтажёр коротких вертикальных роликов (TikTok, Shorts) из стримов.
Ниже {k} кандидатов из стрима {channel} «{title}». У каждого: окно времени в секундах от начала стрима,
чем момент выделился по сигналам и расшифровка речи с временем каждой фразы.

Выбери {n} лучших фрагментов для коротких роликов. Хороший фрагмент: понятен без контекста, есть завязка и
развязка (панчлайн, реакция, эмоция, неожиданность), его хочется досмотреть. Плохой: пустой разговор,
непонятная игровая ситуация без реакции, оборванная мысль.

Для каждого выбранного укажи точные границы внутри окна кандидата: начало на начале фразы, которая
вводит в ситуацию, конец сразу после реакции или панчлайна. Длина от 15 до 60 секунд.

Ответ только JSON-массивом, лучшие первыми:
[{{"id": номер кандидата, "start": секунда, "end": секунда, "title": "заголовок на {lang}, 2-6 слов, капсом",
"reason": "почему зайдёт, по-русски, до 60 знаков", "score": от 1 до 100, "kind": "game" или "talk"}}]

{cands}"""


def ask_claude(cands, info, n, cfg):
    if not m.KEY_FILE.exists():
        return None
    key = m.KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        return None
    import requests
    blocks = []
    for c in cands:
        lines = []
        cur, t0 = [], None
        for w in c["words"]:
            if t0 is None:
                t0 = w["s"]
            cur.append(w["t"])
            if w["t"][-1:] in ".!?…" or len(cur) >= 14:
                lines.append(f"  [{t0:.1f}] {' '.join(cur)}")
                cur, t0 = [], None
        if cur:
            lines.append(f"  [{t0:.1f}] {' '.join(cur)}")
        blocks.append(f"Кандидат {c['id']}: окно {c['a']:.0f}–{c['b']:.0f} с, сигналы: {c['why']}\n"
                      + ("\n".join(lines) if lines else "  (речи почти нет)"))
    text = PROMPT.format(k=len(cands), n=n, channel=info.get("channel") or "стримера", title=info.get("title", "")[:80],
                         lang="английском" if m.lang() == "en" else "русском", cands="\n\n".join(blocks))[:60000]
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": cfg.get("claude_pick_model", cfg.get("claude_model", "claude-haiku-4-5")),
                                "max_tokens": 2500, "messages": [{"role": "user", "content": text}]}, timeout=180)
        if r.status_code != 200:
            print(f"  Claude ответил {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        out = re.sub(r"```(?:json)?|```", "", out)
        return json.loads(out[out.index("["):out.rindex("]") + 1])
    except Exception as e:
        print(f"  выбор через Claude не получился: {e}")
        return None


def snap(words, a, b):
    """Без Claude: границы по паузам между фразами рядом с нужными секундами."""
    starts = [w["s"] for w in words if a - 6 <= w["s"] <= a + 6]
    gaps = [w["s"] for i, w in enumerate(words) if i and w["s"] - words[i - 1]["e"] > 0.5 and a - 8 <= w["s"] <= a + 6]
    a2 = (gaps or starts or [a])[0] - 0.3
    ends = [w["e"] for i, w in enumerate(words[:-1]) if words[i + 1]["s"] - w["e"] > 0.5 and b - 6 <= w["e"] <= b + 8]
    b2 = (ends or [b])[-1] + 0.4
    return max(0.0, a2), b2


def cut_video(info, a, b, out, say):
    """Фрагмент в хорошем качестве: со ссылки скачивается только этот кусок, из файла вырезается точно."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if info.get("path"):
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.2f}", "-i", info["path"], "-t", f"{b - a:.2f}",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", str(out)], capture_output=True, text=True, creationflags=NOWIN)
        return r.returncode == 0 and out.exists()
    tmpl = str(out.with_suffix("")) + ".%(ext)s"
    r = _ytdlp(["-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--download-sections", f"*{a:.2f}-{b:.2f}",
                "--force-keyframes-at-cuts", "--merge-output-format", "mp4", "-o", tmpl, info["url"]], timeout=1800)
    got = [f for f in out.parent.glob(out.stem + ".*") if f.suffix.lower() in (".mp4", ".mkv", ".webm")]
    if got and got[0] != out:
        got[0].replace(out)
    if r.returncode != 0 and not out.exists():
        say("  не скачался: " + ((r.stderr or "").strip().splitlines() or [""])[-1][:160])
    return out.exists()


def run(src, n=10, cfg=None, say=print, should_stop=lambda: False):
    """Весь путь от ссылки или файла до фрагментов в очереди. Возвращает список паспортов новых фрагментов."""
    cfg = cfg or json.loads(m.CFG_FILE.read_text(encoding="utf-8"))
    say("открываю видео…")
    info = source_info(src)
    if not info["dur"]:
        raise Stop("не получилось узнать длину видео")
    say(f"«{info['title'][:60]}», {int(info['dur'] // 3600)} ч {int(info['dur'] % 3600 // 60)} мин")
    wav = get_audio(info, say)
    if should_stop():
        raise Stop("остановлено")
    say("слушаю громкость…")
    loud = loudness_curve(wav)
    n_sec = len(loud)
    chat, clips = [], []
    if info["kind"] == "twitch":
        say("читаю чат записи…")
        chat = twitch_chat(info["vod"], info["dur"], say)
        say(f"сообщений в чате: {len(chat)}" if chat else "чат записи Twitch не отдал, ищу без него")
        clips = twitch_clips(info)
        if clips:
            say(f"клипов зрителей с этого стрима: {len(clips)}")
    elif info["kind"] == "youtube":
        chat = youtube_chat(info, say)
        if chat:
            say(f"сообщений в чате: {len(chat)}")
    score, parts = score_curve(loud, chat, clips, info.get("heatmap"), n_sec)
    peaks = pick_peaks(score, max(20, int(n * 2.5)))
    say(f"нашёл мест-кандидатов: {len(peaks)}, распознаю речь вокруг них…")
    tmp = VODS / f"{info['id']}_cand.wav"
    cands = []
    for k, pk in enumerate(peaks):
        if should_stop():
            raise Stop("остановлено")
        a, b = max(0, pk - 45), min(n_sec, pk + 25)
        cut_wav(wav, a, b, tmp)
        cache = tmp.with_suffix(".words.json")
        cache.unlink(missing_ok=True)
        words = [dict(w, s=w["s"] + a, e=w["e"] + a) for w in m.transcribe(tmp, cfg)]
        cache.unlink(missing_ok=True)
        cands.append({"id": k + 1, "peak": pk, "a": a, "b": b, "words": words, "why": describe(pk, parts),
                      "power": float(score[pk])})
        say(f"распознаю речь: {k + 1} из {len(peaks)}")
    tmp.unlink(missing_ok=True)

    say("выбираю лучшие фрагменты…")
    chosen = []
    picked = ask_claude(cands, info, n, cfg)
    if picked:
        byid = {c["id"]: c for c in cands}
        for p in picked:
            c = byid.get(int(p.get("id", 0)))
            if not c:
                continue
            a, b = float(p.get("start", c["a"])), float(p.get("end", c["b"]))
            a, b = max(c["a"] - 10, a), min(c["b"] + 10, b)
            if b - a < 10:
                continue
            chosen.append({"c": c, "a": a, "b": min(b, a + 75), "title": str(p.get("title", "")).strip(),
                           "reason": str(p.get("reason", "")).strip() or c["why"], "score": int(p.get("score") or 50),
                           "kind": "cs" if p.get("kind") == "game" else "talk"})
    if not chosen:                               # без Claude: самые сильные места по сигналам
        top = sorted(cands, key=lambda c: -c["power"])[:n]
        mx = max([c["power"] for c in top] + [1e-6])
        for c in top:
            a, b = snap(c["words"], c["peak"] - 25, c["peak"] + 12)
            chosen.append({"c": c, "a": a, "b": max(b, a + 15), "title": "", "reason": c["why"],
                           "score": int(40 + 55 * max(0.0, c["power"]) / mx), "kind": "talk"})
    chosen = chosen[:n]

    game_cat = " ".join(cfg.get("cs_categories") or []) + " " + str(cfg.get("game_label") or "")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out_dir = BASE / "clips" / "raw" / (info.get("channel") or "stream")
    made = []
    for i, ch in enumerate(chosen):
        if should_stop():
            raise Stop("остановлено")
        a, b = ch["a"], ch["b"]
        say(f"вырезаю фрагмент {i + 1} из {len(chosen)} ({int(a // 60)}:{int(a % 60):02d})")
        cid = f"vod_{info['id']}_{int(a)}"
        target = out_dir / f"{cid}.mp4"
        if not cut_video(info, a, b, target, say):
            continue
        real = m.probe(target)["dur"]
        words = [dict(w, s=round(w["s"] - a, 3), e=round(w["e"] - a, 3)) for w in ch["c"]["words"]
                 if a - 0.2 <= w["s"] and w["e"] <= a + real + 0.2]
        target.with_suffix(".words.json").write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
        kind = ch["kind"]
        if kind == "talk" and info.get("game") and any(g and g.lower() in info["game"].lower() for g in game_cat.split()):
            kind = "cs"
        p = {"clip_id": cid, "streamer": info.get("channel") or "", "title": ch["title"] or info["title"][:60],
             "format": kind, "twitch_views": 0, "duration": round(real, 1), "found_at": now,
             "file": str(target.relative_to(BASE)), "source": "vod", "score": ch["score"], "reason": ch["reason"],
             "vod": info.get("url") or info.get("path"), "vod_at": round(a, 1)}
        with PASSPORTS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
        made.append(p)
    if not made:
        raise Stop("ни один фрагмент не вырезался")
    return made


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    res = run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 10)
    for p in res:
        print(f"{p['score']:3d}  {p['vod_at']:7.0f} с  {p['title']}  ({p['reason']})")
