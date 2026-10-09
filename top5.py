# -*- coding: utf-8 -*-
"""
Формат «Топ-5»: берёт видео, в котором несколько моментов идут подряд, режет его на фрагменты,
переставляет их в случайном порядке и собирает в нашем стиле: заголовок, список мест, который
открывается по ходу ролика, окно с видео, субтитры, баннер на стоп-кадре в середине.

Запуск:  python top5.py видео.mp4 --streamer sasavot
         python top5.py видео.mp4 --n 5 --title "Самые смешные моменты" --seed 3
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

import montage as m

BASE = Path(__file__).resolve().parent
CACHE = BASE / "cache"
W, H = 1080, 1920
AREA = (80, 620, 920, 900)                # область под фрагмент: x, y, ширина, высота (рамка встаёт по самому фрагменту)
WIN = AREA


def find_cuts(path, n):
    """Границы между фрагментами: резкая смена кадра. Если сверху есть список мест, который
    пополняется, граница подтверждается появлением новой строки."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    hh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    prev, d, white = None, [], []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        g = cv2.resize(cv2.cvtColor(f[int(hh * 0.38):int(hh * 0.80)], cv2.COLOR_BGR2GRAY), (96, 72)).astype(np.float32)
        d.append(0.0 if prev is None else float(np.abs(g - prev).mean()))
        prev = g
        white.append(float((f[:int(hh * 0.41)].min(2) > 225).mean()))
    cap.release()
    d, white = np.array(d), np.array(white)
    dur = len(d) / fps
    cand = []
    for i in range(6, len(d) - 6):
        nb = np.concatenate([d[i - 6:i - 1], d[i + 2:i + 7]])
        if d[i] > 18 and d[i] > 4 * (np.median(nb) + 0.5):
            a0, a1 = max(0, i - int(fps)), max(1, i - 3)
            b0, b1 = min(len(d) - 1, i + 6), min(len(d), i + 6 + int(fps * 0.6))
            step = float(white[b0:b1].mean() - white[a0:a1].mean()) if b1 > b0 else 0.0
            if cand and i / fps - cand[-1][0] < 3.0:
                if d[i] > cand[-1][1]:
                    cand[-1] = (i / fps, float(d[i]), step)
            else:
                cand.append((i / fps, float(d[i]), step))
    confirmed = [c for c in cand if c[2] > 0.003]
    pool = confirmed if len(confirmed) >= 2 else cand
    pool = sorted(sorted(pool, key=lambda c: -c[1])[:max(0, n - 1)])
    times = [0.0] + [c[0] for c in pool] + [dur]
    return [(times[i], times[i + 1]) for i in range(len(times) - 1) if times[i + 1] - times[i] > 1.0]


def window_box(path, t0, t1):
    """Границы самого фрагмента внутри готовой нарезки: без шапки с текстом и без размытых полей.
    Фрагмент берётся целиком, даже если он составной (например, камера и под ней видео из ТикТока):
    ищем боковые границы, которые идут по всей его высоте."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(path))
    sw, sh = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fr = []
    for frac in np.linspace(0.1, 0.9, 9):
        cap.set(cv2.CAP_PROP_POS_MSEC, (t0 + (t1 - t0) * frac) * 1000)
        ok, f = cap.read()
        if ok:
            fr.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32))
    cap.release()
    if len(fr) < 3:
        return [0, 0, sw, sh]
    st = np.stack(fr)
    ex = np.zeros((sh, sw), np.float32)
    ey = np.zeros((sh, sw), np.float32)
    ex[:, 1:-1] = np.median(np.abs(st[:, :, 2:] - st[:, :, :-2]), 0)      # устойчивые вертикальные границы
    ey[1:-1, :] = np.median(np.abs(st[:, 2:, :] - st[:, :-2, :]), 0)      # устойчивые горизонтальные

    def peak(p, a, b):
        seg = p[a:b]
        i = int(np.argmax(seg))
        return a + i, float(seg[i]), float(np.median(seg))

    # горизонтальные края: верх окна под шапкой и низ окна
    rowp = np.percentile(ey, 60, axis=1)
    ty, tv, tm = peak(rowp, int(sh * 0.25), int(sh * 0.55))
    by, bv, bm = peak(rowp, int(sh * 0.60), int(sh * 0.92))
    top_ok, bot_ok = tv > 4 * tm + 4, bv > 4 * bm + 4
    hy0, hy1 = (ty + 1 if top_ok else 0), (by + 1 if bot_ok else sh)

    # боковые края: пара симметричных столбцов, где граница идёт по большой части высоты
    thr = 10.0
    ya = int(sh * 0.33)
    cov = (ex[ya:int(sh * 0.97)] > thr).mean(0)
    best = None
    for x0 in range(2, int(sw * 0.45)):
        if cov[x0] < 0.2:
            continue
        xm = sw - x0
        lo, hi = max(int(sw * 0.55), xm - int(0.06 * sw)), min(sw - 2, xm + int(0.06 * sw))
        if hi <= lo:
            continue
        x1 = lo + int(np.argmax(cov[lo:hi]))
        if cov[x1] >= 0.2 and (best is None or cov[x0] + cov[x1] > best[0]):
            best = (cov[x0] + cov[x1], x0, x1)
    if best is not None:
        _, x0, x1 = best
        on = (ex[:, max(0, x0 - 1):x0 + 2].max(1) > thr) | (ex[:, max(0, x1 - 1):x1 + 2].max(1) > thr)
        on[:ya] = False
        runs, cur, gap = [], None, 0
        for y in range(sh):
            if on[y]:
                cur = [y, y] if cur is None else [cur[0], y]
                gap = 0
            elif cur is not None:
                gap += 1
                if gap > 14:
                    runs.append(cur)
                    cur = None
        if cur is not None:
            runs.append(cur)
        if runs:
            y0, y1 = max(runs, key=lambda r: r[1] - r[0])
            y1 += 1
            if bot_ok and hy1 > y1:               # нижний край окна найден ниже: боковые границы там просто слабые
                y1 = hy1
            if y1 - y0 > 0.12 * sh:
                return [x0 + 1, y0, x1 - x0 - 1, y1 - y0]
    return [0, hy0, sw, hy1 - hy0]               # окно во всю ширину кадра


def ai_labels(texts, streamer, cfg):
    """Общий заголовок и короткие подписи к местам через Claude. None, если ключа нет."""
    if not m.KEY_FILE.exists() or not any(texts):
        return None
    try:
        import requests
        body = "\n".join(f"{i + 1}) {t[:600] or '(без слов)'}" for i, t in enumerate(texts))
        prompt = (f"Это расшифровки {len(texts)} моментов со стримов ({streamer or 'стример'}) для ролика-топа.\n{body}\n\n"
                  "Верни ТОЛЬКО JSON: {\"title\": \"...\", \"labels\": [\"...\"]}\n"
                  "title: заголовок топа, 3-5 слов, например «Самые смешные моменты» и ник. Без мата и эмодзи.\n"
                  f"labels: ровно {len(texts)} подписей в том же порядке, каждая 1-3 слова по сути момента, без мата.")
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": m.KEY_FILE.read_text(encoding="utf-8-sig").strip(),
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": cfg.get("claude_model", "claude-haiku-4-5"), "max_tokens": 400,
                                "messages": [{"role": "user", "content": prompt}]}, timeout=60)
        if r.status_code != 200:
            print(f"  Claude API ответил {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        labels = [re.sub(r'[\\"«».!]+', "", str(x)).strip()[:28] for x in j.get("labels", [])]
        if len(labels) != len(texts):
            return None
        return {"title": re.sub(r'[\\"«».!]+', "", str(j.get("title", ""))).strip(), "labels": labels}
    except Exception as e:
        print(f"  подписи через Claude не получились: {e}")
        return None


def read_source_list(src, dur, n, cfg):
    """Читает из исходного видео заголовок и подписи к местам (по последнему кадру, где открыт
    весь список), чтобы подписи переезжали вместе со своими фрагментами. None, если не вышло."""
    if not m.KEY_FILE.exists():
        return None
    try:
        import base64
        import requests
        jpg = m.grab_jpeg(src, max(0.0, dur - 0.5))
        if len(jpg) < 1000:
            return None
        prompt = (f"На кадре ролик-топ: сверху заголовок и нумерованный список из {n} мест с подписями.\n"
                  "Перепиши текст точно, как на кадре. Верни ТОЛЬКО JSON без пояснений:\n"
                  "{\"title\": \"заголовок одной строкой\", \"items\": {\"1\": \"подпись\", \"2\": \"подпись\"}}\n"
                  "В items должны быть все номера, которые видны. Пометки в скобках вроде «самое смешное в конце» "
                  "в заголовок не включай. Если списка на кадре нет, верни {\"title\": \"\", \"items\": {}}.")
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": m.KEY_FILE.read_text(encoding="utf-8-sig").strip(),
                                   "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": cfg.get("claude_model", "claude-haiku-4-5"), "max_tokens": 400,
                                "messages": [{"role": "user", "content": [
                                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                 "data": base64.b64encode(jpg).decode()}},
                                    {"type": "text", "text": prompt}]}]}, timeout=90)
        if r.status_code != 200:
            print(f"  Claude API ответил {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        items = {int(k): str(v).strip()[:32] for k, v in (j.get("items") or {}).items() if str(k).isdigit() and str(v).strip()}
        if len(items) < 2:
            return None
        return {"title": str(j.get("title", "")).strip(), "items": items}
    except Exception as e:
        print(f"  не удалось прочитать список из исходника: {e}")
        return None


def make(src, streamer="", title="", n=5, seed=None, send=True, cfg=None, enc=None):
    src = Path(src).resolve()
    cfg = cfg or json.loads((BASE / "montage.json").read_text(encoding="utf-8"))
    enc = enc or m.pick_encoder(cfg)
    CACHE.mkdir(exist_ok=True)
    info = m.probe(src)
    fps = 60 if info["fps"] > 45 else 30
    segs = find_cuts(src, n)
    if len(segs) < 2:
        print("  не нашёл границ между фрагментами: в видео должен быть ряд моментов подряд")
        return None
    print(f"{src.name}: фрагментов {len(segs)}: " + ", ".join(f"{a:.0f}-{b:.0f} с" for a, b in segs))

    # 1) каждый фрагмент вырезаем из исходника: только окно с видео, без чужой шапки
    parts = []
    boxes = [window_box(src, a, b) for a, b in segs]
    full = [bx_ for bx_ in boxes if bx_[2] >= info["w"] - 4]     # у окон во всю ширину верх общий: под шапкой
    if full:
        top = sorted(bx_[1] for bx_ in boxes)[len(boxes) // 2]
        for bx_ in full:
            if bx_[1] < top:
                bx_[3] -= top - bx_[1]
                bx_[1] = top
    accent = m.pick_accent(src, cfg)

    # подписи из исходника привязываем к фрагментам: фрагмент, шедший i-м, занимал место N-i
    srclist = read_source_list(src, info["dur"], len(segs), cfg)
    have_labels = bool(srclist) and all(srclist["items"].get(len(segs) - i) for i in range(len(segs)))
    for i, (a, b) in enumerate(segs):
        crop = [v // 2 * 2 for v in boxes[i]]                # фрагмент берём целиком, ничего не обрезая
        # рамка по самому фрагменту: вписываем его в отведённую область, сохраняя пропорции
        ar = crop[2] / crop[3]
        w = min(AREA[2], AREA[3] * ar)
        h = w / ar
        w, h = int(w) // 2 * 2, int(h) // 2 * 2
        x, y = AREA[0] + (AREA[2] - w) // 2, AREA[1] + (AREA[3] - h) // 2
        key = hashlib.md5(repr((x, y, w, h, accent)).encode()).hexdigest()[:10]
        frame, mask = CACHE / f"frame_top_{key}.png", CACHE / f"mask_{w}x{h}_40.png"
        if not frame.exists():
            m.frame_png([(x, y, w, h, 40)], accent + (255,), frame)
        if not mask.exists():
            m.rounded_mask(w, h, 40, mask)
        f = CACHE / f"top_{os.getpid()}_seg{i}.mp4"
        for stale in (f.with_suffix(".words.json"), f.with_suffix(".layout.json")):
            stale.unlink(missing_ok=True)
        dur_i = b - a - 0.08
        vf = (f"[0:v]crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},split=2[fa][fb];"
              f"[fa]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=16,"
              f"scale={W}:{H}:flags=bilinear,eq=brightness=-0.24:saturation=1.25[bg];"
              f"[bg][1:v]overlay=0:0[l0];"
              f"[fb]scale={w}:{h}:flags=lanczos,format=rgba[f0];[f0][2:v]alphamerge[f1];"
              f"[l0][f1]overlay={x}:{y},setsar=1,fps={fps},format=yuv420p[v]")
        r = m.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a + 0.04:.3f}", "-t", f"{dur_i:.3f}", "-i", str(src),
                   "-loop", "1", "-framerate", str(fps), "-i", str(frame),
                   "-loop", "1", "-framerate", str(fps), "-i", str(mask),
                   "-filter_complex", vf, "-map", "[v]", "-map", "0:a", "-t", f"{dur_i:.3f}",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "15",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(f)])
        if r.returncode != 0:
            print(r.stderr[-600:])
            return None
        dur = m.probe(f)["dur"]
        # речь нужна только для подписей от Claude и субтитров; если подписи есть в исходнике, не тратим время
        need_words = cfg.get("top_subs", False) or not have_labels
        parts.append({"file": f, "dur": dur, "words": m.merge_hyphens(m.transcribe(f, cfg)) if need_words else [],
                      "label": srclist["items"].get(len(segs) - i) if srclist else None})

    # 2) новый порядок: места меняются случайно
    order = list(range(len(parts)))
    rnd = random.Random(seed)
    for _ in range(20):
        rnd.shuffle(order)
        if order != list(range(len(parts))):
            break
    parts = [parts[i] for i in order]
    N = len(parts)
    print("  новый порядок фрагментов: " + " ".join(str(i + 1) for i in order))

    # 3) заголовок и подписи к местам
    if all(p.get("label") for p in parts):           # подписи переехали вместе со своими фрагментами
        ai = None
        labels = [p["label"] for p in parts]
        print("  подписи взяты из исходника: " + " | ".join(labels))
    else:
        ai = ai_labels([" ".join(w["t"] for w in p["words"]) for p in parts], streamer, cfg)
        labels = ai["labels"] if ai else [f"Момент {N - i}" for i in range(N)]
    src_title = srclist["title"] if srclist and srclist.get("title") else ""
    head = (title or src_title or (ai["title"] if ai and ai["title"] else "Топ моментов " + (streamer or "стрима"))).upper()

    # 4) общая шкала времени и место баннера
    starts, t = [], 0.0
    for p in parts:
        starts.append(t)
        t += p["dur"]
    content = t
    words = [{"s": w["s"] + st, "e": w["e"] + st, "t": w["t"]} for p, st in zip(parts, starts) for w in p["words"]]
    banner = BASE / cfg["banner_talk"]
    if not banner.exists():
        sys.exit(f"Нет файла баннера: {banner}")
    ban_dur = m.probe(banner)["dur"]
    splits = m.banner_splits(words, [(0.0, content)], content, ban_dur, cfg)     # один баннер или два
    ban_at = [sp + k * ban_dur for k, sp in enumerate(splits)]                   # начало баннеров в готовом ролике
    total = content + ban_dur * len(splits)
    bx, by, bw, bh = cfg["banner_talk_crop"]
    ban_h = int(round(bh * W / bw / 2)) * 2

    def shift(x):
        return x + ban_dur * sum(1 for sp in splits if x >= sp - 1e-6)

    # 5) текст: шапка, список мест, субтитры
    acc = "&H00{:02X}{:02X}{:02X}&".format(accent[2], accent[1], accent[0])
    font = cfg["font"]
    hl = head.split()
    cut = max(1, len(hl) // 2)
    for i in range(1, len(hl)):
        if abs(len(" ".join(hl[:i])) - len(head) / 2) < abs(len(" ".join(hl[:cut])) - len(head) / 2):
            cut = i
    l1, l2 = " ".join(hl[:cut]), " ".join(hl[cut:])
    ass = [f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Head,{font},60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,6,0,8,80,80,95,1
Style: Note,{font},32,{acc[:-1]},{acc[:-1]},&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,0,8,80,80,236,1
Style: List,{font},48,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,5,0,7,0,0,0,1
Style: Sub,{font},{cfg['sub_size']},&H0000E6FF,&H0000E6FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,8,0,8,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"""]
    T = m.ass_time
    ass.append(f"Dialogue: 0,{T(0)},{T(total)},Head,,0,0,0,,{l1}" + (r"\N{\c" + acc + "}" + l2 if l2 else ""))
    ass.append(f"Dialogue: 0,{T(0)},{T(total)},Note,,0,0,0,,(самое смешное в конце)")
    step = min(60, 300 // N)
    for i in range(N):                                   # i-й по счёту фрагмент получает место N-i
        rank = N - i
        y = 292 + (rank - 1) * step
        a = shift(starts[i])
        b = shift(starts[i + 1]) if i < N - 1 else total
        lab = m.censor(labels[i]) if " " not in labels[i] else " ".join(m.censor(x) or x for x in labels[i].split())
        pos = rf"{{\pos(130,{y})}}"
        if a > 0.05:
            ass.append(f"Dialogue: 0,{T(0)},{T(a)},List,,0,0,0,,{pos}{rank}.")
        ass.append(f"Dialogue: 0,{T(a)},{T(b)},List,,0,0,0,,{pos}{{\\c{acc}}}{rank}. {lab}")
        if b < total - 0.05:
            ass.append(f"Dialogue: 0,{T(b)},{T(total)},List,,0,0,0,,{pos}{rank}. {lab}")
    sub_y = WIN[1] + WIN[3] - 150
    for i, w in enumerate(words if cfg.get("top_subs", False) else []):       # в топе субтитров нет
        txt = m.censor(w["t"]).upper()
        if not txt:
            continue
        s, e = shift(w["s"]), shift(w["e"])
        e = min(e, s + 0.40 + 0.06 * len(txt))
        if i + 1 < len(words):
            nxt = shift(words[i + 1]["s"])
            if 0 <= nxt - e < 0.20 or nxt < e:
                e = nxt
        for b0 in ban_at:
            if s < b0 <= e:
                e = b0
        e = max(e, s + 0.08)
        size = cfg["sub_size"] if len(txt) <= 11 else int(cfg["sub_size"] * 11 / len(txt))
        ass.append(f"Dialogue: 1,{T(s)},{T(e)},Sub,,0,0,0,,{{\\pos({W // 2},{sub_y})\\fs{size}\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{txt}")
    ass_file = CACHE / f"top_{os.getpid()}.ass"
    ass_file.write_text("\n".join(ass) + "\n", encoding="utf-8-sig")

    # 6) сборка
    groups = [[] for _ in range(len(splits) + 1)]          # куски между баннерами
    for p, st in zip(parts, starts):
        cur, en = st, st + p["dur"]
        for gi, sp in enumerate(splits):
            if cur < sp < en:
                groups[gi].append((p["file"], cur - st, sp - st))
                cur = sp
        gi = sum(1 for sp in splits if cur >= sp - 1e-6)
        if en - cur > 0.02:
            groups[gi].append((p["file"], cur - st, en - st))
    if any(not g for g in groups):
        print("  видео слишком короткое для баннера")
        return None
    inputs, fc = [], []

    def chain(pieces, tag):
        vs, as_ = "", ""
        for i, (f, a, b) in enumerate(pieces):
            k = len(inputs) // 6
            inputs.extend(["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(f)])
            fc.append(f"[{k}:v]setpts=PTS-STARTPTS,fps={fps},setsar=1[{tag}v{i}]")
            fc.append(f"[{k}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
                      f"afade=t=in:d=0.012,afade=t=out:st={max(0, b - a - 0.012):.4f}:d=0.012[{tag}a{i}]")
            vs += f"[{tag}v{i}]"
            as_ += f"[{tag}a{i}]"
        fc.append(f"{vs}concat=n={len(pieces)}:v=1:a=0[{tag}v]")
        fc.append(f"{as_}concat=n={len(pieces)}:v=0:a=1[{tag}a]")

    for gi, g in enumerate(groups):
        chain(g, f"g{gi}")
    nb = len(inputs) // 6
    nban = len(splits)
    t1 = ban_at[0]
    gain = max(-12.0, min(15.0, m.loudness(src) - m.loudness(banner) + cfg["banner_gain_db"]))
    vchain = ""
    for gi in range(len(groups)):
        if gi < nban:
            fc.append(f"[g{gi}v]tpad=stop_mode=clone:stop_duration={ban_dur:.4f}[g{gi}f]")
            vchain += f"[g{gi}f]"
        else:
            vchain += f"[g{gi}v]"
    fc.append(f"{vchain}concat=n={len(groups)}:v=1:a=0[cv]")
    fc.append(f"[{nb}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain:.1f}dB,"
              f"alimiter=limit=0.89,apad=whole_dur={ban_dur:.4f},atrim=0:{ban_dur:.4f}"
              + (f",asplit={nban}" + "".join(f"[ba{k}]" for k in range(nban)) if nban > 1 else "[ba0]"))
    achain = "".join(f"[g{gi}a]" + (f"[ba{gi}]" if gi < nban else "") for gi in range(len(groups)))
    fc.append(f"{achain}concat=n={len(groups) + nban}:v=0:a=1[aout]")
    ban_y = WIN[1] + (WIN[3] - ban_h) // 2
    fc.append(f"[{nb}:v]crop={bw}:{bh}:{bx}:{by},scale={W}:{ban_h},format=rgba,fps={fps}"
              + (f",split={nban}" + "".join(f"[bs{k}]" for k in range(nban)) if nban > 1 else "[bs0]"))
    last = "cv"
    for k, b0 in enumerate(ban_at):
        # до своего момента баннер идёт прозрачными кадрами: так основное видео не зависит от того,
        # как конкретная версия ffmpeg ведёт себя, пока второй поток ещё не начался
        fc.append(f"[bs{k}]setpts=PTS-STARTPTS,tpad=start_duration={b0:.4f}:start_mode=add:color=black@0.0[bv{k}]")
        fc.append(f"[{last}][bv{k}]overlay=0:{ban_y}:enable='between(t,{b0:.4f},{b0 + ban_dur:.4f})':eof_action=pass[wb{k}]")
        last = f"wb{k}"
    fc.append(f"[{last}]null[wb]")
    fc.append(f"[wb]ass={ass_file.name},format=yuv420p[vout]")

    out = BASE / "clips" / "ready" / "top" / (re.sub(r'[\\/:*?"<>|]+', " ", src.stem)[:50] + "_top.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / f"top_{os.getpid()}_tmp.mp4"

    def render(e):
        cmd = ["ffmpeg", "-v", "error", "-y"] + inputs + ["-i", str(banner),
               "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
               "-t", f"{total:.3f}", "-r", str(fps)] + m.encoder_args(e) + [
               "-c:a", "aac", "-b:a", "192k", "-flags:a", "+bitexact", str(tmp)]
        return m.run(cmd, cwd=str(CACHE))

    r = render(enc)
    if r.returncode != 0 and enc == "h264_amf":
        r = render("libx264")
    if r.returncode != 0:
        print(r.stderr[-1500:])
        return None
    r = m.run(["ffmpeg", "-v", "error", "-y", "-i", str(tmp), "-map", "0:v", "-map", "0:a", "-c", "copy",
               "-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact",
               "-bsf:v", "filter_units=remove_types=6", "-movflags", "+faststart", str(out)])
    tmp.unlink(missing_ok=True)
    ass_file.unlink(missing_ok=True)
    for p_ in parts:
        Path(p_["file"]).unlink(missing_ok=True)
    if r.returncode != 0:
        print(r.stderr[-600:])
        return None

    final_title = head.capitalize()
    hashtags = (f"#{streamer.lower()} " if streamer else "") + "#топ #стример #twitch #нарезки"
    p = {"streamer": streamer or "стример", "format": "top", "duration": round(total, 2), "final_title": final_title,
         "description": "", "potential": 50, "order": [i + 1 for i in order], "labels": labels, "source": str(src)}
    out.with_suffix(".txt").write_text(f"{final_title}\n\n{hashtags}\n", encoding="utf-8")
    p["tg_sent"] = m.tg_send(out, p, hashtags, "manual") if send else False
    out.with_suffix(".json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  заголовок: {final_title}\n  готово: {total:.1f} с, баннер на "
          + " и ".join(f"{x:.1f}" for x in ban_at) + " с" + (", отправлен в Telegram" if p["tg_sent"] else ""))
    print(f"  файл: {out}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--streamer", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--n", type=int, default=5, help="сколько моментов в видео")
    ap.add_argument("--seed", type=int, default=None, help="число для повторяемого порядка")
    ap.add_argument("--no-send", action="store_true")
    a = ap.parse_args()
    make(a.video, a.streamer, a.title, a.n, a.seed, not a.no_send)
