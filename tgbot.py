"""Отметки «залил / не буду» прямо в Telegram.

Под каждым роликом, который продюсер поставил в план, бот показывает две кнопки. Нажатие отмечается
в плане (analytics/schedule.json того профиля, где ролик): «Залил» — ролик вышел, «Не буду» — слот
освобождается под следующий сильный ролик. Команда /plan (или слово «план») — план на сегодня с отметками.

Бота слушает только одна копия программы на компьютере (замок на порту), даже если профилей несколько:
отметку она находит в плане любого профиля по короткому номеру ролика.
"""
import hashlib
import json
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import montage as m

BASE = m.BASE


def pid_for(clip_id, file=""):
    return hashlib.md5(f"{clip_id}|{file}".encode("utf-8")).hexdigest()[:10]


def buttons(pid):
    return {"inline_keyboard": [[{"text": "✅ Залил", "callback_data": f"pl:{pid}:ok"},
                                 {"text": "✕ Не буду", "callback_data": f"pl:{pid}:no"}]]}


PENDING = BASE / "analytics" / "tg_pending.json"   # ролики без плана (Запас и т.п.): ждут, на какой аккаунт залил


def _accounts():
    try:
        import agents
        return agents.settings()["accounts"][:6]
    except Exception:
        return []


def pick_buttons(p, hashtags=""):
    """Кнопки под роликом без места в плане: «✅ @аккаунт» для каждого своего аккаунта и «✕ Не буду».
    Нажатие записывает ролик в план как залитый на этот аккаунт (сегодня, текущее время)."""
    pid = "r" + pid_for(p.get("clip_id", ""), str(p.get("file_out") or p.get("final_title", "")))[:9]
    try:
        pend = json.loads(PENDING.read_text(encoding="utf-8"))
    except Exception:
        pend = {}
    cut = time.time() - 7 * 86400
    pend = {k: v for k, v in pend.items() if v.get("t", 0) >= cut}
    ag = p.get("agents") or {}
    pend[pid] = {"t": time.time(), "clip": p.get("clip_id", ""), "streamer": p.get("streamer", ""),
                 "score": ag.get("score"), "caption": p.get("final_title", ""), "hashtags": hashtags}
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps(pend, ensure_ascii=False, indent=1), encoding="utf-8")
    accs = _accounts()
    rows, row = [], []
    for i, a in enumerate(accs):
        row.append({"text": f"✅ @{a}", "callback_data": f"rs:{pid}:{i}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not accs:
        rows.append([{"text": "✅ Залил", "callback_data": f"rs:{pid}:ok"}])
    rows.append([{"text": "✕ Не буду", "callback_data": f"rs:{pid}:no"}])
    return {"inline_keyboard": rows}


def mark_pending(pid, action):
    """Отметка ролика без плана. Возвращает (аккаунт или "", залит ли) или None, если ролик не найден."""
    for r in _roots():
        f = r / "analytics" / "tg_pending.json"
        try:
            pend = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        x = pend.get(pid)
        if not x:
            continue
        if action == "no":
            return "", False
        acc = ""
        if action.isdigit():
            try:
                import agents
                st = json.loads((r / "analytics" / "settings.json").read_text(encoding="utf-8"))
                accs = []
                for a in st.get("accounts") or []:
                    mt = re.search(r"tiktok\.com/@([\w.\-]+)", str(a))
                    h = (mt.group(1) if mt else str(a)).strip().lstrip("@").strip("/").lower()
                    if h and h not in accs:
                        accs.append(h)
                acc = accs[int(action)] if int(action) < len(accs) else ""
            except Exception:
                acc = ""
        sf = r / "analytics" / "schedule.json"
        try:
            sc = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            sc = []
        sc = [y for y in sc if y.get("pid") != pid]
        now = datetime.now()
        sc.append({"pid": pid, "day": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "account": acc,
                   "status": "posted", "marked_at": now.strftime("%Y-%m-%d %H:%M"), "extra": True,
                   "streamer": x.get("streamer", ""), "score": x.get("score"), "caption": x.get("caption", ""),
                   "hashtags": x.get("hashtags", ""), "clip": x.get("clip", "")})
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")
        return acc, True
    return None


def _schedules():
    """Планы всех профилей на этом компьютере: C:\\clipper, C:\\clipper_dota, C:\\clipper_us и т.п."""
    roots = {BASE}
    try:
        roots |= {p for p in BASE.parent.iterdir() if p.is_dir() and p.name.lower().startswith("clipper")}
    except Exception:
        pass
    return [r / "analytics" / "schedule.json" for r in roots if (r / "analytics" / "schedule.json").exists()]


def mark(pid, action):
    """Ставит отметку. Возвращает запись плана или None."""
    for f in _schedules():
        try:
            sc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for x in sc:
            if x.get("pid") == pid:
                x["status"] = "posted" if action == "ok" else "dropped"
                x["marked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                f.write_text(json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8")
                return x
    return None


def plan_text():
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    for f in _schedules():
        try:
            sc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows += [x for x in sc if x.get("day") == today]
    if not rows:
        return "На сегодня в плане пусто."
    rows.sort(key=lambda x: (x.get("account", ""), x.get("time", "")))
    icon = {"posted": "✅", "dropped": "✕"}
    out, acc = ["План на сегодня:"], None
    for x in rows:
        if x.get("account") != acc:
            acc = x.get("account")
            out.append(f"\n@{acc}")
        out.append(f"{icon.get(x.get('status'), '⏳')} {x.get('time')} · {x.get('streamer', '')} · "
                   f"{x.get('score', '?')}/10 · {str(x.get('caption', ''))[:50]}")
    done = sum(1 for x in rows if x.get("status") == "posted")
    out.append(f"\nЗалито {done} из {len(rows)}")
    return "\n".join(out)


def _roots():
    """Папки профилей на этом компьютере, где стоит тот же бот, что и у этого профиля."""
    roots = {BASE}
    try:
        roots |= {p for p in BASE.parent.iterdir() if p.is_dir() and p.name.lower().startswith("clipper")}
    except Exception:
        pass
    try:
        mine = m.TG_TOKEN.read_text(encoding="utf-8-sig").strip()
    except Exception:
        mine = ""
    out = []
    for r in roots:
        try:
            if r == BASE or (r / "telegram_token.txt").read_text(encoding="utf-8-sig").strip() == mine:
                out.append(r)
        except Exception:
            pass
    return out


def allowed_chats():
    """Чаты, которым бот подчиняется: куда приходят ролики любого профиля с этим ботом (личка, группа
    с темами, «Запас», «Отчёты»). Чужие игнорируем. Бота может слушать любой профиль, поэтому смотрим все."""
    out = set()
    for r in _roots():
        for name in ("telegram_topics.json", "telegram_reserve.json", "telegram_reports.json"):
            try:
                f = r / name
                if f.exists():
                    out.add(str(json.loads(f.read_text(encoding="utf-8"))["chat_id"]))
            except Exception:
                pass
        try:
            f = r / "telegram_chat.txt"
            if f.exists():
                out.add(f.read_text(encoding="utf-8-sig").strip().split(":")[-1])
        except Exception:
            pass
    return out


def set_reserve(token, chat_id, thread=None, fname="telegram_reserve.json"):
    """Запоминает группу («Запас» или «Отчёты») во всех профилях с этим же ботом."""
    n = 0
    roots = {BASE}
    try:
        roots |= {p for p in BASE.parent.iterdir() if p.is_dir() and p.name.lower().startswith("clipper")}
    except Exception:
        pass
    for r in roots:
        tf = r / "telegram_token.txt"
        try:
            if tf.exists() and tf.read_text(encoding="utf-8-sig").strip() == token:
                (r / fname).write_text(json.dumps({"chat_id": chat_id, "thread": thread}), encoding="utf-8")
                n += 1
        except Exception:
            pass
    return n


LIVE_DIR = None                                  # webapp подставляет папку с записями запущенных профилей
YT = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*v=|live/|shorts/)|youtu\.be/|twitch\.tv/videos/)\S+")
HELP = """Команды:
/status — что сейчас делает программа (по каждому профилю)
/search — найти новые клипы прямо сейчас
/stray ссылки — нарезать видео с YouTube или Twitch (можно просто кинуть ссылки сообщением, хоть 10 штук)
/queue — очередь нарезки
/plan — план выкладки на сегодня
/report — отчёт цифрами сейчас
/week — разбор недели от Claude
/reports — (в отдельной группе) присылать отчёты сюда
/reserve — (в отдельной группе) присылать сюда ролики «Запас»"""


def live():
    """Запущенные профили на этом компьютере: [{key, label, port, token}]."""
    out = []
    if not LIVE_DIR:
        return out
    import urllib.request
    for f in sorted(Path(LIVE_DIR).glob("*.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
            with urllib.request.urlopen(f"http://127.0.0.1:{e['port']}/ping", timeout=1.5) as r:
                if r.read() == b"prodaem-clipper":
                    out.append(e)
        except Exception:
            pass
    out.sort(key=lambda e: (e.get("key") != "", e.get("key", "")))
    return out


def call(e, data, timeout=20):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{e['port']}/api/remote", data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": e["token"]})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _dur(sec):
    h, mm = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h} ч {mm} мин" if h else f"{mm} мин"


def status_text():
    ps = live()
    if not ps:
        return "Программа не отвечает. Похоже, она закрыта."
    out = ["🖥 Программа работает"]
    for e in ps:
        try:
            st = call(e, {"act": "status"})
        except Exception as ex:
            out.append(f"\n{e.get('label') or 'Профиль'}: не отвечает ({ex})")
            continue
        out.append(f"\n{st.get('label') or 'Профиль'} · работает {_dur(st.get('up', 0))}")
        if st.get("job"):
            out.append(f"🎬 Сейчас: {st['job']}")
        elif st.get("finding"):
            out.append("🔎 Ищет клипы")
        else:
            out.append("💤 Ничего не монтирует")
        if st.get("jobs"):
            out.append(f"Ждут монтажа: {st['jobs']}")
        out.append(f"Клипов в очереди: {st.get('queue', 0)} · смонтировано за запуск: {st.get('done', 0)}"
                   + (f", ошибок {st['failed']}" if st.get("failed") else ""))
        if st.get("vod") or st.get("vod_wait"):
            out.append(f"✂️ Нарезка видео: {'идёт' if st.get('vod') else 'не идёт'}, в очереди ждут {st.get('vod_wait', 0)}")
        out.append(f"💾 Свободно на диске: {st.get('free_gb', '?')} ГБ")
    return "\n".join(out)


def stray_target(ps):
    """Куда класть видео на нарезку: профиль DOTA, если он есть, иначе главный."""
    for e in ps:
        if e.get("key") == "dota" or "dota" in str(e.get("label", "")).lower():
            return e
    return ps[0] if ps else None


class Bot:
    def __init__(self, log=print):
        self.log = log
        self.lock = None
        self.stop = threading.Event()

    def _token(self):
        if not m.TG_TOKEN.exists():
            return None
        t = m.TG_TOKEN.read_text(encoding="utf-8-sig").strip()
        return t or None

    def _api(self, token, method, http_timeout=35, **data):
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=data, timeout=http_timeout)
        return r.json()

    def _take_lock(self, token):
        """Один слушатель на бота на всём компьютере."""
        port = 47700 + int(hashlib.md5(token.split(":")[0].encode()).hexdigest(), 16) % 200
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            s.listen(1)
            return s
        except OSError:
            s.close()
            return None

    def run(self):
        off_file = BASE / "telegram_offset.txt"
        while not self.stop.is_set():
            token = self._token()
            if not token:
                self.stop.wait(60)
                continue
            if self.lock is None:
                self.lock = self._take_lock(token)
                if self.lock is None:            # бота уже слушает другой профиль
                    self.stop.wait(300)
                    continue
                try:
                    self._api(token, "setMyCommands", commands=[
                        {"command": "status", "description": "что делает программа"},
                        {"command": "search", "description": "найти новые клипы"},
                        {"command": "stray", "description": "нарезать видео по ссылкам"},
                        {"command": "queue", "description": "очередь нарезки"},
                        {"command": "plan", "description": "план выкладки на сегодня"},
                        {"command": "report", "description": "отчёт цифрами"},
                        {"command": "week", "description": "разбор недели"},
                        {"command": "help", "description": "все команды"}])
                except Exception:
                    pass
            try:
                off = int(off_file.read_text().strip()) if off_file.exists() else 0
            except Exception:
                off = 0
            try:
                r = self._api(token, "getUpdates", http_timeout=40, offset=off, timeout=25,
                              allowed_updates=["callback_query", "message"])
            except Exception:
                self.stop.wait(15)
                continue
            if not r.get("ok"):
                self.stop.wait(30)
                continue
            for u in r.get("result", []):
                off = max(off, u["update_id"] + 1)
                try:
                    self.handle(token, u)
                except Exception as e:
                    self.log(f"телеграм-бот: {e}")
            try:
                off_file.write_text(str(off))
            except Exception:
                pass

    def handle(self, token, u):
        cq = u.get("callback_query")
        chat = str((((cq or {}).get("message") or u.get("message") or {}).get("chat") or {}).get("id", ""))
        msg0 = u.get("message") or {}
        low0 = (msg0.get("text") or "").strip().lower()
        if low0.startswith(("/reports", "/otchet", "/отчеты", "/отчёты")):
            ok_chats = allowed_chats()
            sender = str((msg0.get("from") or {}).get("id", ""))
            if not ok_chats or sender in ok_chats or str(msg0["chat"]["id"]) in ok_chats or m.TG_TOPICS.exists():
                n = set_reserve(token, msg0["chat"]["id"], msg0.get("message_thread_id"), "telegram_reports.json")
                data = {"chat_id": msg0["chat"]["id"], "text": "📊 Готово: сюда будут приходить отчёт каждый день в 22:00, "
                        "разбор недели по понедельникам и сообщения, если программа не работала." + ("" if n else
                        "\n(не нашёл профиль с этим ботом, проверь токен в настройках)")}
                if msg0.get("message_thread_id"):
                    data["message_thread_id"] = msg0["message_thread_id"]
                self._api(token, "sendMessage", **data)
            return
        if low0.startswith(("/reserve", "/zapas", "/запас")):
            ok_chats = allowed_chats()
            sender = str((msg0.get("from") or {}).get("id", ""))
            if not ok_chats or sender in ok_chats or str(msg0["chat"]["id"]) in ok_chats or m.TG_TOPICS.exists():
                n = set_reserve(token, msg0["chat"]["id"], msg0.get("message_thread_id"))
                data = {"chat_id": msg0["chat"]["id"], "text": "🟡 Готово: сюда будут приходить ролики «Запас» — оценка 5–6 "
                        "и те, что не влезли в план. Выкладывай их, если основных не хватает." + ("" if n else
                        "\n(не нашёл профиль с этим ботом, проверь токен в настройках)")}
                if msg0.get("message_thread_id"):
                    data["message_thread_id"] = msg0["message_thread_id"]
                self._api(token, "sendMessage", **data)
            return
        ok_chats = allowed_chats()
        if ok_chats and chat not in ok_chats:   # пишут из чужого чата: не слушаемся
            if cq:
                self._api(token, "answerCallbackQuery", callback_query_id=cq["id"])
            return
        if cq and str(cq.get("data", "")).startswith("rs:"):
            parts = str(cq["data"]).split(":")
            res = mark_pending(parts[1], parts[2]) if len(parts) == 3 and parts[2] != "noop" else None
            if res is None:
                self._api(token, "answerCallbackQuery", callback_query_id=cq["id"],
                          text="" if len(parts) == 3 and parts[2] == "noop" else "Этого ролика уже нет")
                return
            acc, ok = res
            now = datetime.now().strftime("%H:%M")
            label = (f"✅ Залито на @{acc} в {now}" if acc else f"✅ Залито в {now}") if ok else "✕ Не выкладываем"
            self._api(token, "answerCallbackQuery", callback_query_id=cq["id"],
                      text=("Отмечено: " + (f"залит на @{acc}" if acc else "залит")) if ok else "Отмечено: не выкладываем")
            msg = cq.get("message") or {}
            if msg:
                self._api(token, "editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                          reply_markup={"inline_keyboard": [[{"text": label, "callback_data": f"rs:{parts[1]}:noop"}]]})
            self.log(f"телеграм: ролик из запаса — {label}")
            return
        if cq:
            parts = str(cq.get("data", "")).split(":")
            if len(parts) != 3 or parts[0] != "pl" or parts[2] not in ("ok", "no"):
                self._api(token, "answerCallbackQuery", callback_query_id=cq["id"])
                return
            x = mark(parts[1], parts[2])
            if not x:
                self._api(token, "answerCallbackQuery", callback_query_id=cq["id"],
                          text="Этого ролика уже нет в плане")
                return
            ok = parts[2] == "ok"
            label = f"✅ Залито в {datetime.now().strftime('%H:%M')}" if ok else "✕ Не выкладываем, слот свободен"
            self._api(token, "answerCallbackQuery", callback_query_id=cq["id"],
                      text=("Отмечено: залит на @" if ok else "Слот освобождён на @") + x.get("account", ""))
            msg = cq.get("message") or {}
            if msg:
                self._api(token, "editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                          reply_markup={"inline_keyboard": [[{"text": label, "callback_data": f"pl:{parts[1]}:noop"}]]})
            self.log(f"телеграм: @{x.get('account')} {x.get('time')} — {'залит' if ok else 'не будет'}")
            return
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        if not text or not msg.get("chat"):
            return
        data = {"chat_id": msg["chat"]["id"]}
        if msg.get("message_thread_id"):
            data["message_thread_id"] = msg["message_thread_id"]
        raw = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0] if text.startswith("/") else text

        def say(t):
            self._api(token, "sendMessage", text=t[:3900], disable_web_page_preview=True, **data)

        if cmd in ("/plan", "план", "plan"):
            say(plan_text())
        elif cmd in ("/status", "статус", "status"):
            say(status_text())
        elif cmd in ("/help", "/start", "помощь", "команды"):
            say(HELP)
        elif cmd in ("/search", "поиск", "/find"):
            ps = live()
            if not ps:
                return say("Программа не отвечает. Похоже, она закрыта.")
            n = 0
            for e in ps:
                try:
                    r = call(e, {"act": "find"})
                    n += 1 if (r.get("started") or r.get("busy")) else 0
                except Exception:
                    pass
            say(f"🔎 Ищу новые клипы (профилей: {n}). Хорошие смонтируются и придут сюда сами.")
        elif cmd in ("/queue", "очередь"):
            e = stray_target(live())
            if not e:
                return say("Программа не отвечает. Похоже, она закрыта.")
            items = call(e, {"act": "vod_queue"}).get("items") or []
            if not items:
                return say("Очередь нарезки пуста. Кинь ссылки на видео с YouTube сообщением.")
            icon = {"wait": "⏳", "run": "✂️", "done": "✅", "error": "⚠️"}
            say("Очередь нарезки:\n" + "\n".join(f"{icon.get(x['status'], '•')} {x['url'][:70]}" for x in items))
        elif cmd in ("/report", "отчет", "отчёт", "/week", "разбор"):
            week = cmd in ("/week", "разбор")
            ps, ok, err = live(), 0, ""
            for e in ps:
                try:
                    r = call(e, {"act": "week" if week else "report"})
                    if r.get("ok"):
                        ok += 1
                    elif r.get("error") and r["error"] != "no_accounts":
                        err = r["error"]
                except Exception:
                    pass
            say(("📊 Собираю цифры, " + ("разбор недели" if week else "отчёт") + " придёт в группу отчётов через пару минут.")
                if ok else (err or "Не вышло: программа закрыта или в аналитике не вписаны аккаунты."))
        elif cmd in ("/stray", "/vod", "/cut") or YT.search(raw):
            urls = list(dict.fromkeys(u.rstrip(").,;") for u in YT.findall(raw)))
            if not urls:
                return say("Пришли ссылки на видео с YouTube или Twitch: /stray ссылка1 ссылка2 … (или просто ссылки сообщением)")
            e = stray_target(live())
            if not e:
                return say("Программа не отвечает. Похоже, она закрыта, ссылки не добавлены.")
            r = call(e, {"act": "stray_add", "urls": urls})
            say(f"✂️ В очередь нарезки ({e.get('label') or 'профиль'}): {r.get('added', 0)}"
                + (f", повторов пропущено {r['dup']}" if r.get("dup") else "")
                + f". Всего ждут {r.get('wait', 0)}. Режу по одному, когда программа ничего не монтирует, "
                  "фрагменты сами пройдут отбор и придут сюда.")



def start(log=print):
    b = Bot(log)
    threading.Thread(target=b.run, daemon=True).start()
    return b
