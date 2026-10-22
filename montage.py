# -*- coding: utf-8 -*-
"""
Шаг 2: автомонтаж клипов в формат 9:16.
Окно со стримером на размытом фоне, заголовок, субтитры по словам,
баннер на стоп-кадре в середине, чистые метаданные.

Запуск:
  python montage.py                 - смонтировать все новые клипы из passports.jsonl
  python montage.py файл.mp4 --format talk --streamer ник --title "Заголовок"
"""
import argparse
import base64
import difflib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
BASE = Path(os.environ.get("CLIPPER_DATA") or Path(__file__).resolve().parent).resolve()   # папка профиля с данными
CODE = Path(__file__).resolve().parent                     # папка с программой
CFG_FILE = BASE / "montage.json"
PASSPORTS = BASE / "passports.jsonl"
CACHE = BASE / "cache"
W, H = 1080, 1920

BAD = re.compile(
    r"^(?:за|на|по|вы|у|до|от|отъ|разъ|объ|при|про|недо|долбо|съ|въ|подъ|пере|ни|не)?"
    r"(?:ху[йеёяию]|пизд|[её]б(?:а|у|и|л|н|ё|е|ыр|т)|бля(?:$|[дт])|сук[аиуе]$|сучк|"
    r"мудак|мудил|пид[оа]р|залуп|гандон|шлюх)", re.I)


BAD_EN = re.compile(r"(?:fuck|shit|bitch|cunt|asshole|nigg|fagg?ot|retard|pussy|cocksuck|whore|slut|bastard|"
                    r"dickhead|motherf)", re.I)
_LANG = []


def lang():
    """Язык профиля из настроек: ru или en. От него зависят фильтр мата, подсказки Claude и хэштеги."""
    if not _LANG:
        try:
            _LANG.append(str(json.loads(CFG_FILE.read_text(encoding="utf-8")).get("language", "ru")).lower())
        except Exception:
            _LANG.append("ru")
    return _LANG[0]


def base_tags():
    try:                                         # свои хэштеги профиля, например для доты
        t = json.loads(CFG_FILE.read_text(encoding="utf-8")).get("tags")
        if t:
            return str(t)
    except Exception:
        pass
    return "#cs2 #counterstrike #twitch #clips" if lang() == "en" else "#стример #twitch #нарезки"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def probe(path):
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)])
    j = json.loads(r.stdout)
    s = j["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"w": int(s["width"]), "h": int(s["height"]),
            "fps": float(num) / float(den or 1), "dur": float(j["format"]["duration"])}


def loudness(path):
    r = run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
             "-vn", "-f", "null", "-"])
    mean = re.search(r"mean_volume: (-?[\d.]+)", r.stderr)
    return float(mean.group(1)) if mean else -20.0


def censor(word):
    core = re.sub(r"[^\w-]", "", word)
    if len(core) >= 3 and (BAD_EN if lang() == "en" else BAD).search(core.lower()):
        return core[0] + "*" * (len(core) - 2) + core[-1]
    return core


# ---------- расшифровка речи ----------
def transcribe(clip, cfg):
    cache = clip.with_suffix(".words.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  faster-whisper не установлен: ролик будет без субтитров и без вырезки пауз")
        return []
    global _model
    if "_model" not in globals():
        _model = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8")
    # звук достаём через ffmpeg сами: так не зависим от версии библиотеки PyAV
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "f32le", "-"], capture_output=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    if audio.size < 16000:
        return []
    segs, _ = _model.transcribe(audio, language=cfg["language"],
                                word_timestamps=True, vad_filter=True)
    words = []
    for s in segs:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                words.append({"s": round(w.start, 3), "e": round(w.end, 3), "t": t})
    cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


def merge_hyphens(words):
    """Whisper режет слова вроде «какой-то» на «какой» и «-то»: склеиваем обратно."""
    out = []
    for w in words:
        t = w["t"].strip()
        if not t.strip("-"):
            continue
        if out and (t.startswith("-") or out[-1]["t"].endswith("-")) and w["s"] - out[-1]["e"] < 0.4:
            out[-1] = {"s": out[-1]["s"], "e": w["e"], "t": out[-1]["t"] + t}
        elif t.strip("-"):
            out.append({"s": w["s"], "e": w["e"], "t": t.strip("-") if t.startswith("-") else t})
    return out


FIX_PROMPT = """Это автоматическая расшифровка речи стримера: по одному слову в строке, с номером.
В ней есть ошибки распознавания: неверные окончания и падежи, перепутанные похожие по звучанию слова,
искажённые слова. Исправь их по смыслу фразы.

Правила:
- количество слов и их порядок менять нельзя: на выходе ровно {n} слов под теми же номерами;
- не добавляй, не убирай и не склеивай слова, каждое слово остаётся одним словом без пробелов;
- сленг, мат, имена, ники и игровые термины оставляй как есть, исправляй только явные ошибки распознавания;
- если не уверен, оставь слово как было;
- без знаков препинания.

Верни ТОЛЬКО JSON-массив из {n} строк, без пояснений и без markdown.

{body}"""


def ai_fix_words(words, clip, cfg):
    """Исправляет ошибки распознавания в субтитрах через Claude, не меняя число слов и их время.
    Без ключа или при любой неудаче возвращает слова как были."""
    if not cfg.get("fix_subs", True) or not KEY_FILE.exists() or len(words) < 3:
        return words
    cache = clip.with_suffix(".fixed.json")
    if cache.exists():
        try:
            fixed = json.loads(cache.read_text(encoding="utf-8"))
            if len(fixed) == len(words):
                return [dict(w, t=t) for w, t in zip(words, fixed)]
        except ValueError:
            pass
    try:
        import requests
        key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
        out_all, rejected = [], 0
        for start in range(0, len(words), 150):          # длинные расшифровки правим по частям
            part = words[start:start + 150]
            body = "\n".join(f"{i + 1}. {re.sub(r'[^\w-]', '', w['t'])}" for i, w in enumerate(part))
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": cfg.get("claude_model", "claude-haiku-4-5"),
                                    "max_tokens": 200 + 8 * len(part),
                                    "messages": [{"role": "user",
                                                  "content": FIX_PROMPT.format(n=len(part), body=body)}]}, timeout=90)
            if r.status_code != 200:
                print(f"  Claude API (субтитры) ответил {r.status_code}: {r.text[:160]}")
                return words
            txt = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
            arr = json.loads(txt[txt.index("["):txt.rindex("]") + 1])
            if len(arr) != len(part):
                return words                              # число слов не совпало: оставляем как было
            for w, new in zip(part, arr):
                old = re.sub(r"[^\w-]", "", w["t"])
                new = re.sub(r"[^\w-]", "", str(new))
                # слишком непохожую замену не принимаем: это уже не исправление, а пересказ
                ok = new and " " not in new and difflib.SequenceMatcher(None, old.lower(), new.lower()).ratio() >= 0.5
                rejected += 0 if ok else 1
                out_all.append(new if ok else old)
        if rejected > 0.3 * len(words):                  # модель переписала текст, а не исправила: не берём
            return words
        changed = sum(1 for w, t in zip(words, out_all) if re.sub(r"[^\w-]", "", w["t"]).lower() != t.lower())
        if changed:
            print(f"  субтитры: исправлено слов: {changed}")
        cache.write_text(json.dumps(out_all, ensure_ascii=False), encoding="utf-8")
        return [dict(w, t=t) for w, t in zip(words, out_all)]
    except Exception as e:
        print(f"  исправление субтитров через Claude не получилось: {e}")
        return words


# ---------- заголовок ----------
LAUGH = re.compile(r"^(а*(ха|хи|хе|ах)+х*а*|лол|кек|хд|xd|lol|kek|бус|жесть|имба|ору|рофл)$", re.I)


def fix_title(title, words):
    """Запасной заголовок без Claude: первая фраза стримера. Название клипа с Twitch
    берётся, только если речи в клипе нет совсем."""
    tw = [x for x in (re.sub(r"[^\w-]", "", x) for x in title.split()) if x]
    phrase = []
    for i, w in enumerate(words):
        t = re.sub(r"[^\w-]", "", w["t"])
        if not t:
            continue
        if phrase and (w["s"] - words[i - 1]["e"] > 0.6 or len(phrase) >= 5):
            if len(phrase) >= 2:
                break
        phrase.append(t)
        if len(phrase) >= 5:
            break
    return " ".join(censor(x) or x for x in phrase) if len(phrase) >= 2 else " ".join(tw)


KEY_FILE = BASE / "claude_key.txt"
PROMPT = """Ты придумываешь заголовки для коротких вертикальных роликов (TikTok) из нарезок стримеров.
Стример: {streamer}. Ниже расшифровка речи из клипа (в ней могут быть ошибки распознавания).

Расшифровка:
{text}

Верни ТОЛЬКО JSON без пояснений и без markdown:
{{"title": "...", "description": "...", "score": 0}}

Правила:
- title: 3-6 слов на русском, по делу: что произошло или что стример сказал. Должен цеплять,
  но не врать и не обещать того, чего нет в клипе. Без мата, без эмодзи, без кавычек, без точки
  в конце. Можно начать с ника или имени стримера. Не упоминай сайты, кейсы, промокоды и ставки.
- description: одна короткая фраза для описания ролика, до 80 символов, без хэштегов.
- score: от 1 до 10, насколько клип цепляет как самостоятельный ролик (есть ли понятная мысль,
  шутка или эмоция и законченность)."""


PROMPT_EN = """You write titles for short vertical videos (TikTok) cut from streamers' clips.
Streamer: {streamer}. Below is a speech transcript of the clip (it may contain recognition errors).

Transcript:
{text}

Return ONLY JSON, no explanations, no markdown:
{{"title": "...", "description": "...", "score": 0}}

Rules:
- title: 3-6 words in English, to the point: what happened or what the streamer said. It should hook,
  but must not lie or promise what is not in the clip. No profanity, no emoji, no quotes, no trailing
  period. You may start with the streamer's name. Do not mention sites, cases, promo codes or betting.
- description: one short phrase for the video description, up to 80 characters, no hashtags.
- score: 1 to 10, how well the clip works as a standalone video (a clear thought, joke or emotion,
  and a sense of completeness)."""


def ai_meta(words, streamer, cfg):
    """Заголовок, описание и оценка клипа через Claude API. None, если ключа нет или запрос не удался."""
    if not KEY_FILE.exists() or len(words) < 4:
        return None
    key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        return None
    try:
        import requests
        text = " ".join(w["t"] for w in words)[:4000]
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": cfg.get("claude_model", "claude-haiku-4-5"), "max_tokens": 300,
                  "messages": [{"role": "user",
                                "content": (PROMPT_EN if lang() == "en" else PROMPT).format(
                                    streamer=streamer or "unknown", text=text)}]},
            timeout=60)
        if r.status_code != 200:
            print(f"  Claude API ответил {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        out = re.sub(r"```(?:json)?|```", "", out).strip()
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        title = re.sub(r"[\"«»“”.!]+", "", str(j.get("title", ""))).strip()
        if len(title.split()) < 2:
            return None
        return {"title": title, "description": str(j.get("description", "")).strip()[:120],
                "score": j.get("score")}
    except Exception as e:
        print(f"  заголовок через Claude не получился: {e}")
        return None


# ---------- поиск камеры через Claude (по двум кадрам) ----------
CAM_PROMPT = """Это два кадра из одного клипа со стрима на Twitch. Найди все окна с веб-камерами людей.
Ответь ТОЛЬКО JSON без пояснений и без markdown:
{"cams": [[x0, y0, x1, y1]], "content": null, "captions": null, "ads": [], "game_on_screen": false, "what": "..."}

cams: список окон, в которых через веб-камеру виден живой человек (стример и его гости), не больше 4.
  Каждое окно это [x0, y0, x1, y1] в процентах от ширины и высоты кадра (0-100), как можно точнее
  по краям самого окна. Первым укажи главного стримера. Если стример снят на весь кадр, верни одно
  окно [0, 0, 100, 100]. Если людей в кадре нет, верни пустой список.
  Не считай камерой: аватарки и заглушки участников с выключенной камерой, рекламные баннеры,
  лица в видео или на сайте, которые стример смотрит. Камера стоит на одном месте в обоих кадрах.
content: окно с основным содержимым помимо камер (игра, видео, сайт, документ) в тех же процентах,
  или null, если кроме камер ничего содержательного нет (пустой фон, заглушки, логотипы).
captions: если хотя бы на одном кадре видны встроенные субтитры стрима (текст речи стримера,
  который выводится поверх картинки, обычно внизу или вверху по центру), верни область, где они
  появляются, [x0, y0, x1, y1] в процентах с запасом по высоте. Иначе null. Чат, донаты, названия
  и рекламу субтитрами не считай.
ads: список всех областей с рекламой на кадрах, каждая [x0, y0, x1, y1] в процентах: логотипы и
  баннеры спонсоров, букмекеров и казино, промокоды, рекламные виджеты и карусели, QR-коды.
  Бери область с небольшим запасом. Если рекламы нет, верни пустой список.
game_on_screen: true, если основное содержимое экрана это видеоигра.
what: 3-6 слов о том, что на экране."""
ZONES_FILE = BASE / "streamer_zones.json"


def local_caption_zone(clip):
    """Ищет встроенные субтитры стрима по их фиолетовой подсветке текущего слова (виджет субтитров
    Twitch). Возвращает область [x0, y0, x1, y1] в процентах или None. Работает без Claude."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    hh, ww = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    tops, bots, frames = [], [], 0
    for k in range(16):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / 16))
        ok, f = cap.read()
        if not ok:
            continue
        hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
        m = ((hsv[:, :, 0] >= 122) & (hsv[:, :, 0] <= 142) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)).astype(np.uint8)
        m[:int(hh * 0.6)] = 0                       # субтитры стоят в нижней части кадра, по центру
        m[:, :int(ww * 0.2)] = 0
        m[:, int(ww * 0.8):] = 0
        nlab, _, st, _ = cv2.connectedComponentsWithStats(m, 8)
        hit = False
        for i in range(1, nlab):
            x, y, w, h, area = [int(v) for v in st[i][:5]]
            if area > 0.0006 * ww * hh and w > h * 0.8 and area > 0.6 * w * h:      # сплошная плашка под словом
                tops.append(y)
                bots.append(y + h)
                hit = True
        frames += hit
    cap.release()
    if frames < 2:
        return None
    y0 = max(0.0, (min(tops) - 0.055 * hh) / hh * 100)
    y1 = min(100.0, (max(bots) + 0.065 * hh) / hh * 100)
    return [20, y0, 80, y1]


def caption_band(captions, streamer, sh):
    """Полоса кадра по высоте [ymin, ymax] в пикселях, свободная от встроенных субтитров стрима.
    Зона субтитров запоминается по стримеру: у него она всегда в одном месте."""
    zones = json.loads(ZONES_FILE.read_text(encoding="utf-8")) if ZONES_FILE.exists() else {}
    ok = isinstance(captions, list) and len(captions) == 4
    if ok:
        y0, y1 = float(captions[1]), float(captions[3])
        ok = 0 <= y0 < y1 <= 100 and (y1 - y0) <= 30
    if ok and streamer:
        zones[streamer] = [y0, y1]
        ZONES_FILE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
    elif streamer in zones:
        y0, y1 = zones[streamer]
        ok = True
    if not ok:
        return None
    if (y0 + y1) / 2 >= 50:                      # субтитры внизу: берём всё, что выше
        return [0, int(sh * max(0.0, y0 - 1.0) / 100)]
    return [int(sh * min(100.0, y1 + 1.0) / 100), sh]


def fit_crop(box, ratio, face=None, valign=0.35):
    """Наибольший прямоугольник с нужным соотношением сторон внутри box [x0,y0,x1,y1].
    Если известно лицо, оно ставится по центру по горизонтали и чуть выше середины по вертикали."""
    bw_, bh_ = box[2] - box[0], box[3] - box[1]
    w = bw_
    h = w / ratio
    if h > bh_:
        h = bh_
        w = h * ratio
    cx = face[0] if face else (box[0] + box[2]) / 2
    x = min(max(cx - w / 2, box[0]), box[2] - w)
    y = (face[1] - 0.42 * h) if face else box[1] + (bh_ - h) * valign
    y = min(max(y, box[1]), box[3] - h)
    return [int(x) // 2 * 2, int(y) // 2 * 2, max(2, int(w) // 2 * 2), max(2, int(h) // 2 * 2)]


def to_ratio(crop, ratio):
    """Подгоняет вырезку [x,y,w,h] под нужное соотношение сторон, ничего не растягивая."""
    x, y, w, h = crop
    if w / h > ratio:
        nw = int(h * ratio) // 2 * 2
        return [x + (w - nw) // 2 // 2 * 2, y, nw, h]
    nh = int(w / ratio) // 2 * 2
    return [x, y + int((h - nh) * 0.35) // 2 * 2, w, nh]


def grab_jpeg(clip, t):
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(clip), "-frames:v", "1",
                        "-vf", "scale=960:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-"],
                       capture_output=True)
    return r.stdout


def face_center(clip, rect):
    """Центр лица (x, y) внутри окна камеры в пикселях исходника или None."""
    try:
        import cv2
        import numpy as np
        if not hasattr(cv2, "CascadeClassifier"):
            return None
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        cap = cv2.VideoCapture(str(clip))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        x0, y0, x1, y1 = rect
        xs, ys = [], []
        for k in range(8):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / 8))
            ok, f = cap.read()
            if not ok:
                continue
            g = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            k2 = 480 / max(1, g.shape[1])
            g = cv2.resize(g, (480, max(1, int(g.shape[0] * k2))))
            faces = casc.detectMultiScale(g, 1.1, 4, minSize=(30, 30))
            if len(faces):
                x, y, w, h = max(faces, key=lambda b: b[2])
                xs.append(x0 + (x + w / 2) / k2)
                ys.append(y0 + (y + h / 2) / k2)
        cap.release()
        return (float(np.median(xs)), float(np.median(ys))) if ys else None
    except Exception:
        return None


def trim_black(clip, box):
    """Убирает чёрные поля по краям окна (когда границы окна названы с запасом)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return box
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    x0, y0, x1, y1 = box
    mx = None
    for k in range(6):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / 6))
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        mx = g if mx is None else np.maximum(mx, g)
    cap.release()
    if mx is None:
        return box
    black = mx < 6                                  # пиксель чёрный во всех кадрах (тёмная комната сюда не попадает)

    def span(share):
        on = np.where(share < 0.70)[0]              # строка считается полем, если чёрная на 70% (логотип не мешает)
        return (int(on[0]), int(on[-1]) + 1) if len(on) else (0, len(share))

    ty0, ty1 = span(black.mean(1))
    tx0, tx1 = span(black[ty0:ty1].mean(0)) if ty1 > ty0 else (0, black.shape[1])
    if (ty1 - ty0) < 0.3 * (y1 - y0) or (tx1 - tx0) < 0.3 * (x1 - x0):
        return box                                  # почти всё окно чёрное: не трогаем
    return [x0 + tx0, y0 + ty0, x0 + tx1, y0 + ty1]


def verify_cam(clip, box):
    """True, если в окне подтверждена живая веб-камера (см. cam_score)."""
    return cam_score(clip, box) >= 2


def cam_score(clip, box):
    """Насколько окно похоже на живую веб-камеру.
    2: лицо стоит на одном месте большую часть клипа и двигается (камера подтверждена).
    1: фон неподвижный, картинка живая, лицо видно хотя бы изредка (стример прикрыл лицо, отвернулся).
    0: не камера: чужое видео со сменой планов, игра, кадр на паузе, картинка.
    Если проверить нечем (нет OpenCV), возвращает 2."""
    try:
        import cv2
        import numpy as np
        if not hasattr(cv2, "CascadeClassifier"):
            return 2
    except ImportError:
        return 2
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    prof = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    alt = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    x0, y0, x1, y1 = box
    n, found, crops = 12, [], []
    sure = 0                                       # в скольких кадрах лицо нашёл строгий детектор
    for k in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / n))
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        k2 = 480 / max(1, g.shape[1])
        g = cv2.resize(g, (480, max(8, int(g.shape[0] * k2))))
        crops.append(g.astype(np.float32))
        if len(casc.detectMultiScale(g, 1.1, 6, minSize=(30, 30))) or len(alt.detectMultiScale(g, 1.1, 4, minSize=(30, 30))):
            sure += 1                              # мягкий детектор «видит лица» и в радаре, и в значках; строгий нет
        faces = list(casc.detectMultiScale(g, 1.05, 3, minSize=(24, 24)))
        faces += list(prof.detectMultiScale(g, 1.08, 4, minSize=(24, 24)))
        flipped = prof.detectMultiScale(cv2.flip(g, 1), 1.08, 4, minSize=(24, 24))
        faces += [(g.shape[1] - x - w, y, w, h) for (x, y, w, h) in flipped]
        for (x, y, w, h) in faces:
            found.append((x + w / 2, y + h / 2, w, x, y, h, len(crops) - 1))
    cap.release()
    if len(crops) < 4:
        return 0
    # 1) у веб-камеры фон один и тот же весь клип; в чужом видео и в игре кадр резко меняется
    small = [cv2.resize(c, (160, 90)) for c in crops]
    jumps = [float(np.abs(p - q).mean()) for p, q in zip(small, small[1:])]
    big, med = sum(j > 30 for j in jumps), float(np.median(jumps))
    if big >= 4 or med > 25:
        return 0
    # слабое подтверждение: картинка живая, фон стоит на месте, и либо лицо хоть раз нашлось,
    # либо фон совсем спокойный (стример прикрыл лицо рукой или смотрит вниз)
    # доля пикселей, которые хоть немного меняются от кадра к кадру: у камеры «дышит» весь кадр,
    # а у статичной графики (радар, счёт, заставка) меняются только отдельные точки
    moving = float(np.median([float((np.abs(p - q) > 3).mean()) for p, q in zip(small, small[1:])]))
    weak = 0
    if med >= 1.0 and moving >= 0.2:
        if sure >= 1:
            weak = 1.5                # лицо хоть раз уверенно нашлось
        elif big <= 2 and med <= 22:
            weak = 1                  # лица не видно, но окно ведёт себя как камера
    if big >= 2 or sure < 2:
        return weak                 # фон заметно меняется или лица толком не видно: камера не подтверждена
    # 2) лицо есть и стоит на одном месте
    need = max(3, int(0.25 * len(crops)))
    if len(found) < need:
        return weak
    a = np.array(found, dtype=float)
    gw, gh = crops[0].shape[1], crops[0].shape[0]
    stack = np.stack(crops)
    for cx, cy, w in a[:, :3]:                     # перебираем места, где лицо видно в достаточном числе кадров
        m_ = (np.abs(a[:, 0] - cx) < 0.20 * gw) & (np.abs(a[:, 1] - cy) < 0.25 * gh) & \
             (a[:, 2] > w / 1.8) & (a[:, 2] < w * 1.8)
        if len(set(a[m_, 6])) < need:
            continue
        # 3) лицо живое: на паузе, на картинке или у фигурки на фоне оно не двигается
        fx, fy = int(np.median(a[m_, 3])), int(np.median(a[m_, 4]))
        fw, fh = int(np.median(a[m_, 2])), int(np.median(a[m_, 5]))
        st = stack[:, fy:fy + fh, fx:fx + fw]
        if st.size and float(np.median(np.abs(np.diff(st, axis=0)).mean((1, 2)))) > 3.0:
            return 2
    return weak


def pick_cam(clip, streamer, cfg, sw, min_score=1):
    """Камера стримера из настроек. У одного стримера может быть несколько вариантов места
    (своя игра, просмотр турнира, общение): берётся тот, где в клипе реально видно живое лицо.
    Координаты заданы для кадра шириной 1920. None, если ни один вариант не подтвердился."""
    cands = cfg["cams"].get(streamer)
    if not cands:
        return None
    if isinstance(cands[0], (int, float)):
        cands = [cands]
    best = (0, None)
    for c in cands:
        box = [int(v * sw / 1920) // 2 * 2 for v in c]
        sc = cam_score(clip, [box[0], box[1], box[0] + box[2], box[1] + box[3]])
        if sc == 2:
            return box
        if sc > best[0]:              # лицо прикрыто или в профиль: место размечено вручную, этого достаточно
            best = (sc, box)
    return best[1] if best[0] >= min_score else None


def cam_timeline(clip, streamer, cfg, sw, dur):
    """Если стример посреди клипа переключает сцену и камера переезжает в другой угол, возвращает
    расписание [(начало, конец, окно камеры), ...] по времени исходника. None, если камера весь
    клип в одном месте или размечено меньше двух вариантов."""
    cands = cfg["cams"].get(streamer)
    if not cands or isinstance(cands[0], (int, float)) or len(cands) < 2:
        return None
    try:
        import cv2
        if not hasattr(cv2, "CascadeClassifier"):
            return None
    except ImportError:
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    alt = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml")
    import numpy as np
    boxes = [[int(v * sw / 1920) // 2 * 2 for v in c] for c in cands]
    cap = cv2.VideoCapture(str(clip))
    step = max(0.5, dur / 60)
    times, faces, looks = [], [], []
    t = step / 2
    while t < dur:
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, f = cap.read()
        if not ok:
            break
        seen, look = [], []
        for k, (x, y, w, h) in enumerate(boxes):
            g = cv2.cvtColor(f[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
            if g.size == 0:
                look.append(np.zeros((27, 48), np.float32))
                continue
            look.append(cv2.resize(g, (48, 27)).astype(np.float32))
            k2 = 480 / g.shape[1]
            g = cv2.resize(g, (480, max(8, int(g.shape[0] * k2))))
            if len(casc.detectMultiScale(g, 1.1, 6, minSize=(30, 30))) or len(alt.detectMultiScale(g, 1.1, 4, minSize=(30, 30))):
                seen.append(k)
        times.append(t)
        faces.append(seen)
        looks.append(look)
        t += step
    cap.release()
    if len(times) < 4:
        return None
    # как выглядит каждое место, когда там точно камера: усредняем кадры, где в нём нашлось лицо
    refs = {}
    for k in range(len(boxes)):
        got = [looks[i][k] for i in range(len(times)) if k in faces[i]]
        if len(got) >= 2:
            refs[k] = np.median(np.stack(got), 0)
    if len(refs) < 2:
        return None

    def same(a, b):
        a, b = a - a.mean(), b - b.mean()
        d = float(np.sqrt((a * a).sum() * (b * b).sum()))
        return float((a * b).sum() / d) if d > 1e-6 else 0.0

    # для каждого замера выбираем место, которое сейчас похоже на свою камеру (фон комнаты тот же)
    plan, cur = [], None
    for i in range(len(times)):
        sims = {k: same(looks[i][k], r) for k, r in refs.items()}
        k_best = max(sims, key=sims.get)
        if sims[k_best] >= 0.55:
            cur = k_best
        plan.append(cur)
    used = [c for c in plan if c is not None]
    if len(set(used)) < 2:
        return None
    plan = [c if c is not None else used[0] for c in plan]
    for i in range(1, len(plan) - 1):               # одиночные выбросы убираем
        if plan[i - 1] == plan[i + 1] != plan[i]:
            plan[i] = plan[i - 1]
    cap = cv2.VideoCapture(str(clip))

    def look_at(t_, k):
        cap.set(cv2.CAP_PROP_POS_MSEC, t_ * 1000)
        ok_, f_ = cap.read()
        if not ok_:
            return None
        x, y, w, h = boxes[k]
        return cv2.resize(cv2.cvtColor(f_[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY), (48, 27)).astype(np.float32)

    out, a0 = [], 0.0
    for i in range(1, len(plan)):
        if plan[i] != plan[i - 1]:
            lo, hi = times[i - 1], times[i]         # уточняем момент переключения сцены делением пополам
            for _ in range(5):
                mid = (lo + hi) / 2
                new, old = look_at(mid, plan[i]), look_at(mid, plan[i - 1])
                if new is None or old is None:
                    break
                if same(new, refs[plan[i]]) > same(old, refs[plan[i - 1]]):
                    hi = mid
                else:
                    lo = mid
            b0 = (lo + hi) / 2
            out.append((a0, b0, boxes[plan[i - 1]]))
            a0 = b0
    cap.release()
    out.append((a0, dur, boxes[plan[-1]]))
    return out if len(out) >= 2 else None


def on_ads(box, ads, share=0.3):
    """True, если заметную часть окна [x0,y0,x1,y1] занимает реклама."""
    area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
    for a in ads or []:
        ix = max(0, min(box[2], a[2]) - max(box[0], a[0]))
        iy = max(0, min(box[3], a[3]) - max(box[1], a[1]))
        if ix * iy > share * area:
            return True
    return False


def claude_layout(clip, info, streamer=""):
    """Спрашивает у Claude, где окно камеры. None, если ключа нет или ответ непригоден."""
    if not KEY_FILE.exists():
        return None
    key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        return None
    try:
        import requests
        content = []
        for frac in (0.2, 0.5, 0.8):
            jpg = grab_jpeg(clip, info["dur"] * frac)
            if len(jpg) < 1000:
                return None
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                        "data": base64.b64encode(jpg).decode()}})
        content.append({"type": "text", "text": CAM_PROMPT})
        cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        # границы окон точнее находит модель посильнее; если она недоступна, берём обычную
        models = [cfg.get("claude_vision_model", "claude-sonnet-5-5"), cfg.get("claude_model", "claude-haiku-4-5")]
        r = None
        for model in dict.fromkeys(models):
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": model, "max_tokens": 400,
                                    "messages": [{"role": "user", "content": content}]}, timeout=90)
            if r.status_code == 200:
                break
        if r.status_code != 200:
            print(f"  Claude API (камера) ответил {r.status_code}: {r.text[:200]}")
            return None
        out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        j = json.loads(out[out.index("{"):out.rindex("}") + 1])
        game = bool(j.get("game_on_screen"))
        what = str(j.get("what", ""))[:60]
        sw, sh = info["w"], info["h"]
        band = caption_band(local_caption_zone(clip) or j.get("captions"), streamer, sh)
        ads = []
        for b in (j.get("ads") or [])[:8]:
            if isinstance(b, list) and len(b) == 4:
                a0, b0, c0, d0 = [float(v) for v in b]
                if 0 <= a0 < c0 <= 100 and 0 <= b0 < d0 <= 100 and (c0 - a0) * (d0 - b0) <= 1000:
                    ads.append([max(0, int(sw * (a0 - 0.8) / 100)), max(0, int(sh * (b0 - 0.8) / 100)),
                                min(sw, int(sw * (c0 + 0.8) / 100)), min(sh, int(sh * (d0 + 0.8) / 100))])
        base = {"share": 1.0, "side": "mid", "game": game, "what": what, "by": "claude", "v": 8,
                "cam": None, "cams_px": [], "content_px": None, "band": band, "ads_px": ads}

        def to_px(b, inset=0.6):
            a, b0, c, d = [float(v) for v in b]
            if not (0 <= a < c <= 100 and 0 <= b0 < d <= 100):
                return None
            box = [int(sw * (a + inset) / 100), int(sh * (b0 + inset) / 100),
                   int(sw * (c - inset) / 100), int(sh * (d - inset) / 100)]
            if band:                              # окно не должно заходить в зону встроенных субтитров
                ny0, ny1 = max(box[1], band[0]), min(box[3], band[1])
                if ny1 - ny0 >= 0.5 * (box[3] - box[1]):
                    box[1], box[3] = ny0, ny1
            return box

        cams = [b for b in (j.get("cams") or []) if isinstance(b, list) and len(b) == 4][:4]
        if not cams:
            return dict(base, mode="none", share=0.0)
        areas = [(c[2] - c[0]) * (c[3] - c[1]) / 10000 for c in cams]
        content = to_px(j["content"], 0.3) if isinstance(j.get("content"), list) and len(j["content"]) == 4 else None
        if content:
            content = trim_black(clip, content)
        if content and (content[2] - content[0]) * (content[3] - content[1]) < 0.06 * sw * sh:
            content = None
        if len(cams) == 1 and areas[0] >= 0.45:
            # Claude считает, что стример на весь кадр. Перепроверим: вдруг это чужое видео, а камера в углу
            return dict(base, mode="unverified", full=True)
        px = [to_px(c) for c in cams]
        px = [trim_black(clip, b) for b, ar in zip(px, areas) if b and ar >= 0.008]
        px = [b for b in px if not on_ads(b, ads)]        # реклама с нарисованным лицом камерой не считается
        px = [b for b in px if verify_cam(clip, b)]       # оставляем только настоящие живые камеры
        if not px:
            return dict(base, mode="unverified", content_px=content)
        base["cams_px"], base["content_px"] = px, content
        if len(px) >= 2:
            return dict(base, mode="multi")
        if not content:
            return dict(base, mode="solo")
        rect = px[0]
        mid = (rect[0] + rect[2]) / 2 / sw
        return dict(base, mode="react", rect=rect, cam=fit_crop(rect, 1040 / 420, face_center(clip, rect)),
                    side="left" if mid < 0.4 else "right" if mid > 0.6 else "mid")
    except Exception as e:
        print(f"  поиск камеры через Claude не получился: {e}")
        return None


# ---------- тип кадра: стример на весь экран или реакция с камерой в углу ----------
def detect_layout(clip, streamer=""):
    """Возвращает {"mode": "talk" | "react" | "none", "cam": [x,y,w,h] | None, "share": доля кадров с лицом,
    "side": "left" | "right" | "mid"}. Камера стримера ищется как небольшое лицо, которое стоит
    на одном месте весь клип и при этом двигается (в отличие от плакатов и фигурок на фоне)."""
    cache = clip.with_suffix(".layout.json")
    if cache.exists():
        old = json.loads(cache.read_text(encoding="utf-8"))
        if (old.get("by") == "claude" and old.get("v") == 8) or (not KEY_FILE.exists() and "band" in old):
            return old
    by_claude = claude_layout(clip, probe(clip), streamer)
    if by_claude and by_claude["mode"] != "unverified":
        cache.write_text(json.dumps(by_claude, ensure_ascii=False), encoding="utf-8")
        return by_claude
    extra = by_claude                 # Claude не нашёл подтверждённую камеру: ищем сами, остальное берём у него
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"mode": "talk", "cam": None, "share": None, "side": "mid"}
    if not hasattr(cv2, "CascadeClassifier"):
        print('  OpenCV этой версии без поиска лиц. Поставь: python -m pip install "opencv-python==4.13.0.92"')
        return {"mode": "talk", "cam": None, "share": None, "side": "mid"}
    cap = cv2.VideoCapture(str(clip))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    n, w0 = 20, 1600
    h0 = int(w0 * sh / sw)
    grays, dets = [], []
    for k in range(n):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (k + 0.5) / n))
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(cv2.resize(f, (w0, h0)), cv2.COLOR_BGR2GRAY)
        grays.append(g.astype(np.float32))
        dets.append([tuple(map(int, d)) for d in casc.detectMultiScale(g, 1.08, 4, minSize=(34, 34))])
    cap.release()
    res = {"mode": "none", "cam": None, "share": 0.0, "side": "mid"}
    if grays:
        stack = np.stack(grays)
        clusters = []
        for fi, ds in enumerate(dets):
            for (x, y, w, h) in ds:
                cx, cy = x + w / 2, y + h / 2
                for c in clusters:
                    if abs(c["cx"] - cx) < 0.07 * w0 and abs(c["cy"] - cy) < 0.09 * h0 and 0.6 < w / c["w"] < 1.7:
                        c["items"].append((fi, x, y, w, h))
                        m = len(c["items"])
                        c["cx"] += (cx - c["cx"]) / m
                        c["cy"] += (cy - c["cy"]) / m
                        c["w"] += (w - c["w"]) / m
                        break
                else:
                    clusters.append({"cx": cx, "cy": cy, "w": float(w), "items": [(fi, x, y, w, h)]})
        live = []
        for c in clusters:
            c["share"] = len({i[0] for i in c["items"]}) / len(grays)
            x, y, w, h = [int(np.median([i[j] for i in c["items"]])) for j in (1, 2, 3, 4)]
            c["box"] = (x, y, w, h)
            c["motion"] = float(stack[:, y:y + h, x:x + w].std(0).mean())
            # рекламная карусель с лицами меняется слишком резко и видна не во всех кадрах
            carousel = c["motion"] > 45 and c["share"] < 0.7
            if c["share"] >= 0.3 and c["motion"] > 8 and not carousel:
                live.append(c)
        small = [c for c in live if c["box"][3] < 0.20 * h0 and
                 (c["cx"] < 0.36 * w0 or c["cx"] > 0.64 * w0 or c["cy"] < 0.30 * h0 or c["cy"] > 0.70 * h0)]
        big = [c for c in live if c["box"][3] >= 0.16 * h0 and 0.25 * w0 < c["cx"] < 0.75 * w0]
        if small:
            c = max(small, key=lambda c: (c["share"], c["box"][3]))
            k = sw / w0
            fx, fy, fw, fh = [v * k for v in c["box"]]
            cw = max(3.8 * fw, 0.25 * sw)
            ch = cw * 420 / 1040
            x0 = min(max(fx + fw / 2 - cw / 2, 0), sw - cw)
            y0 = min(max(fy + fh / 2 - 0.42 * ch, 0), sh - ch)
            fcx = fx + fw / 2
            res = {"mode": "react", "cam": [int(x0) // 2 * 2, int(y0) // 2 * 2, int(cw) // 2 * 2, int(ch) // 2 * 2],
                   "share": c["share"], "side": "left" if fcx < 0.36 * sw else "right" if fcx > 0.64 * sw else "mid",
                   "face": [int(fx), int(fy), int(fw), int(fh)]}
        elif big:
            res = {"mode": "talk", "cam": None, "share": max(c["share"] for c in big), "side": "mid"}
        elif live:
            # лицо есть, но мелкое и не в углу: стример стоит далеко от камеры (танцы, IRL)
            res = {"mode": "talk", "cam": None, "share": max(c["share"] for c in live), "side": "mid"}
        else:
            res["share"] = max([c["share"] for c in clusters], default=0.0)
    if extra:
        if extra.get("full"):
            res.update(mode="talk", cam=None)
        elif res["mode"] == "react" and on_ads([res["cam"][0], res["cam"][1], res["cam"][0] + res["cam"][2],
                                                res["cam"][1] + res["cam"][3]], extra.get("ads_px")):
            res.update(mode="none", cam=None)
        elif res["mode"] == "react":
            c = res["cam"]
            fx, fy, fw, fh = res.get("face") or (c[0], c[1], c[2], c[3])
            # проверяем полосу вокруг лица во всю ширину найденного окна камеры
            near = [c[0], max(0, int(fy - 0.5 * fh)), c[0] + c[2], int(fy + 1.3 * fh)]
            if not verify_cam(clip, near):
                res.update(mode="none", cam=None)
        res.update(band=extra.get("band"), game=extra.get("game"), what=extra.get("what"),
                   ads_px=extra.get("ads_px") or [], by="claude", v=8)
        if res["mode"] == "react" and extra.get("content_px"):
            res["content_px"] = extra["content_px"]
    else:
        res["band"] = caption_band(local_caption_zone(clip), streamer, probe(clip)["h"])
    cache.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    return res


def talk_crop(streamer, sw, sh, cfg):
    crop = cfg["crops"].get(streamer)
    if crop:                                  # координаты в настройках заданы для кадра шириной 1920
        crop = [int(v * sw / 1920) // 2 * 2 for v in crop]
    if not crop:
        ch = int(sh * 0.82)
        cw = int(ch * 960 / 924)
        crop = [(sw - cw) // 2, int(sh * 0.03), cw, ch]
    return crop


# ---------- вырезка тишины и точка баннера ----------
def audio_activity(clip):
    """Громкость звука клипа каждые 0.1 с (дБ) и обычный уровень. None, если звук не прочитался."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    if a.size < 8000:
        return None
    k = 800
    n = a.size // k
    db = 20 * np.log10(np.sqrt((a[:n * k].reshape(n, k) ** 2).mean(axis=1)) + 1e-6)
    return db, float(np.median(db))


def is_active(act, a, b, share=0.35, below=5.0):
    """Идёт ли в куске [a, b] что-то громкое: драка, способности, крики. Тишину можно вырезать, это нет."""
    if act is None or b - a <= 0:
        return False
    db, med = act
    i, j = int(a * 10), max(int(a * 10) + 1, int(b * 10))
    part = db[i:j]
    return part.size > 0 and float((part > med - below).mean()) >= share


def keep_segments(words, dur, fmt, cfg, clip=None):
    if not words:
        return [(0.0, dur)]
    max_gap = cfg["max_gap_talk"] if fmt == "talk" else cfg["max_gap_cs"]
    pad = cfg["keep_gap"] / 2
    lead = cfg["lead_talk"] if fmt == "talk" else cfg["lead_cs"]
    # в игре стример молчит именно в самые яркие моменты: драку по звуку игры не вырезаем
    act = audio_activity(clip) if (clip is not None and fmt != "talk" and cfg.get("keep_loud_game", True)) else None
    segs = []
    a, b = words[0]["s"], words[0]["e"]
    for w in words[1:]:
        if w["s"] - b > max_gap and not is_active(act, b, w["s"]):
            segs.append((a, b))
            a = w["s"]
        b = max(b, w["e"])
    segs.append((a, b))
    if act is not None:                              # начало до первых слов и конец после последних, если там громко
        if is_active(act, 0.0, segs[0][0]):
            segs[0] = (0.0, segs[0][1])
        if is_active(act, segs[-1][1], dur):
            segs[-1] = (segs[-1][0], dur)
    out = []
    for i, (a, b) in enumerate(segs):
        a2 = max(0.0, a - (lead if i == 0 else pad))
        b2 = min(dur, b + (lead if i == len(segs) - 1 else pad))
        if out and a2 <= out[-1][1]:
            out[-1] = (out[-1][0], b2)
        else:
            out.append((a2, b2))
    return out


def src_to_out(t, segs):
    acc = 0.0
    for a, b in segs:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


def out_to_src(t, segs):
    acc = 0.0
    for a, b in segs:
        if t <= acc + (b - a):
            return a + (t - acc)
        acc += b - a
    return segs[-1][1]


def zoom_events(clip, max_n=None):
    """Моменты для зума: где стример резко громче обычного (крик, смех, реакция).
    [(секунда клипа, сила 0..1, тряска)]. Тряска только на самых громких."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    if a.size < 8000 * 3:
        return []
    k = 800                                          # шаг 0.1 с
    n = a.size // k
    db = 20 * np.log10(np.sqrt((a[:n * k].reshape(n, k) ** 2).mean(axis=1)) + 1e-6)
    sm = np.convolve(db, np.ones(3) / 3, mode="same")
    base = float(np.median(sm))
    out, last = [], -99.0
    max_n = max_n or max(1, int(n / 10 / 7))         # не чаще одного зума на 7 секунд в среднем
    order = np.argsort(-sm)
    for i in order:
        ex = float(sm[i] - base)
        if ex < 8 or len(out) >= max_n:
            break
        t = i / 10
        if t < 0.8 or any(abs(t - x[0]) < 3.5 for x in out):
            continue
        # начало всплеска: зум должен прийти вместе с криком, а не после
        j = i
        while j > 0 and sm[j - 1] > base + ex * 0.5 and i - j < 8:
            j -= 1
        out.append((round(float(j) / 10, 2), float(min(1.0, (ex - 8) / 10 + 0.4)), bool(ex >= 15)))
    return sorted(out)


def zoom_expr(events):
    """Выражения ffmpeg для зума и тряски окна по времени ролика: (масштаб, сдвиг x, сдвиг y)."""
    if not events:
        return None
    bumps = []
    for t, amp, _ in events:
        bumps.append(f"{0.10 + 0.08 * amp:.3f}*clip((t-{t - 0.08:.2f})/0.14\\,0\\,1)*clip(({t + 1.35:.2f}-t)/0.55\\,0\\,1)")
    z = "1+" + "+".join(bumps)
    sh = [f"clip((t-{t:.2f})/0.05\\,0\\,1)*clip(({t + 0.45:.2f}-t)/0.3\\,0\\,1)" for t, _, k in events if k]
    if sh:
        env = "(" + "+".join(sh) + ")"
        sx, sy = f"14*sin(t*53)*{env}", f"10*cos(t*47)*{env}"
    else:
        sx = sy = "0"
    return z, sx, sy


def pick_split(words, segs, cfg):
    total = sum(b - a for a, b in segs)
    mid = total / 2
    best = None
    for w1, w2 in zip(words, words[1:]):
        if w2["s"] - w1["e"] < 0.12:
            continue
        g = src_to_out((w1["e"] + w2["s"]) / 2, segs)
        if abs(g - mid) <= cfg["banner_snap_sec"] and (best is None or abs(g - mid) < abs(best - mid)):
            best = g
    return best if best is not None else mid


def snap_to_pause(words, segs, target, tol):
    """Ближайшая к target пауза между словами (по шкале смонтированного ролика), не дальше tol секунд."""
    best = None
    for w1, w2 in zip(words, words[1:]):
        if w2["s"] - w1["e"] < 0.12:
            continue
        g = src_to_out((w1["e"] + w2["s"]) / 2, segs)
        if abs(g - target) <= tol and (best is None or abs(g - target) < abs(best - target)):
            best = g
    return best if best is not None else target


def banner_splits(words, segs, content, ban_dur, cfg):
    """Где ставить баннер. Обычно один, в середине. Если готовый ролик выходит от полутора минут,
    по условиям оффера баннеров два: на 30-й секунде и на 60-й."""
    if content + ban_dur < float(cfg.get("two_banners_from", 90)):
        return [pick_split(words, segs, cfg)]
    # 30-я и 60-я секунды готового ролика; вторая точка на шкале без баннеров раньше на длину первого
    t_a, t_b = cfg.get("two_banners_at", [30, 60])
    return [snap_to_pause(words, segs, float(t_a), 0.5), snap_to_pause(words, segs, float(t_b) - ban_dur, 0.5)]


# ---------- графика ----------
def rounded_mask(w, h, r, path):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w - 1, h - 1), r, fill=255)
    m.save(path)


def frame_png(boxes, accent, path):
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    for x, y, w, h, r in boxes:
        d.rounded_rectangle((x - 6, y + 12, x + w + 6, y + h + 30), r + 8, fill=(0, 0, 0, 170))
    im = Image.alpha_composite(im, sh.filter(ImageFilter.GaussianBlur(26)))
    d = ImageDraw.Draw(im)
    for x, y, w, h, r in boxes:
        d.rounded_rectangle((x - 4, y - 4, x + w + 3, y + h + 3), r + 3, outline=accent, width=6)
    im.save(path)


def ass_time(t):
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def split_title(title):
    words = [censor(w) or w for w in title.upper().split()]
    words = [w for w in words if w]
    if len(words) < 2:
        return " ".join(words), ""
    best, total = 1, len(" ".join(words))
    for i in range(1, len(words)):
        if abs(len(" ".join(words[:i])) - total / 2) < abs(len(" ".join(words[:best])) - total / 2):
            best = i
    return " ".join(words[:best]), " ".join(words[best:])


ACCENTS = ["#FFE600", "#00E5FF", "#7CFF2B", "#FF8A00", "#FF4FD8"]     # жёлтый, голубой, салатовый, оранжевый, розовый


def pick_accent(clip, cfg):
    """Цвет рамок и второй строки заголовка: свой для каждого ролика, один на все окна ролика."""
    import hashlib
    colors = cfg.get("accent_colors") or ACCENTS
    hx = colors[int(hashlib.md5(clip.stem.encode("utf-8")).hexdigest(), 16) % len(colors)].lstrip("#")
    return tuple(int(hx[i:i + 2], 16) for i in (0, 2, 4))


def write_ass(path, title, words, segs, split_out, ban_dur, total, sub_y, cfg,
              title_y=200, title_size=64, accent=(255, 230, 0), show_title=True, sub_style=None, tags=None):
    """sub_style: {"fg": цвет букв, "box": цвет плашки под словом или None}. tags: готовые подписи (ник и т.п.)."""
    bgr = lambda c: "&H00{:02X}{:02X}{:02X}".format(c[2], c[1], c[0])
    st_ = sub_style or {}
    s_fg = bgr(st_.get("fg") or (255, 230, 0))
    if st_.get("box"):
        s_tail = f"{bgr(st_['box'])},&H00000000,1,0,0,0,100,100,0,0,3,16,0,8,40,40,0,1"
    else:
        s_tail = "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,8,0,8,40,40,0,1"
    l1, l2 = split_title(title)
    longest = max(len(l1), len(l2), 1)
    tsize = title_size if longest <= 18 else max(36, int(title_size * 18 / longest))
    font, bold = font_face(cfg["font"])
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{tsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,6,0,8,{cfg.get('side_margin', 80)},{cfg.get('side_margin', 80)},{title_y},1
Style: Sub,{font},{cfg['sub_size']},{s_fg},{s_fg},{s_tail.replace(",1,0,0,0,100,", f",{bold},0,0,0,100,", 1)}
Style: Tag,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,3,10,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    acc_ass = "&H00{:02X}{:02X}{:02X}&".format(accent[2], accent[1], accent[0])       # в ASS порядок BGR
    ttext = l1 + (r"\N{\c" + acc_ass + "}" + l2 if l2 else "")
    if show_title:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Title,,0,0,0,,{ttext}")
    for tg in tags or []:
        lines.append(f"Dialogue: 0,{ass_time(0)},{ass_time(total)},Tag,,0,0,0,,{tg}")

    splits = list(split_out) if isinstance(split_out, (list, tuple)) else [split_out]
    starts = [sp + k * ban_dur for k, sp in enumerate(splits)]       # когда баннеры начинаются в готовом ролике

    def shift(t):
        o = src_to_out(t, segs)
        return o + ban_dur * sum(1 for sp in splits if o >= sp - 1e-6)

    anim = cfg.get("sub_anim") or "pop"
    timed = []                                       # (начало, конец, текст, пауза перед словом)
    for i, w in enumerate(words):
        txt = censor(w["t"]).upper()
        if not txt:
            continue
        s = shift(w["s"])
        e = shift(w["e"])
        # слово не висит в тишине: держим его не дольше, чем нужно, чтобы прочитать
        e = min(e, s + 0.40 + 0.06 * len(txt))
        if i + 1 < len(words):
            nxt = shift(words[i + 1]["s"])
            if 0 <= nxt - e < 0.20:        # следующее слово идёт сразу: держим до него без мигания
                e = nxt
            elif nxt < e:
                e = nxt
        for b0 in starts:                 # слово не должно висеть на баннере
            if s < b0 <= e:
                e = b0
        if e - s < 0.08:
            e = s + 0.08
        gap = (w["s"] - words[i - 1]["e"]) if i else 9.0
        timed.append((s, e, txt, gap, w["t"][-1:] in ".!?…,"))
    lines += sub_events(timed, anim, sub_y, cfg, st_, accent, starts)
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8-sig")


SUB_ANIMS = {"pop": "Выпрыгивание", "karaoke": "Фраза с подсветкой слова", "bounce": "Падение с отскоком",
             "wiggle": "Покачивание", "glow": "Неоновое свечение", "none": "Без субтитров"}


def sub_events(timed, anim, sub_y, cfg, st_, accent, banner_starts):
    """Строки субтитров для выбранной анимации. timed: [(начало, конец, слово, пауза перед ним, конец фразы)]."""
    bgr = lambda c: "&H{:02X}{:02X}{:02X}&".format(c[2], c[1], c[0])
    x, y, base = W // 2, sub_y, cfg["sub_size"]
    out = []
    if anim == "none":                               # ролик без субтитров: речь всё равно нужна для вырезки пауз
        return out
    if anim != "karaoke":
        for s, e, txt, _, _ in timed:
            size = base if len(txt) <= 11 else int(base * 11 / len(txt))
            if anim == "bounce":
                fx = (rf"{{\move({x},{y - 80},{x},{y},0,150)\fs{size}\fscx118\fscy118\t(0,150,\fscx100\fscy100)"
                      rf"\t(150,220,\fscx104\fscy92)\t(220,300,\fscx100\fscy100)}}")
            elif anim == "wiggle":
                fx = (rf"{{\pos({x},{y})\fs{size}\fscx88\fscy88\frz-7\t(0,110,\fscx100\fscy100\frz5)"
                      rf"\t(110,220,\frz-3)\t(220,330,\frz0)}}")
            elif anim == "glow":
                fx = (rf"{{\pos({x},{y})\fs{size}\bord6\3c{bgr(accent)}\blur4\fscx85\fscy85"
                      rf"\t(0,90,\fscx100\fscy100)\t(0,250,\blur2)}}")
            else:
                fx = rf"{{\pos({x},{y})\fs{size}\fscx85\fscy85\t(0,90,\fscx100\fscy100)}}"
            out.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,{fx}{txt}")
        return out
    # фраза из 2–4 слов на экране, слово, которое сейчас звучит, крупнее и другого цвета
    chunks, cur = [], []
    for item in timed:
        s, e, txt, gap, endp = item
        if cur and (gap > 0.45 or len(cur) >= 4 or sum(len(t[2]) for t in cur) + len(txt) > 17
                    or any(cur[-1][0] < b0 <= s for b0 in banner_starts)):
            chunks.append(cur)
            cur = []
        cur.append(item)
        if endp:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    fg = st_.get("fg") or (255, 230, 0)
    box = st_.get("box")
    if box:                                          # плашки: у звучащего слова своя яркая плашка и тёмные буквы
        hl_box = (255, 230, 0) if abs(box[0] - 255) + abs(box[1] - 230) + abs(box[2]) > 120 else (255, 255, 255)
        hl, rest = (10, 10, 10), fg
    else:
        hl_box = None
        hl = fg if sum(fg) < 700 else (255, 230, 0)
        rest = (255, 255, 255)
    for ch in chunks:
        chars = sum(len(t[2]) for t in ch) + len(ch) - 1
        size = base if chars <= 13 else max(54, int(base * 13 / chars))
        end_all = ch[-1][1]
        for k, (s, e, txt, _, _) in enumerate(ch):
            e2 = ch[k + 1][0] if k + 1 < len(ch) else end_all
            if e2 - s < 0.05:
                continue
            parts = []
            for j, t in enumerate(ch):
                if j == k:
                    bx = rf"\3c{bgr(hl_box)}" if hl_box else ""
                    parts.append(rf"{{\c{bgr(hl)}{bx}\fscx100\fscy100\t(0,80,\fscx112\fscy112)}}{t[2]}")
                else:
                    bx = rf"\3c{bgr(box)}" if box else ""
                    parts.append(rf"{{\c{bgr(rest)}{bx}\fscx100\fscy100}}{t[2]}")
            intro = rf"\fscx90\fscy90\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            fx = rf"{{\pos({x},{y})\fs{size}{intro}}}"
            out.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e2)},Sub,,0,0,0,,{fx}" + " ".join(parts))
    return out


# ---------- потенциал ролика ----------
def potential(p, res, share):
    """Оценка 0-100: насколько ролик похож на тот, что набирает просмотры.
    Пока это формула из признаков; после накопления статистики веса подстроим по реальным просмотрам."""
    parts = []
    if res.get("ai_score") is not None:
        parts.append((0.50, max(0.0, min(1.0, (float(res["ai_score"]) - 1) / 9))))
    else:
        parts.append((0.50, 0.5))      # без оценки ИИ считаем содержание средним
    parts.append((0.15, min(1.0, math.log10(p.get("twitch_views", 0) + 1) / 3)))
    parts.append((0.15, min(1.0, res.get("speech_share", 0) / 0.6)))
    content = res["duration"] - res["banner_len"]
    fit = 1.0 if 12 <= content <= 35 else 0.7 if content <= 50 else 0.4
    if content < 12:
        fit = 0.5
    parts.append((0.10, fit))
    if share is not None:
        parts.append((0.10, min(1.0, share / 0.6)))
    total = sum(w for w, _ in parts)
    return int(round(100 * sum(w * v for w, v in parts) / total))


def potential_label(x):
    return "высокий" if x >= 75 else "средний" if x >= 50 else "низкий"


# ---------- отправка в Telegram ----------
TG_TOKEN = BASE / "telegram_token.txt"
TG_CHAT = BASE / "telegram_chat.txt"


def tg_chat_id(token, fresh=False):
    """Чат, куда слать ролики: тот, кто последним написал боту. Запоминается вместе с ботом:
    если вставили токен другого бота, чат ищется заново."""
    bot = token.split(":")[0]
    if TG_CHAT.exists() and not fresh:
        saved = TG_CHAT.read_text(encoding="utf-8-sig").strip()
        if ":" not in saved and saved:
            return saved                         # старый формат: чат уже работал, оставляем
        if saved.split(":", 1)[0] == bot:
            return saved.split(":", 1)[1]
    import requests
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    if not r.get("ok"):
        print(f"  Telegram не принял токен: {r.get('description', '')}")
        return None
    chats = [(u.get("message") or u.get("my_chat_member") or {}).get("chat", {}).get("id") for u in r.get("result", [])]
    chats = [c for c in chats if c]
    if not chats:
        print("  Telegram: бот не видит твой чат. Открой бота в Telegram, нажми «Запустить» или напиши /start, "
              "потом в программе «Настройки и ключи» → «Проверить отправку»")
        return None
    TG_CHAT.write_text(f"{bot}:{chats[-1]}", encoding="utf-8")
    return str(chats[-1])


def tg_test(token):
    """Пробная отправка: находит чат с ботом заново и шлёт сообщение. (успех, текст для окна)."""
    import requests
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
        if not me.get("ok"):
            return False, "Telegram не принял токен: проверь, что скопирован целиком"
        if TG_TOPICS.exists():
            t = json.loads(TG_TOPICS.read_text(encoding="utf-8"))
            chat, thread = t["chat_id"], t.get("clips")
        else:
            chat, thread = tg_chat_id(token, fresh=True), None
        if not chat:
            return False, (f"Бот @{me['result'].get('username', '')} не видит твой чат: открой его в Telegram, "
                           "нажми «Запустить» (/start) и нажми проверку ещё раз")
        data = {"chat_id": chat, "text": "PRODAEM CLIPPER: проверка связи. Сюда будут приходить готовые ролики ✓"}
        if thread:
            data["message_thread_id"] = thread
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20).json()
        if r.get("ok"):
            return True, f"Отправил проверочное сообщение от @{me['result'].get('username', '')}: посмотри в Telegram"
        return False, "Telegram не дал отправить: " + str(r.get("description", ""))[:160]
    except Exception as e:
        return False, f"нет связи с Telegram: {e}"


TG_TOPICS = BASE / "telegram_topics.json"


def tg_setup():
    """Настраивает раздельную отправку в темы группы. Темы ты создаёшь сам, с любыми названиями:
    в теме для клипов отправь команду /clips, в теме для своего видео отправь /my,
    потом запусти: python montage.py --tg-setup"""
    import requests
    if not TG_TOKEN.exists():
        sys.exit("Нет файла telegram_token.txt")
    token = TG_TOKEN.read_text(encoding="utf-8-sig").strip()
    api = f"https://api.telegram.org/bot{token}"
    upd = requests.get(f"{api}/getUpdates", timeout=30).json().get("result", [])
    group, found, names = None, {}, {}
    for u in upd:
        msg = u.get("message") or {}
        chat = msg.get("chat") or (u.get("my_chat_member") or {}).get("chat") or {}
        if chat.get("type") != "supergroup":
            continue
        group = chat
        thread = msg.get("message_thread_id")
        text = (msg.get("text") or "").lower()
        if not thread:
            continue
        topic = (msg.get("reply_to_message") or {}).get("forum_topic_created") or msg.get("forum_topic_created") or {}
        if topic.get("name"):
            names[thread] = topic["name"]
        if text.startswith("/clips"):
            found["clips"] = thread
        elif text.startswith("/my"):
            found["manual"] = thread
    if not group:
        sys.exit("Не вижу группу. Добавь бота в группу администратором и отправь команды в темах: "
                 "/clips в теме для клипов и /my в теме для своего видео.")
    if not group.get("is_forum"):
        sys.exit(f"В группе «{group.get('title')}» не включены темы.")
    missing = [k for k in ("clips", "manual") if k not in found]
    if missing:
        need = {"clips": "/clips в теме для клипов", "manual": "/my в теме для своего видео"}
        sys.exit("Не хватает команды: " + " и ".join(need[k] for k in missing)
                 + ". Отправь её в нужной теме и запусти настройку ещё раз.")
    if found["clips"] == found["manual"]:
        print("Обе команды в одной теме: клипы и своё видео будут приходить туда вместе.")
    saved = {"chat_id": group["id"], "clips": found["clips"], "manual": found["manual"]}
    TG_TOPICS.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    for k, label in (("clips", "клипы"), ("manual", "своё видео")):
        requests.post(f"{api}/sendMessage", data={"chat_id": group["id"], "message_thread_id": found[k],
                                                 "text": f"Сюда будут приходить: {label}"}, timeout=30)
    print(f"Готово. Группа «{group.get('title')}»: клипы пойдут в тему «{names.get(found['clips'], 'с командой /clips')}», "
          f"своё видео в тему «{names.get(found['manual'], 'с командой /my')}».")


def tg_send(path, p, hashtags, kind="clips"):
    """Отправляет готовый ролик файлом (без сжатия). kind: "clips" для найденных клипов,
    "manual" для своего видео; если настроены темы, каждый вид идёт в свою. True, если ушло."""
    if not TG_TOKEN.exists():
        print("  в Telegram не отправлено: не введён токен бота (Настройки и ключи → Telegram)")
        return False
    try:
        import requests
        token = TG_TOKEN.read_text(encoding="utf-8-sig").strip()
        thread = None
        if TG_TOPICS.exists():
            topics = json.loads(TG_TOPICS.read_text(encoding="utf-8"))
            chat, thread = topics["chat_id"], topics.get(kind)
        else:
            chat = tg_chat_id(token)
        if not chat:
            print("  в Telegram не отправлено: бот не знает, куда слать")
            return False
        size = Path(path).stat().st_size / 1e6
        if size > 49:
            print(f"  в Telegram не отправлено: ролик {size:.0f} МБ, а бот может отправить только до 50 МБ")
            return False
        warn = "ВНИМАНИЕ: на экране игра, партнёрка может не засчитать\n" if p.get("game_on_screen") else ""
        cap = (warn + f"Потенциал: {p['potential']}/100 ({potential_label(p['potential'])})\n"
               f"Стример: {p['streamer']} | {p['format']} | {p['duration']} с\n\n"
               f"{p['final_title']}\n{p.get('description') or ''}\n\n{hashtags}")[:1000]
        name = re.sub(r'[\\/:*?"<>|]+', " ", f"{p['potential']:03d}_{p['streamer']}_{p['final_title']}")[:80] + ".mp4"
        data = {"chat_id": chat, "caption": cap}
        if thread:
            data["message_thread_id"] = thread
        for attempt in range(1, 5):              # связь с Telegram иногда рвётся посреди загрузки: пробуем ещё
            try:
                with open(path, "rb") as f:
                    r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                      data=data, files={"document": (name, f, "video/mp4")}, timeout=600)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 4:
                    raise
                print(f"  Telegram: связь оборвалась, попытка {attempt + 1} из 4 через {attempt * 10} с")
                time.sleep(attempt * 10)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 4:
                    break
                wait = 15
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 15)) + 1
                except Exception:
                    pass
                print(f"  Telegram занят ({r.status_code}), жду {wait} с")
                time.sleep(wait)
                continue
            break
        if r.status_code != 200:
            print(f"  Telegram ответил {r.status_code}: {r.text[:200]}")
            return False
        if attempt > 1:
            print("  Telegram: отправлено со второй попытки" if attempt == 2 else f"  Telegram: отправлено с попытки {attempt}")
        return True
    except Exception as e:
        print(f"  в Telegram не отправилось: {e}")
        return False


# ---------- кодировщик ----------
def pick_encoder(cfg):
    if cfg["encoder"] != "auto":
        return cfg["encoder"]
    r = run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=640x360:d=0.2",
             "-c:v", "h264_amf", "-f", "null", "-"])
    return "h264_amf" if r.returncode == 0 else "libx264"


def encoder_args(enc):
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                "-qp_i", "18", "-qp_p", "20", "-qp_b", "22"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


# ---------- сборка одного ролика ----------
# ---------- стили монтажа ----------
STYLES = {"classic": "Рамка", "neon": "Неон", "gradient": "Градиент", "efir": "Эфир", "glass": "Стекло",
          "split": "Сплит", "sticker": "Стикер", "full": "На весь экран"}
GEO_STYLES = ("split", "sticker", "full")        # этим стилям нужны два окна: камера и игра/контент
SYS_FONTS = ["Arial", "Arial Black", "Impact", "Bahnschrift", "Segoe UI Black", "Comic Sans MS", "Verdana",
             "Trebuchet MS", "Tahoma", "Georgia", "Courier New"]
# шрифты как в трендовых роликах: бесплатные (лицензия OFL), все с русскими буквами.
# Скачиваются с Google Fonts при первом запуске в папку fonts.
FONT_LIB = {"Montserrat Black": ("Montserrat", 900), "Montserrat ExtraBold": ("Montserrat", 800),
            "Rubik Black": ("Rubik", 900), "Rubik Bold": ("Rubik", 700), "Golos Text Black": ("Golos Text", 900),
            "Unbounded Black": ("Unbounded", 900), "Russo One": ("Russo One", 400), "Oswald Bold": ("Oswald", 700),
            "Dela Gothic One": ("Dela Gothic One", 400), "Exo 2 Black": ("Exo 2", 900), "Inter Black": ("Inter", 900),
            "Manrope ExtraBold": ("Manrope", 800), "Anton": ("Anton", 400), "Comfortaa Bold": ("Comfortaa", 700),
            "Ubuntu Bold": ("Ubuntu", 700), "Lobster": ("Lobster", 400), "Pacifico": ("Pacifico", 400),
            "Press Start 2P": ("Press Start 2P", 400)}
FONT_DIR = BASE / "fonts"
FONT_MAP = BASE / "fonts_map.json"            # имя в программе -> имя шрифта внутри файла (не в папке fonts:
                                              # оттуда ffmpeg читает только сами шрифты)


def _font_map():
    try:
        return json.loads(FONT_MAP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def has_cyrillic(path):
    """Есть ли в шрифте русские буквы: сравниваем «Ж» с заведомо пустым символом."""
    from PIL import ImageFont
    f = ImageFont.truetype(str(path), 60)
    return f.getmask("Ж").getbbox() != f.getmask("\U000FFFFD").getbbox() and \
        bytes(f.getmask("Ж")) != bytes(f.getmask("\U000FFFFD"))


def get_font(key, log=None):
    """Скачивает шрифт из библиотеки, если его ещё нет. Возвращает имя для субтитров или None."""
    mp = _font_map()
    if key in mp and (FONT_DIR / mp[key]["file"]).exists():
        return mp[key]["name"] if mp[key].get("ok") else None
    if key not in FONT_LIB or time.time() - _FONT_FAIL.get(key, 0) < 600:
        return None                                  # недавно не скачался: не ждём сеть на каждом примере
    fam, wt = FONT_LIB[key]
    try:
        import requests
        css = requests.get(f"https://fonts.googleapis.com/css?family={fam.replace(' ', '+')}:{wt}&subset=cyrillic,latin",
                           headers={"User-Agent": "Mozilla/4.0"}, timeout=20).text
        urls = re.findall(r"url\((https://[^)]+?\.ttf)\)", css)
        if not urls:
            raise RuntimeError("Google Fonts не дал файл")
        data = requests.get(urls[0], timeout=60).content
        if data[:4] not in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
            raise RuntimeError("скачался не шрифт")
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        fn = re.sub(r"\W+", "_", key) + ".ttf"
        (FONT_DIR / fn).write_bytes(data)
        from PIL import ImageFont
        family, style = ImageFont.truetype(str(FONT_DIR / fn), 20).getname()
        name = family if (style or "Regular") in ("Regular", "Normal", "") or style in family else f"{family} {style}"
        ok = has_cyrillic(FONT_DIR / fn)
        mp = _font_map()
        mp[key] = {"file": fn, "name": name, "ok": ok}
        FONT_MAP.write_text(json.dumps(mp, ensure_ascii=False, indent=1), encoding="utf-8")
        if log:
            log(f"шрифт {key}: " + ("готов" if ok else "без русских букв, не используется"))
        return name if ok else None
    except Exception as e:
        _FONT_FAIL[key] = time.time()
        if log:
            log(f"шрифт {key} не скачался: {e}")
        return None


_FONT_FAIL = {}


def ensure_fonts(log=None):
    for k in FONT_LIB:
        get_font(k, log)


def available_fonts():
    """Шрифты для выбора: скачанные из библиотеки (с русскими буквами) и системные."""
    mp = _font_map()
    lib = [k for k in FONT_LIB if not (k in mp and not mp[k].get("ok"))]
    return lib + SYS_FONTS


def font_face(key):
    """(имя для субтитров, жирность 0/1): у шрифтов из библиотеки жирность уже своя, поддельная не нужна."""
    if key in FONT_LIB:
        name = get_font(key)
        return (name, 0) if name else ("Arial", 1)
    return key, 1


FONTS = list(FONT_LIB) + SYS_FONTS


def _lin(c1, c2, size):
    g = Image.linear_gradient("L").rotate(-45).resize(size)
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), g)


def neon_seam(y, accent):
    """Тонкая неоновая линия на стыке окон: мягкая тень на обе картинки, свечение цвета профиля и яркая середина.
    Рисуется поверх видео (слой надписей), поэтому свечение ложится на камеру и игру."""
    def bgr(c):
        return "&H{:02X}{:02X}{:02X}&".format(c[2], c[1], c[0])
    light = tuple(int(v + (255 - v) * 0.6) for v in accent)

    def bar(h, blur, col, alpha):
        return ("{\\an7\\pos(0,%d)\\bord0\\shad0\\blur%g\\1c%s\\1a&H%02X&\\p1}m -40 0 l 1120 0 l 1120 %d l -40 %d{\\p0}"
                % (y - h // 2, blur, bgr(col), alpha, h, h))
    return [bar(76, 22, (0, 0, 0), 0x60),      # тень от линии на обе картинки
            bar(40, 20, accent, 0x28),         # широкое свечение
            bar(9, 5, accent, 0x00),           # плотное свечение
            bar(3, 0.7, light, 0x00)]          # яркая тонкая середина


def style_png(style, boxes, accent, path, rot=None, progress_y=None):
    """Подложка стиля: всё, что рисуется под окнами (рамки, свечение, панели, тени).
    boxes: [(x, y, w, h, радиус)], rot: {номер окна: угол наклона}."""
    rot = rot or {}
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def soft(fn, blur):
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        fn(ImageDraw.Draw(layer))
        return layer.filter(ImageFilter.GaussianBlur(blur))

    def shadows(d):
        for i, (x, y, w, h, r) in enumerate(boxes):
            if i not in rot:
                d.rounded_rectangle((x - 6, y + 12, x + w + 6, y + h + 30), r + 8, fill=(0, 0, 0, 170))

    if style == "neon":
        cols = [(0, 229, 255), (255, 60, 200)]
        for i, (x, y, w, h, r) in enumerate(boxes):
            c = cols[i % 2]
            g = soft(lambda d: d.rounded_rectangle((x - 6, y - 6, x + w + 5, y + h + 5), r + 6,
                                                   outline=c + (255,), width=16), 22)
            im = Image.alpha_composite(Image.alpha_composite(im, g), g)
        d = ImageDraw.Draw(im)
        for x, y, w, h, r in boxes:
            d.rounded_rectangle((x - 4, y - 4, x + w + 3, y + h + 3), r + 3, outline=(255, 255, 255, 255), width=5)
    elif style == "gradient":
        im = Image.new("RGBA", (W, H), (11, 12, 15, 255))

        def blobs(d):
            d.ellipse((-300, -200, 600, 700), fill=(124, 92, 255, 150))
            d.ellipse((500, 1200, 1400, 2100), fill=(0, 210, 255, 130))
            d.ellipse((600, 300, 1300, 900), fill=(255, 60, 200, 70))

        im = Image.alpha_composite(im, soft(blobs, 160))
        grad = _lin((124, 92, 255), (0, 229, 255), (W, H)).convert("RGBA")
        ring = Image.new("L", (W, H), 0)
        dr = ImageDraw.Draw(ring)
        for x, y, w, h, r in boxes:
            dr.rounded_rectangle((x - 12, y - 12, x + w + 11, y + h + 11), r + 10, fill=255)
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        glow.paste(grad, (0, 0), ring)
        im = Image.alpha_composite(im, glow.filter(ImageFilter.GaussianBlur(26)))
        im.paste(grad, (0, 0), ring)
    elif style == "efir":
        d = ImageDraw.Draw(im)
        ln, th = 70, 10
        for x, y, w, h, r in boxes:
            for cx, cy, sx, sy in ((x - 14, y - 14, 1, 1), (x + w + 13, y - 14, -1, 1),
                                   (x - 14, y + h + 13, 1, -1), (x + w + 13, y + h + 13, -1, -1)):
                d.line((cx, cy, cx + sx * ln, cy), fill=accent + (255,), width=th)
                d.line((cx, cy, cx, cy + sy * ln), fill=accent + (255,), width=th)
        if progress_y:
            d.rectangle((0, progress_y, W, progress_y + 11), fill=(255, 255, 255, 60))
    elif style == "glass":
        tint = _lin((90, 60, 255), (0, 200, 255), (W, H)).convert("RGBA")
        tint.putalpha(80)
        im = Image.alpha_composite(im, tint)
        x0, y0 = min(b[0] for b in boxes) - 40, min(b[1] for b in boxes) - 40
        x1, y1 = max(b[0] + b[2] for b in boxes) + 40, max(b[1] + b[3] for b in boxes) + 40
        im = Image.alpha_composite(im, soft(lambda d: d.rounded_rectangle((x0, y0 + 20, x1, y1 + 20), 64,
                                                                          fill=(0, 0, 0, 150)), 36))
        panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(panel).rounded_rectangle((x0, y0, x1, y1), 64, fill=(255, 255, 255, 52),
                                                outline=(255, 255, 255, 160), width=3)
        im = Image.alpha_composite(im, panel)
    elif style == "split":
        pass                                     # линия на стыке рисуется поверх окон (neon_seam)
    elif style == "full":
        col = Image.new("L", (1, H), 0)
        for yy in range(H):
            a = int(215 * min(1, (yy - 1150) / 520)) if yy > 1150 else (int(150 * (1 - yy / 420)) if yy < 420 else 0)
            col.putpixel((0, yy), a)
        dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        dark.putalpha(col.resize((W, H)))
        im = Image.alpha_composite(im, dark)
        im = Image.alpha_composite(im, soft(shadows, 30))
        d = ImageDraw.Draw(im)
        for x, y, w, h, r in boxes:
            d.rounded_rectangle((x - 6, y - 6, x + w + 5, y + h + 5), r + 6, fill=(255, 255, 255, 255))
    elif style == "sticker":
        im = Image.alpha_composite(im, soft(shadows, 26))
        for i, (x, y, w, h, r) in enumerate(boxes):
            if i in rot:                         # тень под наклонённой карточкой камеры
                card = Image.new("RGBA", (w + 40, h + 40), (0, 0, 0, 0))
                ImageDraw.Draw(card).rounded_rectangle((0, 0, w + 39, h + 39), r + 14, fill=(0, 0, 0, 190))
                rc = card.rotate(rot[i], expand=True, resample=Image.BICUBIC)
                layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                layer.alpha_composite(rc, (x + w // 2 - rc.width // 2 + 14, y + h // 2 - rc.height // 2 + 24))
                im = Image.alpha_composite(im, layer.filter(ImageFilter.GaussianBlur(22)))
    else:
        frame_png(boxes, accent + (255,), path)
        return
    im.save(path)


def banner_list(cfg):
    """Все баннеры офферов: {название: {"file", "speed", "blur", "crop", "chroma"}}.
    Если списка ещё нет (старые настройки), он собирается из banner_talk и banner_cs."""
    lst = cfg.get("banners")
    if isinstance(lst, dict) and lst:
        return lst
    out = {}
    for fmt in ("cs", "talk"):
        f = cfg.get(f"banner_{fmt}")
        if not f:
            continue
        name = Path(str(f)).stem.split(".")[0]
        if name in out:
            continue
        out[name] = {"file": f, "speed": float(cfg.get("banner_speed", 1.0)),
                     "blur": cfg.get(f"banner_{fmt}_blur", 0) or 0, "crop": cfg.get(f"banner_{fmt}_crop"),
                     "chroma": cfg.get(f"banner_{fmt}_chroma", "auto")}
    return out


def banner_for(cfg, fmt, name=None):
    """Настройки баннера для ролика: выбранный в окне, иначе отмеченный как основной, иначе старые banner_talk/cs."""
    lst = banner_list(cfg)
    name = name or cfg.get("banner_use")
    if name in lst:
        b = dict(lst[name])
    else:
        b = {"file": cfg.get(f"banner_{fmt}"), "speed": float(cfg.get("banner_speed", 1.0)),
             "blur": cfg.get(f"banner_{fmt}_blur", 0) or 0, "crop": cfg.get(f"banner_{fmt}_crop"),
             "chroma": cfg.get(f"banner_{fmt}_chroma", "auto")}
    if not b.get("file"):
        raise RuntimeError("не выбран баннер оффера: добавь его кнопкой «Баннеры офферов…»")
    b["speed"] = min(2.0, max(0.5, float(b.get("speed") or 1.0)))
    b["blur"] = max(0, min(40, float(b.get("blur") or 0)))
    b.setdefault("chroma", "auto")
    return b


def banner_crop(b):
    """Какую часть кадра баннера брать: заданную вручную, весь кадр (size="full", по умолчанию)
    или, по-старому, среднюю полосу в четверть экрана (size="band")."""
    if b.get("crop"):
        return list(b["crop"])
    try:
        bi_ = probe(find_banner(b["file"]))
    except Exception:
        bi_ = {"w": 1920, "h": 1080}
    bw = bi_["w"] // 2 * 2
    if b.get("size", "full") != "band":
        return [0, 0, bw, bi_["h"] // 2 * 2]
    bh = min(bi_["h"], int(bw * 480 / W)) // 2 * 2
    return [0, (bi_["h"] - bh) // 2 // 2 * 2, bw, bh]


def banner_size(b):
    """Размер баннера в ролике (ширина, высота). Пропорции файла не меняются никогда.
    Целиком: баннер шире экрана уменьшается до ширины экрана, а меньший остаётся в своём родном размере.
    Полоса: растягивается на всю ширину, как раньше."""
    bx, by, bw, bh = banner_crop(b)
    if b.get("size", "full") == "band":
        ow = W
    else:
        ow = min(W, bw)
    oh = int(round(bh * ow / bw / 2)) * 2
    if oh > H * 0.6:                             # слишком высокий баннер (вертикальный файл) ужимаем по высоте
        oh = int(H * 0.6) // 2 * 2
        ow = int(round(bw * oh / bh / 2)) * 2
    return ow // 2 * 2, oh


def banner_filter(b, cfg, quiet=False):
    """Цепочка ffmpeg для кадра баннера (без ускорения): обрезка, размер, хромакей, размытие.
    Возвращает файл, (ширину, высоту) баннера в ролике и цепочку."""
    banner = find_banner(b["file"])
    bx, by, bw, bh = banner_crop(b)
    ow, oh = banner_size(b)
    key_ = banner_chroma(banner, b.get("chroma", "auto"))
    ck = ""
    if key_:                                     # вырезаем фон баннера и убираем цветную кайму по краям
        kr, kg, kb = int(key_[2:4], 16), int(key_[4:6], 16), int(key_[6:8], 16)
        sim = float(b.get("chroma_sim") or cfg.get("banner_chroma_sim", 0.12))
        sim = max(0.02, min(0.5, sim))
        # сравниваем по цвету целиком, а не только по оттенку: иначе тёмно-зелёный фон задевает серые плашки баннера
        ck = f",format=rgba,colorkey={key_}:{sim:.3f}:0.06"
        if kg > kr + 40 and kg > kb + 40:
            ck += ",despill=type=green"
        elif kb > kr + 40 and kb > kg + 40:
            ck += ",despill=type=blue"
        if not quiet:
            print(f"  баннер: вырезан фон цвета {key_}")
    bl = f",gblur=sigma={b['blur'] / 5:.2f}" if b.get("blur") else ""
    return banner, (ow, oh), f"crop={bw}:{bh}:{bx}:{by},scale={ow}:{oh}:flags=lanczos{ck},format=rgba{bl}"


def banner_preview(b, cfg, png, t=1.0):
    """Кадр баннера с текущими настройками поверх тёмного фона: для окна выбора баннера."""
    banner, (ow, oh), chain = banner_filter(b, cfg, quiet=True)
    dur = probe(banner)["dur"]
    t = max(0.0, min(t, dur - 0.1))
    # шахматка под баннером: сразу видно, где фон вырезан, а где нет
    r = run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"color=c=0x2a2d36:s={W}x{oh + 80},geq=lum='if(mod(floor(X/24)+floor(Y/24),2),62,40)':cb=128:cr=128",
             "-ss", f"{t:.2f}", "-i", str(banner), "-filter_complex",
             f"[1:v]{chain}[b];[0:v][b]overlay={(W - ow) // 2}:40:shortest=1,scale=540:-2", "-frames:v", "1", str(png)])
    if not Path(png).exists():
        raise RuntimeError("не получилось показать баннер: " + (r.stderr or "")[-300:])
    return {"dur": dur, "sec": round(dur / b["speed"], 1), "w": ow, "h": oh}


def find_banner(name):
    """Файл баннера в папке программы. Прощает лишнее расширение (banner.mov.mp4), приписку « (1)»
    и регистр букв. Если не нашёл, объясняет, что именно лежит в папке."""
    name = str(name).strip()
    p = Path(name) if Path(name).is_absolute() else BASE / name
    if p.is_file():
        return p
    stem = Path(name).stem.split(".")[0].lower()
    cands = sorted((f for f in BASE.iterdir() if f.is_file() and f.name.lower().startswith(stem)
                    and f.suffix.lower() in (".mov", ".mp4", ".webm", ".mkv")),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if cands:
        print(f"  баннер: беру файл {cands[0].name}")
        return cands[0]
    near = [f.name for f in BASE.iterdir() if f.is_file() and f.suffix.lower() in (".mov", ".mp4", ".webm", ".mkv")]
    raise RuntimeError(f"нет файла баннера {Path(name).name} в папке {BASE}. Видео в папке: "
                       + (", ".join(near) if near else "нет ни одного"))


def _banner_frames(banner):
    """Несколько кадров баннера (начало, середина, ближе к концу) маленького размера для анализа цвета."""
    CACHE.mkdir(exist_ok=True)
    try:
        dur = probe(banner)["dur"]
    except Exception:
        dur = 2.0
    out = []
    for t in (0.3, dur * 0.5, dur * 0.8):
        png = CACHE / f"chroma_{os.getpid()}_{threading.get_ident()}.png"
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, min(t, dur - 0.1)):.2f}", "-i", str(banner),
             "-frames:v", "1", "-vf", "scale=320:-2", str(png)])
        if png.exists():
            out.append(Image.open(png).convert("RGB"))
            png.unlink(missing_ok=True)
    return out


def _key_color(pix, want=None):
    """Самый частый насыщенный цвет среди пикселей. want="green"/"blue" — только такие оттенки."""
    from collections import Counter
    cnt = Counter()
    for r, g, b_ in pix:
        if want == "green" and not (g > r + 40 and g > b_ + 40):
            continue
        if want == "blue" and not (b_ > r + 40 and b_ > g + 30):
            continue
        if max(r, g, b_) - min(r, g, b_) < 80:
            continue
        cnt[(r // 12, g // 12, b_ // 12)] += 1
    if not cnt:
        return None, 0.0
    (qr, qg, qb), n = cnt.most_common(1)[0]
    sel = [c for c in pix if (c[0] // 12, c[1] // 12, c[2] // 12) == (qr, qg, qb)]
    med = [sorted(c[i] for c in sel)[len(sel) // 2] for i in range(3)]
    return med, n / max(1, len(pix))


def banner_chroma(banner, setting="auto"):
    """Цвет хромакея у баннера в виде 0xRRGGBB или None, если вырезать нечего.
    auto: если у файла нет своей прозрачности, а по краям кадра много ровного насыщенного цвета, он вырезается.
    green / blue: найти в баннере свой оттенок зелёного или синего и вырезать его.
    Можно задать цвет вручную, например "0x00FF00", или выключить: null / "off"."""
    if not setting or setting == "off":
        return None
    if setting not in ("auto", "green", "blue"):
        return "0x" + str(setting).lstrip("#").replace("0x", "").upper()[:6]
    key = (str(banner), Path(banner).stat().st_mtime if Path(banner).exists() else 0, setting)
    if key in _CHROMA_CACHE:
        return _CHROMA_CACHE[key]
    res = None
    if setting == "auto":
        fmt_ = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=pix_fmt",
                    "-of", "csv=p=0", str(banner)]).stdout.strip().lower()
        if "a" in fmt_.replace("gray", ""):      # rgba, yuva, argb, bgra: прозрачность уже есть
            _CHROMA_CACHE[key] = None
            return None
    best = (None, 0.0)
    for im in _banner_frames(banner):
        w, h = im.size
        px = im.load()
        if setting == "auto":                    # смотрим рамку кадра: хромакей почти всегда у краёв
            pix = [px[x, y] for x in range(w) for y in (0, 1, 2, h - 3, h - 2, h - 1)] + \
                  [px[x, y] for y in range(h) for x in (0, 1, 2, w - 3, w - 2, w - 1)]
        else:
            pix = [px[x, y] for x in range(0, w, 2) for y in range(0, h, 2)]
        med, frac = _key_color(pix, None if setting == "auto" else setting)
        if med and frac > best[1]:
            best = (med, frac)
    med, frac = best
    if med and (frac >= 0.35 if setting == "auto" else frac >= 0.01):
        res = "0x{:02X}{:02X}{:02X}".format(*med)
    elif setting == "green":
        res = "0x00FF00"
    elif setting == "blue":
        res = "0x0000FF"
    _CHROMA_CACHE[key] = res
    return res


_CHROMA_CACHE = {}


SATIS_DIR = BASE / "satisfying"


def pick_satis(clip, cfg, force=False):
    """Залипательное видео для нижнего окна: случайный файл из папки satisfying.
    В автоматическом режиме применяется к доле роликов из настройки satis_share (0 = никогда)."""
    files = sorted(f for f in SATIS_DIR.glob("*") if f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")) \
        if SATIS_DIR.exists() else []
    if not files:
        return None
    import hashlib
    hv = int(hashlib.md5(clip.stem.encode("utf-8")).hexdigest(), 16)
    if not force and (hv % 100) >= int(float(cfg.get("satis_share", 0)) * 100):
        return None
    return files[(hv // 100) % len(files)]


def montage(clip, fmt, streamer, title, out, cfg, enc, fixed_title=None, satis=None, show_title=True, force=None,
            preview=None):
    """preview={"t": секунда, "png": путь}: не монтировать, а быстро нарисовать один кадр с этой раскладкой."""
    CACHE.mkdir(exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    info = probe(clip)
    fps = 60 if info["fps"] > 45 else 30
    sw, sh = info["w"], info["h"]

    if preview:                                  # для примера речь не распознаём и баннер не вставляем
        words, ai, title = [], None, fixed_title or ""
        segs, content = [(0.0, info["dur"])], info["dur"]
        ban_dur, split_out, splits_src, total = 0.0, [], [], info["dur"]
    else:
        words = ai_fix_words(merge_hyphens(transcribe(clip, cfg)), clip, cfg)
        ai = ai_meta(words, streamer, cfg)
        title = fixed_title or (ai["title"] if ai else fix_title(title, words))
        segs = keep_segments(words, info["dur"], fmt, cfg, clip)
        cut = info["dur"] - sum(b - a for a, b in segs)
        if fmt != "talk" and cut > 0.5:
            print(f"  вырезано тишины: {cut:.1f} с (громкие моменты игры оставлены)")
        content = sum(b - a for a, b in segs)
        bnr = banner_for(cfg, fmt, (force or {}).get("banner"))
        banner = find_banner(bnr["file"])
        ban_speed = bnr["speed"]                                       # баннер можно ускорить
        print(f"  баннер: {Path(banner).name}, ускорение {ban_speed:g}x" + (f", размытие {bnr['blur']:g}" if bnr["blur"] else ""))
        ban_dur = probe(banner)["dur"] / ban_speed
        split_out = banner_splits(words, segs, content, ban_dur, cfg)     # один баннер или два
        splits_src = [out_to_src(x, segs) for x in split_out]
        total = content + ban_dur * len(split_out)
    try:
        _bp = banner_for(cfg, fmt, (force or {}).get("banner"))
        ban_w, ban_h = banner_size(_bp)
    except Exception:
        if not preview:
            raise
        ban_w, ban_h = W, 272                    # для примера кадра баннер не нужен, нужна только его высота

    # раскладка окон: список (что вырезать из исходника, куда поставить на экране, радиус углов)
    # разметка стримера (камера, обрезка, зона субтитров) сделана для обычного кадра стрима 16:9.
    # Если прислали видео другой формы (квадрат, вертикальное), она к нему не подходит.
    skey = streamer if abs(sw / sh - 16 / 9) < 0.06 else ""
    lay = {"mode": "talk", "cam": None, "share": None, "side": "mid"} if preview else detect_layout(clip, skey)
    user_content = None                          # область «что он смотрит», которую обвёл человек
    if force:
        # раскладку указал человек:
        #   {"mode": "cam", "rect": [x0,y0,x1,y1]}   камера в окошке, остальное это игра или то, что он смотрит
        #   {"mode": "full"}                         стример на весь кадр
        #   {"mode": "solo", "rect": [...]}          показать только обведённую область (один стример)
        #   {"mode": "multi", "rects": [[...], ...]} несколько стримеров, у каждого своё окно
        #   {"mode": "none"}                         камеры нет
        lay = dict(lay, cam=None, rect=None, cams_px=[], content_px=None, side="mid", mode="talk")

        def clean_rect(r):
            x0, y0, x1, y1 = [int(v) for v in r]
            return [max(0, min(x0, x1)), max(0, min(y0, y1)), min(sw, max(x0, x1)), min(sh, max(y0, y1))]

        if force["mode"] == "cam":
            fr_ = clean_rect(force["rect"])
            mid = (fr_[0] + fr_[2]) / 2 / sw
            lay.update(mode="react", rect=fr_, side="left" if mid < 0.4 else "right" if mid > 0.6 else "mid")
        elif force["mode"] == "solo":
            lay.update(mode="solo", cams_px=[clean_rect(force["rect"])])
        elif force["mode"] == "multi":
            lay.update(mode="multi", cams_px=[clean_rect(r) for r in force["rects"]][:4])
        if force.get("content"):
            uc = clean_rect(force["content"])
            if uc[2] - uc[0] >= 40 and uc[3] - uc[1] >= 40:
                user_content = uc
        if force.get("captions"):
            # человек отметил, где у стримера встроенные субтитры: всё режется мимо этой полосы,
            # и место запоминается за стримером
            cy0, cy1 = sorted(float(v) for v in force["captions"])
            lay["band"] = caption_band([0, 100 * cy0 / sh, 100, 100 * cy1 / sh], skey, sh)
        elif force.get("captions") is False:
            lay["band"] = None                    # человек сказал, что субтитров в кадре нет
    elif fmt == "talk" and lay["mode"] in ("talk", "none"):
        # у стримера размечена камера, и она уверенно нашлась в углу: значит это реакция, а не разговор
        forced = pick_cam(clip, skey, cfg, sw, min_score=2)
        if forced:
            mid = (forced[0] + forced[2] / 2) / sw
            lay = dict(lay, mode="react", cam=forced, rect=None,
                       side="left" if mid < 0.4 else "right" if mid > 0.6 else "mid")
    band = lay.get("band") or [0, sh]
    mode = lay["mode"] if fmt == "talk" else "cs"
    cams_px = lay.get("cams_px") or []
    content_px = lay.get("content_px")
    wins = []
    dyn, when, timeline = [], [], None      # окна, которые видны только часть ролика, и их интервалы
    stacked = True
    M = int(cfg.get("side_margin", 80))       # отступ окон от краёв экрана: ТикТок обрезает бока
    SW = (W - 2 * M) // 2 * 2
    GAME = (SW, 1152)                         # основное окно: 60% высоты экрана

    def centre_crop(avoid=True):
        bh_ = band[1] - band[0]
        cw = min(sw, int(bh_ * GAME[0] / GAME[1]))
        x0 = (sw - cw) // 2
        cam0 = lay.get("cam")
        if avoid and cam0:                   # отодвигаем окно от камеры, чтобы стример не дублировался
            rect = lay.get("rect")
            left_edge = rect[2] + 8 if rect else cam0[0] + cam0[2] + int(cam0[2] * 0.2)
            right_edge = rect[0] - 8 if rect else cam0[0] - int(cam0[2] * 0.2)
            if lay["side"] == "left":
                x0 = min(sw - cw, max(x0, left_edge))
            elif lay["side"] == "right":
                x0 = max(0, min(x0, right_edge - cw))
        return [x0 // 2 * 2, band[0] // 2 * 2, cw // 2 * 2, bh_ // 2 * 2]

    if mode == "multi":
        n = len(cams_px)
        if content_px:                        # камеры в ряд сверху, содержимое в большом окне снизу
            wn = (SW - 20 * (n - 1)) // n // 2 * 2
            for i, box in enumerate(cams_px):
                wins.append((fit_crop(box, wn / 420, face_center(clip, box)), (M + i * (wn + 20), 250, wn, 420), 30))
            wins.append((fit_crop(content_px, GAME[0] / GAME[1], None, 0.5), (M, 700) + GAME, 48))
            sub_y, ban_cy = 700 + 640, 700 + GAME[1] // 2
        else:
            hw = (SW - 20) // 2 // 2 * 2
            dst = {2: [(M, 250, SW, 570), (M, 850, SW, 570)],
                   3: [(M, 250, SW, 374), (M, 646, SW, 374), (M, 1042, SW, 374)]}.get(
                n, [(M, 250, hw, 574), (M + hw + 20, 250, hw, 574), (M, 846, hw, 574), (M + hw + 20, 846, hw, 574)])
            for box, d in zip(cams_px, dst):
                wins.append((fit_crop(box, d[2] / d[3], face_center(clip, box)), d, 40))
            sub_y, ban_cy = 1440, 835
    elif mode in ("react", "cs"):
        if force:
            cam = fit_crop(lay["rect"], SW / 420, face_center(clip, lay["rect"])) if force["mode"] == "cam" else None
            lay["cam"] = cam
        else:
            cam = pick_cam(clip, skey, cfg, sw)
            timeline = cam_timeline(clip, skey, cfg, sw, info["dur"])
            if timeline:
                cam = timeline[0][2]
        marked = bool(cfg["cams"].get(skey))
        if not cam and mode == "react" and not (marked and not force):
            cam = lay["cam"]          # у стримера ещё нет разметки: берём то, что нашёл автопоиск
        if not cam and mode == "cs" and cfg.get("cs_auto_cam", True) and not force and not marked:
            # размеченного места нет или оно не подошло к этой сцене: берём камеру, которую нашёл Claude
            # или локальный поиск. Обе проходят строгую проверку (живое лицо на неподвижном фоне),
            # поэтому значки, радар и аватарки сюда не попадают.
            cam = lay.get("cam") or (fit_crop(cams_px[0], SW / 420, face_center(clip, cams_px[0])) if cams_px else None)
        if mode == "react" and content_px:
            main_crop = fit_crop(content_px, GAME[0] / GAME[1], None, 0.5)
            rect = lay.get("rect") or ([cam[0], cam[1], cam[0] + cam[2], cam[1] + cam[3]] if cam else None)
            if rect:                         # окно контента не должно захватывать камеру стримера
                cw_ = main_crop[2]
                left = (content_px[0], rect[0] - 8)          # свободная полоса слева от камеры
                right = (rect[2] + 8, content_px[2])         # и справа от неё
                overlap = not (main_crop[0] + cw_ <= rect[0] or main_crop[0] >= rect[2])
                best = max((left, right), key=lambda iv: iv[1] - iv[0])
                if overlap and best[1] - best[0] >= cw_:
                    centre = (content_px[0] + content_px[2]) / 2
                    x = min(max(centre - cw_ / 2, best[0]), best[1] - cw_)
                    main_crop[0] = int(x) // 2 * 2
        else:
            main_crop = centre_crop(avoid=(mode == "react"))
        if cam and timeline:
            # камера переезжает между сценами: в одном окне по очереди показываем нужное место
            def to_out(t):
                o = src_to_out(t, segs)
                return o + ban_dur * sum(1 for sp in split_out if o >= sp - 1e-6)
            for a_, b_, box in timeline:
                dyn.append(len(wins))
                wins.append((box, (M, 250, SW, 420), 36))
                when.append((to_out(a_), to_out(b_) if b_ < info["dur"] - 0.01 else total + 1))
            wins.append((main_crop, (M, 700) + GAME, 48))
            sub_y, ban_cy = 700 + 640, 700 + GAME[1] // 2
        elif cam and user_content:
            # контент обвёл человек: показываем ровно его, окно подстраивается по высоте под форму области
            ucw, uch = user_content[2] - user_content[0], user_content[3] - user_content[1]
            # окно делаем крупнее, чем форма области: по бокам она подрезается, но не больше чем на треть
            keep = min(1.0, max(0.4, float(cfg.get("content_keep", 0.65))))
            gh_ = min(GAME[1], max(300, int(SW * uch / ucw / keep) // 2 * 2))
            wins.append((cam, (M, 250, SW, 420), 36))
            wins.append(([user_content[0] // 2 * 2, user_content[1] // 2 * 2, ucw // 2 * 2, uch // 2 * 2],
                         (M, 700, SW, gh_), 48))
            sub_y, ban_cy = (700 + 640 if gh_ > 900 else 700 + gh_ + 40), 700 + gh_ // 2
        elif cam:
            wins.append((cam, (M, 250, SW, 420), 36))
            wins.append((main_crop, (M, 700) + GAME, 48))
            sub_y, ban_cy = 700 + 640, 700 + GAME[1] // 2
        elif mode == "cs" and user_content:
            ucw, uch = user_content[2] - user_content[0], user_content[3] - user_content[1]
            keep = min(1.0, max(0.4, float(cfg.get("content_keep", 0.65))))
            gh_ = min(1200, max(300, int(960 * uch / ucw / keep) // 2 * 2))
            wins.append(([user_content[0] // 2 * 2, user_content[1] // 2 * 2, ucw // 2 * 2, uch // 2 * 2],
                         (60, 300, 960, gh_), 48))
            sub_y, ban_cy = (300 + 1000 if gh_ > 1000 else 300 + gh_ + 40), 300 + gh_ // 2
        elif mode == "cs":
            # КС без камеры: одно большое окно с игрой по центру прицела, голос стримера остаётся
            bh_ = band[1] - band[0]
            cw_ = min(sw, int(bh_ * 960 / 1200))
            wins.append(([(sw - cw_) // 2 // 2 * 2, band[0] // 2 * 2, cw_ // 2 * 2, bh_ // 2 * 2], (60, 300, 960, 1200), 48))
            sub_y, ban_cy = 300 + 1200 - 200, 300 + 600
        else:
            # камеры нет: одно окно как в разговорном формате
            stacked = False
            bh_ = band[1] - band[0]
            cw_ = min(sw, int(bh_ * 960 / 924))
            wins.append(([(sw - cw_) // 2 // 2 * 2, band[0] // 2 * 2, cw_ // 2 * 2, bh_ // 2 * 2], (60, 420, 960, 924), 48))
            sub_y, ban_cy = 420 + 924 + 45, 420 + 924 // 2
    else:                                     # стример на весь кадр или одна камера без содержимого
        stacked = False
        if mode == "solo" and cams_px:
            crop = fit_crop(cams_px[0], 960 / 924, face_center(clip, cams_px[0]))
        elif (lay.get("band") or not skey) and not cfg["crops"].get(skey):
            full = [0, band[0], sw, band[1]]        # окно строим вокруг лица
            crop = fit_crop(full, 960 / 924, face_center(clip, full))
        else:
            crop = talk_crop(skey, sw, sh, cfg)
        if satis:
            # стример сверху, залипательное видео снизу, субтитры на стыке окон
            stacked = True
            top_box = cams_px[0] if (mode == "solo" and cams_px) else [0, band[0], sw, band[1]]
            # камера 60% высоты блока, залипательное видео 40%, субтитры на стыке
            wins.append((fit_crop(top_box, SW / 800, face_center(clip, top_box)), (M, 250, SW, 800), 44))
            sub_y, ban_cy = 250 + 800 - 30, H // 2            # баннер строго по центру экрана
        else:
            wins.append((crop, (60, 420, 960, 924), 48))
            sub_y, ban_cy = 420 + 924 + 45, 420 + 924 // 2
    if not (mode in ("talk", "solo", "none") and fmt == "talk"):
        satis = None                              # формат только для роликов, где в кадре один стример
    SAT = (M, 1080, SW, 530)
    title_y = cfg.get("title_y_cs", 95) if stacked else cfg["title_y"]
    title_size = 56 if stacked else 64
    wins = [(to_ratio(c, d[2] / d[3]), d, r) for c, d, r in wins]      # без растяжения картинки
    cam_first = next((c for c, d, r in wins if d[3] == 420 and d[1] == 250), None)    # какое место взято под камеру
    if not show_title and stacked:               # заголовка нет: поднимаем окна на освободившееся место
        up = 100
        wins = [(c, (d[0], d[1] - up, d[2], d[3]), r) for c, d, r in wins]
        SAT = (SAT[0], SAT[1] - up, SAT[2], SAT[3])
        sub_y -= up
        if not satis:
            ban_cy -= up
    # реклама размывается, только если это небольшая область: окно со стримером закрывать нельзя
    content_i = len(wins) - 1 if ((mode in ("react", "cs") and stacked) or (mode == "multi" and content_px)) else None
    ads = []
    for ad in lay.get("ads_px") or []:
        safe = True
        for i, (c, d, r) in enumerate(wins):
            ix = max(0, min(ad[2], c[0] + c[2]) - max(ad[0], c[0]))
            iy = max(0, min(ad[3], c[1] + c[3]) - max(ad[1], c[1]))
            if ix * iy > (0.35 if i == content_i else 0.15) * c[2] * c[3]:
                safe = False
        if safe:
            ads.append(ad)

    import hashlib
    accent = pick_accent(clip, cfg)

    # ---- стиль монтажа и шрифт ----
    style = (force or {}).get("style") or cfg.get("style", "classic")
    if style == "random":                        # свой стиль у каждого ролика, при пересборке тот же
        names = list(STYLES)
        style = names[int(hashlib.md5(("st" + clip.stem).encode("utf-8")).hexdigest(), 16) % len(names)]
    if style not in STYLES:
        style = "classic"
    if (force or {}).get("font"):
        cfg = dict(cfg, font=force["font"])
    if (force or {}).get("sub_anim"):
        cfg = dict(cfg, sub_anim=force["sub_anim"])
    two = len(wins) == 2 and cam_first is not None and not dyn and not satis and stacked
    if style in GEO_STYLES and not two:
        style = "classic"                        # этим стилям нужны два окна: камера и игра
    rot, bg_mode, bg_dark, bg_crop = {}, "blur", -0.22, None
    sub_style, tags, progress_y = None, [], None
    nick = re.sub(r"[^\w-]", "", streamer or "").upper()[:16]
    acc_bgr = "&H{:02X}{:02X}{:02X}&".format(accent[2], accent[1], accent[0])
    if two and style in ("split", "sticker", "full", "glass"):
        cam_c, game_c = wins[0][0], wins[1][0]
        top = wins[0][1][1]                      # 150 без заголовка, 250 с заголовком
        if style == "split":                     # встык на всю ширину, на стыке тонкая неоновая линия
            seam = top + 524
            gh = 1870 - (seam + 4)
            wins = [(cam_c, (0, top, 1080, 524), 0), (game_c, (0, seam + 4, 1080, gh), 0)]
            sub_y, ban_cy, bg_mode = seam - 58, seam + 4 + gh // 2, "black"
            tags += neon_seam(seam + 2, accent)
        elif style == "full":                    # игра во весь кадр, камера карточкой сверху
            bg_crop, bg_mode = to_ratio(game_c, 1080 / 1920), "full"
            wins = [(cam_c, (150, top - 10, 780, 346), 44)]
            sub_y, ban_cy = 1250, 960
        elif style == "sticker":                 # камера наклонённой карточкой поверх игры
            wins = [(game_c, (70, top + 300, 940, 1180), 44), (cam_c, (160, top - 10, 760, 340), 26)]
            rot = {1: 5}
            sub_y, ban_cy = top + 300 + 700, top + 300 + 590
            if nick:
                tags.append("{\\an5\\pos(%d,%d)\\frz-6\\fs46\\c&H000000&\\3c%s}%s" % (790, top + 350, acc_bgr, nick))
        else:                                    # стекло: субтитры между окнами, игру не закрывают
            wins = [(cam_c, (80, top, 920, 400), 40), (game_c, (80, top + 600, 920, 1000), 40)]
            sub_y, ban_cy = top + 455, top + 600 + 500
        wins = [(to_ratio(c, d[2] / d[3]), d, r) for c, d, r in wins]
    if style == "neon":
        bg_dark, sub_style = -0.36, {"fg": (255, 255, 255), "box": (255, 60, 200)}
    elif style == "gradient":
        sub_style = {"fg": (255, 110, 220)}
    elif style == "efir":
        bg_dark, sub_style = -0.38, {"fg": (255, 255, 255)}
        wins = [(c, d, 18) for c, d, r in wins]
        low = max([d[1] + d[3] for _, d, _ in wins] + ([SAT[1] + SAT[3]] if satis else []))
        progress_y = low + 34 if low + 50 < H else None
        if progress_y and sub_y - 30 < progress_y < sub_y + 120:      # полоска не должна лечь на субтитры
            progress_y = sub_y + 135
        if nick and cam_first is not None and wins:
            cd = wins[0][1]
            tags.append("{\\an4\\pos(%d,%d)\\fs34\\3c&H000000&\\3a&H50&\\c&H3028FF&}●{\\c&HFFFFFF&} %s"
                        % (cd[0] + 26, cd[1] + 48, nick))
    elif style == "glass":
        sub_style = {"fg": (20, 20, 30), "box": (255, 255, 255)}
    elif style == "split":
        sub_style = {"fg": (0, 0, 0), "box": accent}
    elif style in ("full", "sticker"):
        sub_style = {"fg": accent}

    all_boxes = list(dict.fromkeys(d + (r,) for _, d, r in wins)) + ([SAT + (44,)] if satis else [])
    key = hashlib.md5(repr([style, all_boxes, accent, rot, progress_y]).encode()).hexdigest()[:10]
    frame = CACHE / f"frame_{key}.png"
    if not frame.exists():
        style_png(style, all_boxes, accent, frame, rot, progress_y)
    masks = []
    for i, (_, d, r) in enumerate(wins):
        mw, mh, mr = (d[2] + 40, d[3] + 40, r + 14) if i in rot else (d[2], d[3], r)
        mpath = CACHE / f"mask_{mw}x{mh}_{mr}.png"
        if not mpath.exists():
            rounded_mask(mw, mh, mr, mpath)
        masks.append(mpath)

    def bg_chain():
        if bg_mode == "full":
            return f"[b]crop={bg_crop[2]}:{bg_crop[3]}:{bg_crop[0]}:{bg_crop[1]},scale=1080:1920,setsar=1[bg]"
        chain = ("[b]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,gblur=sigma=16,"
                 f"scale=1080:1920:flags=bilinear,eq=brightness={bg_dark}:saturation=1.25")
        if bg_mode == "black":
            chain += ",drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill"
        return chain + "[bg]"

    def win_chain(i, mask_in, en=""):
        crop, d, r = wins[i]
        pad = f",pad={d[2] + 40}:{d[3] + 40}:20:20:color=white" if i in rot else ""
        zm = ""
        if ZOOM.get("expr") and i == ZOOM["win"]:      # плавный зум и тряска окна на громких моментах
            z, sx, sy = ZOOM["expr"]
            zm = (f",scale=w='2*trunc({d[2]}*({z})/2)':h='2*trunc({d[3]}*({z})/2)':eval=frame,"
                  f"crop={d[2]}:{d[3]}:'clip((iw-ow)/2+{sx}\\,0\\,iw-ow)':'clip((ih-oh)/2+{sy}\\,0\\,ih-oh)'")
        out_ = [f"[s{i}]crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},scale={d[2]}:{d[3]}{zm}{pad},format=rgba[w{i}a]",
                f"[w{i}a][{mask_in}:v]alphamerge[w{i}b]"]
        if i in rot:                             # наклонённое окно ставим по центру своего места
            ang = f"{-rot[i]}*PI/180"
            out_.append(f"[w{i}b]rotate={ang}:c=none:ow=rotw({ang}):oh=roth({ang})[w{i}c]")
            out_.append(f"[l{i}][w{i}c]overlay=x={d[0] + d[2] // 2}-w/2:y={d[1] + d[3] // 2}-h/2{en}[l{i + 1}]")
        else:
            out_.append(f"[l{i}][w{i}b]overlay={d[0]}:{d[1]}{en}[l{i + 1}]")
        return out_

    ZOOM = {"expr": None, "win": 1 if (style == "sticker" and len(wins) > 1) else 0}
    want_zoom = (force or {}).get("zoom", cfg.get("zoom", True))
    if want_zoom and not dyn:
        try:
            ev = zoom_events(clip)
            if preview and preview.get("video"):         # пример в движении: время от его начала
                t0 = float(preview.get("t", 0))
                ev = [(t - t0, a, k) for t, a, k in ev if t0 <= t < t0 + float(preview["video"])]
                ZOOM["expr"] = zoom_expr(ev)
            elif not preview:                          # готовый ролик: время с учётом вырезанных пауз и баннеров
                out_ev = []
                for t, a, k in ev:
                    if not any(sa <= t <= sb for sa, sb in segs):
                        continue
                    o = src_to_out(t, segs)
                    o += ban_dur * sum(1 for sp in split_out if o >= sp - 1e-6)
                    out_ev.append((o, a, k))
                ZOOM["expr"] = zoom_expr(out_ev)
                if out_ev:
                    print(f"  зумов на эмоциях: {len(out_ev)}" + (f", с тряской: {sum(1 for x in out_ev if x[2])}"
                                                                  if any(x[2] for x in out_ev) else ""))
        except Exception as e:
            print(f"  зумы пропущены: {e}")
    sub_auto = sub_y                                 # где субтитры встали бы сами
    if force and force.get("sub_y"):                 # положение субтитров задал человек
        sub_y = int(force["sub_y"])
    ass = CACHE / f"subs_{os.getpid()}_{threading.get_ident()}.ass"   # у примера и монтажа свои файлы
    if preview:
        # один кадр с этой раскладкой: окна, пример субтитров и место, куда встанет баннер.
        # preview["video"] = секунды: вместо кадра короткий ролик со звуком, чтобы посмотреть пример в движении
        vdur = float(preview.get("video") or 0)
        sd = vdur or 1.0
        sample = [{"s": 0.0, "e": sd, "t": "субтитры"}]
        if cfg.get("sub_anim") == "karaoke":      # подсветка видна только на фразе из нескольких слов
            step = sd / 3
            sample = [{"s": i * step, "e": (i + 1) * step - 0.02, "t": t} for i, t in enumerate(("вот", "это", "момент"))]
        cached = clip.with_suffix(".words.json")
        if vdur and cached.exists():             # клип уже распознавали: в примере настоящие субтитры
            try:
                t0 = float(preview.get("t", 0))
                real = [dict(w, s=w["s"] - t0, e=w["e"] - t0) for w in json.loads(cached.read_text(encoding="utf-8"))
                        if t0 <= w["s"] < t0 + vdur]
                sample = real or sample
            except Exception:
                pass
        write_ass(ass, title, sample, [(0.0, sd)], [], 0.0, sd, sub_y, cfg, title_y, title_size, accent,
                  show_title and bool(title), sub_style, tags)
        n_w = len(wins)
        pf = [f"[0:v]{'fps=30,' if vdur else ''}split={n_w + 1}[b]" + "".join(f"[s{i}]" for i in range(n_w)), bg_chain(),
              "[bg][1:v]overlay=0:0:shortest=1[l0]" if vdur else "[bg][1:v]overlay=0:0[l0]"]
        for i in range(n_w):
            pf += win_chain(i, 2 + i)
        last = f"l{n_w}"
        loop = ["-loop", "1"] if vdur else []
        pcmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{float(preview.get('t', 0)):.2f}"] + \
               (["-t", f"{vdur:.2f}"] if vdur else []) + ["-i", str(clip)] + loop + ["-i", str(frame)]
        for mpath in masks:
            pcmd += loop + ["-i", str(mpath)]
        if satis:
            smask = CACHE / f"mask_{SAT[2]}x{SAT[3]}_44.png"
            if not smask.exists():
                rounded_mask(SAT[2], SAT[3], 44, smask)
            pcmd += ["-ss", "3", "-i", str(satis)] + loop + ["-i", str(smask)]
            ns = 2 + n_w
            pf.append(f"[{ns}:v]scale={SAT[2]}:{SAT[3]}:force_original_aspect_ratio=increase,"
                      f"crop={SAT[2]}:{SAT[3]},setsar=1,format=rgba[sat0]")
            pf.append(f"[sat0][{ns + 1}:v]alphamerge[sat1]")
            pf.append(f"[{last}][sat1]overlay={SAT[0]}:{SAT[1]}[lsat]")
            last = "lsat"
        by0 = max(0, min(H - ban_h, ban_cy - ban_h // 2))
        if progress_y:                           # полоска времени: в примере показана на 40%
            pf.append(f"[{last}]drawbox=x=0:y={progress_y}:w=432:h=12:color=0x{accent[0]:02X}{accent[1]:02X}{accent[2]:02X}:t=fill[lpb]")
            last = "lpb"
        box = "" if vdur else f"drawbox=x={(W - ban_w) // 2}:y={by0}:w={ban_w}:h={ban_h}:color=white@0.55:t=6,"
        pf.append(f"[{last}]{box}ass={ass.name}:fontsdir=../fonts,scale=540:960[vout]")
        if vdur:
            outp = str(Path(preview["video_out"]).resolve())
            r = run(pcmd + ["-filter_complex", ";".join(pf), "-map", "[vout]", "-map", "0:a?", "-t", f"{vdur:.2f}",
                            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-r", "30",
                            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", outp], cwd=str(CACHE))
            ass.unlink(missing_ok=True)
            if r.returncode != 0:
                print(r.stderr[-600:])
                return None
            return {"video": outp, "sub_y": sub_y, "sub_auto": sub_auto, "style": style}
        r = run(pcmd + ["-filter_complex", ";".join(pf), "-map", "[vout]", "-frames:v", "1",
                        str(Path(preview["png"]).resolve())], cwd=str(CACHE))
        ass.unlink(missing_ok=True)
        if r.returncode != 0:
            print(r.stderr[-600:])
            return None
        return {"png": str(preview["png"]), "sub_y": sub_y, "sub_auto": sub_auto, "style": style}
    write_ass(ass, title, words, segs, split_out, ban_dur, total, sub_y, cfg, title_y, title_size, accent, show_title,
              sub_style, tags)

    # громкость баннера под уровень клипа
    gain = max(-12.0, min(15.0, loudness(clip) - loudness(banner) + cfg["banner_gain_db"]))

    # куски ролика между баннерами
    groups = [[] for _ in range(len(splits_src) + 1)]
    for a, b in segs:
        cur = a
        for gi, sp in enumerate(splits_src):
            if cur < sp < b:
                groups[gi].append((cur, sp))
                cur = sp
        gi = sum(1 for sp in splits_src if cur >= sp - 1e-6)
        if b - cur > 0.02:
            groups[gi].append((cur, b))
    if any(not g for g in groups):
        sys.exit("Клип слишком короткий для баннера")

    fc = []
    inputs = []

    def part(parts, tag):
        vs, as_ = "", ""
        for i, (a, b) in enumerate(parts):
            n = len(inputs) // 6
            inputs.extend(["-ss", f"{a:.4f}", "-t", f"{b - a:.4f}", "-i", str(clip)])
            fc.append(f"[{n}:v]setpts=PTS-STARTPTS,fps={fps}[{tag}v{i}]")
            fc.append(f"[{n}:a]asetpts=PTS-STARTPTS,"
                      f"aresample=48000,aformat=channel_layouts=stereo,"
                      f"afade=t=in:d=0.012,afade=t=out:st={max(0, b - a - 0.012):.4f}:d=0.012[{tag}a{i}]")
            vs += f"[{tag}v{i}]"
            as_ += f"[{tag}a{i}]"
        fc.append(f"{vs}concat=n={len(parts)}:v=1:a=0[{tag}v]")
        fc.append(f"{as_}concat=n={len(parts)}:v=0:a=1[{tag}a]")

    for gi, g in enumerate(groups):
        part(g, f"g{gi}")
    nb = len(inputs) // 6          # номер входа с баннером
    nban = len(groups) - 1
    ban_at = [sp + k * ban_dur for k, sp in enumerate(split_out)]     # начало каждого баннера в готовом ролике
    t1 = ban_at[0]
    vchain = ""
    for gi in range(len(groups)):
        if gi < nban:                                                 # стоп-кадр под баннер
            fc.append(f"[g{gi}v]tpad=stop_mode=clone:stop_duration={ban_dur:.4f}[g{gi}f]")
            vchain += f"[g{gi}f]"
        else:
            vchain += f"[g{gi}v]"
    fc.append(f"{vchain}concat=n={len(groups)}:v=1:a=0[cv]")
    sp_a = f"atempo={ban_speed:.3f}," if abs(ban_speed - 1) > 0.01 else ""
    sp_v = f"setpts=PTS/{ban_speed:.3f}," if abs(ban_speed - 1) > 0.01 else ""
    fc.append(f"[{nb}:a]{sp_a}aresample=48000,aformat=channel_layouts=stereo,volume={gain:.1f}dB,"
              f"alimiter=limit=0.89,apad=whole_dur={ban_dur:.4f},atrim=0:{ban_dur:.4f}"
              + (f",asplit={nban}" + "".join(f"[ba{k}]" for k in range(nban)) if nban > 1 else "[ba0]"))
    achain = "".join(f"[g{gi}a]" + (f"[ba{gi}]" if gi < nban else "") for gi in range(len(groups)))
    fc.append(f"{achain}concat=n={len(groups) + nban}:v=0:a=1[aout]")

    src = "cv"
    for i, (ax0, ay0, ax1, ay1) in enumerate(ads):                           # размываем чужую рекламу
        aw, ah = (ax1 - ax0) // 2 * 2, (ay1 - ay0) // 2 * 2
        if aw < 8 or ah < 8:
            continue
        fc.append(f"[{src}]split=2[ad{i}m][ad{i}c]")
        fc.append(f"[ad{i}c]crop={aw}:{ah}:{ax0}:{ay0},scale={max(2, aw // 24)}:{max(2, ah // 24)},"
                  f"scale={aw}:{ah}:flags=bilinear,gblur=sigma=6[ad{i}b]")
        fc.append(f"[ad{i}m][ad{i}b]overlay={ax0}:{ay0}[cvb{i}]")
        src = f"cvb{i}"
    n_w = len(wins)
    fc.append(f"[{src}]split={n_w + 1}[b]" + "".join(f"[s{i}]" for i in range(n_w)))
    fc.append(bg_chain())
    fc.append(f"[bg][{nb + 1}:v]overlay=0:0[l0]")
    for i in range(n_w):
        en = ""
        if i in dyn:
            a_, b_ = when[dyn.index(i)]
            en = f":enable='between(t,{a_:.3f},{b_:.3f})'"
        fc += win_chain(i, nb + 2 + i, en)
    last = f"l{n_w}"
    if satis:                                    # нижнее окно: залипательное видео по кругу, без звука
        ns = nb + 2 + n_w                        # на время каждого баннера оно замирает вместе со стримером
        chain_s = ""
        for k in range(nban + 1):
            fc.append(f"[{ns + k}:v]scale={SAT[2]}:{SAT[3]}:force_original_aspect_ratio=increase,"
                      f"crop={SAT[2]}:{SAT[3]},fps={fps},setsar=1,setpts=PTS-STARTPTS"
                      + (f",tpad=stop_mode=clone:stop_duration={ban_dur:.4f}" if k < nban else "") + f"[sp{k}]")
            chain_s += f"[sp{k}]"
        fc.append(f"{chain_s}concat=n={nban + 1}:v=1:a=0,format=rgba[sat0]")
        fc.append(f"[sat0][{ns + nban + 1}:v]alphamerge[sat1]")
        fc.append(f"[{last}][sat1]overlay={SAT[0]}:{SAT[1]}[lsat]")
        last = "lsat"
    if progress_y:                               # полоска времени заполняется по ходу ролика
        fc.append(f"color=c=0x{accent[0]:02X}{accent[1]:02X}{accent[2]:02X}:s={W}x12:r={fps}[pbar]")
        fc.append(f"[{last}][pbar]overlay=x='-w+w*t/{total:.3f}':y={progress_y}:shortest=1[lpb]")
        last = "lpb"
    ban_y = max(0, min(H - ban_h, ban_cy - ban_h // 2))
    ban_x = (W - ban_w) // 2
    _, _, ban_chain = banner_filter(bnr, cfg)
    fc.append(f"[{nb}:v]{ban_chain},{sp_v}fps={fps}"
              + (f",split={nban}" + "".join(f"[bs{k}]" for k in range(nban)) if nban > 1 else "[bs0]"))
    for k, b0 in enumerate(ban_at):
        # до своего момента баннер идёт прозрачными кадрами: так основное видео не зависит от того,
        # как конкретная версия ffmpeg ведёт себя, пока второй поток ещё не начался
        fc.append(f"[bs{k}]setpts=PTS-STARTPTS,tpad=start_duration={b0:.4f}:start_mode=add:color=black@0.0[bv{k}]")
        fc.append(f"[{last}][bv{k}]overlay={ban_x}:{ban_y}:enable='between(t,{b0:.4f},{b0 + ban_dur:.4f})':"
                  f"eof_action=pass[wb{k}]")
        last = f"wb{k}"
    fc.append(f"[{last}]null[wb]")
    fc.append(f"[wb]ass={ass.name}:fontsdir=../fonts,format=yuv420p[vout]")

    tmp = CACHE / f"render_{os.getpid()}_{threading.get_ident()}.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y"] + inputs + ["-i", str(banner),
           "-loop", "1", "-framerate", str(fps), "-i", str(frame)]
    for mpath in masks:
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(mpath)]
    if satis:
        smask = CACHE / f"mask_{SAT[2]}x{SAT[3]}_44.png"
        if not smask.exists():
            rounded_mask(SAT[2], SAT[3], 44, smask)
        import hashlib as _h
        sdur = max(1.0, probe(satis)["dur"] - 0.2)
        offset = int(_h.md5(clip.stem.encode("utf-8")).hexdigest(), 16) % 7      # разное начало у разных роликов
        marks = [0.0] + list(split_out) + [content]
        for k in range(len(marks) - 1):          # кусок залипательного видео на каждый отрезок между баннерами
            cmd += ["-stream_loop", "-1", "-ss", f"{(offset + marks[k]) % sdur:.3f}",
                    "-t", f"{marks[k + 1] - marks[k] + 0.2:.3f}", "-i", str(satis)]
        cmd += ["-loop", "1", "-framerate", str(fps), "-i", str(smask)]
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-t", f"{total:.3f}", "-r", str(fps)] + encoder_args(enc) + [
            "-c:a", "aac", "-b:a", "192k", "-flags:a", "+bitexact", str(tmp)]
    r = run(cmd, cwd=str(CACHE))
    if r.returncode != 0:
        if enc == "h264_amf":
            why = [x for x in (r.stderr or "").strip().splitlines() if x.strip()][-1:] if r is not None else []
            print("  кодировщик видеокарты не сработал, пробую обычный" + (f" ({why[0][:150]})" if why else ""))
            return montage(clip, fmt, streamer, title, out, cfg, "libx264", fixed_title, satis, show_title, force)
        print(r.stderr[-1500:])
        return None
    # самопроверка: картинка должна идти с первой секунды и до конца, иначе ролик в работу не отдаём
    chk = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=start_time,duration", "-of", "csv=p=0", str(tmp)]).stdout.strip().split(",")
    try:
        v_start, v_dur = float(chk[0]), float(chk[1])
    except (ValueError, IndexError):
        v_start, v_dur = 0.0, total
    if v_start > 0.2 or v_dur < total - 1.0:
        print(f"  сборка вышла с браком: видео начинается с {v_start:.1f} с, длится {v_dur:.1f} из {total:.1f} с")
        if enc == "h264_amf":
            print("  пересобираю обычным кодировщиком")
            return montage(clip, fmt, streamer, title, out, cfg, "libx264", fixed_title, satis, show_title, force)
        tmp.unlink(missing_ok=True)
        return None
    # вторым проходом без перекодирования убираем все служебные метки
    clean = ["ffmpeg", "-v", "error", "-y", "-i", str(tmp), "-map", "0:v", "-map", "0:a",
             "-c", "copy", "-map_metadata", "-1", "-map_chapters", "-1",
             "-fflags", "+bitexact", "-bsf:v", "filter_units=remove_types=6",
             "-movflags", "+faststart", str(out.resolve())]
    r = run(clean)
    tmp.unlink(missing_ok=True)
    ass.unlink(missing_ok=True)
    if r.returncode != 0:
        print(r.stderr[-800:])
        return None
    return {"layout": mode + ("+satis" if satis else ""), "style": style, "cam_used": cam_first,
            "src_w": sw, "final_title": title, "description": ai["description"] if ai else "",
            "ai_score": ai["score"] if ai else None, "title_by": "claude" if ai else "auto",
            "duration": round(total, 2), "banner_at": round(t1, 2), "banners": nban,
            "banner_len": round(ban_dur * nban, 2), "cut_sec": round(info["dur"] - content, 2),
            "words": len(words), "speech_share": round(
                sum(w["e"] - w["s"] for w in words) / max(content, 0.1), 2)}


def refix(json_path, force, remember, cfg, enc, send=True):
    """Пересобирает готовый ролик с камерой, которую указал человек, и по желанию запоминает
    это место для стримера, чтобы следующие клипы из такой же сцены собирались правильно."""
    j = Path(json_path)
    p = json.loads(j.read_text(encoding="utf-8"))
    raw = Path(p.get("file", ""))
    raw = raw if raw.is_absolute() else BASE / raw
    if not raw.exists():
        print("  исходник уже удалён, пересобрать не из чего")
        return None
    fmt = p.get("format") if p.get("format") in ("talk", "cs") else "talk"
    manual = p.get("source") == "manual"
    streamer = p.get("streamer", "")
    streamer = "" if streamer == "стример" else streamer
    out = j.with_suffix(".mp4")
    print(f"{streamer or raw.name}: пересборка с новой камерой")
    res = montage(raw, fmt, streamer, p.get("title", ""), out, cfg, enc, fixed_title=p.get("final_title") or None,
                  show_title=manual or bool(cfg.get("clip_titles", False)), force=force)
    if not res:
        print("  не получилось пересобрать")
        return None
    p.update(res)
    hashtags = (f"#{streamer} " if streamer else "") + base_tags()
    if "potential" not in p:
        p["potential"] = 50
    p["tg_sent"] = tg_send(out, p, hashtags, "manual" if manual else "clips") if send else False
    j.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    if remember and force["mode"] == "cam" and res.get("cam_used") and streamer:
        k = 1920 / res["src_w"]
        box = [int(v * k) // 2 * 2 for v in res["cam_used"]]
        c = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        cur = c["cams"].get(streamer) or []
        if cur and isinstance(cur[0], (int, float)):
            cur = [cur]
        if not any(abs(o[0] - box[0]) < 60 and abs(o[1] - box[1]) < 60 and abs(o[2] - box[2]) < 80 for o in cur):
            cur.append(box)
            c["cams"][streamer] = cur
            CFG_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  место камеры запомнено для {streamer}: {box}")
    print("  готово" + (", отправлен в Telegram" if p["tg_sent"] else ""))
    return out


def process_file(clip, fmt, streamer, title, send, cfg, enc, satis=False):
    """Монтирует любой свой файл в нашем стиле. Заголовок: свой, если задан, иначе от Claude.
    Готовый ролик кладётся в clips/ready/manual и по желанию уходит в Telegram."""
    clip = Path(clip).resolve()
    streamer = (streamer or "").strip().lower()
    out = BASE / "clips" / "ready" / "manual" / (re.sub(r'[\\/:*?"<>|]+', " ", clip.stem)[:60] + ".mp4")
    print(f"{clip.name}: монтаж ({'КС' if fmt == 'cs' else 'стример'})")
    res = montage(clip, fmt, streamer, title or clip.stem, out, cfg, enc, fixed_title=(title or "").strip() or None,
                  satis=pick_satis(clip, cfg, force=True) if satis else None)
    if not res:
        print("  не получилось смонтировать")
        return None
    p = {"clip_id": clip.stem, "streamer": streamer or "стример", "title": title or clip.stem, "format": fmt,
         "twitch_views": 0, "file": str(clip), "source": "manual"}
    p.update(res)
    lay = detect_layout(clip, streamer)
    p["potential"] = potential(p, res, lay.get("share"))
    p["game_on_screen"] = bool(lay.get("game")) and fmt == "talk"
    hashtags = (f"#{streamer} " if streamer else "") + base_tags()
    out.with_suffix(".txt").write_text((res["description"] or res["final_title"]) + f"\n\n{hashtags}\n", encoding="utf-8")
    p["tg_sent"] = tg_send(out, p, hashtags, "manual") if send else False
    out.with_suffix(".json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  заголовок: {res['final_title']}")
    print(f"  потенциал: {p['potential']}/100 ({potential_label(p['potential'])})"
          + (", отправлен в Telegram" if p["tg_sent"] else ""))
    print(f"  готово: {out}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", nargs="?")
    ap.add_argument("--format", choices=["talk", "cs"], default="talk")
    ap.add_argument("--streamer", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0, help="сколько роликов сделать за запуск")
    ap.add_argument("--resend", action="store_true", help="дослать в Telegram готовые ролики, которые ещё не отправлены")
    ap.add_argument("--find", default="", help="найти исходник готового ролика по словам из заголовка")
    ap.add_argument("--grid", default="", help="сохранить кадры с сеткой координат из исходника ролика (по словам из заголовка)")
    ap.add_argument("--tg-setup", action="store_true", help="настроить отправку в две темы группы Telegram")
    ap.add_argument("--satis", action="store_true", help="залипательное видео в нижнем окне (папка satisfying)")
    ap.add_argument("--grids", action="store_true", help="сохранить по кадру с сеткой координат для каждого стримера КС")
    a = ap.parse_args()
    if a.tg_setup:
        tg_setup()
        return
    if a.grid:
        import cv2
        outdir = BASE / "clips" / "grids"
        outdir.mkdir(parents=True, exist_ok=True)
        made = 0
        for j in sorted((BASE / "clips" / "ready").glob("*/*.json")):
            p = json.loads(j.read_text(encoding="utf-8"))
            if a.grid.lower() not in (p.get("final_title", "") + " " + p.get("streamer", "")).lower():
                continue
            raw = Path(p.get("file", ""))
            raw = raw if raw.is_absolute() else BASE / raw
            if not raw.exists():
                print(f"{p.get('final_title')}: исходник уже удалён")
                continue
            cap = cv2.VideoCapture(str(raw))
            n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            for k, frac in enumerate((0.15, 0.5, 0.85)):
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * frac))
                ok, f = cap.read()
                if not ok:
                    continue
                h, w = f.shape[:2]
                for x in range(0, w, 100):
                    cv2.line(f, (x, 0), (x, h), (0, 255, 255), 1)
                    cv2.putText(f, str(x), (x + 3, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                for y in range(0, h, 100):
                    cv2.line(f, (0, y), (w, y), (0, 255, 255), 1)
                    cv2.putText(f, str(y), (3, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                name = f"{p.get('streamer')}_{re.sub(r'[^0-9A-Za-z]+', '', p.get('clip_id', ''))[:10]}_{k + 1}.jpg"
                cv2.imwrite(str(outdir / name), f, [cv2.IMWRITE_JPEG_QUALITY, 80])
                made += 1
            cap.release()
            print(f"{p.get('streamer')}: {p.get('final_title')}  (три кадра с сеткой)")
        print(f"Сохранено кадров: {made}. Папка: {outdir}")
        if made and sys.platform == "win32":
            subprocess.run(["explorer", str(outdir)])
        return
    if a.find:
        hits = 0
        for j in sorted((BASE / "clips" / "ready").glob("*/*.json")):
            p = json.loads(j.read_text(encoding="utf-8"))
            if a.find.lower() in (p.get("final_title", "") + " " + p.get("streamer", "")).lower():
                raw = Path(p.get("file", ""))
                raw = raw if raw.is_absolute() else BASE / raw
                print(f"{p.get('streamer')}: {p.get('final_title')}\n  исходник: {raw}" + ("" if raw.exists() else "  (файл уже удалён)"))
                hits += 1
                if raw.exists() and sys.platform == "win32":
                    subprocess.run(["explorer", "/select,", str(raw)])
        print(f"Найдено: {hits}")
        return
    cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    enc = pick_encoder(cfg)
    print("Кодировщик:", enc)

    if a.clip:
        clip = Path(a.clip).resolve()
        if a.out:
            res = montage(clip, a.format, a.streamer.lower(), a.title or clip.stem, Path(a.out), cfg, enc,
                          fixed_title=a.title or None, satis=pick_satis(clip, cfg, force=True) if a.satis else None)
            print("Готово:" if res else "Ошибка:", a.out, res or "")
        else:
            process_file(clip, a.format, a.streamer, a.title, True, cfg, enc, a.satis)
        return

    if a.grids:
        import cv2
        outdir = BASE / "clips" / "grids"
        outdir.mkdir(parents=True, exist_ok=True)
        done_s = set()
        for line in reversed(PASSPORTS.read_text(encoding="utf-8").splitlines()):
            p = json.loads(line)
            clip = BASE / p["file"]
            if p["format"] != "cs" or p["streamer"] in done_s or not clip.exists():
                continue
            cap = cv2.VideoCapture(str(clip))
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) // 2)
            ok, f = cap.read()
            cap.release()
            if not ok:
                continue
            h, w = f.shape[:2]
            for x in range(0, w, 100):
                cv2.line(f, (x, 0), (x, h), (0, 255, 255), 1)
                cv2.putText(f, str(x), (x + 3, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
            for y in range(0, h, 100):
                cv2.line(f, (0, y), (w, y), (0, 255, 255), 1)
                cv2.putText(f, str(y), (3, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
            cv2.imwrite(str(outdir / f"{p['streamer']}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 80])
            done_s.add(p["streamer"])
            print("сохранён кадр:", p["streamer"])
        print(f"Кадров: {len(done_s)}. Папка: {outdir}")
        return

    if a.resend:
        sent = 0
        for j in sorted((BASE / "clips" / "ready").glob("*/*.json")):
            p = json.loads(j.read_text(encoding="utf-8"))
            video = j.with_suffix(".mp4")
            if p.get("tg_sent") or not video.exists():
                continue
            kind = "manual" if p.get("source") == "manual" or p.get("format") == "top" else "clips"
            if tg_send(video, p, f"#{p['streamer']} " + base_tags(), kind):
                p["tg_sent"] = True
                j.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
                sent += 1
                print("отправлен:", p["final_title"])
        print(f"Дослано роликов: {sent}")
        return

    if not PASSPORTS.exists():
        sys.exit("Нет passports.jsonl. Сначала запусти clipfinder.py")
    done = 0
    passports = [json.loads(x) for x in PASSPORTS.read_text(encoding="utf-8").splitlines() if x.strip()]
    passports.sort(key=lambda p: (p.get("found_at", ""), p.get("twitch_views", 0)), reverse=True)
    for p in passports:
        clip = BASE / p["file"]
        out = BASE / "clips" / "ready" / p["format"] / f"{p['streamer']}_{p['clip_id'][:16]}.mp4"
        if out.exists() or not clip.exists():
            continue
        if (p.get("duration") or 99) < cfg.get("min_duration", 10):
            continue                       # слишком короткие клипы не берём
        if a.limit and done >= a.limit:
            break
        skip = out.with_suffix(".skip")
        if skip.exists():
            continue
        lay = detect_layout(clip, p["streamer"])
        share = lay["share"]
        if p["format"] == "talk" and lay["mode"] == "none":
            out.parent.mkdir(parents=True, exist_ok=True)
            skip.write_text("стримера нет в кадре", encoding="utf-8")
            print(f"{p['streamer']}: {p['title'][:50]}\n  пропущен: стримера нет в кадре")
            continue
        print(f"{p['streamer']}: {p['title'][:50]}")
        res = montage(clip, p["format"], p["streamer"], p["title"], out, cfg, enc,
                      satis=pick_satis(clip, cfg) if p["format"] == "talk" else None,
                      show_title=bool(cfg.get("clip_titles", False)))     # на клипах Twitch заголовок не рисуем
        if not res:
            continue
        p.update(res)
        p["face_share"] = share
        p["game_on_screen"] = bool(lay.get("game")) and p["format"] == "talk"
        p["on_screen"] = lay.get("what", "")
        p["potential"] = potential(p, res, share)
        hashtags = f"#{p['streamer']} " + base_tags()
        l1, l2 = split_title(res["final_title"])
        out.with_suffix(".txt").write_text(
            (res["description"] or f"{l1} {l2}".strip().capitalize()) + f"\n\n{hashtags}\n",
            encoding="utf-8")
        p["tg_sent"] = False
        if p["potential"] >= cfg.get("min_potential_send", 0):
            p["tg_sent"] = tg_send(out, p, hashtags)
        out.with_suffix(".json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        done += 1
        print(f"  заголовок: {res['final_title']}")
        print(f"  потенциал: {p['potential']}/100 ({potential_label(p['potential'])})"
              + (", отправлен в Telegram" if p["tg_sent"] else "")
              + {"react": " | формат: реакция", "multi": " | формат: несколько камер",
                 "solo": " | формат: одна камера"}.get(res["layout"], "")
              + (" | залипательное видео" if res["layout"].endswith("+satis") else "")
              + (" | НА ЭКРАНЕ ИГРА" if p["game_on_screen"] else ""))
        print(f"  готово: {res['duration']} с, вырезано {res['cut_sec']} с, баннер на {res['banner_at']} с"
              + (" (и второй на 60-й секунде)" if res.get("banners", 1) > 1 else ""))
    print(f"\nСмонтировано новых роликов: {done}. Папка: {BASE / 'clips' / 'ready'}")


if __name__ == "__main__":
    main()
