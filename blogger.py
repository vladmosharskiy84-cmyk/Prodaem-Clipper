"""Монтаж блогера (стиль «Вальдемар», профиль «Мурчик»): говорящая голова в ч/б карточке на чёрном фоне.

Человек закидывает видео, программа сама:
- вырезает паузы между фразами (джамп-каты) и чуть меняет крупность плана на стыках;
- ч/б обработка, скруглённая карточка 3:4 по центру, сверху и снизу чёрное (чужие чёрные поля обрезаются);
- первые 3–4 слова — крючок: курсив лесенкой, красный и жёлтый со свечением, каждое слово появляется,
  когда его говорят, и под каждое щёлкает выбранный звук;
- дальше субтитры по одному слову, белые; смысловые слова красным;
- музыка, которую выбрал человек: тише голоса, приглушается, когда он говорит, тихое вступление
  пропускается, в конце плавно затихает.
"""
import json
import math
import re
import subprocess
import time
import wave
from pathlib import Path

import montage as m

BASE = m.BASE
MUSIC_DIR = BASE / "music"
SOUND_DIR = BASE / "sounds"
OUT_DIR = BASE / "clips" / "ready" / "blogger"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")

W, H = 1080, 1920
CARD_W, CARD_H, CARD_R = 930, 1240, 46            # карточка 3:4, как у Вальдемара
CARD_X, CARD_Y = (W - CARD_W) // 2, (H - CARD_H) // 2
SUB_Y = 1190                                       # субтитры на уровне груди
SUB_SIZE = 76
HOOK_Y = 1110                                      # центр блока крючка
HOOK_SIZE, HOOK_GAP = 150, 165
RED, YELLOW, WHITE = "&H2828FF&", "&H00DCFF&", "&HFFFFFF&"   # ASS: BGR, яркие и насыщенные
SUB_RED = "&H1F1FE8&"

DEFAULTS = {"hook_font": None, "hook_text": "", "hook_caps": False, "hook_words": 0, "click": "mouse", "click_vol": 70, "bw": True, "cut_pauses": True, "max_gap": 0.22, "cut_fillers": True,
            "music": None, "music_db": -20, "zoom_cuts": True, "accents": True, "tg": True}

HOOK_FONTS = ["Playfair Display Italic", "Monotype Corsiva", "Marck Script", "Lora Bold Italic"]
SUB_FONTS = ["Inter SemiBold", "Montserrat Bold"]
WEB_FONTS = {"Playfair Display Italic": ("Playfair Display", "500", True), "Marck Script": ("Marck Script", "400", False), "Lora Bold Italic": ("Lora", "700", True),
             "Inter SemiBold": ("Inter", "600", False), "Montserrat Bold": ("Montserrat", "700", False)}
LOCAL_FONTS = {"Monotype Corsiva": ["C:/Windows/Fonts/MTCORSVA.TTF"],
               "Inter SemiBold": ["/usr/share/fonts/opentype/inter/Inter-SemiBold.otf"],
               "Lora Bold Italic": ["/usr/share/fonts/truetype/google-fonts/Lora-Italic-Variable.ttf"]}

CLICKS = {"mouse": "Мышка", "mouse_soft": "Мышка мягкая", "tap": "Тап по экрану", "none": "Без щелчков"}


class Stop(Exception):
    pass


# ---------- шрифты ----------
def _font_file(name, download=True):
    """Файл шрифта в папке fonts (копия из Windows или скачанный с Google Fonts один раз) или None."""
    m.FONT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in (".ttf", ".otf"):
        p = m.FONT_DIR / (re.sub(r"\W+", "_", name) + ext)
        if p.exists() and p.stat().st_size > 1000:
            return p
    for src in LOCAL_FONTS.get(name, []) + ([str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts/MTCORSVA.TTF")]
                                            if name == "Monotype Corsiva" else []):
        s = Path(src)
        if s.exists():
            p = m.FONT_DIR / (re.sub(r"\W+", "_", name) + s.suffix.lower())
            p.write_bytes(s.read_bytes())
            return p
    if name in WEB_FONTS and download:
        fam, wt, ital = WEB_FONTS[name]
        try:
            import requests
            q = f"{fam.replace(' ', '+')}:{'ital,' if ital else ''}wght@{'1,' if ital else ''}{wt}"
            css = requests.get(f"https://fonts.googleapis.com/css2?family={q}&subset=cyrillic",
                               headers={"User-Agent": "Mozilla/4.0"}, timeout=20).text
            urls = re.findall(r"url\((https://[^)]+?\.ttf)\)", css)
            if urls:
                data = requests.get(urls[0], timeout=60).content
                if data[:4] in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
                    p = m.FONT_DIR / (re.sub(r"\W+", "_", name) + ".ttf")
                    p.write_bytes(data)
                    return p
        except Exception:
            pass
    return None


def _pick(names, download):
    """(имя шрифта для субтитров, путь, жирный, курсив) первого доступного шрифта с русскими буквами."""
    from PIL import ImageFont
    for name in names:
        p = _font_file(name, download)
        try:
            if p and m.has_cyrillic(p):
                fam, style = ImageFont.truetype(str(p), 20).getname()
                return fam, p, int("Bold" in (style or "")), int("Italic" in (style or ""))
        except Exception:
            continue
    return None


def user_hook_fonts():
    m.FONT_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in m.FONT_DIR.glob("hook_user_*") if p.suffix.lower() in (".ttf", ".otf"))


def add_hook_font(files):
    """Свой шрифт для крючка (например Mozart): копируем в папку fonts. Возвращает (имя файла, есть ли русские буквы)."""
    for f in files:
        f = Path(f)
        if f.suffix.lower() in (".ttf", ".otf") and f.exists():
            p = m.FONT_DIR / ("hook_user_" + re.sub(r"[^\w.\-]+", "_", f.name))
            m.FONT_DIR.mkdir(parents=True, exist_ok=True)
            p.write_bytes(f.read_bytes())
            try:
                cyr = m.has_cyrillic(p)
            except Exception:
                cyr = False
            return p.name, cyr
    return None, False


def hook_font(download=True, custom=None):
    """Шрифт крючка: свой (если выбран и в нём есть русские буквы), иначе Corsiva / похожий курсив."""
    if custom:
        p = m.FONT_DIR / custom
        try:
            if p.exists() and m.has_cyrillic(p):
                from PIL import ImageFont
                fam, style = ImageFont.truetype(str(p), 20).getname()
                return fam, p, int("Bold" in (style or "")), int("Italic" in (style or ""))
        except Exception:
            pass
    return _pick(HOOK_FONTS, download) or ("Georgia", None, 1, 1)


def sub_font(download=True):
    return _pick(SUB_FONTS, download) or ("Arial", None, 1, 0)


def text_width(path, size, text):
    """Ширина текста так, как её нарисует libass: он подгоняет под размер высоту строки шрифта."""
    from PIL import ImageFont
    try:
        f = ImageFont.truetype(str(path), size)
        return f.getlength(text) * size / max(1, sum(f.getmetrics()))
    except Exception:
        return len(text) * size * 0.5


# ---------- звуки ----------
def _synth(kind):
    import numpy as np
    sr = 48000
    rng = np.random.default_rng(3)
    out = np.zeros(int(sr * 0.14))
    parts = {"mouse": ((0.0, 1.0, 3200, 900), (0.055, 0.55, 2600, 900)),        # нажал и отпустил
             "mouse_soft": ((0.0, 0.8, 1900, 600), (0.06, 0.4, 1600, 600)),
             "tap": ((0.0, 1.0, 900, 260),)}[kind]
    for t0, amp, f0, dec in parts:
        n = int(sr * 0.03)
        t = np.arange(n) / sr
        body = np.sin(2 * math.pi * f0 * t) * np.exp(-t * dec) + rng.normal(0, 0.5, n) * np.exp(-t * dec * 1.7)
        i = int(t0 * sr)
        out[i:i + n] += amp * body
    out = out / max(1e-6, np.abs(out).max()) * 0.9
    return sr, out


def click_file(name):
    """Файл щелчка: свой из папки sounds или встроенный (синтезированный, без чужих звуков)."""
    if not name or name == "none":
        return None
    if name in CLICKS:
        p = m.CACHE / f"click_{name}_v2.wav"
        if not p.exists():
            sr, out = _synth(name)
            m.CACHE.mkdir(exist_ok=True)
            with wave.open(str(p), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes((out * 32767).astype("<i2").tobytes())
        return p
    p = SOUND_DIR / name
    return p if p.exists() else click_file("mouse")


def _list(folder):
    folder.mkdir(parents=True, exist_ok=True)
    return sorted(p.name for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT)


def music_list():
    return _list(MUSIC_DIR)


def sound_list():
    return [[k, v] for k, v in CLICKS.items()] + [[n, n] for n in _list(SOUND_DIR)]


def _add(files, folder):
    folder.mkdir(parents=True, exist_ok=True)
    added = []
    for f in files:
        f = Path(f)
        if f.suffix.lower() in AUDIO_EXT and f.exists():
            (folder / f.name).write_bytes(f.read_bytes())
            added.append(f.name)
    return added


def add_music(files):
    return _add(files, MUSIC_DIR)


def add_sound(files):
    return _add(files, SOUND_DIR)


def music_start(path):
    """Пропускаем тихое вступление трека: начинаем, где музыка уже звучит в полную силу."""
    try:
        import numpy as np
        raw = subprocess.run(["ffmpeg", "-v", "error", "-t", "60", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000",
                              "-f", "f32le", "-"], capture_output=True, creationflags=NOWIN).stdout
        a = np.frombuffer(raw, dtype=np.float32)
        if a.size < 16000:
            return 0.0
        k = 4000                                          # шаг 0.5 с
        n = a.size // k
        db = 20 * np.log10(np.sqrt((a[:n * k].reshape(n, k) ** 2).mean(axis=1)) + 1e-6)
        ref = float(np.percentile(db, 70))
        for i, v in enumerate(db):
            if v >= ref - 6:
                return round(i * 0.5, 1)
    except Exception:
        pass
    return 0.0


# ---------- слова ----------
def pick_hook(words, n=0):
    """Сколько первых слов идут крючком: 3 или 4. Не тянем крючок через длинную паузу или конец фразы."""
    if not words:
        return 0
    if n:
        return min(n, len(words))
    k = min(4, len(words))
    for i in range(2, k - 1):                     # после 3-го слова пауза или точка: крючок из трёх
        if words[i + 1]["s"] - words[i]["e"] > 0.6 or re.search(r"[.!?…]$", words[i]["t"]):
            return i + 1
    return k


def hook_lines(hw):
    """Короткие слова («я», «моя», «не») садятся в одну строку со следующим, как у Вальдемара."""
    lines, cur = [], []
    for i, w in enumerate(hw):
        cur.append(w)
        if len(re.sub(r"\W", "", w["t"])) > 3 or i == len(hw) - 1:
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)
    return lines


def ai_accents(words, cfg):
    """Смысловые слова для красного: просим Claude (дёшево), иначе самые длинные."""
    if len(words) < 6:
        return set()
    key = m.KEY_FILE.read_text(encoding="utf-8-sig").strip() if m.KEY_FILE.exists() else ""
    if key:
        try:
            import requests
            txt = " ".join(f"{i}:{w['t']}" for i, w in enumerate(words))
            prompt = ("Это речь блогера, номер:слово. Выбери 6–10% самых смысловых и эмоциональных слов, которые стоит "
                      "выделить красным в субтитрах (например «несчастлив», «разрушал», «созидаю», «прибить»). "
                      "Не служебные слова. Ответ ТОЛЬКО JSON-список номеров.\n\n" + txt[:6000])
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": cfg.get("cheap_model", "claude-haiku-5-5"), "max_tokens": 200,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=60)
            if r.status_code == 200:
                out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
                ids = json.loads(out[out.index("["):out.rindex("]") + 1])
                return {int(i) for i in ids if 0 <= int(i) < len(words)}
        except Exception:
            pass
    long_ = sorted(range(len(words)), key=lambda i: -len(re.sub(r"\W", "", words[i]["t"])))
    return set(long_[:max(1, len(words) // 14)])


def map_words(words, segs):
    """Время слов в готовом ролике (после вырезки пауз)."""
    out, acc = [], 0.0
    for a, b in segs:
        for w in words:
            if a - 0.02 <= w["s"] < b:
                out.append(dict(w, s=round(w["s"] - a + acc, 3), e=round(min(w["e"], b) - a + acc, 3)))
        acc += b - a
    return out



# ---------- динамика: убрать «э», «ну», запинки и паузы, не теряя смысла ----------
FILLERS = {"э", "ээ", "эээ", "эм", "эмм", "эммм", "м", "мм", "ммм", "хм", "хмм", "ам", "аа", "ааа", "э-э", "а-а", "м-м", "ы", "ыы"}
VERB_PROMPT = "Э-э, ну, мм, короче, это самое. Эм, а-а, в общем, как бы, типа."


def transcribe_verbatim(src, cfg):
    """Расшифровка «как сказано»: с «э», «мм» и запинками, чтобы их можно было вырезать.
    Обычное распознавание их выкидывает из текста, а звук остаётся в ролике."""
    cache = Path(src).with_suffix(".verb.json")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return m.transcribe(Path(src), cfg)
    if getattr(m, "_model", None) is None:
        m._model = WhisperModel(cfg["whisper_model"], device="cpu", compute_type="int8")
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, creationflags=NOWIN).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    if audio.size < 16000:
        return []
    segs, _ = m._model.transcribe(audio, language=cfg.get("language", "ru"), word_timestamps=True, vad_filter=False,
                                  initial_prompt=VERB_PROMPT, condition_on_previous_text=False)
    words = []
    for s in segs:
        for w in s.words or []:
            t = w.word.strip()
            if t:
                words.append({"s": round(w.start, 3), "e": round(w.end, 3), "t": t})
    cache.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    return words


def _norm(t):
    return re.sub(r"[^\w-]", "", str(t).lower())


def drop_words(words, cfg, say=print):
    """Номера слов, которые можно вырезать без потери смысла: «э», «мм», слова-паразиты, запинки,
    повторы и оборванные начала фраз. Сначала простые правила, потом Claude смотрит на весь рассказ."""
    drop = {i for i, w in enumerate(words) if _norm(w["t"]) in FILLERS}
    for i in range(1, len(words)):               # «я я», «он он»: повтор подряд
        if _norm(words[i]["t"]) and _norm(words[i]["t"]) == _norm(words[i - 1]["t"]) and words[i]["s"] - words[i - 1]["e"] < 0.6:
            drop.add(i - 1)
    key = m.KEY_FILE.read_text(encoding="utf-8-sig").strip() if m.KEY_FILE.exists() else ""
    if key and len(words) >= 8:
        try:
            import requests
            txt = " ".join(f"{i}:{w['t']}" for i, w in enumerate(words))
            prompt = ("Это дословная расшифровка рассказа блогера (номер:слово). Ролик монтируется динамично: нужно вырезать "
                      "всё, что не несёт смысла: звуки-заминки (э, мм, а-а), слова-паразиты, сказанные как заполнитель "
                      "(ну, вот, короче, типа, как бы, это самое, в общем, значит), запинки, повторы слов и оборванные начала "
                      "фраз, которые человек тут же переговорил. ВАЖНО: суть истории не должна теряться — не трогай слова, "
                      "без которых меняется или рвётся смысл, имена, цифры, отрицания, эмоции, концовки фраз. Если сомневаешься — "
                      "оставь. Ответ ТОЛЬКО JSON-список номеров слов, которые вырезать.\n\n" + txt[:12000])
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                              json={"model": cfg.get("cheap_model", "claude-haiku-5-5"), "max_tokens": 1200,
                                    "messages": [{"role": "user", "content": prompt}]}, timeout=90)
            if r.status_code == 200:
                out = "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
                ids = {int(i) for i in json.loads(out[out.index("["):out.rindex("]") + 1]) if 0 <= int(i) < len(words)}
                if len(ids) <= 0.3 * len(words):            # вырезать треть рассказа — это уже пересказ, не берём
                    drop |= ids
                else:
                    say("Claude предложил вырезать слишком много слов: беру только «э» и повторы")
        except Exception as e:
            say(f"разбор слов-паразитов через Claude не получился: {e}")
    return drop


def _energy(src):
    """Громкость каждые 10 мс: резать лучше в самом тихом месте рядом с границей слова."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
                         capture_output=True, creationflags=NOWIN).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    k = 160
    n = a.size // k
    return np.sqrt((a[:n * k].reshape(n, k) ** 2).mean(axis=1)) if n else None


def tight_segments(words, keep, dur, src, max_gap=0.22):
    """Куски видео только со словами, которые остаются. Режем там, где выкинуто слово или пауза длиннее max_gap,
    и точку реза сдвигаем в самое тихое место рядом, чтобы не съесть начало или конец слова."""
    en = _energy(src)

    def quiet(a, b):
        if en is None:
            return (a + b) / 2
        i0, i1 = max(0, int(a * 100)), min(len(en), int(b * 100) + 1)
        if i1 <= i0:
            return (a + b) / 2
        j = int(en[i0:i1].argmin())
        return (i0 + j) / 100

    segs = []
    cur = None
    prev = None
    for i, w in enumerate(words):
        if i not in keep:
            if cur:
                segs.append(cur)
                cur = None
            prev = None
            continue
        if cur and prev is not None and w["s"] - words[prev]["e"] <= max_gap:
            cur[1] = w["e"]
        else:
            if cur:
                segs.append(cur)
            cur = [w["s"], w["e"]]
        prev = i
    if cur:
        segs.append(cur)
    out = []
    for a, b in segs:
        a2 = quiet(max(0.0, a - 0.12), a - 0.01)          # начало: тишина перед словом
        b2 = quiet(b + 0.02, min(dur, b + 0.16))          # конец: тишина после слова, хвост не обрезаем
        a2, b2 = max(0.0, a2), min(dur, max(b2, b + 0.03))
        if out and a2 - out[-1][1] < 0.06:
            out[-1] = (out[-1][0], b2)
        else:
            out.append((a2, b2))
    return [(a, b) for a, b in out if b - a > 0.08]


# ---------- субтитры ----------
def _t(x):
    return m.ass_time(max(0.0, x))


def _clean(t):
    return str(t).replace("{", "(").replace("}", ")").replace("\\", "")


def write_ass(path, hook, hook_end, rest, accents, hook_face, subf, palette="red_white"):
    """hook: [{"t", "s"}] — главные слова и момент появления; rest: обычные субтитры (идут после крючка)."""
    hname, hpath, hbold, hital = hook_face
    sname, spath, sbold, _ = subf
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{hname},{HOOK_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{-1 if hbold else 0},{-1 if hital else 0},0,0,100,100,0,0,1,0,0,8,0,0,0,204
Style: Sub,{sname},{SUB_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,{-1 if sbold else 0},0,0,0,100,100,0,0,1,0,0,5,0,0,0,204

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if hook:
        lines = hook_lines(hook)
        cols = {"red_white": [RED, WHITE], "red": [RED], "red_yellow": [RED, YELLOW, YELLOW, RED]}.get(palette, [RED, WHITE])
        left, right = CARD_X + 30, CARD_X + CARD_W - 30
        n = len(lines)
        top = HOOK_Y - HOOK_GAP * n / 2
        t0 = hook[0]["s"]
        for li, line in enumerate(lines):
            col = cols[li % len(cols)]
            dark = "&H000046&" if col == RED else "&H202020&" if col == WHITE else "&H00344A&"
            raw = " ".join(_clean(w["t"]) for w in line)
            size = HOOK_SIZE
            wd = text_width(hpath, size, raw) if hpath else len(raw) * size * 0.45
            if wd > right - left:                    # длинная строка не вылезает за карточку
                size = int(size * (right - left) / wd)
                wd = right - left
            # лесенка: первая строка у левого края, последняя у правого, остальные между ними
            k = li / (n - 1) if n > 1 else 0.0
            cx = int(left + wd / 2 + (right - left - wd) * k)
            y = int(top + HOOK_GAP * li)

            def body(alpha_to):
                parts = []
                for w in line:
                    a = int((w["s"] - t0) * 1000)
                    if alpha_to:
                        parts.append(f"{{\\3a&HFF&\\t({a},{a + 220},\\3a{alpha_to})}}{_clean(w['t'])}")
                    else:
                        parts.append(f"{{\\1a&HFF&\\3a&HFF&\\t({a},{a + 220},\\1a&H00&\\3a&H00&)}}{_clean(w['t'])}")
                return " ".join(parts)
            base = f"\\an8\\pos({cx},{y})\\fs{size}\\fad(0,220)"
            # слой 0: густая тёмная дымка под словом (читается на любом фоне), слой 1: само слово, яркое, с тёмной кромкой
            ev.append(f"Dialogue: 0,{_t(t0)},{_t(hook_end)},Hook,,0,0,0,,{{{base}\\1a&HFF&\\bord34\\blur30\\3c&H000000&}}{body('&H0C&')}")
            ev.append(f"Dialogue: 1,{_t(t0)},{_t(hook_end)},Hook,,0,0,0,,{{{base}\\1c{col}\\bord2\\blur0.8\\3c{dark}}}{body(None)}")
    for i, w in enumerate(rest):                     # дальше по одному слову
        st = w["s"]
        nxt = rest[i + 1]["s"] if i + 1 < len(rest) else w["e"] + 0.5
        en = min(nxt, max(w["e"], st + 0.25) + 0.3) if nxt - w["e"] > 0.6 else nxt
        en = max(en, st + 0.12)
        acc = i in accents
        col = SUB_RED if acc else WHITE
        sc = 110 if acc else 100
        ev.append(f"Dialogue: 0,{_t(st)},{_t(en)},Sub,,0,0,0,,{{\\an5\\pos({W // 2},{SUB_Y})\\bord6\\blur9\\3c&H000000&"
                  f"\\3a&H40&\\1c{col}\\fscx{sc - 7}\\fscy{sc - 7}\\t(0,80,\\fscx{sc}\\fscy{sc})}}{_clean(w['t'])}")
    Path(path).write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ---------- монтаж ----------
def black_bars(src, dur):
    """Если во входном видео уже есть чёрные поля (готовый ролик, запись экрана), находим, что внутри них."""
    boxes = []
    for f in (0.2, 0.5, 0.8):
        r = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{dur * f:.2f}", "-i", str(src), "-t", "0.6",
                            "-vf", "cropdetect=limit=28:round=2:reset=0", "-an", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NOWIN)
        got = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr or "")
        if got:
            boxes.append(tuple(int(v) for v in got[-1]))
    if not boxes:
        return None
    w = max(b[0] for b in boxes)
    h = max(b[1] for b in boxes)
    x = min(b[2] for b in boxes)
    y = min(b[3] for b in boxes)
    return w, h, x, y


def card_crop(sw, sh, zoom=1.0, box=None):
    """Кусок исходника 3:4 для карточки: по ширине целиком, по высоте с запасом над головой."""
    bw, bh, bx, by = box or (sw, sh, 0, 0)
    ratio = CARD_W / CARD_H
    if bw / bh > ratio:
        ch = bh / zoom
        cw = ch * ratio
    else:
        cw = bw / zoom
        ch = cw / ratio
    x = bx + (bw - cw) / 2
    y = by + max(0.0, min(bh - ch, (bh - ch) * 0.32))     # лицо обычно в верхней трети
    ev = lambda v: int(v) // 2 * 2
    return ev(cw), ev(ch), ev(x), ev(y)


def make(src, cfg, enc, opts=None, say=print, out=None):
    o = dict(DEFAULTS, **(cfg.get("blogger") or {}), **(opts or {}))
    src = Path(src).resolve()
    info = m.probe(src)
    if not info.get("dur"):
        raise Stop("не получилось открыть видео")
    sw, sh, dur = info["w"], info["h"], info["dur"]
    say("распознаю речь…")
    words = m.merge_hyphens(transcribe_verbatim(src, cfg) if o.get("cut_fillers", True) else m.transcribe(src, cfg))
    if not words:
        say("речь не распозналась: ролик будет без субтитров")
    if o["cut_pauses"] and words:
        drop = drop_words(words, cfg, say) if o.get("cut_fillers", True) else set()
        if drop:
            say("вырезаю заминки и паразиты: " + ", ".join(words[i]["t"] for i in sorted(drop))[:200])
        keep = [i for i in range(len(words)) if i not in drop]
        segs = tight_segments(words, set(keep), dur, src, float(o["max_gap"]))
        words = [words[i] for i in keep]
    else:
        segs = [(0.0, dur)]
    words = m.ai_fix_words(words, src, cfg)
    segs = [(a, b) for a, b in segs if b - a > 0.08] or [(0.0, dur)]
    total = sum(b - a for a, b in segs)
    if o["cut_pauses"]:
        say(f"паузы вырезаны: {dur - total:.1f} с, кусков {len(segs)}")
    box = black_bars(src, dur)
    if box and (box[0] < sw - 16 or box[1] < sh - 16):
        say(f"в видео чёрные поля: беру только картинку {box[0]}×{box[1]}")
    else:
        box = None
    tw = map_words(words, segs)
    custom = [x for x in re.split(r"\s+", str(o.get("hook_text") or "").strip()) if x][:6]
    if custom:                                   # главные слова вписаны вручную (как «Бокс Спорт Связи»)
        texts = custom
        say("главные слова: " + " ".join(custom))
    else:                                        # иначе — первые 3–4 сказанных слова
        texts = [w["t"] for w in tw[:pick_hook(tw, int(o.get("hook_words") or 0))]]
    case = o.get("hook_case") or ("caps" if o.get("hook_caps") else "title")
    def _case(t):
        t = re.sub(r"[.,!?…:;]+$", "", t)
        return t.upper() if case == "caps" else (t[:1].upper() + t[1:]) if case == "title" else t
    texts = [_case(t) for t in texts if t]
    hook, hook_end = [], 0.0
    if texts:                                    # каждое слово появляется, когда начинается очередное сказанное слово
        starts = [w["s"] for w in tw] or [0.2]
        t = max(0.15, starts[0])
        for i, x in enumerate(texts):
            if i:
                t = max(t + 0.35, starts[i] if i < len(starts) else t + 0.5)
            hook.append({"t": x, "s": round(t, 3)})
        hook_end = min(total, max(hook[-1]["s"] + 0.65, hook[0]["s"] + 2.2))   # как в примере: ~2 с на экране
    rest = [w for w in tw if w["s"] >= hook_end - 0.05]     # пока висят главные слова, обычных субтитров нет
    acc_all = ai_accents(tw, cfg) if o["accents"] else set()
    first = len(tw) - len(rest)
    acc = {i - first for i in acc_all if i >= first}
    m.CACHE.mkdir(exist_ok=True)
    tag = re.sub(r"\W+", "_", src.stem)[:40]
    ass = m.CACHE / f"blog_{tag}.ass"
    hf = hook_font(custom=o.get("hook_font"))
    say(f"шрифт крючка: {hf[0]}")
    write_ass(ass, hook, hook_end, rest, acc, hf, sub_font(), o.get("hook_palette") or "red_white")
    mask = m.CACHE / f"blog_mask_{CARD_W}x{CARD_H}.png"
    if not mask.exists():
        m.rounded_mask(CARD_W, CARD_H, CARD_R, mask)

    fps = 60 if info.get("fps", 30) > 45 else 30
    grade = ("hue=s=0,eq=contrast=1.14:brightness=0.01:gamma=0.96,colorbalance=rm=0.03:gm=0.015:bm=-0.03:rh=0.02:bh=-0.02,"
             "vignette=angle=PI/5:mode=forward," if o["bw"] else "eq=contrast=1.05:saturation=1.05,")
    fc, vlab, alab = [], [], []
    zs, last_z = [], 1.08
    since = 99.0
    for a, b in segs:                                # крупность меняем на стыке, но не чаще раза в 1.5 с
        if o["zoom_cuts"] and since >= 1.5:
            last_z = 1.08 if last_z == 1.0 else 1.0
            since = 0.0
        zs.append(last_z)
        since += b - a
    for i, (a, b) in enumerate(segs):                # каждый кусок: своя крупность плана (лёгкий «зум на стыке»)
        z = zs[i]
        cw, ch, cx, cy = card_crop(sw, sh, z, box)
        fc.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS,crop={cw}:{ch}:{cx}:{cy},"
                  f"scale={CARD_W}:{CARD_H}:flags=lanczos,fps={fps},setsar=1[v{i}]")
        fc.append(f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,afade=t=in:d=0.012,"
                  f"afade=t=out:st={max(0, b - a - 0.015):.3f}:d=0.015[a{i}]")
        vlab.append(f"[v{i}]")
        alab.append(f"[a{i}]")
    fc.append("".join(f"{v}{a_}" for v, a_ in zip(vlab, alab)) + f"concat=n={len(segs)}:v=1:a=1[cv][voice0]")
    fc.append(f"[cv]{grade}cas=0.4,format=rgba[cv2];[1:v]format=gray[mk];[cv2][mk]alphamerge[card]")
    fc.append(f"color=c=black:s={W}x{H}:r={fps}:d={total:.3f}[bg];[bg][card]overlay={CARD_X}:{CARD_Y}:shortest=1,"
              f"ass={ass.name}:fontsdir=../fonts,format=yuv420p[vout]")
    # звук: голос ровнее; щелчки под словами крючка; музыка тише голоса и прячется, когда он говорит
    mus = o.get("music")
    mp = (Path(mus) if Path(mus).is_absolute() else MUSIC_DIR / mus) if mus else None
    if mp and not mp.exists():
        say(f"музыка не найдена: {mus}")
        mp = None
    fc.append("[voice0]highpass=f=70,acompressor=threshold=0.12:ratio=3:attack=8:release=120:makeup=2,"
              "aformat=sample_rates=48000:channel_layouts=stereo" + (",asplit=2[voice][vkey]" if mp else "[voice]"))
    inputs = ["-i", str(src), "-loop", "1", "-i", str(mask)]
    mix, idx = ["[voice]"], 2
    ck = click_file(o.get("click", "mouse") if o.get("clicks", True) else "none")
    if ck and hook:
        vol = max(0.0, min(2.0, float(o.get("click_vol", 70)) / 100))
        for j, w in enumerate(hook):
            inputs += ["-i", str(ck)]
            d = int(max(0.0, w["s"] - 0.02) * 1000)
            fc.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:0.6,adelay={d}|{d},"
                      f"volume={vol:.2f}[ck{j}]")
            mix.append(f"[ck{j}]")
            idx += 1
    if mp:
        ss = music_start(mp)
        inputs += ["-stream_loop", "-1", "-ss", f"{ss:.1f}", "-i", str(mp)]
        fc.append(f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={float(o['music_db']):.1f}dB,afade=t=in:d=0.4,afade=t=out:st={max(0.0, total - 1.6):.2f}:d=1.6[mraw]")
        fc.append("[mraw][vkey]sidechaincompress=threshold=0.04:ratio=5:attack=25:release=350[mus]")
        mix.append("[mus]")
        say(f"музыка: {mp.name}" + (f", с {ss:.0f} с (тихое вступление пропущено)" if ss else ""))
    if len(mix) > 1:
        fc.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=first,alimiter=limit=0.95[aout]")
    else:
        fc.append("[voice]anull[aout]")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(out) if out else OUT_DIR / f"{tag}_{time.strftime('%m%d_%H%M%S')}.mp4"
    say("монтирую…")
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
           *m.encoder_args(enc), "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           "-t", f"{total:.3f}", str(out)]
    r = subprocess.run(cmd, cwd=str(m.CACHE), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NOWIN)
    if r.returncode != 0 or not out.exists():
        raise Stop("ffmpeg: " + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:300])
    meta = {"src": str(src), "out": str(out), "duration": round(total, 2),
            "hook": " ".join(w["t"] for w in hook), "words": len(tw), "music": str(mp) if mp else None,
            "text": " ".join(w["t"] for w in tw)[:2000]}
    say(f"готово: {total:.1f} с, крючок «{meta['hook']}»")
    return out, meta


def tg_send(path, caption):
    """Готовый ролик в Telegram файлом (если темы настроены — в тему своих видео)."""
    if not m.TG_TOKEN.exists():
        return False
    try:
        import requests
        token = m.TG_TOKEN.read_text(encoding="utf-8-sig").strip()
        thread = None
        if m.TG_TOPICS.exists():
            tp = json.loads(m.TG_TOPICS.read_text(encoding="utf-8"))
            chat, thread = tp["chat_id"], tp.get("manual") or tp.get("clips")
        else:
            chat = m.tg_chat_id(token)
        if not chat:
            return False
        p = Path(path)
        if p.stat().st_size > 49e6:
            small = m.tg_copy(p)
            if not small:
                return False
            p = Path(small)
        data = {"chat_id": chat, "caption": caption[:1000]}
        if thread:
            data["message_thread_id"] = thread
        with open(p, "rb") as f:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument", data=data,
                              files={"document": (Path(path).name, f, "video/mp4")}, timeout=600)
        return r.status_code == 200
    except Exception as e:
        print(f"  в Telegram не ушло: {e}")
        return False
