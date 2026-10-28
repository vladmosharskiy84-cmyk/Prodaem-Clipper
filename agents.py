"""Агенты после монтажа: критик, судья и продюсер.

Критик смотрит кадры готового ролика и цифры (как начинается, где пик, где тишина) и пишет, что не так.
Судья по словам критика решает: выкладывать, переделать (обрезать начало до пика или хвост) или в брак.
Если «переделать», ролик монтируется заново и критик смотрит ещё раз (не больше 2 кругов).
Продюсер для одобренного ролика выбирает аккаунт и время по ролям аккаунтов и аналитике
и пишет описание с вопросом для комментариев. Всё по ключу Claude из настроек.
"""
import base64
import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import montage as m

BASE = m.BASE
DIR = BASE / "analytics"
SCHEDULE = DIR / "schedule.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SLOTS = ["14:00", "18:00", "21:00"]


def slots():
    """Слоты выхода: по умолчанию 14/18/21, после недели аналитики — часы с лучшими просмотрами."""
    try:
        sl = json.loads((DIR / "settings.json").read_text(encoding="utf-8")).get("slots")
        if sl and len(sl) >= 3:
            return list(sl)[:3]
    except Exception:
        pass
    return list(SLOTS)
DEFAULT_ROLES = {
    "prodaem.cs": "тимфайты, рампаги, камбэки: самые эпичные драки",
    "prodaem.tw": "эмоции стримеров: крики, горение, реакции",
    "prodaem.narezhet": "фейлы и смешное: тупые смерти, неудачи",
    "shsuussusuu": "старые смешные моменты Стрея, ностальгия",
}


# ---------- общее ----------
def _key():
    if not m.KEY_FILE.exists():
        return None
    k = m.KEY_FILE.read_text(encoding="utf-8-sig").strip()
    return k or None


def _claude(content, cfg, max_tokens=900, vision=False):
    """Ответ Claude в виде JSON-объекта или None. Сначала модель посильнее, при отказе обычная."""
    key = _key()
    if not key:
        return None
    import requests
    models = [cfg.get("agent_model", "claude-haiku-5-5"), cfg.get("claude_model", "claude-haiku-4-5")]
    for model in dict.fromkeys(models):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": model, "max_tokens": max_tokens,
                                    "messages": [{"role": "user", "content": content}]}, timeout=120)
        except Exception as e:
            print(f"  агент: нет связи с Claude ({e})")
            continue
        if r.status_code != 200:
            continue
        out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        out = re.sub(r"```(?:json)?|```", "", out).strip()
        try:
            return json.loads(out[out.index("{"):out.rindex("}") + 1])
        except Exception:
            continue
    return None


def _playbook(prefix):
    try:
        import leaders
        t = leaders.rules_text()
    except Exception:
        t = ""
    return (prefix + t + "\n") if t else ""


def settings():
    try:
        s = json.loads((DIR / "settings.json").read_text(encoding="utf-8"))
    except Exception:
        s = {}
    accs = []                                    # только аккаунты, вписанные в «Аналитике» на этом компьютере
    for a in s.get("accounts") or []:
        mt = re.search(r"tiktok\.com/@([\w.\-]+)", str(a))
        h = (mt.group(1) if mt else str(a)).strip().lstrip("@").strip("/").lower()
        if h and h not in accs:
            accs.append(h)
    roles = {str(k).lstrip("@").lower(): v for k, v in (s.get("roles") or {}).items()}
    return {"accounts": accs, "roles": {a: roles.get(a) or DEFAULT_ROLES.get(a, "дота-клипы") for a in accs},
            "min_views": s.get("min_views", 6000) if s.get("terms", 1) >= 2 or s.get("min_views", 3000) != 3000 else 6000, "rules": s.get("rules") or [],
            "min_score": float(s.get("min_score", 7)), "fresh_hours": float(s.get("fresh_hours", 48))}


def frames(video, dur, n=6):
    """Кадры готового ролика для критика: начало (крючок), середина, конец."""
    ts = [0.3, 1.2, dur * 0.3, dur * 0.5, dur * 0.75, max(0.0, dur - 0.6)][:n]
    out = []
    for t in ts:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=360:-2", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                           capture_output=True, creationflags=NOWIN)
        if r.stdout:
            out.append((t, base64.b64encode(r.stdout).decode()))
    return out


def transcript(words):
    lines, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["s"]
        cur.append(w["t"])
        if w["t"][-1:] in ".!?…" or len(cur) >= 10:
            lines.append(f"[{t0:.1f}] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        lines.append(f"[{t0:.1f}] {' '.join(cur)}")
    return "\n".join(lines) or "(речи почти нет)"


# ---------- критик ----------
CRITIC = """Ты строгий критик коротких роликов TikTok из клипов стримов по Dota 2. Ролик уже смонтирован:
сверху камера стримера, снизу игра, субтитры по одному слову, в середине баннер партнёрки (его не трогаем).
Звук не меняем. Задача ролика: пройти пробный показ TikTok (~700 просмотров) и набрать 6000+ (в выплату идут только ролики от 6000).
Для этого важнее всего первые 1-2 секунды, отсутствие пустых мест и резкий конец сразу после реакции.

Что есть:
- кадры готового ролика (секунда указана перед каждым);
- цифры готового ролика: {metrics}
  (hook_loud: насколько первые 2 с громче обычного, дБ; hook_motion: насколько динамичнее, 1 = как весь ролик;
  dead_air: секунд тишины дольше секунды; peak_at: секунда самого громкого места);
- исходный клип длиной {src_dur:.1f} с; громкие места в исходнике (секунды): {peaks};
- речь исходника с секундами:
{text}
{playbook}
Оцени честно. Если начало пустое, а сильный момент позже, предложи start_at: секунда ИСХОДНИКА, с которой
начать (за 0.3-0.8 с до крика или первой фразы реакции). Если после реакции хвост, предложи end_at.
Не предлагай обрезку, если ролик станет короче 10 секунд.

Ответ ТОЛЬКО JSON:
{{"score": от 1 до 10, "hook": "что видит зритель в первую секунду и цепляет ли", "issues": ["конкретная проблема", ...],
"good": ["что хорошо", ...], "start_at": число или null, "end_at": число или null,
"moment": "одной фразой, что за момент (для продюсера)", "kind": "драка|эмоция|фейл|смешное|разговор|другое"}}"""


def critic(video, clip, words, cfg, log=print):
    from accounts import hook_metrics
    met = hook_metrics(video)
    dur = met.get("dur") or m.probe(video)["dur"]
    try:
        peaks = [round(t, 1) for t, _, _ in m.zoom_events(clip, max_n=6)]
    except Exception:
        peaks = []
    src_dur = m.probe(clip)["dur"]
    content = []
    for t, b64 in frames(video, dur):
        content.append({"type": "text", "text": f"кадр на {t:.1f} с"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
    content.append({"type": "text", "text": CRITIC.format(metrics=json.dumps(met, ensure_ascii=False), src_dur=src_dur,
                                                          peaks=peaks or "нет", text=transcript(words)[:5000],
                                                          playbook=_playbook("\nПравила, выведенные из прострелов "
                                                                             "лидеров ниши (сверяй ролик с ними):\n"))})
    res = _claude(content, cfg, 900, vision=True)
    if not res:
        return None
    res["metrics"] = met
    res["src_dur"] = src_dur
    try:
        res["score"] = int(res.get("score") or 5)
    except Exception:
        res["score"] = 5
    for k in ("start_at", "end_at"):
        v = res.get(k)
        res[k] = float(v) if isinstance(v, (int, float)) and 0 <= float(v) <= src_dur else None
    a, b = res["start_at"] or 0.0, res["end_at"] or src_dur
    if b - a < 10:
        res["start_at"] = res["end_at"] = None
    log(f"  критик: {res['score']}/10. " + "; ".join(str(x) for x in (res.get("issues") or [])[:3]))
    return res


# ---------- судья ----------
JUDGE = """Ты судья в команде клиппера Dota 2 для TikTok. Платят только за ролики, набравшие {min_views}+ просмотров,
поэтому слабый ролик выкладывать вредно: он тратит слот и учит TikTok, что аккаунт не досматривают.
Круг проверки {rnd} из 3.{prev}

Отчёт критика:
{report}

Правила автора, которые важнее всего:
{rules}

Реши:
- "publish": ролик готов, недочёты мелкие;
- "fix": ролик станет заметно сильнее, если обрезать начало до сильного места или убрать хвост (только если критик
  дал start_at или end_at, и это не первый круг без пользы);
- "reject": сам момент слабый, никакая обрезка не поможет пройти 6000.

Ответ ТОЛЬКО JSON: {{"decision": "publish|fix|reject", "reason": "одна короткая фраза по-русски",
"start_at": число или null, "end_at": число или null}}"""


def judge(report, rnd, cfg, prev=""):
    st = settings()
    rules = "\n".join(f"- {r}" for r in st["rules"]) or "- звук не трогаем\n- паузы не трогаем без нужды"
    rules += _playbook("\nПравила из прострелов лидеров ниши:\n")
    rep = {k: v for k, v in report.items() if k not in ("metrics",)}
    rep["metrics"] = report.get("metrics")
    res = _claude(JUDGE.format(min_views=st["min_views"], rnd=rnd, prev=prev,
                               report=json.dumps(rep, ensure_ascii=False), rules=rules), cfg, 300)
    if not res:                                  # без ответа судьи решаем по оценке критика
        sc = report.get("score", 5)
        return {"decision": "publish" if sc >= 5 else "reject", "reason": f"оценка критика {sc}/10",
                "start_at": None, "end_at": None}
    d = str(res.get("decision", "publish")).lower()
    res["decision"] = d if d in ("publish", "fix", "reject") else "publish"
    for k in ("start_at", "end_at"):
        v = res.get(k, report.get(k))
        res[k] = float(v) if isinstance(v, (int, float)) else None
    if res["decision"] == "fix" and res["start_at"] is None and res["end_at"] is None:
        res["decision"] = "publish"
    return res


# ---------- продюсер ----------
PRODUCER = """Ты продюсер четырёх TikTok-аккаунтов с клипами по Dota 2 (аудитория СНГ).
Роли аккаунтов:
{roles}
{stats}
Новый ролик: стример {streamer}; момент: {moment}; тип: {kind}; оценка критика {score}/10.
Речь из ролика: {text}

Расставь аккаунты, которым ролик подходит по роли, от лучшего к худшему (не больше трёх; если подходит только
один, дай один). Напиши описание для TikTok: 1 короткая цепляющая фраза на русском плюс вопрос зрителям, чтобы
писали комментарии. Без кликбейта-лжи. Хэштеги: 3-5 штук по теме (стример, dota2 и т.п.).

Ответ ТОЛЬКО JSON: {{"accounts": ["ник без @", ...], "caption": "...", "hashtags": "#... #...", "why": "коротко"}}"""

PER_DAY = 3


def _load_schedule():
    try:
        sc = json.loads(SCHEDULE.read_text(encoding="utf-8"))
    except Exception:
        sc = []
    cut = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    return [x for x in sc if x.get("day", "") >= cut]


def _save_schedule(sc):
    DIR.mkdir(parents=True, exist_ok=True)
    SCHEDULE.write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")


def _target_day():
    """День, на который расписываем: сегодня, а если все слоты сегодня уже прошли, завтра."""
    now = datetime.now()
    last = datetime.strptime(now.strftime("%Y-%m-%d") + " " + slots()[-1], "%Y-%m-%d %H:%M")
    return (now if now + timedelta(minutes=20) <= last else now + timedelta(days=1)).strftime("%Y-%m-%d")


def _future(day, t):
    return datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M") >= datetime.now() + timedelta(minutes=20)


def producer(p, report, words, cfg, log=print):
    """Только 3 лучших ролика в день на аккаунт. Свободный слот — ставим. Нет слота — новый ролик вытесняет
    самый слабый из ещё не вышедших, если он сильнее. Иначе ролик в запас: в Telegram он не уходит."""
    st = settings()
    accs = st["accounts"]
    if not accs:
        return None
    sc = [x for x in _load_schedule() if x.get("account") in accs]   # чужие аккаунты (из старых версий) убираем
    stats = ""
    try:
        rep = json.loads((DIR / "report.json").read_text(encoding="utf-8"))
        a_ = rep.get("accounts") or {}
        stats = "Аналитика (медиана просмотров, % застрявших на пробе): " + ", ".join(
            f"@{a}: {v.get('median')} / {v.get('stuck')}%" for a, v in a_.items()) + "\n"
    except Exception:
        pass
    score = float((report or {}).get("score") or 0)
    res = _claude(PRODUCER.format(
        roles="\n".join(f"- @{a}: {r}" for a, r in st["roles"].items()), stats=stats,
        streamer=p.get("streamer", ""), moment=(report or {}).get("moment") or p.get("final_title", ""),
        kind=(report or {}).get("kind", ""), score=score,
        text=" ".join(w["t"] for w in words)[:1500]), cfg, 400) or {}
    ranked = [str(a).lstrip("@").lower() for a in (res.get("accounts") or [res.get("account", "")])]
    ranked = [a for a in dict.fromkeys(ranked) if a in accs] or list(accs)
    day = _target_day()
    base = {"caption": str(res.get("caption") or p.get("final_title", "")).strip(),
            "hashtags": str(res.get("hashtags") or f"#{p.get('streamer', '')} #dota2").strip(),
            "why": str(res.get("why", "")).strip(), "clip": p.get("clip_id", ""), "file": p.get("file_out", ""),
            "score": score, "streamer": p.get("streamer", "")}
    plan, bumped = None, None
    for acc in ranked:                                       # свободный слот на подходящем аккаунте
        taken = {x["time"] for x in sc if x["account"] == acc and x["day"] == day and x.get("status") != "dropped"}
        free = [t for t in slots() if t not in taken and _future(day, t)]
        if free:
            plan = dict(base, account=acc, day=day, time=free[0])
            break
    if not plan:                                             # слотов нет: вытесняем самый слабый ещё не вышедший
        cands = [x for x in sc if x["account"] in ranked and x["day"] == day and _future(day, x["time"])
                 and not x.get("status")]                 # уже залитые и отменённые не трогаем
        if cands:
            weak = min(cands, key=lambda x: float(x.get("score") or 0))
            if score > float(weak.get("score") or 0):
                sc.remove(weak)
                bumped = weak
                plan = dict(base, account=weak["account"], day=day, time=weak["time"])
    if not plan:
        log(f"  продюсер: в запас — на сегодня у подходящих аккаунтов уже по {PER_DAY} ролика сильнее")
        return {"reserve": True, "why": f"на {day[5:]} у подходящих аккаунтов уже по {PER_DAY} ролика сильнее"}
    sc = [x for x in sc if not (x["account"] == plan["account"] and x["day"] == day and x["time"] == plan["time"]
                                and x.get("status") == "dropped")]
    try:
        import tgbot
        plan["pid"] = tgbot.pid_for(plan["clip"], plan["file"])
    except Exception:
        pass
    sc.append(plan)
    _save_schedule(sc)
    now = datetime.now()
    plan["when"] = ("сегодня " if day == now.strftime("%Y-%m-%d") else "завтра ") + plan["time"]
    if bumped:
        plan["bumped"] = bumped
        log(f"  продюсер: вытеснил более слабый ролик ({bumped.get('streamer', '')}, {bumped.get('score')}/10) "
            f"со слота @{plan['account']} {plan['time']}")
    log(f"  продюсер: @{plan['account']}, {plan['when']}. {plan['why']}")
    return plan


# ---------- весь круг ----------
def stale(p, hours):
    """Клип с Twitch старше hours часов: уже неактуален. Нарезка стрима и свои файлы не проверяются."""
    if p.get("own") or p.get("source") == "vod" or not p.get("created_at"):
        return None
    try:
        made = datetime.strptime(str(p["created_at"])[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None
    from datetime import timezone
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - made).total_seconds() / 3600
    return age if age > hours else None


def review(make, clip, words, cfg, log=print, p=None):
    """make(force_extra) монтирует ролик и возвращает (путь, результат монтажа).
    Возвращает (путь, результат, отчёт агентов) после критика, судьи и, если надо, переделки."""
    st = settings()
    age = stale(p or {}, st["fresh_hours"])
    if age is not None and _key():
        log(f"  судья: в брак — клипу {age / 24:.1f} дн., это уже неактуально")
        return None, None, {"rounds": [], "decision": "reject", "score": 0, "issues": [],
                            "reason": f"клипу {age / 24:.1f} дн.: неактуально, выкладываем только свежее"}
    out, res = make({})
    if not out or not _key():
        return out, res, None
    rounds, prev, fix = [], "", {}
    for rnd in range(1, 4):
        log(f"  критик смотрит ролик (круг {rnd})…")
        try:
            rep = critic(out, clip, words, cfg, log)
        except Exception as e:
            log(f"  критик не сработал: {e}")
            rep = None
        if not rep:
            break
        jd = judge(rep, rnd, cfg, prev)
        log(f"  судья: {dict(publish='выкладывать', fix='переделать', reject='в брак')[jd['decision']]}. {jd['reason']}")
        rounds.append({"critic": {k: v for k, v in rep.items() if k != "metrics"}, "metrics": rep.get("metrics"),
                       "judge": jd})
        if jd["decision"] != "fix" or rnd == 3:
            break
        new = {k: jd[k] for k in ("start_at", "end_at") if jd.get(k) is not None}
        if new == fix:                           # то же самое уже пробовали
            break
        fix = new
        prev = f" В прошлом круге уже обрезали: {new}. Оцени, стало ли лучше."
        log("  переделываю по решению судьи…")
        o2, r2 = make(fix)
        if not o2:
            break
        out, res = o2, r2
    if not rounds:
        return out, res, None
    last = rounds[-1]
    decision = last["judge"]["decision"]
    if decision == "fix":
        decision = "publish"
    reason = last["judge"].get("reason", "")
    sc_ = float(last["critic"].get("score") or 0)
    if decision == "publish" and sc_ < st["min_score"]:   # строго: проходят только сильные
        decision, reason = "reject", f"оценка {sc_:g}/10 ниже порога {st['min_score']:g}"
        log(f"  судья: в брак — {reason}")
    return out, res, {"rounds": rounds, "decision": decision, "score": last["critic"].get("score"),
                      "reason": reason, "moment": last["critic"].get("moment", ""),
                      "kind": last["critic"].get("kind", ""), "issues": last["critic"].get("issues", []),
                      "fix": fix or None}
