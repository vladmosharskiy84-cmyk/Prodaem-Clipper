# -*- coding: utf-8 -*-
"""
Реакция «своя камера»: запись, где Влад (или друг, который согласен) смотрит видео, + само видео
(ссылка или файл) → 5 частей до минуты: камера сверху 30%, видео снизу 70%.

Звук берём только из видео: камера нужна ради лица. Камеру с видео сводим сами по звуку
(микрофон телефона слышит видео из колонок); если не вышло — с начала, либо offset вручную:
с какой секунды записи камеры началось видео. Лучшие моменты ищутся по самому видео
(громкость и речь, Claude выбирает фрагменты с завязкой и развязкой).
Субтитры по одному слову (шрифт и место — из окна), баннер оффера по центру в середине части.

Запуск:  python reaction.py камера.mp4 ссылка_или_видео.mp4 [частей]
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import highlights as H
import montage as m

BASE = m.BASE
W, H_ = 1080, 1920
# стиль Dota «Карточки»: размытый фон из видео, скруглённые карточки с мягкой тенью; камера сверху, видео крупнее, чем в Dota
CAM_BOX = (48, 100, 984, 444, 40)               # x, y, ширина, высота, радиус
VID_BOX = (48, 574, 984, 1272, 40)              # до 1846 — как в Dota
CAM_H = CAM_BOX[1] + CAM_BOX[3]
VID_H = VID_BOX[3]
# условия баннерки для реакций: ролик до 2 минут, один баннер в середине (пауза ~4 с входит в эти 2 минуты).
# Из видео берём лучший кусок подряд (n частей × PART_LEN, при 5 частях ~10 мин) и режем встык: часть 2 начинается
# там, где закончилась часть 1. Реклама блогера (SponsorBlock + проверка Claude по расшифровке) вырезается.
PART_LEN = 115
SNAP = 6.0                                      # границу частей двигаем в паузу между словами не дальше чем на 6 с
OUT = BASE / "clips" / "ready" / "reaction"
TMP = BASE / "cache" / "reaction"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUB_Y = {"top": VID_BOX[1] + 120, "center": VID_BOX[1] + VID_BOX[3] // 2, "bottom": H_ - 330}   # рамки не у краёв: TikTok их прячет


def cards_assets():
    """Подложка с тенью и маски скруглённых углов: те же функции, что у Dota «Карточки»."""
    m.CACHE.mkdir(parents=True, exist_ok=True)
    frame = m.CACHE / "rx_cards_frame.png"
    if not frame.exists():
        m.style_png("cards", [CAM_BOX, VID_BOX], (255, 255, 255), frame)
    masks = []
    for x, y, w, h, r in (CAM_BOX, VID_BOX):
        mp = m.CACHE / f"mask_{w}x{h}_{r}.png"
        if not mp.exists():
            m.rounded_mask(w, h, r, mp)
        masks.append(mp)
    return frame, masks[0], masks[1]


class Stop(Exception):
    pass


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NOWIN, **kw)


def has_audio(path):
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(r.stdout.strip())


def fetch_video(src, say):
    """Файл как есть; ссылку (YouTube, TikTok и т.п.) качаем тем же yt-dlp, что и стримы."""
    if Path(src).exists():
        return Path(src)
    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / ("v_" + re.sub(r"\W+", "_", src)[-60:] + ".mp4")
    if out.exists() and out.stat().st_size > 10000:
        return out
    say("скачиваю видео по ссылке…")
    r = _run([sys.executable, "-m", "yt_dlp", "--no-warnings", "-q", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
              "--merge-output-format", "mp4", "-o", str(out), src], timeout=1800)
    if not out.exists():
        raise Stop("видео по ссылке не скачалось: " + (r.stderr or "")[-200:])
    return out


def wav16(src, out, seconds=None):
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)] + (["-t", str(seconds)] if seconds else []) + \
          ["-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    _run(cmd, check=True)
    return out


def auto_offset(cam, video, say):
    """С какой секунды камеры идёт видео: сравниваем огибающие звука (микрофон камеры слышал видео).
    None — если у камеры нет звука или совпадение слабое."""
    if not has_audio(cam) or not has_audio(video):
        return None
    import numpy as np
    import wave

    def env(path):                               # громкость с шагом 0,05 с
        with wave.open(str(path), "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32)
        k = 800
        a = a[:len(a) // k * k].reshape(-1, k)
        e = np.log1p(np.sqrt((a ** 2).mean(axis=1)))
        return (e - e.mean()) / (e.std() + 1e-6)
    TMP.mkdir(parents=True, exist_ok=True)
    ec = env(wav16(cam, TMP / "sync_cam.wav", 600))
    ev = env(wav16(video, TMP / "sync_vid.wav", 300))
    if len(ec) < 200 or len(ev) < 200:
        return None
    probe = ev[:min(len(ev), 1200)]               # первая минута видео
    corr = np.correlate(ec, probe, "valid") / len(probe)
    if not len(corr):
        return None
    i = int(np.argmax(corr))
    if corr[i] < 0.3:
        return None
    say(f"синхронизировал камеру с видео по звуку: видео начинается на {i * 0.05:.1f} с записи камеры")
    return i * 0.05


def write_subs(path, words, font, pos, xy=None):
    """Субтитры по одному слову, чётко, с лёгким «прыжком» слова — как в остальных роликах клиппера."""
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H_}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},88,&H0000FFEE,&H0000FFEE,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,7,2,5,80,80,0,1


[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    t = lambda s: f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"
    x, y = W // 2, SUB_Y.get(pos, SUB_Y["bottom"])
    if xy:                                       # своё место, перетащенное в макете
        x, y = int(float(xy[0]) * W), int(float(xy[1]) * H_)
    ev = []
    for i, w in enumerate(words):
        end = min(w["e"] + 0.25, words[i + 1]["s"]) if i + 1 < len(words) else w["e"] + 0.4
        txt = re.sub(r"[{}\\]", "", w["t"]).strip().strip(".,!?…:;-–—\"'«»()").upper()   # «-контроля» → «КОНТРОЛЯ»
        if txt:
            ev.append(f"Dialogue: 1,{t(w['s'])},{t(max(end, w['s'] + 0.12))},Sub,,0,0,0,,"
                      f"{{\\pos({x},{y})\\fscx85\\fscy85\\t(0,90,\\fscx100\\fscy100)}}{txt}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


def sponsor_ads(src):
    """Рекламные вставки из SponsorBlock (зрители сами отмечают их в роликах YouTube). Пусто — не YouTube или нет данных."""
    mt = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", str(src))
    if not mt:
        return []
    url = "https://sponsor.ajay.app/api/skipSegments?" + urllib.parse.urlencode(
        {"videoID": mt.group(1), "categories": json.dumps(["sponsor", "selfpromo"])})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "prodaem-clipper"}), timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:                            # 404 — для ролика ничего не отмечено
        return []
    return [(float(x["segment"][0]), float(x["segment"][1])) for x in data if x.get("segment")]


def _merge(iv):
    out = []
    for a, b in sorted((float(a), float(b)) for a, b in iv if float(b) > float(a)):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


class _Clean:
    """Время видео без рекламы: «секунда видео ↔ секунда без рекламы» и куски видео для отрезка без рекламы."""
    def __init__(self, dur, ads):
        self.ads = _merge([(max(0.0, a), min(dur, b)) for a, b in ads])
        self.spans, t = [], 0.0
        for a, b in self.ads:
            if a > t:
                self.spans.append((t, a))
            t = max(t, b)
        if dur > t:
            self.spans.append((t, dur))
        self.total = sum(b - a for a, b in self.spans)

    def in_ad(self, t):
        return any(a <= t < b for a, b in self.ads)

    def to_clean(self, t):
        c = 0.0
        for a, b in self.spans:
            if t < a:
                return c
            if t <= b:
                return c + t - a
            c += b - a
        return c

    def pieces(self, ca, cb):
        out, c = [], 0.0
        for a, b in self.spans:
            lo, hi = max(ca, c), min(cb, c + (b - a))
            if hi > lo:
                out.append((a + lo - c, a + hi - c))
            c += b - a
        return out


ADS_PROMPT = """Ниже расшифровка куска видео блогера с отметками времени [секунды от начала видео]. Название: «{title}».
1) Найди рекламные вставки: блогер рекламирует сервис, приложение, магазин, букмекера или казино, курс, VPN, промокод,
«спонсор этого видео», «ссылка в описании», зовёт перейти, скачать или зарегистрироваться ради партнёра.
Обычный разговор, мнение о вещи по теме видео, просьба подписаться или поставить лайк — НЕ реклама.
Для каждой вставки — начало и конец в секундах, с запасом, чтобы вставка попала целиком.
2) Придумай общий заголовок серии роликов по этому куску: 2–6 слов, капсом, на русском, правдиво, без кликбейта-лжи.
Ответ только JSON: {{"ads": [[начало, конец], ...], "title": "..."}}"""


def find_ads(words, cfg, title_hint=""):
    """Claude читает расшифровку куска и отмечает рекламу блогера; заодно — заголовок серии. {} — если не вышло."""
    if not words or not m.KEY_FILE.exists():
        return {}
    key = m.KEY_FILE.read_text(encoding="utf-8-sig").strip()
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["s"]
        cur.append(w["t"])
        if w["t"][-1:] in ".!?…" or len(cur) >= 16:
            lines.append(f"[{t0:.0f}] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.0f}] {' '.join(cur)}")
    text = ADS_PROMPT.format(title=title_hint[:80]) + "\n\n" + "\n".join(lines)[:60000]
    try:
        import requests
        r = requests.post("https://api.anthropic.com/v1/messages",
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": cfg.get("cheap_model", "claude-haiku-5-5"), "max_tokens": 1500,
                                "messages": [{"role": "user", "content": text}]}, timeout=120)
        if r.status_code != 200:
            return {}
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        d = json.loads(out[out.index("{"):out.rindex("}") + 1])
    except Exception:
        return {}
    ads = []
    for x in d.get("ads") or []:
        try:
            a, b = float(x[0]), float(x[1])
        except (TypeError, ValueError, IndexError):
            continue
        if b - a >= 3:
            ads.append((a - 1.0, b + 1.0))
    return {"ads": ads, "title": str(d.get("title") or "").strip()[:60]}


def join_pieces(video, pieces, out):
    """Часть, внутри которой была реклама: склеиваем куски видео без неё (картинка и звук вместе)."""
    s0 = max(0.0, pieces[0][0] - 1.0)
    fc = [f"[0:v]trim={a - s0:.3f}:{b - s0:.3f},setpts=PTS-STARTPTS[v{i}];"
          f"[0:a]atrim={a - s0:.3f}:{b - s0:.3f},asetpts=PTS-STARTPTS[a{i}]" for i, (a, b) in enumerate(pieces)]
    fc.append("".join(f"[v{i}][a{i}]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
    r = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{s0:.3f}", "-i", str(video), "-filter_complex", ";".join(fc),
              "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "16",
              "-c:a", "aac", "-b:a", "192k", str(out)])
    if r.returncode != 0 or not out.exists():
        raise Stop("не получилось вырезать рекламу из части: " + (r.stderr or "")[-200:])


def pause_point(words, dur):
    """Где поставить ролик на паузу под баннер: ближе к середине, в промежутке между словами (не посреди фразы)."""
    mid = dur / 2
    gaps = [w["e"] + min(0.1, (n["s"] - w["e"]) / 2) for w, n in zip(words, words[1:]) if n["s"] - w["e"] >= 0.15]
    gaps = [g for g in gaps if 2.0 <= g <= dur - 2.0]
    return min(gaps, key=lambda g: abs(g - mid)) if gaps else max(2.0, min(mid, dur - 2.0))


def render(cam, video, a, b, offset, out, label, words, opts, cfg):
    """Одна часть: видео с секунды a, камера с секунды a + offset; звук — только видео (и баннера)."""
    dur = b - a
    font = m.font_face(opts.get("font") or "Rubik Bold")[0]   # стиль реакций: Rubik Bold, жёлтый, капсом, чёрная обводка
    ass = out.with_suffix(".ass")
    # для зрителя это обычный ролик: камера, видео и субтитры, без плашек «часть N» — номера только в именах файлов
    write_subs(ass, words if opts.get("subs", True) else [], font, opts.get("sub_pos", "bottom"),
               opts.get("sub_xy") if opts.get("sub_pos") == "custom" else None)
    # камера — это просто Влад, который смотрит: идёт с начала как есть, а если короче ролика — по кругу
    inputs = ["-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", str(video),
              "-stream_loop", "-1", "-ss", f"{max(0.0, offset):.2f}", "-t", f"{dur:.2f}", "-i", str(cam)]   # камера с того места, где закончилась прошлая часть
    cc = opts.get("cam_crop") or {}              # подрезка камеры из окна: масштаб и сдвиг (0..1) внутри верхней части
    z = min(3.0, max(1.0, float(cc.get("z") or 1)))
    px = min(1.0, max(0.0, float(cc.get("x", 0.5))))
    py = min(1.0, max(0.0, float(cc.get("y", 0.5))))
    frame, mask_c, mask_v = cards_assets()
    for f_ in (frame, mask_c, mask_v):           # входы 2, 3, 4: подложка и маски (картинки крутим по кругу на время части)
        inputs += ["-loop", "1", "-framerate", "30", "-t", f"{dur:.2f}", "-i", str(f_)]
    cx, cy, cw, ch, _ = CAM_BOX
    vx, vy, vw, vh, _ = VID_BOX
    enh = m.ENHANCE
    fc = (f"[1:v]scale={cw}:{ch}:force_original_aspect_ratio=increase,scale=iw*{z:.3f}:ih*{z:.3f},"
          f"crop={cw}:{ch}:(iw-{cw})*{px:.3f}:(ih-{ch})*{py:.3f},setsar=1,fps=30,{enh},format=rgba[ca];"
          f"[ca][3:v]alphamerge[cb];"
          f"[0:v]split[vb][vg];"
          # фон как в Dota: то же видео, сильно размытое, светлое и сочное
          f"[vb]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=11,scale={W}:{H_}:flags=bicubic,"
          f"eq=brightness=0.06:saturation=1.7:contrast=1.08,setsar=1,fps=30[bg];"
          f"[vg]scale={vw}:{vh}:force_original_aspect_ratio=increase,crop={vw}:{vh},setsar=1,fps=30,{enh},format=rgba[ga];"
          f"[ga][4:v]alphamerge[gb];"
          f"[bg][2:v]overlay=0:0[l1];[l1][cb]overlay={cx}:{cy}[l2];[l2][gb]overlay={vx}:{vy}[st]")
    assp = str(ass).replace("\\", "/").replace(":", "\\:")
    fdir = str(BASE / "fonts").replace("\\", "/").replace(":", "\\:")     # свои шрифты клиппера
    fc += f";[st]ass='{assp}':fontsdir='{fdir}'[sa]"   # субтитры до паузы: на баннере кадр замирает вместе с ними
    last, amap, total, va = "sa", "0:a", dur, has_audio(video)
    if opts.get("banner"):                       # баннер оффера как в клипах: ролик на паузе, баннер по центру
        b_ = m.banner_for(cfg, "talk", opts["banner"])
        bfile, (bw, bh), chain = m.banner_filter(b_, cfg, quiet=True)
        sp = float(b_.get("speed") or 1)
        bdur = min(12.0, m.probe(bfile)["dur"] / sp)
        t0 = pause_point(words, dur)
        inputs += ["-i", str(bfile)]
        fc += (f";[sa]split[s1][s2];"
               f"[s1]trim=0:{t0:.3f},setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration={bdur:.3f}[pa];"
               f"[s2]trim={t0:.3f},setpts=PTS-STARTPTS[pb];[pa][pb]concat=n=2:v=1:a=0[sp];"
               f"[5:v]{chain},setpts=(PTS-STARTPTS)/{sp:.3f}+{t0:.3f}/TB[ban];"
               f"[sp][ban]overlay=(W-w)/2:(H-h)/2:enable='between(t,{t0:.3f},{t0 + bdur:.3f})':eof_action=pass[sb]")
        last, total = "sb", dur + bdur
        if va:                                   # звук видео тоже на паузе: тишина под баннером, дальше с того же места
            fc += (f";[0:a]asplit[a1][a2];[a1]atrim=0:{t0:.3f},asetpts=PTS-STARTPTS,apad=pad_dur={bdur:.3f}[aa];"
                   f"[a2]atrim={t0:.3f},asetpts=PTS-STARTPTS[ab];[aa][ab]concat=n=2:v=0:a=1[am]")
            amap = "[am]"
            if has_audio(bfile):
                ms = int(t0 * 1000)
                fc += (f";[5:a]atempo={sp:.3f},atrim=0:{bdur:.3f},adelay={ms}|{ms}[ba];"
                       f"[am][ba]amix=inputs=2:normalize=0:duration=first[a]")
                amap = "[a]"
    fc += f";[{last}]format=yuv420p[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", fc, "-map", "[v]"] + \
          (["-map", amap] if va else []) + \
          ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", "-t", f"{total:.2f}", str(out)]
    r = _run(cmd)
    ass.unlink(missing_ok=True)
    if r.returncode != 0 or not out.exists():
        raise Stop("монтаж не получился: " + (r.stderr or "")[-300:])


def run(cam, video_src, n=5, opts=None, cfg=None, say=print, should_stop=lambda: False, send=True):
    opts = opts or {}
    cfg = cfg or json.loads(m.CFG_FILE.read_text(encoding="utf-8"))
    cam = Path(cam)
    video = fetch_video(video_src, say)
    if not has_audio(video):
        raise Stop("у видео нет звука: лучшие моменты ищутся по звуку и речи видео")
    offset = 0.0                                 # камеру не подгоняем под видео: как есть, по кругу
    if m.probe(cam)["dur"] < 1:
        raise Stop("файл камеры пустой или не открывается")
    vdur = m.probe(video)["dur"]
    lo, hi = 0.0, vdur
    title = opts.get("title") or video.stem
    TMP.mkdir(parents=True, exist_ok=True)
    tag = f"{os.getpid()}_{threading.get_ident()}"   # свои временные файлы у каждого запуска: две нарезки не мешают друг другу
    wav = wav16(video, TMP / f"video_{tag}.wav")
    say("ищу лучшие моменты в видео…")
    loud = H.loudness_curve(wav)
    score, parts = H.score_curve(loud, [], [], None, len(loud))
    import numpy as np
    score = np.asarray(score, dtype=float)
    ads = _merge(sponsor_ads(video_src))
    if ads:
        say(f"реклама по SponsorBlock: {len(ads)} вставок — вырезаю")
    tmp = TMP / f"cand_{tag}.wav"
    ck = TMP / f"words_{re.sub(r'[^\w-]+', '_', video.stem)[:60]}.json"   # распознанная речь по кускам: повтор не ждёт
    try:
        wcache = json.loads(ck.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        wcache = {}
    start_t, ser_title, words = None, "", []
    for attempt in range(3):                     # нашлась реклама — вырезаем её и добираем длину дальше по видео
        tl = _Clean(vdur, ads)
        cnt = max(1, min(n, int(tl.total // PART_LEN))) if tl.total >= PART_LEN else 1
        need = min(tl.total, cnt * PART_LEN)
        if start_t is None:                      # лучший кусок подряд: сумма «силы момента» по секундам без рекламы
            sec = np.array([s for s in range(min(int(vdur), len(score))) if not tl.in_ad(s + 0.5)], dtype=int)
            vals = np.clip(score[sec], -50, None) if len(sec) else np.zeros(1)
            w = max(1, int(min(len(vals), need)))
            csum = np.concatenate([[0.0], np.cumsum(vals)])
            k = int(np.argmax(csum[w:] - csum[:-w])) if len(vals) > w else 0
            start_t = float(sec[k]) if len(sec) else 0.0
        c0 = max(0.0, min(tl.to_clean(start_t), tl.total - need))
        c1 = c0 + need
        chunks = []                              # речь распознаём кусками по 2 мин (с запасом под границы частей)
        for a, b in tl.pieces(max(0.0, c0 - SNAP), min(tl.total, c1 + SNAP)):
            x = a
            while x < b - 0.5:
                chunks.append((x, min(b, x + 120)))
                x += 120
        for i, (a, b) in enumerate(chunks):
            if should_stop():
                raise Stop("остановлено")
            key = f"{a:.1f}-{b:.1f}"
            if key not in wcache:
                H.cut_wav(wav, a, b, tmp)
                tmp.with_suffix(".words.json").unlink(missing_ok=True)
                wcache[key] = [dict(w, s=w["s"] + a, e=w["e"] + a) for w in m.transcribe(tmp, cfg)]
                tmp.with_suffix(".words.json").unlink(missing_ok=True)
                try:
                    ck.write_text(json.dumps(wcache, ensure_ascii=False), encoding="utf-8")
                except (OSError, TypeError, ValueError):
                    pass
            say(f"распознаю речь: {i + 1} из {len(chunks)}")
        words = sorted((w for a, b in chunks for w in wcache[f"{a:.1f}-{b:.1f}"]), key=lambda w: w["s"])
        words = [w for w in words if not tl.in_ad((w["s"] + w["e"]) / 2)]
        say("выбираю лучшие моменты: проверяю, нет ли рекламы…")
        found = find_ads(words, cfg, title_hint=opts.get("title") or video.stem)
        ser_title = found.get("title") or ser_title
        new = [(a, b) for a, b in found.get("ads", []) if not any(x <= a + 1 and b - 1 <= y for x, y in ads)]
        if not new:
            break
        say(f"нашёл рекламу блогера: {len(new)} вставок — вырезаю и добираю длину")
        ads = _merge(ads + new)
    tl = _Clean(vdur, ads)
    words = [w for w in words if not tl.in_ad((w["s"] + w["e"]) / 2)]
    cw = [dict(w, s=tl.to_clean(w["s"]), e=tl.to_clean(w["e"])) for w in words]   # слова во «времени без рекламы»
    bounds = [c0 + i * PART_LEN for i in range(cnt + 1)]
    bounds[-1] = min(bounds[-1], tl.total)
    gaps = [(x["e"] + y["s"]) / 2 for x, y in zip(cw, cw[1:]) if y["s"] - x["e"] >= 0.25]
    for i in range(len(bounds)):                 # граница — в паузу между словами, а не посреди фразы
        near = [g for g in gaps if abs(g - bounds[i]) <= SNAP and (i == 0 or g > bounds[i - 1] + 20)]
        if near:
            bounds[i] = min(near, key=lambda g: abs(g - bounds[i]))
    title = ser_title or title

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    made, cam_off, cam_dur = [], 0.0, max(1.0, m.probe(cam)["dur"])
    for i in range(cnt):                         # части встык: часть i+1 начинается там, где закончилась часть i
        if should_stop():
            raise Stop("остановлено")
        say(f"монтирую часть {i + 1} из {cnt}")
        ca, cb = bounds[i], bounds[i + 1]
        if cb - ca < 10:
            continue
        pw = [dict(w, s=w["s"] - ca, e=w["e"] - ca) for w in cw if ca <= w["s"] and w["e"] <= cb]
        out = OUT / f"{re.sub(r'[^\w-]+', '_', title)[:40]}_{stamp}_p{i + 1}.mp4"
        pieces = tl.pieces(ca, cb)
        if len(pieces) == 1:
            render(cam, video, pieces[0][0], pieces[0][1], cam_off, out, "", pw, opts, cfg)
        else:                                    # внутри части была реклама: склеиваем куски без неё
            src = TMP / f"part_{tag}.mp4"
            join_pieces(video, pieces, src)
            render(cam, src, 0.0, cb - ca, cam_off, out, "", pw, opts, cfg)
            src.unlink(missing_ok=True)
        cam_off = (cam_off + (cb - ca)) % cam_dur  # камера идёт дальше, а не с начала
        hashtags = "#реакция " + m.base_tags()
        cap = f"{title} — часть {i + 1}" if cnt > 1 else title
        out.with_suffix(".txt").write_text(f"{cap}\n\n{hashtags}\n", encoding="utf-8")
        if send:
            p = {"clip_id": out.stem, "file_out": str(out), "streamer": "реакция", "final_title": cap,
                 "description": "", "potential": 60, "format": "reaction",
                 "duration": round(m.probe(out)["dur"])}          # подпись в Telegram ждёт формат и длину
            m.tg_send(out, p, hashtags, "manual")
        made.append(out)
    tmp.unlink(missing_ok=True)
    wav.unlink(missing_ok=True)
    return made


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for f in run(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5, send=False):
        print(f)
