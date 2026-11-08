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
# условия баннерки для реакций: ролик до 2 минут, один баннер в середине (пауза ~4 с входит в эти 2 минуты)
PART_MIN, PART_MAX = 45, 115
WIN_BEFORE, WIN_AFTER = 80, 50                  # окно кандидата вокруг пика: ~2 мин, чтобы было из чего собрать часть
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
              "-stream_loop", "-1", "-t", f"{dur:.2f}", "-i", str(cam)]
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
    score = np.asarray(score)
    score[:int(lo) + 5] = -1e9
    score[max(0, int(hi) - 5):] = -1e9
    peaks = H.pick_peaks(score, max(8, n * 2), gap=90)    # части длинные (до 2 мин): кандидатов меньше, они дальше друг от друга
    if not peaks:
        raise Stop("не нашёл заметных моментов")
    cands, tmp = [], TMP / f"cand_{tag}.wav"
    ck = TMP / f"cands2_{re.sub(r'[^\w-]+', '_', video.stem)[:60]}.json"   # распознанная речь: повтор того же видео не ждёт снова
    try:
        cached = {int(c["peak"]): c for c in json.loads(ck.read_text(encoding="utf-8"))}
    except (OSError, ValueError, KeyError, TypeError):
        cached = {}
    for k, pk in enumerate(peaks):
        if should_stop():
            raise Stop("остановлено")
        if int(pk) in cached:
            cands.append(dict(cached[int(pk)], id=k + 1))
            say(f"распознаю речь: {k + 1} из {len(peaks)} (уже было)")
            continue
        a, b = max(lo, pk - WIN_BEFORE), min(hi, pk + WIN_AFTER)
        H.cut_wav(wav, a, b, tmp)
        tmp.with_suffix(".words.json").unlink(missing_ok=True)
        words = [dict(w, s=w["s"] + a, e=w["e"] + a) for w in m.transcribe(tmp, cfg)]
        tmp.with_suffix(".words.json").unlink(missing_ok=True)
        cands.append({"id": k + 1, "peak": int(pk), "a": float(a), "b": float(b), "words": words,
                      "why": H.describe(pk, parts), "power": float(score[pk])})
        say(f"распознаю речь: {k + 1} из {len(peaks)}")
        try:                                     # сохраняем после каждого куска: если упадёт дальше, не потеряем
            ck.write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError, ValueError):
            pass
    tmp.unlink(missing_ok=True)

    say("выбираю лучшие моменты…")
    chosen = []
    byid = {c["id"]: c for c in cands}
    for p in H.ask_claude(cands, {"channel": "", "title": title}, n, cfg, length=(PART_MIN, PART_MAX)) or []:
        c = byid.get(int(p.get("id", 0) or 0))
        if not c:
            continue
        a = max(lo, float(p.get("start", c["a"])))
        b = min(hi, float(p.get("end", c["b"])), a + PART_MAX)   # часть до 2 минут вместе с паузой под баннер
        if b - a >= 20 and all(b <= x["a"] or a >= x["b"] for x in chosen):
            chosen.append({"a": a, "b": b, "c": c, "title": str(p.get("title", "")).strip(),
                           "score": int(p.get("score") or 50)})
    if not chosen:                               # без Claude: самые сильные места
        for c in sorted(cands, key=lambda c: -c["power"]):
            a, b = H.snap(c["words"], c["peak"] - 70, c["peak"] + 35)
            a, b = max(lo, a), min(hi, b, a + PART_MAX)
            if b - a >= 20 and all(b <= x["a"] or a >= x["b"] for x in chosen):
                chosen.append({"a": a, "b": b, "c": c, "title": "", "score": 50})
    chosen = sorted(chosen[:n], key=lambda x: x["a"])        # части по порядку просмотра
    if not chosen:
        raise Stop("не нашёл подходящих моментов")

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M")
    made = []
    for i, ch in enumerate(chosen, 1):
        if should_stop():
            raise Stop("остановлено")
        say(f"монтирую часть {i} из {len(chosen)}")
        a, b = ch["a"], ch["b"]
        words = [dict(w, s=w["s"] - a, e=w["e"] - a) for w in ch["c"]["words"] if a <= w["s"] and w["e"] <= b]
        out = OUT / f"{re.sub(r'[^\w-]+', '_', title)[:40]}_{stamp}_p{i}.mp4"
        render(cam, video, a, b, offset, out, "", words, opts, cfg)
        hashtags = "#реакция " + m.base_tags()
        out.with_suffix(".txt").write_text(f"{ch['title']}\n\n{hashtags}\n", encoding="utf-8")
        if send:
            p = {"clip_id": out.stem, "file_out": str(out), "streamer": "реакция", "final_title": ch["title"],
                 "description": "", "potential": ch["score"], "format": "reaction",
                 "duration": round(m.probe(out)["dur"])}          # подпись в Telegram ждёт формат и длину
            m.tg_send(out, p, hashtags, "manual")
        made.append(out)
    wav.unlink(missing_ok=True)
    return made


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for f in run(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 5, send=False):
        print(f)
