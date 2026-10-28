"""Учиться у лидеров: разбор популярных TikTok-роликов по ссылкам и книга правил для критика и судьи.

Ссылки бывают на ролик (tiktok.com/@ник/video/123, vm.tiktok.com/…) или на профиль (tiktok.com/@ник).
У профиля берутся самые просматриваемые ролики и несколько слабых для сравнения.
По каждому ролику: просмотры и вовлечённость, как начинается (звук, движение, пик, тишина), кадры, речь.
Claude разбирает каждый ролик, а потом сводит всё в правила: что делает прострел в этой нише.
Правила сохраняются в analytics/playbook.json, их читают критик и судья.
"""
import base64
import json
import re
import subprocess
import time
from pathlib import Path

import montage as m
import accounts

BASE = m.BASE
DIR = BASE / "analytics"
LEAD = DIR / "leaders"
PLAYBOOK = DIR / "playbook.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Stop(Exception):
    pass


def parse_links(text):
    urls = re.findall(r"https?://[^\s,;]+", text or "")
    out = []
    for u in urls:
        u = u.rstrip(").]'\"")
        if "tiktok.com" not in u:
            continue
        if u not in out:
            out.append(u)
    for h in re.findall(r"(?<![\w/])@([\w.\-]{2,})", text or ""):   # просто @ник тоже считаем профилем
        u = f"https://www.tiktok.com/@{h.lower()}"
        if not any(f"@{h.lower()}" in x.lower() for x in out):
            out.append(u)
    return out


def is_profile(url):
    return bool(re.search(r"tiktok\.com/@[\w.\-]+/?(\?.*)?$", url)) and "/video/" not in url


def video_info(url, say):
    r = accounts._ytdlp(["-J", "--skip-download", url], timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        accounts._update_ytdlp(say)
        r = accounts._ytdlp(["-J", "--skip-download", url], timeout=180)
    if r.returncode != 0 or not r.stdout.strip():
        raise Stop("не открылся ролик " + url[:60] + ": " + ((r.stderr or "").strip().splitlines() or [""])[-1][:160])
    e = json.loads(r.stdout)
    return {"id": str(e.get("id")), "acc": (e.get("uploader") or e.get("channel") or "").lower(),
            "url": e.get("webpage_url") or url, "desc": (e.get("description") or e.get("title") or "").strip(),
            "views": int(e.get("view_count") or 0), "likes": int(e.get("like_count") or 0),
            "comments": int(e.get("comment_count") or 0), "shares": int(e.get("repost_count") or 0),
            "saves": int(e.get("save_count") or 0), "dur": float(e.get("duration") or 0),
            "ts": int(e.get("timestamp") or 0)}


def download(v, say):
    LEAD.mkdir(parents=True, exist_ok=True)
    have = [f for f in LEAD.glob(f"{v['id']}.*") if f.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    if have:
        return have[0]
    r = accounts._ytdlp(["-S", "res:720,+size", "-o", str(LEAD / f"{v['id']}.%(ext)s"), v["url"]], timeout=300)
    have = [f for f in LEAD.glob(f"{v['id']}.*") if f.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")]
    return have[0] if r.returncode == 0 and have else None


def frames(path, dur):
    out = []
    for t in (0.15, 0.8, 2.0, max(2.5, dur * 0.5)):
        if t >= dur:
            continue
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                            "-vf", "scale=300:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True, creationflags=NOWIN)
        if r.stdout:
            out.append((t, base64.b64encode(r.stdout).decode()))
    return out


def speech(path, cfg):
    """Речь первых 15 секунд: что говорят в начале. Без faster-whisper — пусто."""
    try:
        words = m.transcribe(Path(path), cfg)
    except Exception:
        return ""
    return " ".join(f"[{w['s']:.1f}]{w['t']}" if i % 6 == 0 else w["t"] for i, w in enumerate(words) if w["s"] < 15)


ONE = """Это кадры популярного (или для сравнения слабого) TikTok-ролика из ниши Dota 2.
Просмотры: {views}, лайков {likes}, комментариев {comments}, репостов {shares}, сохранений {saves}, длина {dur:.0f} с.
Описание автора: {desc}
Цифры начала: {met} (hook_loud: громче ли первые 2 с, дБ; hook_motion: динамичнее ли, 1 = как весь ролик;
dead_air: секунд тишины; peak_at: секунда самого громкого места; cuts: резких смен кадра)
Речь (секунда в скобках): {text}

Разбери коротко и конкретно, по-русски. Ответ ТОЛЬКО JSON:
{{"first_second": "что видит и слышит зритель в первую секунду", "moment": "что за момент", "kind": "драка|эмоция|фейл|смешное|разговор|обучение|мем|другое",
"layout": "как оформлено: раскладка, субтитры, текст на экране, фон", "hook_text": "текст-крючок на экране или null",
"ending": "чем заканчивается", "why": "почему набрал (или не набрал) просмотры", "score": от 1 до 10}}"""

BOOK = """Ты продюсер. Ниже разборы роликов-лидеров ниши Dota 2 в TikTok (аудитория СНГ) с просмотрами,
и для сравнения слабые ролики тех же авторов. Наш клиппер режет клипы стримов (камера сверху, игра снизу, субтитры
по слову, баннер партнёрки в середине), звук не меняем. Платят за ролики от 6000 просмотров.

{data}

Выведи правила, которые реально отличают прострелы от провалов в этих данных. Конкретно, с цифрами и примерами,
без общих советов. Только честные приёмы: какие моменты брать, как начинать, длина, оформление, текст, описание.
Никаких накруток и уникализации чужих роликов.

Ответ ТОЛЬКО JSON:
{{"summary": "главное в 1-2 предложениях",
"rules": ["правило для критика и судьи, которое можно проверить на ролике", ...],
"hooks": ["как начинаются прострелы: конкретные примеры первой секунды", ...],
"moments": ["какие моменты залетают лучше всего", ...],
"avoid": ["что убивает охват", ...],
"length": "какая длина работает", "captions": "какие описания работают"}}"""


def analyze(text, cfg, say=print, per_profile=6, weak=2):
    links = parse_links(text)
    if not links:
        raise Stop("вставь ссылки на ролики или профили TikTok")
    vids, errors = [], []
    for u in links:
        try:
            if is_profile(u):
                h = accounts.clean_handle(u)
                say(f"собираю ролики @{h}…")
                lst = sorted(accounts.fetch_account(h, say, limit=60), key=lambda v: -v["views"])
                pick = lst[:per_profile] + [v for v in lst[-weak:] if v not in lst[:per_profile]]
                vids += pick
            else:
                say("открываю ролик…")
                vids.append(video_info(u, say))
        except (Stop, accounts.Stop) as e:
            errors.append(str(e))
            say(str(e))
    seen, uniq = set(), []
    for v in vids:
        if v["id"] not in seen:
            seen.add(v["id"])
            uniq.append(v)
    vids = uniq[:30]
    if not vids:
        raise Stop("ни один ролик не открылся. " + " ".join(errors))
    if not accounts.m.KEY_FILE.exists():
        raise Stop("для разбора нужен ключ Claude: Настройки и ключи")
    from agents import _claude
    done = []
    for i, v in enumerate(vids, 1):
        say(f"разбираю ролик {i} из {len(vids)} (@{v['acc']}, {accounts.fmt_n(v['views'])})…")
        f = download(v, say)
        met, content = {}, []
        if f:
            try:
                met = accounts.hook_metrics(f)
            except Exception:
                met = {}
            for t, b64 in frames(f, v["dur"] or met.get("dur") or 10):
                content.append({"type": "text", "text": f"кадр на {t:.1f} с"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
        content.append({"type": "text", "text": ONE.format(
            views=v["views"], likes=v["likes"], comments=v["comments"], shares=v["shares"], saves=v["saves"],
            dur=v["dur"] or met.get("dur", 0), desc=v["desc"][:200], met=json.dumps(met, ensure_ascii=False),
            text=(speech(f, cfg) if f else "")[:1200] or "(нет)")})
        br = _claude(content, cfg, 500, vision=True)
        if br:
            done.append({**{k: v[k] for k in ("acc", "url", "views", "likes", "comments", "shares", "saves", "dur", "desc")},
                         "metrics": met, "breakdown": br})
    if not done:
        raise Stop("Claude не разобрал ни одного ролика: проверь ключ")
    say("пишу книгу правил…")
    data = [{"views": d["views"], "eng": round((d["likes"] + d["comments"] * 2 + d["shares"] * 3 + d["saves"] * 2)
                                                / max(1, d["views"]) * 100, 1), "dur": d["dur"], "acc": d["acc"],
             "desc": d["desc"][:100], "metrics": d["metrics"], **d["breakdown"]} for d in done]
    book = _claude(BOOK.format(data=json.dumps(sorted(data, key=lambda x: -x["views"]), ensure_ascii=False)[:50000]),
                   cfg, 1800)
    if not book:
        raise Stop("книга правил не получилась, попробуй ещё раз")
    res = {"time": time.time(), "links": links, "errors": errors, "videos": done, "book": book}
    DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOK.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    say("готово")
    return res


def load():
    try:
        return json.loads(PLAYBOOK.read_text(encoding="utf-8"))
    except Exception:
        return None


def rules_text(limit=12):
    """Правила из книги для промптов критика и судьи."""
    pb = load()
    if not pb or not pb.get("book"):
        return ""
    b = pb["book"]
    lines = [f"- {x}" for x in (b.get("rules") or [])[:limit]]
    if b.get("length"):
        lines.append(f"- длина: {b['length']}")
    for x in (b.get("avoid") or [])[:4]:
        lines.append(f"- избегать: {x}")
    return "\n".join(lines)
