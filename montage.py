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
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
BASE = Path(__file__).resolve().parent
CFG_FILE = BASE / "montage.json"
PASSPORTS = BASE / "passports.jsonl"
CACHE = BASE / "cache"
W, H = 1080, 1920

BAD = re.compile(
    r"^(?:за|на|по|вы|у|до|от|отъ|разъ|объ|при|про|недо|долбо|съ|въ|подъ|пере|ни|не)?"
    r"(?:ху[йеёяию]|пизд|[её]б(?:а|у|и|л|н|ё|е|ыр|т)|бля(?:$|[дт])|сук[аиуе]$|сучк|"
    r"мудак|мудил|пид[оа]р|залуп|гандон|шлюх)", re.I)


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
    if len(core) >= 3 and BAD.search(core.lower()):
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
    return " ".join(phrase) if len(phrase) >= 2 else " ".join(tw)


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
                                "content": PROMPT.format(streamer=streamer or "неизвестен", text=text)}]},
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
def keep_segments(words, dur, fmt, cfg):
    if not words:
        return [(0.0, dur)]
    max_gap = cfg["max_gap_talk"] if fmt == "talk" else cfg["max_gap_cs"]
    pad = cfg["keep_gap"] / 2
    lead = cfg["lead_talk"] if fmt == "talk" else cfg["lead_cs"]
    segs = []
    a, b = words[0]["s"], words[0]["e"]
    for w in words[1:]:
        if w["s"] - b > max_gap:
            segs.append((a, b))
            a = w["s"]
        b = max(b, w["e"])
    segs.append((a, b))
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
    font = cfg["font"]
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{tsize},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,6,0,8,{cfg.get('side_margin', 80)},{cfg.get('side_margin', 80)},{title_y},1
Style: Sub,{font},{cfg['sub_size']},{s_fg},{s_fg},{s_tail}
Style: Tag,{font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,3,10,0,5,0,0,0,1

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
        size = cfg["sub_size"] if len(txt) <= 11 else int(cfg["sub_size"] * 11 / len(txt))
        fx = rf"{{\pos({W // 2},{sub_y})\fs{size}\fscx85\fscy85\t(0,90,\fscx100\fscy100)}}"
        lines.append(f"Dialogue: 1,{ass_time(s)},{ass_time(e)},Sub,,0,0,0,,{fx}{txt}")
    path.write_text(head + "\n".join(lines) + "\n", encoding="utf-8-sig")


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


def tg_chat_id(token):
    if TG_CHAT.exists():
        return TG_CHAT.read_text(encoding="utf-8-sig").strip()
    import requests
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
    chats = [u["message"]["chat"]["id"] for u in r.get("result", []) if "message" in u]
    if not chats:
        print("  Telegram: напиши своему боту любое сообщение (например /start) и запусти ещё раз")
        return None
    TG_CHAT.write_text(str(chats[-1]), encoding="utf-8")
    return str(chats[-1])


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
        sys.exit("Обе команды отправлены в одну тему. Отправь /clips и /my в разных темах.")
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
            return False
        warn = "ВНИМАНИЕ: на экране игра, партнёрка может не засчитать\n" if p.get("game_on_screen") else ""
        cap = (warn + f"Потенциал: {p['potential']}/100 ({potential_label(p['potential'])})\n"
               f"Стример: {p['streamer']} | {p['format']} | {p['duration']} с\n\n"
               f"{p['final_title']}\n{p.get('description') or ''}\n\n{hashtags}")[:1000]
        name = re.sub(r'[\\/:*?"<>|]+', " ", f"{p['potential']:03d}_{p['streamer']}_{p['final_title']}")[:80] + ".mp4"
        with open(path, "rb") as f:
            data = {"chat_id": chat, "caption": cap}
            if thread:
                data["message_thread_id"] = thread
            r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                              data=data, files={"document": (name, f, "video/mp4")}, timeout=600)
        if r.status_code != 200:
            print(f"  Telegram ответил {r.status_code}: {r.text[:200]}")
            return False
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
FONTS = ["Arial", "Arial Black", "Impact", "Bahnschrift", "Segoe UI Black", "Comic Sans MS", "Verdana",
         "Trebuchet MS", "Tahoma", "Georgia", "Courier New"]


def _lin(c1, c2, size):
    g = Image.linear_gradient("L").rotate(-45).resize(size)
    return Image.composite(Image.new("RGB", size, c2), Image.new("RGB", size, c1), g)


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
        d = ImageDraw.Draw(im)
        bs = sorted(boxes, key=lambda b: b[1])
        for a, b in zip(bs, bs[1:]):
            d.rectangle((0, a[1] + a[3], W, b[1] - 1), fill=accent + (255,))
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
        segs = keep_segments(words, info["dur"], fmt, cfg)
        content = sum(b - a for a, b in segs)
        banner = BASE / cfg[f"banner_{fmt}"]
        if not banner.exists():
            sys.exit(f"Нет файла баннера: {banner}")
        ban_dur = probe(banner)["dur"]
        split_out = banner_splits(words, segs, content, ban_dur, cfg)     # один баннер или два
        splits_src = [out_to_src(x, segs) for x in split_out]
        total = content + ban_dur * len(split_out)
    bx, by, bw, bh = cfg[f"banner_{fmt}_crop"]
    ban_h = int(round(bh * W / bw / 2)) * 2
    blur = cfg.get(f"banner_{fmt}_blur", 0)

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
        if style == "split":                     # встык на всю ширину, текст на стыке
            gh = 1870 - (top + 536)
            wins = [(cam_c, (0, top, 1080, 520), 0), (game_c, (0, top + 536, 1080, gh), 0)]
            sub_y, ban_cy, bg_mode = top + 528 - 58, top + 536 + gh // 2, "black"
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
        out_ = [f"[s{i}]crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]},scale={d[2]}:{d[3]}{pad},format=rgba[w{i}a]",
                f"[w{i}a][{mask_in}:v]alphamerge[w{i}b]"]
        if i in rot:                             # наклонённое окно ставим по центру своего места
            ang = f"{-rot[i]}*PI/180"
            out_.append(f"[w{i}b]rotate={ang}:c=none:ow=rotw({ang}):oh=roth({ang})[w{i}c]")
            out_.append(f"[l{i}][w{i}c]overlay=x={d[0] + d[2] // 2}-w/2:y={d[1] + d[3] // 2}-h/2{en}[l{i + 1}]")
        else:
            out_.append(f"[l{i}][w{i}b]overlay={d[0]}:{d[1]}{en}[l{i + 1}]")
        return out_

    sub_auto = sub_y                                 # где субтитры встали бы сами
    if force and force.get("sub_y"):                 # положение субтитров задал человек
        sub_y = int(force["sub_y"])
    ass = CACHE / f"subs_{os.getpid()}.ass"          # свои временные файлы у каждого запущенного окна
    if preview:
        # один кадр с этой раскладкой: окна, пример субтитров и место, куда встанет баннер
        sample = [{"s": 0.0, "e": 1.0, "t": "субтитры"}]
        write_ass(ass, title, sample, [(0.0, 1.0)], [], 0.0, 1.0, sub_y, cfg, title_y, title_size, accent,
                  show_title and bool(title), sub_style, tags)
        n_w = len(wins)
        pf = [f"[0:v]split={n_w + 1}[b]" + "".join(f"[s{i}]" for i in range(n_w)), bg_chain(),
              "[bg][1:v]overlay=0:0[l0]"]
        for i in range(n_w):
            pf += win_chain(i, 2 + i)
        last = f"l{n_w}"
        pcmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{float(preview.get('t', 0)):.2f}", "-i", str(clip),
                "-i", str(frame)]
        for mpath in masks:
            pcmd += ["-i", str(mpath)]
        if satis:
            smask = CACHE / f"mask_{SAT[2]}x{SAT[3]}_44.png"
            if not smask.exists():
                rounded_mask(SAT[2], SAT[3], 44, smask)
            pcmd += ["-ss", "3", "-i", str(satis), "-i", str(smask)]
            ns = 2 + n_w
            pf.append(f"[{ns}:v]scale={SAT[2]}:{SAT[3]}:force_original_aspect_ratio=increase,"
                      f"crop={SAT[2]}:{SAT[3]},setsar=1,format=rgba[sat0]")
            pf.append(f"[sat0][{ns + 1}:v]alphamerge[sat1]")
            pf.append(f"[{last}][sat1]overlay={SAT[0]}:{SAT[1]}[lsat]")
            last = "lsat"
        by0 = max(0, ban_cy - ban_h // 2)
        if progress_y:                           # полоска времени: в примере показана на 40%
            pf.append(f"[{last}]drawbox=x=0:y={progress_y}:w=432:h=12:color=0x{accent[0]:02X}{accent[1]:02X}{accent[2]:02X}:t=fill[lpb]")
            last = "lpb"
        pf.append(f"[{last}]drawbox=x=0:y={by0}:w={W}:h={ban_h}:color=white@0.55:t=6,"
                  f"ass={ass.name},scale=540:960[vout]")
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
    fc.append(f"[{nb}:a]aresample=48000,aformat=channel_layouts=stereo,volume={gain:.1f}dB,"
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
    bl = f",gblur=sigma={blur / 5:.2f}" if blur else ""
    ban_y = ban_cy - ban_h // 2
    fc.append(f"[{nb}:v]crop={bw}:{bh}:{bx}:{by},scale={W}:{ban_h},format=rgba{bl},fps={fps}"
              + (f",split={nban}" + "".join(f"[bs{k}]" for k in range(nban)) if nban > 1 else "[bs0]"))
    for k, b0 in enumerate(ban_at):
        # до своего момента баннер идёт прозрачными кадрами: так основное видео не зависит от того,
        # как конкретная версия ffmpeg ведёт себя, пока второй поток ещё не начался
        fc.append(f"[bs{k}]setpts=PTS-STARTPTS,tpad=start_duration={b0:.4f}:start_mode=add:color=black@0.0[bv{k}]")
        fc.append(f"[{last}][bv{k}]overlay=0:{ban_y}:enable='between(t,{b0:.4f},{b0 + ban_dur:.4f})':"
                  f"eof_action=pass[wb{k}]")
        last = f"wb{k}"
    fc.append(f"[{last}]null[wb]")
    fc.append(f"[wb]ass={ass.name},format=yuv420p[vout]")

    tmp = CACHE / f"render_{os.getpid()}.mp4"
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
            print("  кодировщик видеокарты не сработал, пробую обычный")
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
    hashtags = (f"#{streamer} " if streamer else "") + "#стример #twitch #нарезки"
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
    hashtags = (f"#{streamer} " if streamer else "") + "#стример #twitch #нарезки"
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
            if tg_send(video, p, f"#{p['streamer']} #стример #twitch #нарезки", kind):
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
        hashtags = f"#{p['streamer']} #стример #twitch #нарезки"
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
