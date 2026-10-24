# -*- coding: utf-8 -*-
"""
Полуавтомат: найденные клипы попадают в очередь, ты просматриваешь каждый, сам обводишь камеру,
выбираешь формат и жмёшь «Смонтировать». Монтаж идёт в фоне, а ты сразу берёшь следующий клип.

Запуск:  python review.py     (или ярлык Clipper на рабочем столе)
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

if "--data" in sys.argv:                             # одна программа, данные профиля (СНГ, US, DOTA) в своей папке
    os.environ["CLIPPER_DATA"] = sys.argv[sys.argv.index("--data") + 1]

import montage as m

BASE = Path(os.environ.get("CLIPPER_DATA") or Path(__file__).resolve().parent).resolve()   # папка профиля с данными
CODE = Path(__file__).resolve().parent                     # папка с программой
STATE_FILE = BASE / "review_state.json"
PASSPORTS = BASE / "passports.jsonl"

FORMATS = [
    ("talk", "Разговорный: стример на весь кадр", "Можно ничего не обводить. Если нужна только часть кадра, обведи её."),
    ("talk_satis", "Разговорный + залипательное снизу", "Как разговорный, снизу пойдёт видео из папки satisfying."),
    ("react", "Реакция: камера + то, что он смотрит", "Обведи окно камеры стримера."),
    ("cs", "КС: камера + игра", "Обведи окно камеры стримера."),
    ("cs_nocam", "КС без камеры: большое окно с игрой", "Ничего обводить не нужно: будет одно большое окно с игрой и голос стримера."),
    ("multi", "Несколько стримеров", "Обведи камеру каждого, от 2 до 4 рамок."),
]


try:                                             # в профиле другой игры подписи форматов называют её, а не КС
    _GL = json.loads((BASE / "montage.json").read_text(encoding="utf-8")).get("game_label")
    if _GL:
        FORMATS = [(k, lab.replace("КС", _GL), h.replace("КС", _GL)) for k, lab, h in FORMATS]
except Exception:
    pass


# ---------- состояние очереди ----------
def load_state():
    """Состояние очереди. Клипы, которые в прошлый раз не досмонтировались (программу закрыли
    или монтаж упал с ошибкой), при запуске возвращаются в очередь."""
    st = {"clips": {}, "last": {}, "own": []}
    if STATE_FILE.exists():
        try:
            st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    for cid in [k for k, v in st.get("clips", {}).items() if v in ("queued", "retry", "failed")]:
        st["clips"].pop(cid)
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def load_queue(st):
    """Клипы, которые ещё не смонтированы и не отклонены: сначала свои файлы, потом свежие и популярные."""
    items = []
    for own in st.get("own", []):
        if own["clip_id"] not in st["clips"] and Path(own["file"]).exists():
            items.append(own)
    found = []
    try:                                          # в очереди только свежие клипы: устаревшие не показываем
        fresh_h = float(json.loads((BASE / "analytics" / "settings.json").read_text(encoding="utf-8")).get("fresh_hours", 48))
    except Exception:
        fresh_h = 48.0
    now_utc = time.time()
    if PASSPORTS.exists():
        seen = set()
        for line in PASSPORTS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            if p["clip_id"] in seen or p["clip_id"] in st["clips"]:
                continue
            seen.add(p["clip_id"])
            raw = BASE / p["file"]
            if not raw.exists():
                continue
            if p.get("created_at") and p.get("source") != "vod":
                try:
                    import calendar
                    made = calendar.timegm(time.strptime(str(p["created_at"])[:19], "%Y-%m-%dT%H:%M:%S"))
                    if now_utc - made > fresh_h * 3600:
                        continue
                except Exception:
                    pass
            old = BASE / "clips" / "ready" / p["format"] / f"{p['streamer']}_{p['clip_id'][:16]}.mp4"
            if old.exists():                      # уже смонтирован автоматическим режимом
                continue
            p = dict(p, file=str(raw))
            found.append(p)
    found.sort(key=lambda p: (p.get("found_at", ""), p.get("score", 0), p.get("twitch_views", 0)), reverse=True)
    return items + found


def add_own(st, path):
    path = str(Path(path).resolve())
    cid = "own_" + str(abs(hash((path, os.path.getmtime(path)))))[:12]
    item = {"clip_id": cid, "streamer": "", "title": Path(path).stem, "format": "talk", "twitch_views": 0,
            "file": path, "own": True, "duration": m.probe(path)["dur"]}
    st.setdefault("own", []).append(item)
    save_state(st)
    return item


# ---------- пул стримеров ----------
STREAMERS_FILE = BASE / "streamers.txt"


def norm_login(text):
    """Из того, что ввели (ник или ссылка на канал), достаёт ник Twitch. Пустая строка, если это не ник."""
    import re
    t = text.strip().lower()
    t = re.sub(r"^https?://", "", t)
    t = re.sub(r"^(www\.|m\.)?twitch\.tv/", "", t)
    t = t.split("/")[0].split("?")[0].lstrip("@")
    return t if re.fullmatch(r"[a-z0-9_]{3,25}", t) else ""


def load_streamers():
    if not STREAMERS_FILE.exists():
        return []
    out = []
    for line in STREAMERS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip().lower()
        if line and line not in out:
            out.append(line)
    return out


def save_streamers(logins):
    STREAMERS_FILE.write_text("# Пул стримеров: один ник Twitch в строке\n" + "\n".join(logins) + "\n", encoding="utf-8")


def streamers_dialog(root, st, dark, on_change, say):
    """Окно, где можно добавить стримера в пул или убрать его."""
    import tkinter as tk
    bg, fg, box, acc = (CARD, TXT, BTN, ACC) if dark else ("#f0f0f0", "#000000", "#ffffff", "#0a58ca")
    win = tk.Toplevel(root)
    win.title("Пул стримеров")
    win.geometry("420x560")
    win.configure(bg=bg)
    win.transient(root)
    tk.Label(win, text="Стримеры, у которых ищем клипы", bg=bg, fg=fg, font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
    info = tk.Label(win, text="", bg=bg, fg="#8b90a0", font=("Segoe UI", 9), anchor="w", justify="left", wraplength=380)
    info.pack(fill="x", padx=16)
    lst = tk.Listbox(win, bg=box, fg=fg, font=("Consolas", 11), selectbackground=acc, selectforeground="white",
                     borderwidth=0, highlightthickness=1, highlightbackground="#252832", activestyle="none", height=16)
    lst.pack(fill="both", expand=True, padx=16, pady=8)
    row = tk.Frame(win, bg=bg)
    row.pack(fill="x", padx=16)
    entry = tk.Entry(row, bg=box, fg=fg, insertbackground=fg, font=("Segoe UI", 11), relief="flat",
                     highlightthickness=1, highlightbackground="#252832")
    entry.pack(side="left", fill="x", expand=True, ipady=5)

    def fill(msg=""):
        logins = load_streamers()
        lst.delete(0, "end")
        for x in logins:
            lst.insert("end", "  " + x)
        info.config(text=msg or f"Всего: {len(logins)}. Можно вставить ник или ссылку на канал.")

    def add(*_):
        login = norm_login(entry.get())
        if not login:
            fill("Не похоже на ник Twitch. Нужны латинские буквы, цифры и подчёркивание.")
            return
        logins = load_streamers()
        if login in logins:
            fill(f"{login} уже есть в списке.")
            return
        save_streamers(logins + [login])
        entry.delete(0, "end")
        fill(f"Добавлен {login}. Его клипы появятся после следующего поиска.")
        say(f"Пул стримеров: добавлен {login}")
        on_change(True)

    def remove():
        if not lst.curselection():
            fill("Сначала выбери стримера в списке.")
            return
        login = lst.get(lst.curselection()[0]).strip()
        save_streamers([x for x in load_streamers() if x != login])
        dropped = 0                                  # его клипы убираем из очереди на отбор
        if PASSPORTS.exists():
            for line in PASSPORTS.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    p = json.loads(line)
                    if p.get("streamer") == login and p["clip_id"] not in st["clips"]:
                        st["clips"][p["clip_id"]] = "skipped"
                        dropped += 1
        save_state(st)
        fill(f"Убран {login}. Из очереди убрано его клипов: {dropped}.")
        say(f"Пул стримеров: убран {login}")
        on_change(False)

    bkw = dict(font=("Segoe UI", 10), relief="flat", bd=0, padx=14, pady=6, cursor="hand2")
    tk.Button(row, text="Добавить", command=add, bg=acc, fg="white", activebackground=acc, **bkw).pack(side="left", padx=(8, 0))
    tk.Button(win, text="Убрать выбранного", command=remove, bg=box, fg=fg, **bkw).pack(fill="x", padx=16, pady=(10, 14))
    entry.bind("<Return>", add)
    fill()
    entry.focus_set()


# ---------- настройки и ключи ----------
CONFIG_FILE = BASE / "config.json"


def needs_setup():
    """True, если ключи Twitch ещё не введены: без них клипы не ищутся."""
    try:
        c = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return not c.get("client_id") or "ВСТАВЬ" in c["client_id"] or "ВСТАВЬ" in c.get("client_secret", "ВСТАВЬ")
    except Exception:
        return True


def _http(url, data=None, headers=None):
    import urllib.error
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=dict({"User-Agent": "prodaem-clipper"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def check_twitch(cid, sec):
    code, _ = _http("https://id.twitch.tv/oauth2/token",
                    {"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"})
    return (True, "ключи Twitch подходят") if code == 200 else \
        (False, "нет связи с Twitch" if code == 0 else "Twitch не принял ключи, проверь оба поля")


def check_claude(key):
    code, _ = _http("https://api.anthropic.com/v1/models", headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return (True, "ключ Claude подходит") if code == 200 else \
        (False, "нет связи с Claude" if code == 0 else "Claude не принял ключ")


def check_telegram(tok):
    code, j = _http(f"https://api.telegram.org/bot{tok}/getMe")
    if code == 200 and j.get("ok"):
        return True, "бот найден: @" + j["result"].get("username", "")
    return False, "нет связи с Telegram" if code == 0 else "Telegram не принял токен"


def save_keys(cid, sec, claude, tg):
    """Раскладывает ключи по файлам. Пустое поле Claude или Telegram убирает соответствующий файл."""
    try:
        c = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        c = {"top_n": 5, "hours": 24, "min_views": 0, "talk_categories": ["Just Chatting", "IRL"],
             "cs_categories": ["Counter-Strike"], "out_dir": "clips"}
    if cid:
        c["client_id"] = cid
    if sec:
        c["client_secret"] = sec
    CONFIG_FILE.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    for f, val in ((m.KEY_FILE, claude), (m.TG_TOKEN, tg)):
        if val:
            f.write_text(val, encoding="ascii", errors="ignore")
        elif f.exists():
            f.unlink()


def settings_dialog(root, dark, on_saved, say):
    """Окно, где вводятся и проверяются ключи: Twitch, Claude, Telegram."""
    import tkinter as tk
    import webbrowser
    bg, fg, box, acc, mut = (CARD, TXT, BTN, ACC, MUTED) if dark else ("#f0f0f0", "#000000", "#ffffff", "#0a58ca", "#555555")
    win = tk.Toplevel(root)
    win.title("Настройки и ключи")
    win.geometry("620x640")
    win.configure(bg=bg)
    win.transient(root)
    q = queue.Queue()
    try:
        c = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    cur = {"cid": "" if "ВСТАВЬ" in c.get("client_id", "ВСТАВЬ") else c.get("client_id", ""),
           "sec": "" if "ВСТАВЬ" in c.get("client_secret", "ВСТАВЬ") else c.get("client_secret", ""),
           "claude": m.KEY_FILE.read_text(encoding="utf-8-sig").strip() if m.KEY_FILE.exists() else "",
           "tg": m.TG_TOKEN.read_text(encoding="utf-8-sig").strip() if m.TG_TOKEN.exists() else ""}
    ent, res = {}, {}
    bkw = dict(font=("Segoe UI", 10), relief="flat", bd=0, padx=12, pady=5, cursor="hand2")

    def section(title, note, link, fields, checker):
        fr = tk.Frame(win, bg=bg)
        fr.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(fr, text=title, bg=bg, fg=fg, font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(fr, text=note, bg=bg, fg=mut, font=("Segoe UI", 9), justify="left", wraplength=570).pack(anchor="w")
        for key, label in fields:
            row = tk.Frame(fr, bg=bg)
            row.pack(fill="x", pady=(6, 0))
            tk.Label(row, text=label, bg=bg, fg=fg, font=("Segoe UI", 10), width=14, anchor="w").pack(side="left")
            e = tk.Entry(row, bg=box, fg=fg, insertbackground=fg, font=("Consolas", 10), relief="flat",
                         highlightthickness=1, highlightbackground="#252832")
            e.insert(0, cur[key])
            e.pack(side="left", fill="x", expand=True, ipady=4)
            ent[key] = e
        row = tk.Frame(fr, bg=bg)
        row.pack(fill="x", pady=(8, 0))
        lab = tk.Label(row, text="", bg=bg, fg=mut, font=("Segoe UI", 9), anchor="w")

        def run_check():
            vals = [ent[k].get().strip() for k, _ in fields]
            if not all(vals):
                lab.config(text="сначала заполни поле", fg="#ff5d5d")
                return
            lab.config(text="проверяю…", fg=mut)
            threading.Thread(target=lambda: q.put((lab, checker(*vals))), daemon=True).start()

        tk.Button(row, text="Проверить", command=run_check, bg=box, fg=fg, **bkw).pack(side="left")
        tk.Button(row, text="Где взять", command=lambda: webbrowser.open(link), bg=box, fg=fg, **bkw).pack(side="left", padx=8)
        lab.pack(side="left", fill="x", expand=True)
        res[title] = lab

    section("Twitch (обязательно)",
            "Нужен, чтобы искать клипы. На сайте нажми «Регистрация приложения», адрес http://localhost, "
            "потом «Управление» и «Новый секретный код».",
            "https://dev.twitch.tv/console/apps", [("cid", "Client ID"), ("sec", "Client Secret")], check_twitch)
    section("Claude (желательно)",
            "Исправляет ошибки в субтитрах и пишет описания. Нужен баланс от 5 долларов. Без ключа программа работает.",
            "https://platform.claude.com/settings/keys", [("claude", "Ключ")], check_claude)
    section("Telegram (желательно)",
            "Готовые ролики приходят в бота. Создай его у @BotFather командой /newbot, вставь токен сюда "
            "и напиши своему боту любое сообщение.",
            "https://t.me/BotFather", [("tg", "Токен бота")], check_telegram)

    foot = tk.Frame(win, bg=bg)
    foot.pack(fill="x", padx=18, pady=18, side="bottom")
    msg = tk.Label(foot, text="", bg=bg, fg="#ff5d5d", font=("Segoe UI", 9), anchor="w")

    def save():
        v = {k: e.get().strip() for k, e in ent.items()}
        if not v["cid"] or not v["sec"]:
            msg.config(text="Без ключей Twitch клипы не ищутся: заполни оба поля.")
            return
        save_keys(v["cid"], v["sec"], v["claude"], v["tg"])
        say("Ключи сохранены.")
        win.destroy()
        on_saved()

    tk.Button(foot, text="Сохранить", command=save, bg=acc, fg="white", activebackground=acc,
              font=("Segoe UI", 11, "bold"), relief="flat", bd=0, padx=22, pady=8, cursor="hand2").pack(side="left")
    msg.pack(side="left", padx=12, fill="x", expand=True)

    def poll():
        try:
            while True:
                lab, (ok, text) = q.get_nowait()
                lab.config(text=text, fg="#3ddc84" if ok else "#ff5d5d")
        except queue.Empty:
            pass
        if win.winfo_exists():
            win.after(200, poll)

    poll()


# ---------- баннеры офферов ----------
def save_banners(cfg, banners, use):
    """Записывает список баннеров и основной баннер в montage.json и в настройки, с которыми работает окно."""
    path = BASE / "montage.json"
    try:
        j = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        j = dict(cfg)
    j["banners"], j["banner_use"] = banners, use
    path.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg["banners"], cfg["banner_use"] = banners, use


def banners_dialog(root, cfg, on_change, say):
    """Окно баннеров офферов: добавить файл, выбрать основной, настроить ускорение, размытие и вырезание фона."""
    import shutil
    import tkinter as tk
    from tkinter import filedialog
    import customtkinter as ctk
    from PIL import Image

    F = lambda n, w="normal": ctk.CTkFont(family="Segoe UI", size=n, weight=w)
    banners = {k: dict(v) for k, v in m.banner_list(cfg).items()}
    use = cfg.get("banner_use") if cfg.get("banner_use") in banners else next(iter(banners), None)
    S = {"sel": use, "job": None, "gen": 0}
    q = queue.Queue()

    win = ctk.CTkToplevel(root, fg_color=BG)
    win.title("Баннеры офферов")
    win.geometry("960x620")
    win.transient(root)
    win.after(200, win.lift)
    win.grid_columnconfigure(1, weight=1)
    win.grid_rowconfigure(0, weight=1)

    left = ctk.CTkFrame(win, fg_color=CARD, corner_radius=16, width=280)
    left.grid(row=0, column=0, sticky="nsw", padx=(16, 8), pady=16)
    left.grid_propagate(False)
    ctk.CTkLabel(left, text="Баннеры", font=F(16, "bold"), text_color=TXT).pack(anchor="w", padx=16, pady=(14, 2))
    ctk.CTkLabel(left, text="★ основной: он ставится в ролики,\nесли при монтаже не выбран другой",
                 font=F(11), text_color=MUTED, justify="left").pack(anchor="w", padx=16)
    lst = ctk.CTkScrollableFrame(left, fg_color=CARD)
    lst.pack(fill="both", expand=True, padx=8, pady=8)

    right = ctk.CTkFrame(win, fg_color=CARD, corner_radius=16)
    right.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=16)
    right.grid_columnconfigure(1, weight=1)

    def lab(text, r):
        ctk.CTkLabel(right, text=text, font=F(13), text_color=MUTED).grid(row=r, column=0, sticky="w", padx=(20, 14), pady=6)

    lab("Название", 0)
    e_name = ctk.CTkEntry(right, height=34, corner_radius=10, fg_color=BTN, border_color=LINE, text_color=TXT, font=F(13))
    e_name.grid(row=0, column=1, sticky="ew", padx=(0, 20), pady=(18, 6))
    lab("Ускорение", 1)
    sp_row = ctk.CTkFrame(right, fg_color=CARD)
    sp_row.grid(row=1, column=1, sticky="ew", padx=(0, 20))
    sp = ctk.CTkSlider(sp_row, from_=1.0, to=2.0, number_of_steps=20, button_color=ACC, button_hover_color=ACC_H,
                       progress_color=ACC, fg_color=BTN)
    sp.pack(side="left", fill="x", expand=True)
    sp_l = ctk.CTkLabel(sp_row, text="", font=F(13, "bold"), text_color=TXT, width=150, anchor="e")
    sp_l.pack(side="left")
    lab("Размытие", 2)
    bl_row = ctk.CTkFrame(right, fg_color=CARD)
    bl_row.grid(row=2, column=1, sticky="ew", padx=(0, 20))
    bl = ctk.CTkSlider(bl_row, from_=0, to=30, number_of_steps=30, button_color=ACC, button_hover_color=ACC_H,
                       progress_color=ACC, fg_color=BTN)
    bl.pack(side="left", fill="x", expand=True)
    bl_l = ctk.CTkLabel(bl_row, text="", font=F(13, "bold"), text_color=TXT, width=150, anchor="e")
    bl_l.pack(side="left")
    lab("Фон баннера", 3)
    CH = {"Вырезать сам (зелёный, синий)": "auto", "Не вырезать": None}
    ch = ctk.CTkSegmentedButton(right, values=list(CH), font=F(12), selected_color=ACC, selected_hover_color=ACC_H,
                                unselected_color=BTN, unselected_hover_color=BTN_H, fg_color=BTN, text_color=TXT)
    ch.grid(row=3, column=1, sticky="w", pady=6)
    pv = ctk.CTkLabel(right, text="", fg_color=BG, corner_radius=12, height=230, text_color=MUTED, font=F(12))
    pv.grid(row=4, column=0, columnspan=2, sticky="ew", padx=20, pady=(12, 4))
    note = ctk.CTkLabel(right, text="", font=F(12), text_color=HINT, anchor="w")
    note.grid(row=5, column=0, columnspan=2, sticky="w", padx=20)
    acts = ctk.CTkFrame(right, fg_color=CARD)
    acts.grid(row=6, column=0, columnspan=2, sticky="ew", padx=20, pady=(10, 18))

    def cur():
        return banners.get(S["sel"])

    def draw_list():
        for w in lst.winfo_children():
            w.destroy()
        if not banners:
            ctk.CTkLabel(lst, text="Пока пусто.\nНажми «Добавить баннер…»", font=F(12), text_color=MUTED).pack(pady=20)
        for name in banners:
            on = name == S["sel"]
            ctk.CTkButton(lst, text=("★ " if name == use_v["v"] else "    ") + name, anchor="w", height=38,
                          corner_radius=10, font=F(13, "bold" if on else "normal"),
                          fg_color=ACC if on else BTN, hover_color=ACC_H if on else BTN_H, text_color=TXT,
                          command=lambda n=name: select(n)).pack(fill="x", pady=3)

    use_v = {"v": use}

    def show_values():
        b = cur()
        for w in (e_name, sp, bl, ch):
            w.configure(state="normal")
        e_name.delete(0, "end")
        if not b:
            for w in (e_name, sp, bl, ch):
                w.configure(state="disabled")
            pv.configure(image=None, text="Добавь файл баннера (.mov или .mp4)")
            return
        e_name.insert(0, S["sel"])
        sp.set(float(b.get("speed") or 1.0))
        bl.set(float(b.get("blur") or 0))
        ch.set("Не вырезать" if not b.get("chroma") else "Вырезать сам (зелёный, синий)")
        labels()
        refresh_preview()

    def labels():
        b = cur()
        if not b:
            return
        sp_l.configure(text=f"{float(b['speed']):.2f}x")
        bl_l.configure(text="выкл" if not b.get("blur") else f"{int(b['blur'])}")

    def select(name):
        S["sel"] = name
        draw_list()
        show_values()

    def on_slide(_=None):
        b = cur()
        if not b:
            return
        b["speed"] = round(float(sp.get()), 2)
        b["blur"] = int(round(float(bl.get())))
        labels()
        if S["job"]:
            win.after_cancel(S["job"])
        S["job"] = win.after(350, refresh_preview)       # картинку обновляем, когда ползунок отпустили

    def on_chroma(v):
        b = cur()
        if b:
            b["chroma"] = CH[v]
            refresh_preview()

    sp.configure(command=on_slide)
    bl.configure(command=on_slide)
    ch.configure(command=on_chroma)

    def refresh_preview():
        S["job"] = None
        b = cur()
        if not b:
            return
        S["gen"] += 1
        gen, bb = S["gen"], dict(b)
        note.configure(text="рисую…")
        png = m.CACHE / f"banner_pv_{gen % 3}.png"

        def work():
            try:
                m.CACHE.mkdir(exist_ok=True)
                info = m.banner_preview(m.banner_for({"banners": {"x": bb}}, "cs", "x"), cfg, png, 1.5)
                q.put((gen, png, info, None))
            except Exception as e:
                q.put((gen, None, None, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            while True:
                gen, png, info, err = q.get_nowait()
                if gen != S["gen"]:
                    continue
                if err:
                    pv.configure(image=None, text=err[:300])
                    note.configure(text="")
                    continue
                im = Image.open(png)
                k = min(1.0, 620 / im.width)
                img = ctk.CTkImage(light_image=im, dark_image=im, size=(int(im.width * k), int(im.height * k)))
                pv.configure(image=img, text="")
                pv._img = img
                note.configure(text=f"Файл {Path(m.find_banner(cur()['file'])).name} · в ролике баннер идёт "
                                    f"{info['sec']} с (без ускорения {info['dur']:.1f} с)")
        except queue.Empty:
            pass
        except Exception:
            pass
        if win.winfo_exists():
            win.after(150, poll)

    def add():
        f = filedialog.askopenfilename(parent=win, title="Файл баннера",
                                       filetypes=[("Видео", "*.mov *.mp4 *.webm *.mkv"), ("Все файлы", "*.*")])
        if not f:
            return
        src = Path(f)
        dst = BASE / src.name
        try:
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)           # баннер должен лежать в папке программы
        except Exception as e:
            note.configure(text=f"не скопировался файл: {e}")
            return
        name, i = src.stem.split(".")[0], 2
        base_name = name
        while name in banners:
            name, i = f"{base_name} {i}", i + 1
        banners[name] = {"file": dst.name, "speed": 1.0, "blur": 0, "crop": None, "chroma": "auto"}
        if not use_v["v"]:
            use_v["v"] = name
        select(name)

    def remove():
        if not S["sel"]:
            return
        banners.pop(S["sel"], None)
        if use_v["v"] not in banners:
            use_v["v"] = next(iter(banners), None)
        S["sel"] = use_v["v"]
        draw_list()
        show_values()

    def make_main():
        if S["sel"]:
            use_v["v"] = S["sel"]
            draw_list()

    def rename():
        new = e_name.get().strip()
        old = S["sel"]
        if old and new and new != old and new not in banners:
            banners[new] = banners.pop(old)
            if use_v["v"] == old:
                use_v["v"] = new
            S["sel"] = new
            draw_list()

    e_name.bind("<FocusOut>", lambda _e: rename())
    e_name.bind("<Return>", lambda _e: rename())

    def save():
        rename()
        save_banners(cfg, banners, use_v["v"])
        say(f"Баннеры сохранены. Основной: {use_v['v'] or 'не выбран'}.")
        on_change()
        win.destroy()

    def btn(parent, text, cmd, main=False):
        return ctk.CTkButton(parent, text=text, command=cmd, height=38, corner_radius=10, font=F(13, "bold" if main else "normal"),
                             fg_color=ACC if main else BTN, hover_color=ACC_H if main else BTN_H, text_color=TXT)

    btn(left, "Добавить баннер…", add, True).pack(fill="x", padx=12, pady=(0, 6))
    btn(left, "Убрать из списка", remove).pack(fill="x", padx=12, pady=(0, 14))
    btn(acts, "★ Сделать основным", make_main).pack(side="left")
    btn(acts, "Сохранить", save, True).pack(side="right")
    draw_list()
    show_values()
    poll()


# ---------- что монтировать ----------
def build_force(fmt_key, rects):
    """Переводит выбор в окне в задание для монтажа: (формат, раскладка, залипательное)."""
    if fmt_key in ("talk", "talk_satis"):
        force = {"mode": "solo", "rect": rects[0]} if rects else {"mode": "full"}
        return "talk", force, fmt_key == "talk_satis"
    if fmt_key == "react":
        if not rects:
            raise ValueError("Для реакции обведи окно камеры стримера.")
        return "talk", {"mode": "cam", "rect": rects[0]}, False
    if fmt_key == "cs":
        if not rects:
            raise ValueError("Обведи окно камеры или выбери формат «КС без камеры».")
        return "cs", {"mode": "cam", "rect": rects[0]}, False
    if fmt_key == "cs_nocam":
        return "cs", {"mode": "none"}, False
    if fmt_key == "multi":
        if len(rects) < 2:
            raise ValueError("Для нескольких стримеров обведи минимум две камеры.")
        return "talk", {"mode": "multi", "rects": rects[:4]}, False
    raise ValueError("Неизвестный формат")


def make_preview(clip, fmt_key, rects, captions, sub_y, t, png, cfg, style=None, font=None, content=None, video=0,
                 sub_anim=None, zoom=True):
    """Быстро рисует один кадр: как ролик будет выглядеть с этой камерой, форматом и субтитрами.
    Возвращает {"png", "sub_y", "sub_auto"} или None."""
    fmt, force, want_satis = build_force(fmt_key, rects)
    if captions:
        force["captions"] = captions
    elif captions is False:
        force["captions"] = False
    if sub_y:
        force["sub_y"] = int(sub_y)
    force.update(style=style, font=font, content=content, sub_anim=sub_anim, zoom=zoom)
    clip = Path(clip)
    sat = m.pick_satis(clip, cfg, force=True) if want_satis else None
    pv = {"t": t, "png": str(png)}
    if video:                                        # короткий ролик-пример вместо кадра; png тогда путь к .mp4
        pv.update(video=video, video_out=str(png))
    return m.montage(clip, fmt, "", "", m.CACHE / "preview_unused.mp4", cfg, "libx264", satis=sat,
                     show_title=False, force=force, preview=pv)


def run_job(job, cfg, enc, log=print):
    """Монтирует один клип по заданию из окна. Возвращает путь к готовому ролику или None."""
    p = dict(job["clip"])
    clip = Path(p["file"])
    streamer = (job.get("streamer") or p.get("streamer") or "").strip().lower()
    own = bool(p.get("own"))
    kind = "manual" if own else "clips"
    if job["fmt_key"] == "top":
        import top5
        return top5.make(clip, streamer, job.get("title", ""), 5, None, True, cfg, enc)
    fmt, force, want_satis = build_force(job["fmt_key"], job["rects"])
    if job.get("captions"):
        force["captions"] = job["captions"]        # полоса встроенных субтитров стрима: [верх, низ] в пикселях
    elif job.get("captions") is False:
        force["captions"] = False
    if job.get("sub_y"):
        force["sub_y"] = int(job["sub_y"])           # положение субтитров задано вручную
    force.update(style=job.get("style"), font=job.get("font"), content=job.get("content"),
                 sub_anim=job.get("sub_anim"), zoom=job.get("zoom", True))
    if job.get("banner"):
        force["banner"] = job["banner"]              # баннер оффера, выбранный в окне для этого клипа
    sat = m.pick_satis(clip, cfg, force=True) if want_satis else None
    if want_satis and not sat:
        log("  в папке satisfying нет видео: собираю обычный разговорный")
    folder = "manual" if own else fmt
    name = (streamer or "video") + "_" + "".join(ch for ch in p["clip_id"][:16] if ch.isalnum() or ch in "_-")
    out = BASE / "clips" / "ready" / folder / f"{name}.mp4"
    title = (job.get("title") or "").strip()
    show_title = bool(title) if own else bool(cfg.get("clip_titles", False))
    log(f"{streamer or clip.name}: монтаж ({dict((k, v) for k, v, _ in FORMATS)[job['fmt_key']]})")

    def make(extra):
        r = m.montage(clip, fmt, streamer, title or p.get("title", clip.stem), out, cfg, enc,
                      fixed_title=title or None, satis=sat, show_title=show_title, force=dict(force, **extra))
        return (out if r else None), r

    ag = None
    if cfg.get("agents", True) and not own:      # критик, судья и продюсер (нужен ключ Claude)
        try:
            import agents
            words = m.transcribe(clip, cfg)
            got, res, ag = agents.review(make, clip, words, cfg, log, p)
            if ag and ag["decision"] == "reject" and not res:          # неактуальный клип: даже не монтируем
                job["agents"] = {"score": 0, "decision": "reject", "reason": ag.get("reason", ""), "plan": None}
                return None
            if ag and ag["decision"] == "publish":
                p["final_title"] = (res or {}).get("final_title", "")
                ag["plan"] = agents.producer(dict(p, streamer=streamer, file_out=str(out)), ag, words, cfg, log)
                if ag["plan"].get("reserve"):
                    ag["decision"], ag["reason"] = "reserve", ag["plan"]["why"]
        except ImportError:
            got, res = make({})
        except Exception as e:
            log(f"  агенты не сработали: {e}")
            got, res = (out, res) if "res" in locals() and res else make({})
    else:
        got, res = make({})
    if not res:
        log("  не получилось смонтировать")
        return None
    p.update(res)
    p["streamer"] = streamer or "стример"
    p["format"] = fmt
    p["source"] = "manual" if own else "review"
    p["potential"] = m.potential(p, res, None)
    hashtags = (f"#{streamer} " if streamer else "") + m.base_tags()
    text = res["description"] or res["final_title"]
    if ag:
        p["agents"] = ag
        if ag.get("plan"):
            pl = ag["plan"]
            hashtags = pl["hashtags"] or hashtags
            text = pl["caption"] or text
    out.with_suffix(".txt").write_text(text + f"\n\n{hashtags}\n", encoding="utf-8")
    if ag and ag["decision"] != "publish":       # брак и запас в Telegram не уходят: только 3 лучших на аккаунт
        p["tg_sent"] = False
        log(f"  в Telegram не отправлен: {'в запасе' if ag['decision'] == 'reserve' else 'в браке'} — {ag.get('reason', '')}")
    else:
        p["tg_sent"] = m.tg_send(out, p, hashtags, kind)
        bumped = ((ag or {}).get("plan") or {}).get("bumped")
        if bumped and p["tg_sent"]:
            m.tg_message(f"↩️ Снят со слота @{bumped['account']} {bumped['time']}: ролик {bumped.get('streamer', '')} "
                         f"({bumped.get('score')}/10, «{bumped.get('caption', '')[:60]}»).\n"
                         f"Вместо него выше — ролик сильнее ({ag.get('score')}/10). Старый не выкладывай.", kind)
    out.with_suffix(".json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  готово: {res['duration']} с, потенциал {p['potential']}/100"
        + (", отправлен в Telegram" if p["tg_sent"] else ""))
    if ag:
        job["agents"] = {"score": ag.get("score"), "decision": ag["decision"], "reason": ag.get("reason", ""),
                         "plan": ag.get("plan")}
    return out


# ---------- простое окно (запасное, если красивое не запустилось) ----------
def gui_classic():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext

    cfg = json.loads((BASE / "montage.json").read_text(encoding="utf-8"))
    enc = m.pick_encoder(cfg)
    st = load_state()
    root = tk.Tk()
    root.title("Клиппер: отбор и монтаж")
    root.geometry("1560x900")
    logq, jobs = queue.Queue(), queue.Queue()
    S = {"items": [], "cur": None, "info": None, "scale": 1.0, "rects": [], "drag": None, "img": None,
         "busy": None, "done": 0, "failed": 0, "cap": None, "capmode": False,
         "pv_img": None, "pv_busy": False, "pv_dirty": False, "content": None, "contmode": False}
    VW = 760                                         # ширина кадра в окне

    class Out:
        def write(self, s):
            logq.put(s)

        def flush(self):
            pass

    sys.stdout = sys.stderr = Out()

    # --- слева: очередь клипов ---
    left = tk.Frame(root, padx=8, pady=8)
    left.pack(side="left", fill="y")
    tk.Label(left, text="Новые клипы", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    lb = tk.Listbox(left, width=40, height=30, exportselection=False)
    lb.pack(fill="y", expand=True)
    count = tk.Label(left, text="", fg="#555")
    count.pack(anchor="w")

    # --- справа: пример готового ролика и положение субтитров ---
    right = tk.Frame(root, padx=8, pady=8)
    right.pack(side="right", fill="y")
    tk.Label(right, text="Как получится", font=("Segoe UI", 11, "bold")).pack(anchor="w")
    pv = tk.Label(right, width=270, height=480, bg="#111", bd=0)
    pv.pack()
    pv_note = tk.Label(right, text="Белая рамка: сюда встанет баннер.\nСубтитры показаны для примера.", fg="#555",
                       justify="left")
    pv_note.pack(anchor="w", pady=(4, 0))
    tk.Button(right, text="Обновить пример", command=lambda: preview_now()).pack(fill="x", pady=(6, 0))
    tk.Label(right, text="Положение субтитров", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 0))
    sub_auto = tk.BooleanVar(value=True)
    tk.Checkbutton(right, text="Авто (как задумано в формате)", variable=sub_auto,
                   command=lambda: on_sub()).pack(anchor="w")
    sub_pos = tk.Scale(right, from_=15, to=92, orient="horizontal", length=270, label="Высота на экране, % сверху")
    sub_pos.set(70)
    sub_pos.pack()

    # --- центр: кадр ---
    mid = tk.Frame(root, padx=8, pady=8)
    mid.pack(side="left", fill="both", expand=True)
    head = tk.Label(mid, text="Выбери клип слева", anchor="w", justify="left", wraplength=VW, font=("Segoe UI", 10, "bold"))
    head.pack(fill="x")
    canvas = tk.Canvas(mid, width=VW, height=428, bg="#111", highlightthickness=0, cursor="crosshair")
    canvas.pack()
    pos = tk.Scale(mid, from_=0, to=100, orient="horizontal", length=VW, showvalue=False)
    pos.pack()
    hint = tk.Label(mid, text="", anchor="w", justify="left", fg="#0a58ca", wraplength=VW)
    hint.pack(fill="x")

    # --- формат и кнопки ---
    opts = tk.Frame(mid)
    opts.pack(fill="x", pady=(4, 0))
    fmt = tk.StringVar(value="talk")
    col = tk.Frame(opts)
    col.pack(side="left", anchor="n")
    for key, label, _ in FORMATS:
        tk.Radiobutton(col, text=label, variable=fmt, value=key, command=lambda: on_fmt()).pack(anchor="w")
    side = tk.Frame(opts, padx=16)
    side.pack(side="left", anchor="n")
    tk.Label(side, text="Ник стримера:").grid(row=0, column=0, sticky="w")
    e_streamer = tk.Entry(side, width=24)
    e_streamer.grid(row=0, column=1, sticky="w")
    tk.Label(side, text="Заголовок (для своих видео):").grid(row=1, column=0, sticky="w", pady=(6, 0))
    e_title = tk.Entry(side, width=38)
    e_title.grid(row=1, column=1, sticky="w", pady=(6, 0))
    btns = tk.Frame(side)
    btns.grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))

    logbox = scrolledtext.ScrolledText(mid, height=9, state="disabled")
    logbox.pack(fill="both", expand=True, pady=(6, 0))
    status = tk.Label(root, text="", anchor="w", bd=1, relief="sunken")
    status.pack(side="bottom", fill="x")

    def say(text):
        logq.put(text + "\n")

    def refresh(keep=None):
        S["items"] = load_queue(st)
        lb.delete(0, "end")
        for p in S["items"]:
            tag = "СВОЁ" if p.get("own") else p.get("streamer", "")
            lb.insert("end", f"{tag[:14]:14s} {int(p.get('twitch_views', 0)):>5d}  {int(p.get('duration') or 0):>3d}с  {p.get('title', '')[:30]}")
        count.config(text=f"В очереди: {len(S['items'])}")
        if S["items"]:
            i = 0
            if keep is not None:
                i = min(keep, len(S["items"]) - 1)
            lb.selection_set(i)
            open_clip(i)
        else:
            S["cur"] = None
            canvas.delete("all")
            head.config(text="Очередь пуста. Нажми «Найти новые клипы» или добавь свой файл.")

    def show_frame(*_):
        p = S["cur"]
        if not p:
            return
        t = S["info"]["dur"] * pos.get() / 100
        png = m.CACHE / f"review_{os.getpid()}.png"
        m.CACHE.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", p["file"], "-frames:v", "1",
                        "-vf", f"scale={VW}:-2", str(png)], capture_output=True)
        if not png.exists():
            return
        S["img"] = tk.PhotoImage(file=str(png))
        canvas.config(height=S["img"].height())
        redraw()

    def cur_sub():
        return None if sub_auto.get() else int(1920 * sub_pos.get() / 100)

    def preview_now():
        """Рисует пример в фоне. Если пока рисуется прошлый, после него нарисуется свежий."""
        p = S["cur"]
        if not p:
            return
        if S["pv_busy"]:
            S["pv_dirty"] = True
            return
        try:
            build_force(fmt.get(), S["rects"])
        except ValueError:
            return                                   # камера ещё не обведена: показывать нечего
        S["pv_busy"], S["pv_dirty"] = True, False
        args = (p["file"], fmt.get(), [list(r) for r in S["rects"]], list(S["cap"]) if S["cap"] else False,
                cur_sub(), S["info"]["dur"] * pos.get() / 100, m.CACHE / f"preview_{os.getpid()}.png")

        def work():
            res = None
            try:
                res = make_preview(*args, cfg)
            except Exception as err:
                logq.put(f"  пример не получился: {err}\n")
            logq.put(("\x02", res))

        threading.Thread(target=work, daemon=True).start()

    def on_sub():
        if not sub_auto.get():
            st.setdefault("sub_pos", {})[fmt.get()] = sub_pos.get()
        else:
            st.setdefault("sub_pos", {}).pop(fmt.get(), None)
        save_state(st)
        preview_now()

    def redraw():
        canvas.delete("all")
        if S["img"]:
            canvas.create_image(0, 0, anchor="nw", image=S["img"])
        for i, r in enumerate(S["rects"]):
            k = S["scale"]
            x0, y0, x1, y1 = [v / k for v in r]
            canvas.create_rectangle(x0, y0, x1, y1, outline="#ffe600", width=3)
            canvas.create_text(x0 + 12, y0 + 12, text=str(i + 1), fill="#ffe600", font=("Segoe UI", 12, "bold"))
        if S["cap"]:                                 # полоса встроенных субтитров стрима
            k = S["scale"]
            y0, y1 = S["cap"][0] / k, S["cap"][1] / k
            canvas.create_rectangle(0, y0, VW, y1, outline="#ff3b30", width=2, stipple="gray25", fill="#ff3b30")
            canvas.create_text(8, y0 + 4, anchor="nw", text="субтитры Twitch: эта полоса обрежется", fill="white",
                               font=("Segoe UI", 9, "bold"))
        if S["drag"]:
            canvas.create_rectangle(*S["drag"], outline="#ff3b30" if S["capmode"] else "#00e5ff", width=2, dash=(4, 3))

    def open_clip(i):
        p = S["items"][i]
        S["cur"] = p
        S["info"] = m.probe(p["file"])
        S["scale"] = S["info"]["w"] / VW
        S["rects"] = []
        S["cap"], S["capmode"] = None, False
        zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8")) if m.ZONES_FILE.exists() else {}
        z = zones.get(p.get("streamer", ""))
        if z and abs(S["info"]["w"] / S["info"]["h"] - 16 / 9) < 0.06:      # у этого стримера полоса уже отмечена
            S["cap"] = [int(S["info"]["h"] * z[0] / 100), int(S["info"]["h"] * z[1] / 100)]
        e_streamer.delete(0, "end")
        e_streamer.insert(0, p.get("streamer", ""))
        e_title.delete(0, "end")
        guess = "cs" if p.get("format") == "cs" else "talk"
        S["content"], S["contmode"] = None, False
        last = st.get("last", {}).get(p.get("streamer", ""))
        if last and not p.get("own"):               # как в прошлый раз у этого стримера: формат и рамки
            guess = last["fmt"]
            k = S["info"]["w"] / 1920
            S["rects"] = [[int(v * k) for v in r] for r in last["rects"]]
        fmt.set(guess)
        on_fmt(clear=False)
        head.config(text=f"{p.get('streamer') or 'своё видео'}   |   {p.get('title', '')}   |   "
                         f"{int(p.get('duration') or S['info']['dur'])} с   |   {int(p.get('twitch_views', 0))} просмотров на Twitch")
        pos.set(40)
        show_frame()
        preview_now()

    def on_fmt(clear=True):
        key = fmt.get()
        hint.config(text=dict((k, h) for k, _, h in FORMATS)[key]
                    + ("  Рамки подставлены как в прошлом клипе этого стримера: проверь и поправь." if S["rects"] and not clear else ""))
        if clear and key != "multi" and len(S["rects"]) > 1:
            S["rects"] = S["rects"][:1]
        if key == "cs_nocam":
            S["rects"] = []
        saved = st.get("sub_pos", {}).get(key)       # своё положение субтитров для этого формата
        sub_auto.set(saved is None)
        if saved is not None:
            sub_pos.set(saved)
        redraw()
        preview_now()

    def press(e):
        S["drag"] = [e.x, e.y, e.x, e.y]

    def move(e):
        if S["drag"]:
            S["drag"][2], S["drag"][3] = max(0, min(VW, e.x)), max(0, e.y)
            redraw()

    def release(e):
        d, S["drag"] = S["drag"], None
        if not d or abs(d[2] - d[0]) < 12 or abs(d[3] - d[1]) < 12:
            redraw()
            return
        k = S["scale"]
        r = [int(min(d[0], d[2]) * k), int(min(d[1], d[3]) * k), int(max(d[0], d[2]) * k), int(max(d[1], d[3]) * k)]
        if S["capmode"]:                             # отмечали полосу субтитров, а не камеру
            S["cap"], S["capmode"] = [r[1], r[3]], False
            cap_btn.config(relief="raised", text="Отметить субтитры Twitch")
            redraw()
            preview_now()
            return
        if fmt.get() == "multi":
            if len(S["rects"]) < 4:
                S["rects"].append(r)
        else:
            S["rects"] = [r]
        redraw()
        preview_now()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", release)
    pos.bind("<ButtonRelease-1>", lambda e: (show_frame(), preview_now()))
    sub_pos.bind("<ButtonRelease-1>", lambda e: (sub_auto.set(False), on_sub()))
    lb.bind("<<ListboxSelect>>", lambda e: lb.curselection() and open_clip(lb.curselection()[0]))

    def clear_rects():
        S["rects"] = []
        redraw()
        preview_now()

    def mark_caps():
        S["capmode"] = not S["capmode"]
        cap_btn.config(relief="sunken" if S["capmode"] else "raised",
                       text="Обведи полосу с субтитрами…" if S["capmode"] else "Отметить субтитры Twitch")

    def clear_caps():
        S["cap"], S["capmode"] = None, False
        cap_btn.config(relief="raised", text="Отметить субтитры Twitch")
        p = S["cur"]
        if p and m.ZONES_FILE.exists():              # забываем полосу и для следующих клипов этого стримера
            zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8"))
            if zones.pop(p.get("streamer", ""), None) is not None:
                m.ZONES_FILE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
        redraw()
        preview_now()

    def play():
        if S["cur"]:
            try:
                os.startfile(S["cur"]["file"])       # откроется в обычном плеере Windows
            except AttributeError:
                subprocess.Popen(["xdg-open", S["cur"]["file"]])

    def next_after(idx):
        refresh(keep=idx)

    def do_montage():
        p = S["cur"]
        if not p:
            return
        try:
            build_force(fmt.get(), S["rects"]) if fmt.get() != "top" else None
        except ValueError as err:
            say(str(err))
            return
        streamer = e_streamer.get().strip().lower()
        jobs.put({"clip": p, "fmt_key": fmt.get(), "rects": [list(r) for r in S["rects"]],
                  "streamer": streamer, "title": e_title.get(),
                  "captions": list(S["cap"]) if S["cap"] else False, "sub_y": cur_sub()})
        st["clips"][p["clip_id"]] = "queued"
        if streamer and fmt.get() != "top":
            k = 1920 / S["info"]["w"]
            st.setdefault("last", {})[streamer] = {"fmt": fmt.get(), "rects": [[int(v * k) for v in r] for r in S["rects"]]}
        save_state(st)
        idx = lb.curselection()[0] if lb.curselection() else 0
        next_after(idx)

    def do_skip():
        p = S["cur"]
        if not p:
            return
        st["clips"][p["clip_id"]] = "skipped"
        save_state(st)
        idx = lb.curselection()[0] if lb.curselection() else 0
        next_after(idx)

    def add_file():
        got = filedialog.askopenfilenames(title="Выбери видео",
                                          filetypes=[("Видео", "*.mp4 *.mov *.mkv *.webm *.avi"), ("Все файлы", "*.*")])
        for f in got:
            add_own(st, f)
        if got:
            refresh()

    finding = {"on": False}

    def find_clips():
        if finding["on"]:
            return
        finding["on"] = True
        say("Ищу новые клипы…")

        def work():
            try:
                r = subprocess.run([sys.executable, "clipfinder.py"], cwd=str(BASE), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
                tail = (r.stdout or "").strip().splitlines()[-1:] or ["поиск завершён"]
                logq.put(tail[0] + "\n")
            finally:
                finding["on"] = False
                logq.put("\x01")

        threading.Thread(target=work, daemon=True).start()

    tk.Button(btns, text="Смонтировать и дальше", command=do_montage, width=24, height=2, bg="#198754", fg="white").grid(row=0, column=0, padx=(0, 8))
    tk.Button(btns, text="Пропустить клип", command=do_skip, width=16, height=2).grid(row=0, column=1, padx=(0, 8))
    tk.Button(btns, text="Стереть рамки", command=clear_rects, width=14).grid(row=1, column=0, sticky="w", pady=(8, 0))
    tk.Button(btns, text="Открыть в плеере", command=play, width=16).grid(row=1, column=1, sticky="w", pady=(8, 0))
    cap_btn = tk.Button(btns, text="Отметить субтитры Twitch", command=mark_caps, width=26)
    cap_btn.grid(row=2, column=0, sticky="w", pady=(8, 0))
    tk.Button(btns, text="Субтитров нет", command=clear_caps, width=16).grid(row=2, column=1, sticky="w", pady=(8, 0))
    tk.Button(left, text="Найти новые клипы", command=find_clips).pack(fill="x", pady=(6, 0))
    tk.Button(left, text="Добавить свой файл…", command=add_file).pack(fill="x", pady=(4, 0))
    tk.Button(left, text="Стримеры: добавить или убрать…",
              command=lambda: streamers_dialog(root, st, False, lambda added: find_clips() if added else refresh(), say)
              ).pack(fill="x", pady=(4, 0))

    def worker():
        while True:
            job = jobs.get()
            S["busy"] = job["clip"].get("title", "")[:40]
            ok = False
            try:
                ok = bool(run_job(job, cfg, enc, log=say))
            except BaseException as err:             # любая ошибка: клип помечается, очередь монтажа идёт дальше
                say(f"  ошибка: {err}")
            st["clips"][job["clip"]["clip_id"]] = "done" if ok else "retry"
            save_state(st)
            S["done" if ok else "failed"] += 1
            S["busy"] = None

    threading.Thread(target=worker, daemon=True).start()

    def pump():
        try:
            while True:
                s = logq.get_nowait()
                if s == "\x01":
                    refresh(keep=lb.curselection()[0] if lb.curselection() else None)
                    continue
                if isinstance(s, tuple):             # готов пример
                    S["pv_busy"] = False
                    res = s[1]
                    if res and Path(res["png"]).exists():
                        S["pv_img"] = tk.PhotoImage(file=res["png"]).subsample(2)
                        pv.config(image=S["pv_img"], width=270, height=480)
                        if sub_auto.get():
                            sub_pos.set(int(round(100 * res["sub_auto"] / 1920)))
                    if S["pv_dirty"]:
                        preview_now()
                    continue
                logbox.config(state="normal")
                logbox.insert("end", s)
                logbox.see("end")
                logbox.config(state="disabled")
        except queue.Empty:
            pass
        status.config(text=f"  Монтируется: {S['busy'] or 'ничего'}    |    ждут монтажа: {jobs.qsize()}    |    "
                           f"готово: {S['done']}    |    с ошибкой: {S['failed']}")
        root.after(200, pump)

    def auto_find():
        find_clips()
        root.after(int(cfg.get("interval_min", 120)) * 60 * 1000, auto_find)

    refresh()
    pump()
    root.after(3000, auto_find)                      # поиск идёт сам: при запуске и потом по расписанию
    root.mainloop()


# ---------- логотип PRODAEM CLIPPER (встроен в скрипт) ----------
LOGO_B64 = (
    "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"
)
PROFILE = (BASE / "profile.txt").read_text(encoding="utf-8-sig").strip()[:12] if (BASE / "profile.txt").exists() else ""
APP_NAME = "PRODAEM CLIPPER" + (" " + PROFILE if PROFILE else "")      # у каждого профиля своё окно и свой ярлык


PROFILE_COLORS = {"": ((124, 92, 255), (0, 229, 255), (90, 80, 255)),
                  "US": ((255, 59, 92), (47, 107, 255), (200, 40, 90)),
                  "DOTA": ((255, 60, 20), (255, 170, 0), (220, 60, 10))}


LOGO_SRC = CODE / "logo_src.png"                    # если файла нет, логотип берётся из кода ниже
LOGO_JPG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAIBAQIBAQICAgICAgICAwUDAwMDAwYEBAMFBwYHBwcGBwcICQsJCAgKCAcHCg0KCgsMDAwMBwkODw"
    "0MDgsMDAz/2wBDAQICAgMDAwYDAwYMCAcIDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAz/wAARCAJN"
    "Ap0DAREAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhBy"
    "JxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAA"
    "AAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRom"
    "JygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExc"
    "bHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD+f8DNABQAv8qAD0oATrQAGgA70AFABQAUAHWgA7UAFABQAUAF"
    "ABQAdaADOKACgA70ALjmgBCMUXAM0AFABQAvQe9AAMEigGA5FAB2oAM9PSkApGRmmK4g60DDqOtAgosMM0AAPFAhccY6ZpgJ19qQw6mgAznmmA"
    "Y70gCgAzg0AGelAAf1HWgBcc96BCY5oGGOKAFAGKBCY/8ArUDAH9KADPH1oATGTQAdaAFwaAEzQAZzQA4E8mkAlMBM4NAAKAAUABoAAaAFznrQ"
    "AmOaAFJz1oAMcfWgBDzQAtACUAFABQADrQAA0AGcGgBQcGhgJQAdTxQACgBScigBKACgAoABQAUAAODQAp5NACUAFABmgABxQAUAHWgAoAAM0A"
    "LnigBBQAY5oAKACgAJ5oAKACgAoAKACgAz7UALn/8AVQAfpQAY4zQAg60ALxigAHP4UAGelAAeT2oAX0zTAMZFFgDBIoADz3oBBiiwwIxxnrRY"
    "NA20CArigdgK85zQ0INvNABg0AIc55pALjPPWmAnSkADuaADrQACgBRxxQIQ9e1AwI4oAD60AJQAUAGKAA0AGeKADrQAUAFAB2oAMUAFACn8qQ"
    "AT3oASmAdsUAFABQAUAA5oAKAA8UAHSgAoAXHFIBKYB2oAKAFPXmgBB19KAAHHpSsAUwCgAoAKACgAHWgBccUAJ2oAKAA0AFABQAUAFAAODQAZ"
    "zQAUAGaACgAxxQAUAKTQAdelACDmgAPNABQAo/nQAE8e1ACUAL0GaAAD9KAFC+tMAA6/pQApAHPpT6gA68dKSGgPPrTATG08UMQuB+dJjDHHTm"
    "iwhR3p+Q7jfwpCA80DFIyPpTsAAZHNCQCbcDrikAqihCAr6UAJtFFh2Aj9aBCDBpAKeD160AIBk0AKB+YosK4h5x6UDAjnAoAO1AAaAEoADQAG"
    "gAoAM0AHSgAoAKACgAoAKADpQAUAFAB1FABQAUAHegAPWgA7UAA680ABoAKADrQAUAFABigAoABQAUAGKACgA60AFACjigBKACgAoAKACgA60A"
    "FABQAUAFABQAUAKDjtQAmKADrQAuc446UAKRg0gAcDpkUAIc0wAn2oAOtABtOaB2A8E9aBDs5HvVDuABApALinYYhXAosSLinYYGiwABRYAxns"
    "aYBjrSAPSmAUWAMUWAAOaVgDHFABj1oAQik0AH2pAIfu0AAAzkGgQEfL6UWGIBwaBCjBHekIQGgAzj8aBh1oAPzAoAMdKAA+tACYxQAUAGKACg"
    "A70AH1oACc0AHWgBQM0rgJTAKACgAzQAGgAoAXtQAlAB2oAKAFzmgBO1ABQAGgBQcUAJQAUAL0oAM4HuKQCDpTAKAFBxSADxTQCAZoAKAFxkUA"
    "JQAYoAKACgAFABQAUAAoAO9AC49ulACGgBenSgBD1oAXFACZxQAp6CgAJzQABSD70WGGMHmnbuHqLyG680CFHJH86aGLjnmmkAtACAc0wv3FNA"
    "Cd6AFxmgPUKAAcCnYBKLBcDRYABxQAZxSAO9AheMUDAcntQAdqAEPAoEwNIYY/SiwBjr70gE4OcUAIVpWEJnB6CgBce1FgEwAfrSEKVyfc0AIO"
    "frQMMcZoAD2zQAY4yaQCdaYCgcUAJQAUAFAABk0AB4OKAAUAFABQAUAFABQAUALilcBM8UwCgA70AHagAoAUrigBAMmgA4oAKAFHJoACCvNACU"
    "AHTNABQAooAQDNABQAUAFABQAUAFABQACgAoAUcUAGM0AGOcdaAAdKAEzzQAueaAEPX0oAXGaAAmgAxkdPxoAXBY8E035DFC4HrTsIXHNNDDrT"
    "AAPSlYBRwKqwNB2ppDQY4osDQpGB3p2CwmOtA7CUwFzikKwY5pgGeKLDsA49DSsID1NFgsIDRYEhc8UWATOTRYBe1KwgI4pWCwmTSsAuOM0MBK"
    "ADHNKwARkdaTQCHFACe/PNIQEZYUAIODx+tFgDGT/hSAMZ/CgAJ5oEJQMU8DFACUAFAAetABQAUAFAB1oAO1ABQAUAFABQAtAAaQCCmAUAHSgA"
    "oAXoKAEoAKACgAoAKAFJzQAhGKACgAoAKACgAoAKACgANABQAUAFABQAUAFABQADg0AKrYoACc9O9AB2oEHQ0DENACgdKaAcOnNFgFVSOlUkAd"
    "BTGAGaEgsLVWHYB1osFgzTSHYPyphYMZoBBjmmkApBanYLCbTRYAwfyp2QC4NFkAbT7UNAAQn8KLAJzmiwrsCMDp0o5QYdAPWlYYAbTzSsFgHJ"
    "9qOUVgzkGlYLiUmgF60WAMdMHNKwhO5pWBBj1pWAKQARRYBCO3pSaBiYBHHNFhBjOfaiwCZz1FSIByfrQMMYHvmgAJzQAgNABQAUAGeKACgAoA"
    "AM0AGKACgAxQAYoAKADpQAoOBQAEc0AJ2oAKAFxge9AB1oASgAoAMUAFABQAUAGaACgAoAKACgAoAKACgAoAKACgAoAKADvQACgBeooAQnIFAC"
    "9RjFAAf1oADweKAFU5NNIAGDn0oAOg9RQAoOT2qkAo6UIYvaqSCwmaY0LjA6imMOtMAHSqSAUDIzTsJNi7emadh3FI4pqIBtGDVcoWE2jijlCw"
    "oXBzTURWDHTFCiOwuM9qfKFgUZ4pcoxKOUVg7UmgsGAetFg5RCnpS5RNCbcmlYNQIwaVguIQKnlC6Dr9TQ0O4u3ipsAh5GaLBYO1TYVuwlJoQo"
    "9qQCZoC4AUrAA49qQDSM+tDQhDSAXPfpQITPWkMO9ACZwaADrQAUAFAB2oAM0AHWgAoABQAdaACgAzQAUAHSgAzQAUAHSgAxgUAHSgAoAKACgA"
    "oAKADrQAdaADtQAUAFABQAUAFABQAUAFABQAUAFABigA7UALigBCMGgBT/ADoAQc0AOHt+dACgZPPFUkNCgYPNOwgpjA0BYU1Vh3Eqgsxeh9ad"
    "ihStUkIVVq0gF7U7AkOC1aiMVUANWoAOEfPatOQdhRHzTVIdgMf5VXsg5RfK70/ZBygI+aPZj5RCnPapdImw3b7VLgFgKcdKXIFhNgpcgAUFTy"
    "ANKkCocRCY9qloLBjFLlFYQrk1NgsBXP8AShoBPelYQjHJqbDD8KmwNCfpSsSHapsAooaGJ2pWEG3j60rAIRmk0KwgxuoARRmpEwx8tACdqBhn"
    "JoAB1oAKADrQAdaAFIxQAY5oATGKAAigAoAU9aAEoAKACgAoAM0ABGKAAdaAF4zQAlABQAUAHagAoADQAUAA680AFABmgAoAKACgAoAKACgAoA"
    "UDI+lAB0FAAOe+KAA8UAKRge9ACEE+9ACjjPFAC+lUOwv3qYrhTGL3ppDSAjnrxVJBqAHNNIdh22rSBigYqrAgqkgsOVCa0UBjlj4rRQGkSIhF"
    "axgUkOWIt0raNIqxIlua6I0ClEkW2x261usOPkHrZH0rWOEbHyB9lwRxTeFewcgjWnt0qXhmhcgx7fHHpWMqFhOJGYMGsXSJsN8nHWs3SENK+1"
    "ZumAhGeKhwE0M2nPSocCbBt4qHEBu0GpcRCbCB06VDiAhFS0AmKlxFYQrzxxU2AaRg9KloVxOlJoe4pGDU2E0JSYMM1IC54pCEIzznmkA0rg8U"
    "n2EBHB9qLAJjj8akA60AIDQAp9uMUAJ0oAKAFA/SgABpAJmmAUAFAATmgBSKQCUwDqaACgAoAO1ACn86ADFACEYNABQAUAFABQAUAAoAKADtQA"
    "UAGaACgAoAKACgAoAKADFAC9aAEoAXrigBO9ACgZ+lAC9DxinYYKME00IcP500AcgUx2DGaaAU8d6pIoAM1SQIcowOlWkMd2q1EQBSTVqLGOVM"
    "VqogSKpFaqBaRIseTW8aY0iSODPvXTCiUoliK1zXdSwzNFAtQ2OQDivRpYPyNlA6PwD8JfEPxP1dbDw5oera7enH7iwtJLmQA9yEBIHueK9TC5"
    "RWru1OLfojswmAr4mfs8PBzfZJv77Htll/wSs+Nt7YrMfBklorjIW61C1gf8VeQMPxFfQR4OzCUfcp3+a/zPpIcD5xNXVH75RX5syfFH/BNv4y"
    "+DtNku7rwLqtzBF942DR3xHvthZ2/SofCOYQTdWk19z/ACMK/Bub0U5ToNrytL/0ls8a13wxd+H9RltL21ntLmBtskU0ZjeM+jKeQfrXi18ulG"
    "XLJWZ87VoShJwmrNdHozNnsD7c15tXBtGEqZWktMHpXBPDNGTgRPbFc1zSoMlxIzDWDok2GNFnNYukxWGNHispUybDGTnmsZQE0IUzn1qXEVhC"
    "hWocRWG4zis3EQ109KhxASocQEqHEBHGMd81LQhpUg80mgDHJqbAJ0qWhMKlgFJoQE+nWgQm04I7GlYBMd+1SAmQOPWkIO9AwFACUAFAC44zQA"
    "EY5xSASmAUAFABQAUAFABQAUAFABQAUAHagA7UALigBKACgBV96QCUwA0AFAB2oAKAADNAAaACgAIoAO9ABmgAoAKAFzkUAHUUAAHzDNAgxxxQ"
    "MUnI6YoABx7U7AOA5z3poBR1+lUAE9KaRSExzVJDFAyatRAcqj3q0gHAZrRRGOEfNaKADlWtVApIesWa3jTY0iZIePpXVClctInhtq7KVAtRLU"
    "NlyD616NLCs1UDe8F+ANW8fa9baVomnXmq6ldtshtbWFpZZD7KBnHqegFe5g8tqVZKFON2+iOvDYSrXqKlQi5SfRK7Pur9lr/gkLF5UGrfFa8e"
    "EEB00DTpR5rZHSedeE/3Y8n/AGh0r9MyfgOckp4vRdv83/l95+l5PwDFJVczl/25F/8ApUl+UfvPt3wB4Z0j4R+HE0jwxpdh4f0qMAC2sYREHI"
    "GNzkcux7sxJPc1+k4bKcLQgqdKCSR+jUIUaFJUMPFQiuiVv+HJ7yb7ZKSxLfU16sIqK0NVUBLn7Ip8pmjPqDih0+b4kL2jucN8ZvgV4O/aA037"
    "L4w0Kz1dlGI7zb5V7APRJlw4H+znB7g15mPyDB4yNqsFf+uu5yZjl+Dx8OTGU1Lz2kvSS1/Q+OPj3/wSKv8ATIZL74ea0mtQjc50zUisF2noEk"
    "4jk/HZ+NfnmbcAVY3lhXfyf+f+Z+eZp4fzV55dPmX8stH8ns/nY+PPHvwx1z4a6/Npev6Tf6RqEP34LuFonx6gEcj3GQexr83xuVVaE3TqxcX5"
    "n57i8FWw1R0sRBxl2asc7PZ7e1eNVwjRxSgQSW2BXDPDNEOJDJB83SuWVEz5SJocGuWdIloiaICsJUrEtDDGM4xWDgTYaU9KhwAaUxWTgToNZO"
    "ahxE0NK5FZOIhpXB4qHEBtZtAIy5NQ0JoTGCe1JoSE6LUNFCAZNITAkY+lTYkKkA6UAI340mJgwwKloQ0cnJpABxQMTpQAUAAoAXBxn0pAJ0pg"
    "FABQAUAFABQADrQAUAFAB2oAKADtQAUAGKACgA6mgA6UAFABQAUAFABQAUAFABjigAAzQAUAFABQAUAA/KgBRxQAoHPrmgEAUYNFgDbk0wArke"
    "tFgHDpVIdwqkgCrRQu3iqSCw4DFWkA4LurWMRkip+FbxiNIcEzgCtowuVpsSrFkjit4UykieKDJ6V2U6NylEswWe49K9Clhnc2UC9a6ZJcSokc"
    "bPI7BVVRkkk4AAr16WCejSNVDZI+r/2Yv+CV3iz4rW9rrPi95PB3h6TEixyx51G7U9kiP+rz/ek5H91hX6HkfA2JxVp1/cj+P3dPn9x93k3A+I"
    "xFquOfsodvtv5fZ+evkff3wO+BfhH9nbw3/ZvhHRbfTRIqrcXjASXl5jvJKeT9BhRngCv1rLcgwmBjy0Yn6ZgsFhcFT9lg4cq6vq/V7v8ALsjt"
    "TKDzwf616/KdPNqQvcbuM1aQOZGZdvqarlYnMhmucdOa0UQ9oVpptx5rRIftCF5sfexj61pGI1JmB8QfBmg/FDw8+l+I9H0/XbE8rFdxBjGfVG"
    "+8je6kGuTGZRhsXHkrwTXoRiIUcTD2WKgpx7P9Oq+R8lfGn/glFYawJr34f6z9jmwW/srVnyjH+7HOBkewcH3avzrOvDhO9TBP5Pb79/zPh8z4"
    "Bo1Lzy6pyv8Allt8pf5p+p8gfFf4HeJ/g5rraf4j0S+0qcNhTKmYpvdJBlHH+6TX5dmmRYjCz5K8HH8vv2PzrMcqxWCqeyxdNwfns/R7P5M46e"
    "y29q+frYTl0POcCtJbkduledPDMzcSCW3x2rjnQM3EhaHFcsqRHKMaLHasZU+wmhjR5AFYygTYYy46Vi4iaGsvNYyiIYVwazcWKw0qDWbiIawx"
    "WbiAmM1m0IaRn61NhCd+nFQ0MQipaFYM5pCQD3qRAeBSGIeRQ0hDSDmoACMUAJQAdaADtQAUAFABQAUAB4oAUcc+lIABx+NACHimAdaACgBQMm"
    "gBKACgAFABQAUAFABQAcUAFABQAE5oAKACgAoAKACgAzQAdDQAUAHWgAoAcTwO9AADzximAD0oGKoIoAXpVWADTADVIY5RzVpDHYxWiQDlHI71"
    "rFDRIg/St4xGiRRmumMCyVIe9dMKdxpFiC2LEV30qFzRRLkNp83SvUo4TqbKNj239nL9hjxt+0QsV7Y2Y0rw9uxJq18CkBAOCIx96VvZeM9WFf"
    "d5Jwfi8wcXSXLF/ae3y7/l5n0+TcK43MLVILkp/wA0tvkt3+Xmj9Bf2b/2MfA37OFjFcafYjWfEKj59X1BA8qn/pin3Yh9Mt6sa/aMm4PwmXpN"
    "rmn3e/y7fL7z9SyvIcDlqToR5p/zS3+XRfLXu2ezR3ry/eYsT6819N7NLY9WTbd2PWXIPNTyiuKZ8D2pcpPMRNPnv71SiHMRNcbRgnmrUSXIgk"
    "uMnJq1AXMRSXAIrSMLBzFSa69+taqJopFWa4AHWt4wsUpFWa7+XArWMQ5ilq9vZa7pMlhqljZarp8ww9reQrNE3/AWBGaxxeW0cTHkrRTTLnNT"
    "j7OrFSj2aTX4nzL8Z/8Agmn4X8dyzXfg69k8K37ZYWV0zXFhIx9G/wBZH/4+PQCvzbPfC+jVvVwUuV9t1/mv60Pj8z4GwOJvPAz9lL+V6x+/df"
    "ivI+TfjX+y34y+At1s8RaNNBau2yK9h/fWk5/2ZF4/A4PtX5Nm3DGLwLf1iFl36ff/AJ2PzvNcgxuXu2KptLpJaxfo/wDOz8jzae3x2r5Othux"
    "4bRXktj6V508MyHEhkgwa5J0SGiJ4sE8c1yzpEtEUic8VzypkNETLx0rCVMQ1k55rFxFYYRtz71jKJLQ0jNYyQhjDFZtAIRnFZtCY1gQKmwCVD"
    "QBipsSwBIP0qbCE6nsKQwbgc0CEzkCpAbyTUgBOaACgBOtABQAUAFAC44oASgBccdRSuAnSmAvXmgBKADFABnigAoAKADPFABQACgAoAUdO9AC"
    "UAB60AHWgAzQAUAGKACgBTkUAJQAUAGKAAUAAGTQAvVqADHOKAFJ70wDHftQAuP0p2GKOKpILC9BVJDQAVSQx2K0SBjlXNaqI0PRfSt4xBImRC"
    "x4rphAtE0cPSu6nSuWkW7e26V6dDD3NYxO8+Df7Pfiv47a+NO8MaRcahIv+tmA2W9sPWSQ/Kg+pyewNfU5Tw7i8dNQwsHJ/gvV7L+rHrZZk2Lx"
    "8/Z4SHNbd9F6vZf1Y+4f2fv+Cb3hP4YxwX/iuSPxbrifMLcqV063P+4eZSPV/l/2e9fu/Dvhrh8MlUx/vyXT7K+XX56eR+rZPwVg8Hapiv3s1/"
    "4Avl19Xp5H07b3bJYxQKFSOIBURQAiAcAADgACv0eGHp09Iqx9XObk7sv2c21QCaznHUykXYZAenesJRMnIc0u3uaz5SXIaZ+OtNRJ5hnn471f"
    "ITzEE03XBq4xFzEEk+B1rRRC5XkuMn2q1AHIrzT+9axgCmVJrrbkCt4wLUynLdYB9q2jEpSKkt3uPXFbRgUpleS9CZ5rdQHzlW81BrizmtpNk9"
    "pcKUlglUSRSqeoZTkEVNbA0a0XGrFNM1jWkk4vVPdPVP5Hhnxa/YF8BfEtJLnRmk8F6u/T7Ohl09z/ALUWcp/wAgD+6a/Ms/8ACzCYi9XBe5Ls"
    "tvu/yaPmMx4NyvFpzw/7mflrD/wHp8mvQ+V/jX+x143+Ckcl3f6Yb/RgTt1OwP2i2wO7Ecx/8DC/jX4znnBmYZc261O8V1Wq+fb5n55m3C2YZe"
    "uerDmh/NHWPz6r5pHkstvz618VUw5801cglt+nevPq4clxK8sGK4alEzaImixXLKkRYjeLA/pXPKmJohdcH2rmnAljGH61hKJNhpGRWMoiGYxW"
    "TQCMN3fFZtCYzGTUkikcH1qGUJnrUkidaTACCakGNYYzSYhDzSYARSaAD1pAJ0NAC9u9ACZxQAuMdaAEzQAdaADpQAvQUAJQAUAKDg0AJ1oAOt"
    "AAaACgAoAKACgBSMUAJQAUAFABQAUAFABQAEUAFAB0oAKAFwPX9KAAYIoAP1oAXoP5UAGBjHNMBccUDFHHFV5CD8qpIpCjk8VaQCgbj3OatIY4"
    "CtYoESKvHSt4xGSRpk10wgUkWoIc16NChc1SNfw14YvvFOtW+nabZ3OoX904SG3t4mkkkJ7BRkmvcwWBqVqsaNKLlJ7JK7fyOrDYepWqKjRi5S"
    "eySu38j7C/Z8/4JhA2MGs/Ea9Wzi4ddHtZh5jD0mlHC/7qEn/aB4r9w4X8KKtVqrmd0v5Vv830+X3n6flPh64pVs1lb+4n/wClP9F96Pqfw54i"
    "8L/C7w/Fo+lSaTpGmWwxHa2gAUdsnbkknuTknuTX7/lvDEcNSjSwlDliuysfoNOVCjSVGglGC6LRf157skT42+GoXJfVIx9IZT/7LXqPIsa1pT"
    "/Ff5kyxVGO8izaftAeETLg61bpg/xxSqPzK1hUyDHrX2T/AA/zMJYuj/MdFovxK0DXpAlnrelXLH+FLpN35ZzXlV8BiaWtSnJfJijWpy2kjqID"
    "5aA9j09682VmxTZKGLdOhqLGTYyXjntVIzcitLPirSC5E83y5q1EEyvNc8etaqIc1itLcDHWtIxJuU7i64+lbRgK9ilcXnWuiMCuYp3F3k1soF"
    "c5VkuM1vGA+cq3Fwc81rGIc5Va4NbKJamRS3GR9K1USlMLfV5rEP5UrIGGGA6MD1BHeoq4anVVqiub0sROHws8s+K37IPgD4vedcPZHwxq83P2"
    "3S0CxOfWSD7h9yu0n1r834g8Lsvx16uHXs5vt1+W36+Z4WZcMZZj25uPspv7Udn6x2+6z8z5j+LX7Bvjn4a201/ZWaeJ9GQFjeaUDMY1HeSLHm"
    "J78FR/er8Lz3w+zTLpvnhzR7r/AC3+668z8/zXgrMcHepCPtaa+1DX71uvxXmeIy2hV2BXBHUelfn9XDO7TPj7FeW2weledVw5LiQSQ4HSuKdB"
    "kOLIJYsVxTpENELx4PSuSdMhoiZDmueUSWhu3I+lYSiIYR+FZSQhpGazaYrid+gqGhpjcZzUsTQp+lSxCYqWAHikDEINIQjetJgJj5TUgAHfj8"
    "aAEoAKADNAB0oAUnP4UAJQAdaAAUAFAB+FABQAUAAoAXODQAhoAKADFAB0oAO1ABQAUAFABigBcnNACUABoAKADrQAuPrQAlAB2oAcRj2piEB/"
    "OkMUDk9qY7DgOKpAGeKYC9KpIfkKByMVaRQoXFapCHqMnNaxQyVFzXXTjcpInihI59K76dLqUfSf7Nn/AAT5174o2VtrXii4PhPw7MBJH5se69"
    "vk/wCmcRxtBHRnx6gMK/YuCfDLMc2UcRWj7Ok+r+JryXReb+5n3eScF4nExWIxj9nTe38zXkunq/WzR9d+DPBXhP4HaEdP8GaJBpZZNk9/MRNf"
    "XnPV5CP/AB0YUdgK/qLhTgDLsojelG8nu+r9X/wy8j9Ny7D4bL6bhgaajfrvJ+r/AKXkYniPXJrmZnkmklf1ZiTX6dhcPGKtFWHUm5Pmk7s5bV"
    "dUxnJr1qVM5ZyaOf1HVSDgVvscc6pj3V+cnJxXPOTRw1KxnTX5LE5Nc86zRxTqmr4a+LPiLwW2dL1nULED+GKdgh/4D0P4ivLxVHD1/wCNTT+Q"
    "4YyrD4ZWPT/A37eniHQXjj1qzs9at1GCyj7POf8AgSgqf++fxr57FcO4WetFuD+9f5/iddPOZx+NXPZfAn7XXg34htHD9uOj3j/8sNQxGp+kmS"
    "n5kH2rwMRk2Jo625l3X+W/5np0M0oVHa9vU9DZg0CyBgyONykHIYeoPeuBLWx6CaexWkl3nrWqiD0KtxcbSQOprVRC5UnnPc1rGBm2Uri73A88"
    "V0RgK5QuZyc810RgHMU5LnJ61soD5ivLdEd/rWqgHMVprnk85rVQHzFWW5xk5raMB85DJek9+a0UBqZC11uq1EpVBguMVSiWqhZ0rWptJuRLby"
    "yQuP4lOKyrYeFWPLUV0dFGtKDvFnN/FH4EeA/ju8sviPRRaatOcnV9L2wXRbHWQYKSf8CGfQivzviHw3y7ME5xjyz7rR/8H5pnFmGS5ZmWuKp8"
    "s/546P59H80fNHxf/wCCaXjHwjazaj4Vlh8b6TGu9hZIUv4R/tW5JY/9sy/0FfgnEPhzmGAk5RXPHy3+7r8vuPz3NOBMdh06mFftYLtpL/wH/J"
    "s+c9U0WfTLyW3uIJYJ4WKSRyIUeNh1BB5BHoa/OcRg5Rk4tWZ8RUpuLcZKzRQntCBzXl1sK1qYyiVJoto6V5lakZtEDxnFcM6Zm0RumB71yyiJ"
    "kbJntzWEokkeKxaE0IVIJNZtC2G7ahjCpE0HQ0mIBUtAIRxx2pAxME9etIQh4HFS0AnakAY56UAJ0oAU84oASgAFABQAUALjFACY4z2oAKADNA"
    "BQAvSgBM4PFAAetABQAUAL296QB2pgJQAUAFABQAUAFABQAUAGaAD9KACgBRye9AMO1AAB9KYCr1osA6qGFWkNC9RVIY5RhqtDFFaxQEiDFdFN"
    "ATxJ713UoXLSPt3/AIJ1fsbacdBg+JXjOyS8SRs+H9MnX5JSp5upVI5UHhB0PJIxtr+hPCrgD65y5ljY+79lP/0p/p9/Y/TeDeHKaprM8ZG/8k"
    "X/AOlP9F8+x9J+K797q9knkYu7Hr2HtX9Z4OjGEFCCskfoVSrKb5pPU5HWNQYqecV7dGmjByOW1a/3Bs5r1aMDCczm9TmLZ9DXoxWhy1HcxbzL"
    "Hv8AWlNHHMzLoHB5rkqRZw1ChKh5znNcU4s45plaUnPWuScWYu5Wlcjv0rknFmEpEXnsjZzWWsdTO7Oq+Hvx38W/DW4VdG1W5W3zzayfvoG/4A"
    "2QPqMH3rhxOGhiH70bvv1+9HXhcbiaUrUW/Tdf16Ht/hP/AIKMeEyqWXiySz0PUMhWmgmEsOfVkyXT/wAe/CvhM3z7IstqunXx1NP+VyXMvu/W"
    "x7kOIcMpcmKkov1/PsezeHPGeleONATVNG1Sw1Wwl+7PaTrKh9uDwfY8105fmeExsFUwlRTT6pp/ke1RqwrR56UlJd1qLcXgbODmvYhAuSsUbi"
    "5DE810RgZ3Kdzc8HniuiMBNlCe79OK2jAVyvJdbhnNaxgHMQT3XBwa1UBKRTnus9xWqgNyK73Jz1xWqgHOQvd4796pQGpiJec9afIWpkiXW48n"
    "FDiaRmTxXB3ZzWcoHTCRvaDqktpOrxSvE69GRiCK83E0Yzi1JXR20qjTumanjv4VeBv2htNEHjnw3banc4Kx6ra4ttShyOvmr9/HZZAy+1fmvE"
    "HA+Bxt5KPLL+vmGOynAZmrY6mm/wCZaSXzW/zuj5c+O/8AwSG8Q2EE2p/DPVYvGdgo3f2ZOFtdViHoFP7ubHqpUnslfh3EXAeNwrcqa5o/18vy"
    "PzvOvDfF0U6uXS9rHttJfo/w9D488W+BNV8Fa1c6brGnXml6haOY5ra6haGWJh1DKwBBr80r5fUi2pxsz82r4apSm6dWLi10ejMSe329jXj16F"
    "jlkirJHg15tSmZNELDHNcc4kkbjBzXNJCGkZFZtEsa3HNQxXE3Z61DRQE4PrUk2DNSwEP51LAQjigTEIJqWgEJwKkQHj/PSgBOvWgYHk0AAoAD"
    "QAuaAE60AFABQAHigAoAKACgAoACMUAFABQAdqAFzkc0AJQAUAFABjigAxQAUAFAB3oAUDJoAToaACgBc4UetAgHWmMOT+VIBwOT9KpAKD3qhi"
    "5796opIB71aGOH3quInuOXlq2ihokQc11Uxo7r9nv4VS/Gr4zeGvC0RK/21fxW8rjrFFnMr/8AAUDN+FfU8PZZLHY2lhI/baXy6/crnp5Tl8sb"
    "jKWEj9ppfLr+B+rPiF7XSXWy0+CO00zT41tLOCLhIYUG1VUegAr++8ky+nhsLToU1ZJI/fqygnyU1aK0S8lscfrV5vzzwa+qoQsc0pM5TWJMg4"
    "zXr0UjCU2czqRJOSK9SkZt3Ma+TkiuuOpzyMy4t9xPXHehowkijNbZOMVDic84FSez+auedI5JU2yrNYk/w1zToGUqLZC2lsTjZ9KxeGuZrDNu"
    "yMzxlrui/Duz87Xbv7PIy7orOLD3M30X+Ee7YFfAca8dZFwzR581re+/hpx1nL5dF5uy8zXFUcLgY+0zCfL2itZP5dPV2R4d8Qfj3qXivzbXT1"
    "/sjTGyPKicmWUf7b9T9Bge1fxrxx43Z1nsZYTCf7Nhn9mD96S/vz3forLunufDZnxBUrXp4Zezp9lu/V9fwXkcKs5wAe1fjyraWZ8+mb/gP4p+"
    "IPhhrK6h4e1i/wBIu1G0yW0xTzB/dYdGX2YEV6mW53jcvqqtgarpy7p/mtmvJ6HZhMwxGFnz4ebi/L9e/wAz6q+CX/BVGe3jjsPHujrerwo1PT"
    "QIplHq8R+Rz/ulPoa/deGPHbE0mqWcU+dfzR0fzjs/VW9D7jLuOHpHHQv/AHo/qtvut6H1B8NvjD4Y+M+m/avDGt2OrKF3PAjbLmEf7cTYdfrj"
    "Hoa/oXh3jLKM5p8+ArKXVrZr1T1X3H2eDzDD4uPNhpqX5/Nbo1rubaSDke1fYQinqdfKzPnugfeulQIZXe5rVQJK093gH1rSMLiuVJLwZPNbxg"
    "K5BJdfhWnKLmIWvM96aiHMxou8E+tDiVGRNHPuA55qZRNoyLtu5z61hNHVBm7oqlsd68+uztpM7Hw/A2Vwa8XEyPToI9F8GQGCaNwWVlIIIOCK"
    "+Zx1mmme1Rdtjc/aD/Zs8B/tmeATovjfTI11SKEx6d4itYlXUdNbnb83/LSPJ5jbIPOMHDD8qzzhunXvKno+3+XY8vOuEcLm9JqppLo+q/rsfj"
    "f+2V+xx4q/Yz+LM/hjxJEk8Mqm403UoAfs2qW5OBLGe3oynlTx0wT+I5lltWhVdOov+CfzpnuR4nK8S8NiVr0fRrv/AMA8anh25r5yvSseDJFS"
    "Va8qpEyZC30rlkiCOudidhHXIqGiRuOKzY0HU1LG0FSSAqWgYVIDW460MTEAH59qmwCYINIBMUAKeaAEoAUnBoASgA7UALikAlMAoAUCgBKACg"
    "BcevFK4CUwCgAoABQAUAFABQAUAKBQAhoAKADpQAfjQAdKACgAxxmgAoAUUAKMdKYCj5hTQxRVAKKtFIO4/wA4q0A8DmtEAqda2gMmiHNdlJDS"
    "Pon/AIJhQh/2xPDkhGTbWmoTL7MLKYA/rX6r4W0VPiChzLbmf4NfqfacBxvnVJ9lN/8AkjPvfWbnzs1/cWGhY/WJPQ5XVyea9eiYSZzuo/ODXp"
    "0jCaMW+t9yk4rupyMmZF1bHdXZCRm0UZrXGeK3TJsVpbLPaqsZez1IJtM3DJHFHImUqFyrrC2Xh3SJNQ1O8t9NsIvvTzttBPoo6sfYV4Gf8Q5b"
    "k2FljMyqxpwXVv8ABd32Su32Oj6lCFN18TJU6a+0/wBOrfkjxH4nftdBI5LLwlbm3U5VtSnUGZv+uaHhR7nn2Br+OPEL6SuIxKlguGIunB6e1l"
    "8T/wAMdo37vXyTPis041p0m6WUxt/fktf+3V09Xr6HiGo6rcateyXF1PLczzHc8krl3c+pJ5Nfyvi8ZWxNaWIxM3OctW5Ntv1b1Z+eVq9StN1K"
    "sm5PdvVsgVs9awUjK4uaYC5I4q02UmLuP51akFy3ouvXnh7UorywurmyuoG3RzQSNHJGfUMCCD9K3w+Kq0Kiq0JOMls02mvRoulUnTkp03Zrqj"
    "6G+Ef/AAUi8WeExFZ+KIYvFVguF82U+Vexr04kAw//AAMEn1r9s4U8cs4y5xpZh+/p99ppeuz+av5n2eW8bYqk+TFr2ke+0vv6/P7z6Z+Fn7Tv"
    "gv41eXHo+rx22oyDA03UMW9zn0XJ2uf9xia/pjhTxW4fztRp0qvJUf2Je7L0XR/9utn3eBznAY5L6vU95/Zej/yfybO0vi9oSsisjDsRiv06m4"
    "y1iz0JRexny3e7vXSqZi0VpLjitVAzZXluNvrVKImRtNnPP/1qpRBCo5Y/4UnEtIuWmQRWEzeK1NjT7fzO1cdSVjspo6rw/p5O04ryMRUPSowO"
    "58OabuK8dK8HFVT1aMbHeeHrQpjAr53FVLnqUTv/AA3bZUZ7181ipdT1KM7Gb+1v+yJpP7bf7MWr+DbqOIeJNOifUfDF6wy9teIufJz/AM85QN"
    "jDpyGwSq4/OuJcEqi9tb1/zPjeO8ljmOEc0vejqvX+tGfz9+I9Hm0bU57W4heGe2kaOWN12tGwOCpHYggjFfkeYUFGTP5mqwcXZ7mLMvWvm60b"
    "HM0QOOa4JozZGwrnkhDTWTI1G4yOKzY0IelQxiHketSSwqQDOaQMG5FJiY3pUgITx9KQB2oASgBTQAnagA7UAFABQAUAKRigBDxQAE5/CgAoAO"
    "tABQADmgA60AFAC5xSASmAd6ADqaACgAzQAA4oAOlABQAdqADNABjFABQAo600AdBQA8VSGKM46U0IByPpVosUjkVogaHVpFAOVSK3ggJ4Rg+9"
    "d1HctH0n/wAEuVD/ALXmjggf8g3Uf/SOWv1zwsX/AAu0n5M+34CX/CxD/DP/ANJZ91anFgHjgV/a9Bn6pLY5vVIuterSZhLcwL6DOfrXoU2YMy"
    "ru33LjrXbCRnYzrmz3MeK6oTJsylNYknpW8ZgoMSLRZbgkKucDJPZR6k9hSqYunSjz1HZHXQwk6jtBHm3xV/aT0H4eWtxb6QIfEOrRo2ZFP+hW"
    "7AZ+8P8AWH2Xj/a7V+I8ceM+Hy/D1Y5QlVqRjJ3+wmlda/a9F96Pm804vwOXydPDJVqi/wDAE/X7Xy08z5W+InxM1r4na0b3WL6W6kGRGn3YoQ"
    "f4UUcKPp+NfwLxPxfm3EGK+t5rWdSWtk9op9IrZL8+tz8wzXOsXmNb2uKnd9F0S7JbJHPjivmjy7CDmhAPC4FaRiFuo/acVqqbY7B5Zx0q/ZsL"
    "MQqQcVDi0FgHTmkAA4zRzAKshQgg4IqlJhc9Z+GP7anjn4bQQ2b6gNb0qEBVtNRzMI1HZHzvTjoAcD0r9R4W8YOI8kahCr7Wn/LO707J7r72l2"
    "Ppsu4sx2FtGUuePaWv3Pdfl5H0l8NP20fA/wARYI4Lu4l8M6q/Hk3zhrZj/szjAH/Awv41/T/CHj5kWZuNHHv6vUf83w/KW3/gXKfc4HijAYzR"
    "v2cn0lt8pbffY9N84ywJKhWSKVQySI25HB5BDDgj6V+74bE0a8FUoyUovVNbM9ydNxs+5G8hrpSMXcaGy+OaGtBpMtWsBJzzzWUmbRRq2FiSVF"
    "cVWZ004nS6LpZO3jivKr1rHdSgdp4e0bJBxXhYmuenSidxoGkkYIFfP4msejSR22gabhRmvCxFW5303Y6/S4xAB/jXjVm2dEZm/pWtPYvHJG21"
    "42DAjtXl16CmnF9Touprll1Pw9/4Kq/DSL4Y/t7/ABJsLYYtr7UV1eMDoPtkMd2wHsGmYfhX4XnOFcMROK2ufyxxTg1hs0r0o7J3+9X/AFPmy5"
    "j2HPb3r4+vCzPmZIpyDFeZNGLI3ziuWSEM71gxMa3FZsQmc85qWMQ1ImwzxUCDtk96QCNyPSkIQjpk4qWA2kAo4xQAgGaADpQAUAFAAen0oAOt"
    "AC54oASgBepoAQCgBSMe9ACdqACgAoABQAUAFAB2oAMZFABnFAADigAxxQAUABGDQAoGaAEoAUAigAByaAAjJoAKYBjIoAcAV/GqQDs0xiZ5q0"
    "Uh6rmtEDHKMmtooY9R2roihonhGcV20UUj6S/4JcD/AIy80c8f8g7Uf/SOWv1zwtX/AAuUvRn2/AP/ACOYf4Z/+ks+7dXAwe1f2pRP1Ob0Oc1E"
    "ZJr0qTOebMS8QHNejTZkzNuLfPTmuqLIsU5LXceep9utbqZpGm3sjmfiP8TNA+FFsH1i5LXTruisLfDXEo9SOiL7t6HGTxXyXEnG+Ayim+eXNU"
    "6RW7/yXm/xMswzHA5XDnx0ve6QWsn+iXm/lc+bfi7+0dr3xSV7JCmk6LnixtmOHHrI/Vz+S8D5a/mvizjfM85k6dSXJT/lX6vr+XkflWd8WY3M"
    "L0o/u6X8sev+J7v8vI86vrctpl114hYn8jXweYYWUsvxDfSnN/8AkrPlziz1x0r+fmQGPxoGOReeetaRiCJUiLDP611wpvco90+CX/BOj4x/H3"
    "wtFrXhvwPqNxpNwMw3d3NDYxXA9YzO6bx7rkV9XlvDWOxVP2lOi2n8vzseth8mxdaHPCGj72X52Od+On7HXxJ/ZsaNvGfhLVNGt52Kx3RCzWsh"
    "9BNGWjJ9t2faubH5FjMG/wDaKTiu/wDwxjistxGH1rQaX3r70eZzWuO2K8qphtLnC4kDR4NcM4WJsNxgVg0ybCEc9qlgB+UeuaVxBup3KTO2+F"
    "/7Qniz4QNjRdWnjtWOXtJsTW7890bIGfVcH3r7Xhjj/PMglfLa7Uf5XrF/9uvb5WfmergM6xmE0oz07PVfd/kfQnww/by0PxNNHb+KbB9CuHOP"
    "tdpumtc+rIfnQfQvX9OcH/STwldqhn1L2T/nj70fmviX/kx9xl3GGEq2hi48ku61j926/E938L6hZeLtMS/0m8tdUsn+7PbSCRfoccg+xwa/or"
    "K+IcvzOgsRga0ZxfVNP+vQ+vouFWCqUmpR7p3R0WnacX7V01atjdQ7nS6RoZbb8teTXxG50U1qdbomgZ2jFeNiMQd9OJ2mgaGRj5a8HEYg9Gmj"
    "tdE0gIo4rw69a52RdjorK2EKivMqSubqZp285HFck0aRqGjZSZYDOc1zVI6G8ap+Qn/BbUqv/BQnxcAP+XHScf8Agsta/Ds+f76on3Z/N/GUr5"
    "vX9V+SPju7WvhcSup8hIpygjNeRUWpiyJhkVyzJGHmsJCYwrk1k0IaOtSwFxUMGJ2qWhIMZOPyqQEIyKQhCM0gEx6GpACCKAAjFACUAFAAeTQA"
    "tAATmgBMYoAKAA9aACgA7UAFAAaACgAoABQAUAFAABQAUAFABQAGgAoAPSgAoAX2oAAMc0AAOD7UALjJpgKq/pTQC4x3qgDOfwpjACrRSHgc1r"
    "FAxymtYjHgV0QAsQ8V3US0fSH/AAS7k2ftc6Of+odqP/pJLX654Wf8jykvJn2/ALtnMP8ADP8A9JZ906o27jiv7Uoo/UpLQwNRcDNejSRhJGRc"
    "jca7qYkihqt9baJpk17e3EFpZwDMk8zBY0/H19qnE42jhoOpWkkkazjCFN1q0lGK3b0R4H8X/wBrdpBLY+EEeBSCr6lMn71sjH7pD9z6nJ9gea"
    "/I+JePcRWi6GXe6v5uvyXT1Z8Lm/HDgnRylW7za1/7dT29Xr6HgGpzz6leS3N1LLcXE7F5JZGLPIx6kk8k+9fjuIp1JydSbbk929Wz83qznUm6"
    "lVuUnq29W/VlYWpJJrnhhG3dmVh93b/8Se+PpbSH9DVZhhv+EzFvtSqP/wAlBrRnnpzX8tWMhR16dKcUIeDk810QRR9Jf8Etf2f9I/aE/a20ew"
    "8Q2q32gaLDLq97bNjZdCLHlxOD1RpXjDDuu4d6+64KymOYZlGhUXurV/L/AIJ7nD+DjiMYo1FeMU213t/wT9mbu4+yXG5SAoAVFUbVRR0AA4AA"
    "7Cv6ow9CEaapxVkj9Mc3PcraraWniPS7iz1C3tr6xvIzFc2l1Es0Fwh6o6MCrA+hFRXwdKrB0qsU0yGlZxaunufF37W3/BIfwh8R3uNW+G1xH4"
    "O1lwztpVwzSaZdN6Rty8BPPHzJ0ACCvzXPfDOFaDqZe7S7dP8Agf1ofN4/hqlWvPDPlfbp/wAA/O742/s3+Mv2efE7aT4w0G+0e5O4wvKm6G6U"
    "dXikGUkX3UmvxLMcixeDqunioOL/AAfo+p8XisFWw8uStGz/AD9O5wskWCRXh1aNtDkZCVx61xuBLGt9DWTRIhyPpUagCn3qkwH5rRSGdB8O/i"
    "hr3wr1xdR8P6reaXdjAZoXwsoBztdT8rrnswI9q93I+IMflNdYjL6sqcvJ6P1WzXqmd2BzDE4OftMNNxfl19Vs/mfXPwB/4KP6T4jvYbDx/ZJp"
    "FwxCjV9PhJgbp/rYRkr3yyZH+yK/pHg/x9lNRwudws/547fNbr5XXkj9GyfjijVtSzCPK/5lt810+X3I+0/DmlWuqaJZ6jYXVpqWmXqb7a8tZV"
    "lgnGcZVl4PIxX7jhs7w2Op+2w01JPs7n32G5KkVOm009mtUddoPh3ftIWuXEYg9KnHudjo+iiJVyvSvEr17nSpWN+0tPJUcV5s53K9oW42wPSs"
    "W7jVUlRsH3qHEuNYt2V0VYH0rGpE2jWPyL/4LUyed/wUE8WN1zY6V/6bbavwfiJWxNRebP5/4y/5G1b5f+ko+Qrthivh8Sz5GbKcg5+tePU3MW"
    "RMcZrkkSxhOTWDExrHis5EjAahjDvUALjOMUmJgKkQh4FSwGk46VIB+npQAnGOOtIAHB57UAJnNABmgAoAKACgAoAMUAFABnIxQAUAFABQAYoA"
    "KAAdaADPFABQAUAFABQAUAFAB3oAO1ABQAUAKBk896AEoAXODzTuA5ST9KaAUcVQAORmmhpijj8a0iUOU5rWImxy8mtIlDx0roggJ4jiu6iWj6"
    "O/4JhDd+1npXr/AGbqGP8AwFkr9e8KX/wu0vRn23ATtnEP8M//AEln3Hqlz5ZK5ya/tejE/UpPQxbseYNwrvgRa7OA+KXxn0z4Z27RygXupMMp"
    "Zxtyvo0h/hHt1PpjmuTHZnDDx93VnnZnnOHy+Npe9PpFfr2X4nzR8UPiNrPxQ1ETanckwxkmC2jG2CAeir6+5yT3Nfmmc1a+Mm5VHfy6H5hm2Z"
    "4nMKnPiJaLZLZei/V6nHS2XPQ18zUwHLueNKBXkseeBxXBPAXexnKAw2e3OOlYywViHELixzoepHH3bOU/+O0sXgf+EjHyfShVf/koKF7+h5a+"
    "Ca/jOW5xiJ1pRAli+/XXR3KR9q/8ES28v9pHxAy8OPDU/wD6UW9fr3haovNv+3X+aPq+E/8Aep/4X+aP0+jvjLGN5ya/pD2dnofcJkEup+X8ua"
    "0VK42ypcXfGSc10QpiOd+I+j6P8TfB8+geI9J0/XtGnzutLyISIp/vITyjDsykMOxrlx2Q4THQdPEwUk/IzqUqdSLhVjzJ9GfCX7R3/BIl5I7z"
    "WPhjfNcBC0h8P6hMFmALfdt5jhXAB+7IQ2F+8xOK/EuKPCCrRk8RlcuaP8r3+T/z+8+VzHhhfHgnf+6/0f8An958S+MPB2p+BtfudK1iwvNL1G"
    "0YpNbXULRSxH0KsARX4lmGCqYaq6FaLjJbpqzPkK1GdKbp1FZrozHaMj2ryZUmjFjcZrCUSRpU56VnYBN2KEwHg9K0jIaZLFLtOa6qVVos9U/Z"
    "p/a98Z/sweJFuvD1+JdOmkDXmk3YMtlegEZDJn5WxxvQhx2OMivrOHuKsxyusp4Oo0usX8L+XfzR7eT5/i8uqc1CWnWL2f8AXdH60fsbfta+BP"
    "2u/CXmaFcrpPimzj33/h66lBuIgAMywtwJosnqPmX+JVyM/wBF8P8AHNHNIqE/dn1X9fmfs2RcSYbMlyw92fWP+XdHtNpaZbPYV9VOZ9LJ8uhc"
    "KhBjHSsDF1BjOB0p2J9oOSUE4osUqg7z8Hg1LibQqn5Jf8Fl5d/7evionvY6V/6bbavwLifTF1V5s/DuMn/wrVfl/wCko+SLvmvgMQ9T5CRUk4"
    "zXlVGZPcikNckxMZWDJY1mx9ahsQ2s2MKkTFBqQEAzSbACM0hCEYHSpsA0jGPWkwDjFIAP60AFAAelACGgA7UAKTzQAlACg0AJQAUAHagAoAM/"
    "SgAAzQACgAoAU8UAJQAUAFABQAYoAKACgAoAO1AB2oAUnNAAuQcUIBSOO9UALwRQgHZ4qkOwVSBaBVooeprSIuo4ctWkShw4rohoBPFyK7qJUT"
    "6I/wCCZ12LP9q/SnP/AEDr/wD9JZK/XfCX3uIKUfKR9pwGr5xBf3Z/+ks+5NUU3Ks6555+lf29RVtGfqvQ8b+Lfxzk0IT6XpDKbzlZrgciD2X1"
    "b37fXp2SjY+fzfNvZXpYd+91fb08/wAjwTVreTULl5ZXeSWRizu5yzE9ST3NeJi8G5u58JWg5Nylq2Zc+m9sZrxK2At0OOVIpXGnt6V5NbL79D"
    "mlTKsmn7RXFPAW6GLgRvYF+1cn1C72M3Abf2Wzw7q5xjFhN/6DWOb4TkyTMX/1D1v/AEkap6S9GeMNkHpX8FSWp5IiURQEsY+cV00i+p9kf8EX"
    "bv7H+0dr5zjPhycf+R7ev2DwmhzZw1/df5o+q4T/AN6n/hf5o/S5tW2oDmv6fVA+1VitLqYfvWsaJVypdawApGa2hREZF5qwVjz+tdtOiBkX+v"
    "suQGI9wa7qeHXVDOA+Lnwj8I/HbSvsnjHSYtTVE2wXaYjvbPnP7uUcgZ52nKnuDXgcScC5ZndLkxVJcy2a0a9GYYzC0MVT9niI38+q9H/SPh/9"
    "on/gnT4h+HbT6l4QlfxdoSZYxxR7dQtVz0eIffAH8Uee5KqK/mbjPwfzbLL18HH21JdviXquvqvuR8VmPDVainUw754/ivl1+X3I+a5Lco5UjB"
    "HUEdK/Fp0nex8y0RsNvWuWcbCGbT1rKwgBwc0J2GPU1opDuSxvtauyjOzKTN7wP471b4feKbHWdE1C70vVdNmE9tdW0hjlhcdGUivYwmNqwkpU"
    "pWa2Z0UMRUpTVSm7SWzP2W/4J3ftwWn7YnwruV1JILLxx4cRBq0EQ2x3sZ4W7jX+EE/Ky9FbHQOoH9EcIcSTzCiqVd/vF+PmftfD3EjzGjy1dK"
    "kd/PzPdJrze5x0r7xQPf8AaCJPkCnyk842a42dKpRGqgttOGPJ5pSjobxqH5Lf8FkZC37efin/AK8dL/8ATdbV/O/Fjtj6q8z8a4yf/CrU9I/+"
    "ko+T7kgivgsSz5GRUl615U2Zsic81yzJG81iyWMJyayYIBUsYdBUsBAefepYhfw9qliEycUAJzsx3pXATJP0pAG7nipYCYyaADoKAEoAKACgA7"
    "UAAoAKACgAoAKADvQAUAFABQAelABQAUAFAARigAoAKACgAoAKACgAoAX7p+lCADyetMBexpgOTjp1NNAGR3pjFU9arqAHr6VaGhVrRAx4PNaR"
    "Ghw6VvAZNDXbRZUT6A/4JvxGX9qfS8Hkaff/APpLJX7D4P8A/JSUX5SPtOA5WziD/uz/APSWfWvxZ+JD+GdHOnWjFb67UguDzAnqPc9B+PtX96"
    "5blyrS9rP4V+LP0XH13Tjyx3f4Hgl/ppklLYyT39a9OvhLu9j5StQvuZ1xp+0EEV51XCHDUomfdaZkkgV5lXAX6HHOiUrjT8n7v1rza2Bsck6R"
    "RuNPwenFeXWwRzTpkJsuOlczwXkZOmRavZf8UrrRx006c/8AjhryuI8Ny8P5lL/qHq/+klqn7k/8L/I8DPGfav8AOqW582HQUluA6MjdXRTKPr"
    "X/AIJB3n2T9oPXX7Hw9OP/ACNBX7T4Mw588a/uP84n1nCP+9T/AML/ADR+iH/CQZ/i4Ff1isOfZjJdfG3O4fnVrDsCjd67n+IV0U8MFzJvddye"
    "tdkMOFzLu9Z3k812Qw47mXeasME7uldkKIXMyfXHifdFIUcdCpwa6o4eMlaSuJOx5v8AGf8AZo8F/HSwnuNStRpOvMMrq1igEjnn/Wx8LIPU8N"
    "x94V+WcZeD+UZ43Wpx9lWf2orf1Wz/AD8zzsdlWGxj99csv5lv811/PzPh/wCOv7P2t/AvxAlvqJhu7G6ybS/gyYbgDGRzyrDIyp5HbIwT/GfG"
    "3AeacN436tj43jL4Zr4Zf5Pun+K1Phc0yitgZpVNYvZrZ/5PyOCYba+FmrM8pjSuayaEKpwPxqkA9ODW8GUieFsN6V30HrctH0T/AMEw/iBfeC"
    "/2zvCEdlLMkWsSTabdIrECWOSF8Bh3AdUf6oK/QOCMfOnnFCMdpOz9LP8AVI+l4VxEqeZ0kvtXT9GmfsPa3OIwWPPev6glC+qP2CU9RWvNp4PW"
    "lyDUhv2su3JpqA7ii52ng0nA0Uj8ov8AgsNKJP26fEzZz/oOl8/9w63r+cOMVbMK3qfkfGL/AOFOfpH/ANJR8o3DZzX55XkfJSZXevNmQRMcnv"
    "XLISE71iSxhGM1mwQDH6VLGJUiDvUsGhcYPtUiExjFACMcGkxiH5TSsIQnB96liDgCgYlABxj3oAKACgAoAKACgAoAKADpmgABxQAUAFABQAUA"
    "L70AIeKADHFABQAUAAoAO9ABQACgBen40AAGe9AAOTQAHg0CAYFMY5eemaaAUkZqgAdTVIaFHSrQxUOMVogY8ferRDQ8DFdEBkkfUV109yke+f"
    "8ABOS8Fn+1JpLMcD7FfAn/ALdpK/YfBu74loxXaX5H1/BH/I3h6S/9JZ9AeO2fXvEN1eHJErkpnso4A/LFf6L4GmqdGNPsfomIXPJyOdn07AwQ"
    "a7uRNHBKjcz7vSsZOK554ZM46mHM+5072rlqYVHDUolC603HbBrzK2EOKpRM64sME8V5VXB3OKdIqPZYPSuSWEsc7pkOuWe3wbrx/wCobcf+gG"
    "vnuMcPycNZlL/pxV/9JNFT/dVH/df5Hze3BP1r/M2e58aNJzjj61KAeOCK3gxn0/8A8Esr42Xxt1t+mdCmH/kaGv3fwLhz59Jf3H/6VE+v4PV8"
    "TU/wP80fdv8AwkBVOTzX9irDH2LZBJ4l56mrjhQZTvfEfPDVvDCktmfceIMn7x5rqhhxalKfWyO9dEcOFyhcatnPNbxojuUpL47+vWuiNMLkLX"
    "hkOM9etachWrOG/ai8PWOv/s4+KI7yNXayhW/tWPWGaNhyPqpdfo1fj3jhlVDF8L4irWj71Jc0X2a/z1XzOXOKcamW1YTWyuvJr+rfM/PxzuPU"
    "4r/PGo7u5+UsQAY6msxC4poe45FzW8I3KRPGtejSj0GfYH/BI34KS+IfjY3jq7Rk0nwlHIkDlflnu5YygQf7sbs5PY7P7wr9j8LuG6mLxv11r3"
    "KfX+81t92v3H2XB+BlPE/W5fDC9v8AE1+id/uP0rHiITSfK3Br+jvq1kfpKkWYtT3r941i6VjRTJFv/Q1Psx85PFdBxyTSlAvmPyo/4K7Tb/24"
    "fE3PSy03/wBILev5j43ds0rLz/RH5Vxg/wDhTn6R/wDSUfLU57V+cV3qfJsgduetefNkNkR61zSYgIyDWbExnJNZtggHPpUNgw6mkAgOKloGGa"
    "liAHPtQAhANJoBGJpANPXNSAUAKTmgBKAFBx24pAJTABzQAUAFABmgAoAKACgANAAaACgAoAKACgA70AFABQAUAFABQAA4oAXbmlcAHXFMA4zx"
    "QIMge9AxeKeg9BVIxTQhRyKoAHSqGthccVaHcVOtaIHuOxWiGh4JBwa3gBLGRgV1w3KR7f8AsAx+b+0jp4HX+z7/AP8ASWSv2TwUa/1qoN9pfk"
    "fW8Fv/AIVY/wCGf/pLPrG70MbOVr/QaGIP0WUjGvvD+M8V208SQZd3opUdK64VkzKaTMi70srnitvdkclSnczLrTOCcGuedE4qlEzbjTAT0xXD"
    "UwxwzolSTTc84rklhdTmlR1KXimxEfgbxAQOml3H/oBr5DxApKHC2ZP/AKcVP/SWN07UKr/uy/I+WG6n0r/Lae5+f2EAGcjvUCHZGRW8GB9G/w"
    "DBNy6+x/F7XGzj/iSSj/yLDX9BfR9hz8RTX/Tt/wDpUT7Hg3/eav8Agf5o+ypNe+X71f20sMj65srTa7hc5ya0jhxXZUuNbJJya2jhxXK02sZP"
    "XNaqgFyL+0s981p7IdhjXW4Y601TCzGiXNXyFKIQlmlxGpdvQUpOMVeTsdNOjKWkVc+fP21/2irL/hEpfBukXUV1e3rL/abxEMluisGEWehcsF"
    "Jx0AweTgfyR4/+JWEqYWXDmW1FOUmvaNNNJJ35b/zN2uuiVnufMcTZrSpUHgqUrzl8Vtkt7et9+2x8o9q/kNrQ/PQ69KhRAULlq0UBksUeK66d"
    "PqUtD2P9mb9kLXPj3qsV1OsukeF43/0jUpUx5g7pCD99z6j5R3PQH9P4F8OMx4gqqpGLhQ6za38o9357Lr2f0OTcP1ca/aVPcp9+/lFdfXZdex"
    "+jnwt0HRvhZ4RstB0K2Sy0vT12xxg5Z2P3nc9WdjySf5YFf2LlXDeGyrBwwWEjaMV9/dvu2fpdClSoU1QoK0V/V35ndaZrylRyDTq4c6FI2LXW"
    "tw4NcM6DLUi9DqIY9axlSK5i3BqO09axlSLUz8uf+CtbeZ+214lb1stN/wDSC3r+V+PI2zWt6/oj8w4v/wCRlL0j/wCko+YJuTX5rWep8rIgau"
    "CZDGNyawkAjHA6VmyWRk5NZsAxUMdwBGakkMjNSADBGTSAD3zSYCOcUmAw8CkwDvSAKADtQAdqACgAoAKACgAxxQAUAKOKADH6UAGPwoABjvSA"
    "SmACgA6GgAoAKACgAoAKACgAoAM5oAKADpQAZ4oAM4oAXk80AKeaYAo75ppAOBqhijn6VSAD+VWihyLkfStEieo5eWFaRLH5zXRACRB0rqghnv"
    "P/AATntDd/tQ6Yg72F8f8AyVkr9f8ABp8vE1GX92X5H1nBn/I0j/hl/wCks+0rvRj0xX94wrn6E2Zl5o5IPFdVOuJsx7/Reoxmu+lXM2zCv9Ix"
    "njNehTrkMyLzStueK7I1U0YySMm70w+mKbimc06aZTfTMA8VDo3OeVIzfGmmFPh74jbHTSrk/wDkM18J4m0lHhPMf+vNT/0lk1qVsNWf9yX5Hy"
    "Aw5Nf5UVNz8uGZ2+4rEQ4HJ45raDA95/YEu/sfxP1lh30iQf8AkWKv6N+jhHm4lmv+nb/9KifX8HO2Jq/4H+aPq4awSuM1/dnsD65yGvqhPemq"
    "JLZC+o4zyTVqkCbIm1AkfT3qlTKQ+O7JH0ocC0ixDIXIAzk1Ekkrs1jBvY5j4lfHXwz8G0Y61elrsLuj0+2xJcyZHGRnCA+rEe2a/MeOPFXIuG"
    "oOGKq81bpTjrLyuui85WM8bjMJgo82KnZ/yrWT+XT1dj5g+Mf7ZPiX4mGez08/8I9o0uV+zWsh82VSMYkl4LZHUDA9jX8acb+NefcQc1CnL2FB"
    "/Zi9Wv70tH8lZd7nw2ZcU4rEJ0qH7uD6Ld+r/TY8hYljk1+Pb7Hy4mKbQEkcRINa06VykdX8MPgr4j+MGrG10HTZrvYQJpz8kFvnu7n5V+nU9g"
    "a+o4c4RzTO6/sMtoufd7Rj6y2Xpv2TPQwOV4nFu1CN0t30Xq/6Z9R/Cb9ifw78OjDd+IHi8S6qMMISpFjAfTaeZP8AgQA/2a/rTgjwCy/BKOIz"
    "pqtNa8v2F8vtfOy8j7LAZBhsM1Or+8l5/Cvl1+f3Huum6q1tbxxDasaAKiKNqoB0AA4Ar9+hgaVKCp0oqKWyR9H7Ry3NnS9aYMCTXNWoI0Ujpd"
    "L8QYC5NeRWwxopanR6br2QPm+nNeZVw5qpG3ZavkjmuCdEu5fTVQpAzWDpaFJn5nf8FVrr7V+2b4jcdDZ6d/6QwV/JPiGuXOK8fNfkj804t/5G"
    "UvSP/pKPmmXnn1r8xq7ny7IGHPrXDNEjWGDWDENIyOlQyXsMNZMEFSxid6kQVDEITzSAVuuaTARhkUmA00mAmcUgADg0AL1NACfSgA70AHagAo"
    "AOtAAKAA0ABNAC5oAQc0AGaADOTQApJPWkAH86AEFMAoAU8mgBKACgAzg0AFABQAUAL+VAAMe9ACAZNACjrTQBnJwBTAcOv0oQCnOKoA71SGKD"
    "g1aKuOXpWqJHAVpEofW8EMlj6iuuG40fQ3/BMqPzf2stJX107UP/AEkkNfrPhLK3EVJ+UvyPreDP+RpH/DP/ANJZ91Xem5HSv7hhVPveYy73Tg"
    "CeMV1QqibMi+0vcMY/+vXfTrEXMTUNI4Py130qxLZi3uk/MeK7qdYhox73S/QGu2FYiyKEmlEDkZrpVVDaRlePdMKfDPxScdNHuj/5DNfA+KdS"
    "/CmYJf8APqp/6SzPFx/2Wtb+SX5HxJKRlq/ynqLU/IGR5xWLEKpyauLA9u/YaP8AxcfV+cf8SqT/ANGxV/Sf0ZteJ6n/AF6f/pUT6vhJ2xFT/A"
    "/zR9NC+I4r++/Zn1yYjXh7Hk0ezGAvDnr1o9mJD0l3n3ocLG0YkHiXxppHw80o32u6jBptt/D5hJkmPoiD5mP0GK+N4r42ybh/DfWMzrKC6Ldv"
    "0S1fyRvWVPD0/bYmShHz3fot2fPnxa/bl1PV1lsPCML6LaNlWvpMNeSj26rGPpk+4r+NOPfpDZpmjlhskToUv5vtv06R/F+aPkcfxhPldLAR5F"
    "/M/ifp0j+L8zwS+vptRupJ7iWSeaVizySMWZyepJPJr+da1apVqOrUk5Serb1bfmz4qdSU5OU3dsjFSo3IsOWPJ61rGm2M2/BPw91r4ia1Hp2h"
    "6bd6neSniOFC2B6k9FHucCvWyvJMbmNdYbA0nUm+iX59EvN2R04XCVsRPkoxcn/X3H1F8Hf+Ce9p4fSDUPG90Ly44caXZyfu19pZRyfomP8AeP"
    "Sv6c4H8AF7uJz+d+vs4vT0lLd+kberPssv4ZpU/fxj5n/Ktvm+vyt6nvOm6ba+HNIjsLC1t7CxhGI7e3QJGo+g6n3r+ocpyfBZdQjhsFTUIR2S"
    "VkfT2SioQVkui0RWmc7/AGr2kgsCXJQjn8abiNMuWuqFWxmuedIfMbGnavtIyTXDVoFKR0Om63jHNebVw5rGR0ul60MZJ4ryqtA3UjatNUR8bj"
    "wfeuGdJpOxrFXPzH/4KFeOIPHX7W/jC5tzmG1nhsAf9q3gjhf/AMfjav4s4+x0K2d4lw2Urfckn+Nz8u4nrxq5lUcell9ySf4nh8vI5xX59VPn"
    "WQuuDXHJCGNWEkIaayaExh6dKzkSJWbGLUgJjPSpYBjJxUiEPSkwEJJxSuAnU54FIQmOaQxc9u1AB1oASgAxmgAHNABQAUAFAB1oAO9AC9sUAI"
    "Tk0AFABQAUALx2oAQ0AHagAoAM0AL1zQAlAB2oAKAFI5oAMc0AIQaAFI5xQAdO/wClAAP50wHDpVIYtMBc8VSBAKuJSHDitIiY5R831rWJQ5RX"
    "RASJoxzXXTWpa3Po7/glvAbj9sDRlHU6dqP/AKRy1+peFcrcQUn5SPquDnbM4/4Z/wDpLP0AutLwO9f2rCqfdtmXe6Z14FdcKpDZl3en+1dtOq"
    "K5kahpuQeDn6V206oGLfaV8x+X9K76dYlsybnSMk8c11wrdgKE+k7R0zXVGsCMP4lacYvhP4tYg8aLd/8Aopq+E8UK1+Fsel/z6qf+ksnFf7rW"
    "/wAEvyZ8ByD5zX+XVXc/HWRkVzMQAc1UQPXv2OtUXT/iNfJvxJc6dIkY/vEOjH9AT+Ff0l9GPEUocWShUdnKlJJd2nF/kmfU8JNfWpxfWL/NP9"
    "D6Tjv2lAJPBr/Q32a6H1xOLjaAankLUbjr67h0zSZL+7ngtLODmSeZwkaD3Jryc2znA5ZQlisdUjThFXbk0kvmzpp4e0XUqe7Fbt6JHjnxQ/bQ"
    "s9FV7TwjALu6XIbULqP92h9Y4z976t/3ya/kTxD+kwvewfC8b/8AT2S0/wC3Y9fWWnkzwcx4rw1BezwEeaX80tl6Lr6v7j578UeMNT8a6vLfat"
    "fXN/dy/elmcsceg9B7DgV/JOa5zjcyxEsXmFWVSo+snf5LsvJWR8HisZWxNR1K8nJvuZy+vNeetTmHBSTWigxl7QPDt94n1aCx060ub+9uWCRQ"
    "QRmSSRj2Cjkmu7B4GtXqxo0IuUpaJJNt+iRpSozqyUKabb6I+pfgX/wTYvNQjh1Lx7dvpVtw66XasGupB6O/Kx/Qbj/umv6C4O8DMXiuWvnUvZ"
    "w/kXxP1e0fld+aZ9jlvCbdqmNlb+6t/m+nyv8AI+nfCvgnRvhxoCaX4f0y20ixQDckK/NKR/E7nLO3uxJr+o+H+GctyegsPgKShHy3fm3u35vU"
    "+wpUoUYKlRiox7L9er9WPulyp7ivqYaDcbmbdRZrpjITRnXEWOnaumLIvYquCrfSt4ksQS7W6mk0CZctb4rjmsJUykzZ0/VPunPSuKrRNFJHSa"
    "XqWVGGry61E1gw+IXxm034L/DLV/E2oujfYIitpCxGbq5YERxgd/m5OOiqx7V8NxrnNPKMuniZvW2i7vov66ahjMXHC4eWIn02830X9dD8rPEG"
    "rXHiDWru/u5Wnur6Z555GPzSOxLMx9ySTX8JY2rKrWlUm7ttt+r3Px6pNzm5y3er+ZQddwrz5xuZMa8RC8isp0WlcViBlOOlcM4ksZjB/wAK55"
    "BYbJzWUiBhH41mxh0qQA1DAMikICeaQCH8aTAaRUiAjn60DBuTxQAmMGgAoAUcikAmaYC4oAToaADrQAGgBfrQAlAAOvpQAYzQAE5oAMUAKBk0"
    "AJQADrQAd6ACgBQcGgBKAFPagAIwaEAAUABPSgA6UAGMH1oAKYBnC/WgBy8Cmhig1QCiqQ7BxVoY5Bz2rWIuo9OtawKHL19K6IATRjk110ykfT"
    "X/AASdg8/9s3Q16/8AEu1L/wBI5a/T/DJ2zym/J/ofU8If8jKP+GX/AKSz9EbzTyF6V/ZFOofb3Mq7sODxya6oVBsy7vT8r0yK64VBGVqGn4XJ"
    "FdtOqIxrzT8kjFd1OqIzrnTR0211QqgUX0zcegrpVYEYHxc0zyvgx40Ppod2fp+6avhvEqrfhjHL/p1P/wBJYsSv9mrf4Jfkz84Zjhj6k1/mbW"
    "+I/HWRGuZiAcmlfUDT8JeK7zwT4ktNUsn8u5tH3rno46FT6gjII9DXvcO8QYzJcxo5pgZWqUnddn3T7pq6fkzpweKqYatGvT3X9W9GfSfhf9p3"
    "wt4l01Hu7t9GuQP3kMsLyAH/AGWQEEfXB9q/v7hf6SnCuOwqnmM3h6vWMlJq/lKKaa7Xs/I++w+d4CtHmnPkfZpv7mk/0Kvi39rbQvDWnvFo0b"
    "63esPlkkRoreP3OcMx9gB9a+c4z+lBk+Gg6WQQdep/M04wXre0n6JL1DF8SYHDL/Z/3k/mor9X+HqeEePvixrnxJvfN1a+lnRCTHCDthh/3UHA"
    "+vU9ya/jvizjrOuIsQ6+a13PtHaEfSK0Xrv3bPi8xzjF42V8RO6Wy2S9Fsc1knvXx99TyxwQnmrjFjJbW2e5lRI0Z3dgqqoyWJ7V0UqTlJRSu2"
    "VGLbsj6R/Z/wD+CdHiP4hm31DxZK3hTRpMOElTN9cL/sxn7n1fB/2TX7bwd4N5rmaVfHr2NLz+N+i+z6y+5n1mW8K1qq9pinyR7fafy6fP7j7E"
    "+FvwU8K/AvSza+F9LhtGdQk15J+8u7nH9+Q84/2RhfQCv6h4X4EynI6fLg6SUusnrJ+r/TbyPtsJg8PhYcmHhbu+r9X/AEvI3bltw6//AF6+2j"
    "Gx0PcoXKZ7dK6YvsBRuUABwK6IvUloz7pMZOPpXRFmbM+5i4rpiyGUpE5wK3ixNFaVcN1xWyJESXaafLcaTLUF8YsZOBWcqdy1E2Lnxlpfgzw3"
    "PrGs6jBp2m2wy8sjcseyoo5Zj2ABJ9K+S4kzvBZRh5YrGzUYrv8Akl1fZLU2c6dGm62IfLFdX+nd+R8Q/tOftJ33x68VIsYltPD+nFhY2jHk56"
    "yvg4Lt+QAwO5P8P8fceYniHMPaNctGGkI/+3PvJ/gtF1b/ADnPM5nj6iUdKcdl+r83+B5na6bNqdykUEUk00rBI40UszsegAHJJ9K+CeHlPVLU"
    "8SMXJ2irtn1t+zF/wSa8Q+PlttX+I13L4K0WQCRLEoG1W7U+kZ4hHvIN3P3COa+/4Z8MswzFqtiV7OH4v/L5/cfbZXwTiasVWxn7uPb7T/y+ev"
    "kU/wDgp78Ifht8CbXwdoXg7QU0nVPLmkupftck81zCNqo829iNxbdgqFHDDGMY6PEnIcryWnQw2FX7yV29fsrTX1e3ozLizA4HCU6VLDQ5Zau9"
    "3e3nfz/U+QZT8xxX4tWeuh8QyE1xyExhOW9axbJA81m2MSpYCk1LAQ5J4NIQmB69KTARvUmkwEyAaTEHAPrSGHSgAJ59aAD8etAAB70AJQAUAG"
    "aAF/CgA5oAD+FAB6UAIetABQAdcUAHSgAoAMUAFABQAUAFAAKADtQAcUAFABnFAC5z70AHagA6UAKB+tNDFUY96pCHDrTGJVoYtWgvoOUVpEB6"
    "DmtoFD15aumIImi6V10kWj6h/wCCRsJl/bZ0Edhpup/+kM1fpXh07Z1Tfkz6bhS/9pR9Jf8ApLP0jvLMFOn6V/XkJn2qZk3VhgniuuFQq5mXen"
    "gZx0rrhUGZV9Y5XGK7KdQRk3enAk8Y/CuyFUDOudOyTiuuFUCpJpwweMH6Vuqo0jmvjRaFfgl42OOmgXh/8hNXxXiNU/4xrG/9e5/+ksddf7NW"
    "/wAEvyZ+ZE6/Mfav83q6tI/GmQHiuOQhc44rMBAapMBwcgdSPerUh3BmLGm5dhCAfnThqwH+XW3s+oHrXwK/ZB8TfGN4buVBoWhSEM1/eKV8xc"
    "8mJPvSHrjovH3q/VuB/CbO+IeWtGn7Kg/tyVrr+7HeX4LzPdy/IMRiEqk/cg+r3fouv5eZ9h/Bj9nHwZ8DTHPo9kb7VlA3anegPMD3MY6Rj/d5"
    "9Sa/rXg/wlybIIqrThz1f55av5dI/L5tn3OAy/CYRfuY3l/M9X8u3yPTk1Uz8sxLepPNfofsVFWR6SZOt0HHWp5LFpjHlwpqkhlaaWtYoClO/P"
    "WuiKJaKdzhufSt4ktGfdLzmumBLRRuFx1H5V0RFZld4wx962Q+VjXsi0MsrMkUMKlpJHYKiAdSSeAKzxGMo4eDqVpJJbt7W9TenQclfot30PFf"
    "ip+2Ho3g/wA2y0BI9f1D7v2gki0hPqD1kP0wP9rtX87cc/SDwGDbwuQR9tUWnO/4a9OsvlZeZ89mHEuGwr5ML+8l3+yv8/y8z538bfE/XfiXq/"
    "2rWdQmvHXIjQnbFCD2RB8qj6DnvX8s5vxTmeb4l4jM6zqS89l6JaL5fM+IxuY4jF1PaYiV/wAl6LZH0D+y1/wSp+I/7SWnWev39t/whngu4xIN"
    "X1SNle7jJ5NtBw83Q4b5Y+D8+eK7sr4VxeZ1VGkrLq2evlPDOKxslKXuQfV7v0XX8EfoB+zt+yn8O/2R9OX/AIRfRlu9fCgS+IdTVZb9jjnyuN"
    "sCn0TnHUnrX7pw94f4LAJSkuaXd/1+R+mZfkODwCX1eN5fzPV/8D0RL8W/G9j4D8Lan4i1W5+zWGmwtcXEzHnA7DPVicADqSQO9fomJzDDZdhJ"
    "4jENRhBNv+u/buelVxEKNN1aztGKu/l/Wh+QX7QHxl1H48/FTVPEupkq97JiGHORbQrxHGPouMnucnqTX8ScVZ5WzfMamOr6cz0XaK2XyW/d3Z"
    "+GZvmM8diZYiel9l2XRHCyHnivkqkjyxhPBrnkyWMrFgHeoYAOTUtAB7UmDBR+vSkIQipfmAmB1pAJjFIBQOM/pQA3pQApoASgBc5HQUAJ2oAB"
    "xQAUALnOKQBTAM8fSgBDzQAdqACgBTQAlAC5yeaQB1pgJQAUABHNABQAUABoAKACgBRxz6UmAH1pgBXAB9aADrTQC9elP0AdjApoA6iqGLnFUM"
    "Tqa0QMkB4rRBfUdHya1gUSKPm9q6YDRPGuQDXfRiUj6p/4I+pu/bh0BcddN1PP/gFNX6NwBpm9N+TPpeFv+RhH0l/6Sz9Lr+zxniv6wpzPs0ZV"
    "3a9eMV2QmaIzLq1wOnFdcJjMq9tM54rrhMRmXFl2xmumNQClLpvPSumNUCCTTMjp3rVVikcv8bNMC/Abx4cYC+Hb4/8AkFq+M8Ral+HcWv8Ap3"
    "P/ANJY67/2asv7kvyZ+Vdyfnb1Br/PHENczPxtkDcnpXDIQn07dazYgzyPWgA7VSQCpya1ghHY/Dr4GeIfiY/mWFp5Nih/e3twfLt4x/vfxH2U"
    "E1+icGeGGf8AE1ZRyyj7j3qSuoL59fSKbPUwGU18W04q0e70X/B+Vz6Y+DH7Png34dQxXNxCPEWsqQ32i6jxbwsP7kXQ/Vs9MjFf2ZwT9HrKsk"
    "jGvmP+0VlreS91P+7HVfN3fax9ZhcrweGScPfn3e3yX+f4Hq8OtNJLvZy2Ont7AV+3LCxhHkgrI9dVZSd5M2bDWdwHPWuSpQNYyNW21ENjnrXH"
    "OkbqRoQX49fzrmlTNEyY3nFRyFpkUkvOauMSrkEj5BrWKEytO3GK2iiSpMhHatolKJX+xNcPtRSxNbe0jBXkzaFFy0irnk3xn/ab8O/CeeS0jm"
    "Gt6snW1tJB5cTekknIH0GT6gV+P8ceNuT5FF4fC/v6/wDLFqy/xS2XorvyW55OYZ7g8D7svfn2T29X/wAOz5k+Kf7Q/ib4uXJTULvyNPUgx2Ft"
    "mO3T3Izlj7sT7Yr+QOLPEjPeIqr+v1bU+kI6RXy3b83fysfB5lnuKxrtUdo/yrRf8H5mR8M/h7rHxd8eaV4a8P2E2qa3rVylpZ2sIy80jHAHoB"
    "3JPAAJJABr5jCQdaahFXbPNw9GdaoqVNXb2R+vX7Kf/BJz4f8A7J+l2Oq+JIbHx74/QLLLPcxeZpelSjnbBE3EjKePMkBzjIVK/ZOFuBqEbYjG"
    "K8uh+pZJw1hsMlUrrmn3ey9F+r/A938Q+JLi7dvtEzynGOT0HpX69hMFSpq1KNkfWW6o4nXQ1wW8vknoK+gw/LFXkaK7PzX/AOCmH7XZ+IXiZ/"
    "AehXe/Q9Em/wCJhNE3y310vVQQcNHGePd9x5AU1/OXipxusbiHlmDf7qD95r7Ul081H8/RH5fxjnqrz+pUH7sfifd9vRfn8j5DuJt54r8LrVW2"
    "fCNkTGuKTJbGsc1m2AhOD71m2AlQwYDpQwEc4GKgQue1IAIyemc0MBhqQFP3e/NAhN3NIdgPFAB1oAM8UAB4+tACUAHU0AGKAFzmgBDQAUAB4o"
    "AM0AKMAUgEpgFAB3oAKACgAoAKADvQAUAGaAAHFAAOKACgAoAXqaAF/lTEhelPYYvUZ5qgFzwKADP41aLQAmtEA8VpEOo6MZraCGSp3rqghliE"
    "dOK9CiWj6v8A+CN8Hn/t0+HlxydM1T/0hmr9E4H93M4S8v8AI+m4W/5GEfSX/pLP08vbTI6V/UlKZ9kZN9Z4rshMpGVd2hwRiuuEykZt3Y4Xpz"
    "XVCoNK5QnshnOOfpXRGYWKktjk8AVsqoWI/wCzc84q/alJdzlfj5ZeV+z38QDjp4cvv/RLV8b4g1L8PYv/AK9z/wDSWOvH/Zq3+CX5M/I+5XDn"
    "nvX8B11qfjTIGGa4pIgQ8+2KyaAQDFCQGz4O8B6r47vjb6ZZyTkY3yfdjiHqzHgCvreFOC854jxP1XKKDqPq9ox/xSei/N9EdWFwVbES5aSvbd"
    "9F6voev+C/gLovhKWKfU5ItcvVw3lqSLSJs9+8n44Hsa/sngT6NOWZbyYziCf1iqrPkWlNPz6z+do9HFnrUcNhsPP3rVJf+Sr/AD/LyPVrfxXL"
    "cwRQtsWGJQiRIoRIwOgCjgCv6ny/C4bD040cNBQitEkrJei6Hr/XZVLc3T7vuLtlrG1xtIA9K75pNanTTqpm5Ya9nqa4qlBHZCZrWWtcjB/WuK"
    "pQN4zN3T9YyoGc159SgdEZGpa6rnnNcc6RspF+31DfXPKkaKROJ9yZqeQtNjWcsB1+tUkPca2Tkc5qkrbmkYX2OI+L3x48L/Bm0P8AbN+GvyoZ"
    "NPtsSXL56EjOFB9WI9s9K+B4w8Tcl4cg1i6nNUtdQjrJ/LovN2MMZmOEwSvipWfSK1b/AMvVnyb8Yf2yPE/xMSayspP7B0eTg21q582QekkvBb"
    "3AwD3Br+ReNPGfPM+vRpy9hRf2YPV/4pbv0Vl3TPgcz4pxWJvTo/u4dlu/V/8ADI8l89n5JzX5L7RvY+avcliUsRxmumlHrIpeZ+t3/BKL9hNP"
    "2bfhxF8RfE1siePfFVr/AMS22lT95oVhIM7iCMrPMvPHKxkDq7gftnAXCqhL6/iI+l/6/rY/UOFck+rR+t1170lon0X+bPqm71dwNrE4FfsVOi"
    "t0fZWV7mHqpN2CRya9GiuXcpOx8df8FJ/20T8CPCs/g7w9eFPGGtQ4nmibDaTbMD82QcrK4+73VctwdhP5p4lcbxwND+zsHL97JatfZX+b6ff2"
    "Pl+Kc+WCpfV6D/eyX/gKfX1fTtv2PzEubgyMSxySck96/l+vVufkTZWJya4JyIY3fWLYhMVm2NCGoYAaQCkVLATk80hATtA4pMBGNDYCNxUsBD"
    "19KADoaQCUAFAC9s+lIA6nvTAKAAHHSgAzz0FACYwaAAUAFAATQAdqACgBTjHvSATFMA6mgAoAKAFGBQAmcj3oAUDPWkwEpgFABQAZ5oAO9ACj"
    "kEdqAAcj3oAB2/xpoBSOOwpgOHH+etMANUh2Fx+tUhoMc1cRjx9c1pEQ6PrW8CiVB611wKRZhr0KJaPrr/gizb+d+3v4dH/UK1Y/+U+ev0Hg7T"
    "MIP+t0fScMv/hQh6S/9JZ+oep2hQ1/TdKZ9kmY13Bniu2EjRGZdWnfvXZCQzPurb6Y9a6YyLRn3Fp8/oK3jPQdiB7OrVQuwn2Sq9oUkcp+0Jp+"
    "P2c/iESOB4avz/5Aavj+PKn/AAg4pf3J/wDpLHXj/stb/BL8mfjxdrh2+tfwriY6n4oyuw5ya8+SIJ9K0i6169W3tIJbmd+ixqWOP8Peu/KMlx"
    "+a4qOCy6jKrUltGKbfrpsu7eiLhTlN8sFc7/w18H7LR3SbXJftUo5+xW7/ACg+jyD+S/nX9R8HfR3oYVxxfFdTmej9jTl+E6i/FQ/8COlPDUH+"
    "+fNLstvm/wDL7zvItfC2Mdpbxw2dnF9y3gXZGv4Dqfc1/UeVrB4LDRwmAoxo0o7Rgkl/wX3b1ZnVzKpV93aPZaIsWmqlgOele7Qxie4QrGja6m"
    "Uxg16lHFW6ndSxBqWer4PWvQp4i56FLEo1LTWT610KqmelTxFzWsdcIxzTcUzsp1joNN1zIHJrlqULnZCobNlrXA+auGphzqhM17DVsgDd1rkq"
    "UDoizVtr0EZzXJKlY3hqyl40+I2ifDTR/wC0Ne1K20216L5hzLMfREHzMfoDjr0r5TiXi7Kshw/1jM6qgui6vyS3fyLxMqOFp+2xU1Ffi/Rbs+"
    "Zfjn+33qXiNZLDwZbvoliQVa+lw15KPVeojB9st7iv5R468dsyx0ZYbJb0ab+19t+nSPyu/NHxeY8Y1GnSwK5F/M/i+XRfn5nznfalNqN1JPcT"
    "SzzzMXeSRyzuT1JJ5Jr+equIqVJOpUblJ6tt3bfdvqfFVKkpycpu7fVkHLH1rC5BLEpB6V0U09y0fc3/AASL/Ybs/ip4mT4meNbJJvCegzkaVY"
    "zoCmt3qEHLAjDQRHBYHh2wvIDgfqPA/C88zn9YlH93F/efZ8L5I8RJYusvci9PN/5L8/mfpfqurvNdSymQvJKxZmJ5JPev6Oo4dRgoJWSP0/n5"
    "ndmXc6j5jYY811xpW1RSR4t+2j+15p37I3w1bUGa3vPEmqq8ejaezZ3uBgzyDqIkJB/2jhQRkkfG8bcW0cnwllrVl8K/V+S/4B5WeZtTy/D+0l"
    "8T+Fd33fkvx2Px88c+NNS+IHim/wBa1i8mv9U1Od7m5uJTl5XY5JP+A4HQcV/KeYY6piKsq1aV5S1b7n4xicRUrVJVarvJ6tmI7ZPNeNORzPzG"
    "M2DXO5CY3OTWbYWEBqGwDOaTEBUgZzUtgHekMBSuIDwKLgI3vikA3Oe/SluAHoKGAmOKQBQApG00AJQAp+tACAZNAB2oAMZNABjigAoAAM0AFA"
    "AKACgAoAKACgA6UAA60AB6+lAB0oAKAAnJoAKADPFAC4xQAlAC9KAAdOmaAAHB6U0AoGDQA7JwPSqQwpgA6VSYBnNaRGPU8CtIh1HpxW8CiRK6"
    "oblItW4zXpUNy4n1/wD8EVMj9vXw6R1/srVh/wCU+ev0LhFf7dB/1uj6Thr/AH+L8pf+ks/UnUVBr+kKTPsImRe2vXFd0JGqMy5ixXXCRaRnXc"
    "Qz7V1RkapFWS2DVoplJELWu3tmqUxpAloPzquc0SOV/aNQQ/s0/EYYHzeGr/8A9EPXyHHLvkeJ/wAEvyY8SrYSv/gl+TPxku0MtyEQM7s2AoGS"
    "TX8R1YSqVVSpJuTdkkrtvsktz8QludDoXw58t1l1eZraM9LePDTN9ey/jz7V+xcMeDNWUo4niao6FN6+zjZ1Zev2aaf968v7om4Q1qP5Lc7Oz1"
    "aDS9PNpp0EdhbH7yx/fl93fq38q/pPJ45ZleEWCyOiqFPrbWcvOc370n6uy2WhjVzCo4unD3Y9l+r6jIr3jBNexRxztZs4Lk8d7g8HmvRpY23U"
    "tTLltfgY5r06ONt1NoVLF63v8nrmvZoYw6YVjRtNS969WljfM7adY0rTUgO9ejSxJ6FLEGpZanjHNd9Otc9CliDb03VcEfN+tdcXc9SjWubtjq"
    "Z9eKJU7o9Ok7m9ZarBaaZPe3V1b2VpbLulnuJBHHGPdjXz+cZng8toSxGNqKEI6ttpL72ejCHLF1anuxW7eiPJfiT+3fbaGkuneDrVb68OUOpX"
    "UZ8pD6xxnlj7tj6Gv5O44+kNCVWWC4cjfp7SS0v/AHY7v1dvRngZhxbRo3p5fHml/M9vkuvz+44bw1+zX46+OutnXPFFzfW8Vwdxlu8tcyA84R"
    "D9xeeM4AB4BFfD5D4OcScVYv8AtTP60qVOWvNPWbW/uxfwrteyXRNHlYXIcfmM/rGMk1fq936Lovu8jrfjP/wT81PS/ATeIfCNteX0OlQ79Rsn"
    "zJOEUZM6f3gACXUdMZAxnb0+KHg9RyilDFZI5TpxVpxbvK6+0vXqunTsdudcIqlQWJwV3Ze9Hd/4l+q6bry+ZHg2tg1/O06NnofCNCLHlqzjTb"
    "YJHs37Fv7Kt7+1L8VYdOYy2nh3TNtzrV+oH+jw54Rc8GWQgqo+rHhWr7XhPhmvnGMjhaS93eT7L/gnuZHlEsfiFB6QWsn5dvV9D9c/D0em+EtA"
    "07SdFtItN0jSbZLOxs4uEt4lHAHqepLHlmJJJJJr+u8syWjgMNHCUI2jE/X6cI04KlSVox0S8i+PEGSAzGu76sbROV/aC+OWifs8fCW+8X69Lm"
    "2tv3Vrao+2XUbgglIE69cEk4O1Qx7V8zxNxFh8mwksRV36Lu+iRz47H0sHQlia2y2830S/rRan48fHz4869+0R8SL/AMTeILky3d2dsUKk+VaR"
    "D7kMak/Kij8ySTkkk/yHnWe4jMsTLFYl3lL7kuiXl/w5+L5nmVbG13iKz1ey6JdEjhXfcevFfOVJ3POuMLYFc0mTcaeazbHYQ8VmDDGaVxA3XN"
    "IAHJpMAIpAB6/54oAQDOc9KQ7B2+lLcQnrSATrQAfSkAHgCgBBQAEYoAOtACk5FAADtoAO9ACUAGOKADFAB0NAB2oAUjHcUAJQAZoAKACgA7UA"
    "HWgAoAKAFOO2aSuAmaYBQAUAL1NACHrQAoyKADOSKAFHWmADpk0wHAjFUhikEH27VSAByatFCqa0iIkQ4NbRZRInLV0wY0WrY5Ir1MO9TWJ9gf"
    "8ABE8D/hvzw3n/AKBerf8ApBPX6BwnL/bor+t0fRcNq+Ph6S/Jn6pahbdz2r+jqUj7NGVcoCDmu2DZojLvI92cV1QZskZtxb7hjFdMZGiiRG35"
    "6CtFIfKRvBk9KfMWojo7RSOlHMbRgcd+0xaKf2bfiFk4/wCKcvhn0zA9fPcUU1XyuvRk7KUZK/qh4yNsDiH/ANO5fkz8h7WOHSWY2yBJCSDIeZ"
    "CPr2H0r8rybB4LKdcuhaezm9Zv59F5JLzufg0pslFyzHk5PevqqOOlUXvu5ySRIk2OtetQxfKYtEy3WW716tLGEMnjuOK9SjixXLEN1hutenSx"
    "Y4su217g9a9ajjehvGRegvenNevRxZvGoX7S/wA969nDYq52U6hq2l6QV56V7VDEHpUqh0GjLJe5KAsEGS2cKo9Se1e1CtFU3Uk7Jbt7L5nuYK"
    "nUqO0FcwvFf7Teh+AkltrGNNc1RcrlWxawn3b+P6Lx7iv518QvpH5PlMp4HJrYmsrq6dqcX/i+1btHR7cyO+tnWDwULR/e1Oy+Fer6/L7zw7x/"
    "8V9c+JepCbVr13hViY4IxthgB/up0/E8nua/i7ivjvO+IcX9ZzWu5pbRWkY/4Y7fN3fdnx+YZticbJPESulslsvRH17+y1+zR4Sl8J6f4j0Odd"
    "cnuODfyqN8DgfNH5fPlMPTlsHOSCCf678J+EuEKGEhm+Xv6xWtrOaV4vqlHVQf3v8AvNH6Zw/kmBp0Y4rDv2jf2n0fa3R/e/M+pfBfwqJhTzIt"
    "wHtX6ljc1V3Zn1MMOmz2TwH4JGiRLIibGXkEcYr4fMsYq14y1R2wpKJ8V/8ABRf/AIJhNeJqPxB+HFgNyhrnWNCgT8XuLZR27tEPUlePlH87cd"
    "8A8snj8tjo9ZRX5r9V/T/O+LOFFrjcCvOUV+a/VfcfCPw/+G+qfE3xtp+gaPbG41LU5hFEpOFXPVmboqgAkseAATX5dluSYnG4mGFw0eacnZf5"
    "+i3Z+eYTCVMTWjQoq8pf1r5Lqfqb+zh8OtH/AGdPhhbeFtI2SbT51/ebdr6hckYaQ/7I+6o7KB3JJ/s/g/gijkeAhQjrN6yfd/1ofsOAwFLA0I"
    "4elrbd931f+Xkd4muEchv1r6v6sehEoeL/AIl6X4G8Kahres30en6ZpkRmuJ2/hUdlH8TE4AUckkAda8rOMfhsrws8bipWhFXf9eeyXVhUrU6U"
    "HVrO0Yq7f9fh3Z+Xn7XH7WesftTfEFr66Mtromn7odJ08tlbWInqexkfALN3IA6ACv4q4u4xrZ5jJYia5YLSMey/zfX7j8gz3O6mYVuZ6QXwrs"
    "v831PIHbLHFfCVJNs8JsYWGK55SJGk5HSsmwsHSoGJ1OO9TcB2eKTEJnI6UgEYikAA46j6UAIeOn40nsAtIYmcUCG/zpAApMAxx9aADoaAEoAU"
    "5pAJ0NMANAB0oAKACgAoAOtABQAUABGKACgAoAXtQAEcdaAEPJoAMUAFABQAUAFABQAUAFABQAUALgHvQAvAOB3piQYySOhpjHAVQxc4poAAzn"
    "mtEO4L1q0MkHUetaxYyWNua6YMaLEBxXp0JFo+w/8AgiNGbj/goH4aA5xperH/AMkJ6+94Sl/t8f66o+k4a/3+PpL8mfq1qP3iK/pGkj7SK0MO"
    "/wDvHpXdTNUjOuRurqibRRVnjBGK1RrFFaVMcVomapEe0HtTuXGI8AbcUam0InF/tMQlv2b/AIgAn/mXb7/0Q9ePn7vl9Vf3X+ReOX/CfiP+vc"
    "vyZ+Ps48qUjnGa/GKdX2c3GWx+AVIdhBIccGvbo11a8WckkSJKea9SjibI52iRJOhJr0KNchomjl5r1aOKJaJkm2ivQp4uzCxZgnOfrXq0MVca"
    "Ldvdmvaw2LNUaWnmW4mCRozu3QDnNfQYWrUlJQpptvojroKUpcsFdlvWvHWjeBbdhqE/2u/AOLK2YFlOP426KPzPtXz/ABf4ucO8LU5UcZP22J"
    "X/AC6ptNp2055bR8931SZ9BSp0MPHnxMrv+Vfq+h5j40+Mus+Mka3MwstO7WluSqEf7R6v+PHsK/jHjvxi4g4nk6Vep7LD9KULqP8A2895v107"
    "JHPis2r1Y+yi+WHZfr3+ZyZYsT6mvy7m7HmD0OOtawl3BHpH7N37SOv/ALN/jqLVdJdLizlKrf6bOSba/jBztYDow52uPmUnjgkH67hbijH5Li"
    "1icDNrvHpJea/J9PvPZyfOsRl1b2tF3XVPZr+tmfr3+yt8dfBn7Tnw+h13wtcYe32x6jpsxH2rS5TnCuB95Tg7XHDAdiGUf1Lw/wAa0M6o89N2"
    "n1XVH7Tk2a4fMKXtaL16rqv67nq9xMltDhccDtXuJOTuetJmfPrjWuHU4IroWGUtGctTXQ+aPH/7OOg+CviLq3jfwpo8NnqurDF/HCMKoyS7wr"
    "/CWOC4HXaCB1z6fCPC2UYDMKuO5LVKiSv0Xey6c3XuePDJ8LQqTxNKNpS3/wCB69TNsPExBBLYbrX6ZUwnYy5tS+nju3sYXmu54re2gUySyyuF"
    "SNQMlmJ4AA5ya87Gwp4ajKvWajGKbbeiSW7bNI1FZtuyR8J/tu/tcSftAeIxpOkSPD4T0mUm3Xob6UcGdx+YUHoDnqSK/iHxO48ln2J9hhpf7P"
    "B+7/ef8z/Rdtd3p+b8SZ99dn7Cg/3cf/Jn3f6L9WfPshJNfjs73PlGNI5NZSEM24ya52hCHpUDEBK+hrMLMOQKQB0zUsQpyRSATB/GgBOpoAU9"
    "D2pWGN6HnvUiEzxRcA60gAjjNAB1oATHFAC5yKAEoADQAucmgBDQAUAFAAevpQAdKAF6H1oATpQAUAFAB0oAKACgAHFABQAUAGaACgAJzQAdqA"
    "CgAzzQAp5oABQAGgAAoAUHNMBwJIHpTAMdapMYoFWmNB6YrRDY8fqK0iwHofzrogxk8D130ZFpn1d/wRo8aW/g3/goV4Ca6ljhh1VrvSQznA8y"
    "5tJoYlHu0jov/Aq+34XxCp4yEn/XU9/h6qqePpt7O6+9M/XPxFA1jeSRt95GIYehr+ncJLngpLqffKLuYVyfNBNelBWNFEozrgHFdCNoqxTnO0"
    "da2ijaMSpJNxWiRqokXmdeRVWNlEVZsHr1puJ0RjY4v9peYt+zp4+APXw/ej/yA9eTnkL4GqvJ/kGO/wBwxH/Xuf5M/Im6gIdj71+H1aMlJ3Pw"
    "SpEpyIYmJB/+vWNOc6L5oP5HHOncdFMGwPumvawmNjVdnozlnCxMrAcelexCrbQxaHq+0V3Qr2JaHo5x7V3U6zepNixbSl3AHU8Y9a9fCV+aSh"
    "1YGhqUlt4ZtEn1O4FsHGUgUbp5B7L2Hua9HP8AN8s4boLE59X9k2rxpr3q0vSP2U/5pWR6EMG+VTq6L8TmfEfxlvNRs2tNMjGlWjDaxjbM8wx/"
    "E/X8Bgc96/njjPx8zjNKcsDk6+qYd6NRd6k1/fqaPXtHlWtnc7/rXJD2dFcq/F/M4wtk5J5PNfg0pO7bOQUc++aLgKASM1pEBwWt4xAlUEAda6"
    "oX6FJHb/Ab47eJf2dPiJZ+JvC+oyWGoWp2uv3obuIkFoZU6PG2BkH0BGCAR7eUZjXwGIWJw8uWS/HyfdHfgMfXwdZVqErNfj5M/WD9lj9ufw5+"
    "1p4WM1iU0rxBZRKdS0iSTLwnoZIieXiJ79VyA3JBb+pOCuMMHnVLlT5a0d4/qu6P2bJM+o5lS93Sa3X6ruv6Z6Dr3iJILZiHA4zknhRX6PSope"
    "9LZHpSlbc+TvA//BS/w340+M9v4ag0rUVtNQuxZ2mptKu2WRjtQmLGVVjgAlieRkDnH5/lnilgMZmkcvhSahKXLGd93stOz6O/yPj6XGNCviVh"
    "owfK3ZSvv20O0+KHhmW9uZNS0hArDLXFuvGfV1Hr6j8fr/Q+VYyMIKliXp0f6M9iaTlY+JP2rP2pJfG8UnhfRpimlQvi9uUb/j+cH7oI/wCWYP"
    "8A30eegGf5E8Z/FKGbVqmSZXL/AGeDtOS+3JdF/cT/APAnrtv8DxBn3tb4PDP3OrX2v+B+Z4x4a8C6341naPR9I1LVXQgMLW2ebZnpnaDj8a/B"
    "svyjHY+p7LAUZ1X/AHYuX32Tt8z5ehha9d2owcvRXO2uv2O/iNDpH2w+FNRaLbuwhR5P++AxbPtivr6vhZxOqftHgp/hf7r3/A7ZZJjo6um/w/"
    "I8617w5qHhi+Nrqdhd6fcgZMVzC0TgfRgDX55j8DicHVdDGU5U5dpJxf3NJnmVaFSm+WpFp+ehRKmvPnAyGOMH1rCSATJNZMYnWpYBz9KlgLgg"
    "UriEJNDATryM0AA696kAA560AIec0hCA46daQwH3TQAZoATtQAUAFABQAUAKeRnvSASmAUAGOKAAc0AKeOKAENACjHfNABnvSAQUwCgAoAKADp"
    "QAUAFABQAAZoAKACgAoABQAvGKADr9aADqaBB3NMaHDg9aYCg00MKpMBRx+NWiriqea1Qh6HBrWDKJY32muynMaNnwp4ovfCOvWWqabcSWt/p1"
    "xHdW08Zw8MqMGVwfUMAR9K9nB4qUGnDdG8KkotSjuj9xf2V/2k9P/bH+Cdl40s5LaPVAFt9esIyAdPvAPmwuSRE+CyE9QcZyrAf01whxBTx+Di"
    "tpR0aP1DAY+GMoKrH4uq7M6q9ASQgcCvu6eqPRiu5n3cwVeMV0wRtFGZcz4roijZRKcsmScmt1E6Iogkn21aiaxiJ9r471XKaxicV+0XctJ8Af"
    "HCj+LQbwf+QWrgzWlzYOa8mRmCtgcR/17n+R+VN7Z4LZ9TX5FicG7n4XOJnzWnynivKqYR3OWcStLaHI71yTw/YwlARS0XUEgV0YfGTpO1XVfi"
    "c8qPVEiMrjqPoa9mjiadT4JIxdNongaJG/eyoij8T+Qr2sJiMLSd8VVjFfe/klr+nmT7Nsz/EHjmTSm8vTYGtnx/r5QDIf90dFHuMmvz/jPxax"
    "OW1XhOH6EqDt/GqJOo/8C1jBdmm5eaZ00qajqjkpr2W9laSZ3lkc5ZnYksfcmv56xWOxGMqyxGKm5zk7uUm22+7b1Zq3fVkRGCTXHsxAoyOaaA"
    "djb3FXFAPAyvSumEBnpf7N37KHjf8Aaq8UNpfg7RZr4wbTdXcjCG0sVJ+9LK2FXvgZ3HBwCeK+hyTJMVmc/ZYSDk+r6L1f9M9TLcoxOOly4eOi"
    "3b0S9X+m59k+Dv8AghzYaXpyjxd8RQdRx88Gi6cZIU+k0rKW/wC/Yr9ayjwdxM4qeKq2v0S/z/yPtKHAtJRviKrb/urT73/kZ3jj/gjJpEWnzH"
    "w/48vlvEUmNNS05fKkPoXjfKj32t9K9PF+B1RrmwuJ17SX+QYjgehb9xVd/wC8lb8P8j581X9njx/+xz8QLbXJ7hLG406UNp+oafOJobhu/bIX"
    "GQUkUbs4wRmuLIfDDN8sxUswxj5PZW5XF/E359l1TtfRWaufO1cqxuVVPrEnaz91rVP+uz37NXPtD9mf9s/SP2gdNSwvxBpniy3T/SLPOIr0Ac"
    "yQ568DLJ1Xk8gEj964b4gp45fVcQ0qnbv6f5H3eR8QUswXsqvu1F07+a/Vfoc7on7GfgDwV8Xl8VWMGoeZBN9otbFpV+y2suchlGN3B5ALYBx6"
    "Cnl/hXlOHzFZlBO6d1G/up91pfTom7I5afDmFoYn6xG+jul0R6Rc6n9mkLo2BX6lGldWZ6k27nl3jX9l74eePvEcmtXvhy2OoSsHlMUskMUzf3"
    "mjRgpJ7nHPfNfBZh4TcNY7F/XMThk5N3dnJJvzimk79brXqeNVynCVa3tqlNN/NfgtDpLC2tvDWlLp1lBb2NjH9y3toliiT6KoAFfoeW5Pg8FS"
    "jRwdJQjHRJJJfcjvSUVyxVl2W33Fa51M2RwGIB6c17MKKkROFzm/GvhjSfiDYPZa3Y2+o2cowUmQFk90bqje6kGvHz/hHK87wrwmZUVUi++6fd"
    "PdPzRzV6UKsPZ1ldef6dj49/aZ/Zpm+CupR31jJJe+Hb9ytvMwy9u/XypMcZxnB/iAPoRX8I+KnhXiOF8Qq1G88NN2jLrF/wAsvO2z6rzPhM3y"
    "l4VqdN3g/wAH2f6dzyRhnn0r8VmjxBgHNYMYvSpATNSAEcVImIT+lDAQnnFTcADY96EAOc0AJnikwAH1/wD1UgA8DFAg6GgYE9aAENABQAUAFA"
    "CkYNIBKYBQAUAA4oAOlAB1oADQAUAFABj0oAO1ABQADrQAvWkAlMAoAKADPFACls0WASgAoAKACgBSdx4oAMAe9MQo560DFDflVJgLzTGGKtDT"
    "FBwa0TBjgcitEwTHoea2hIosQS4x616FCrYtSPVP2W/2qvFf7KnxEj1/wxdqhdfJvbKbLWuowk5MUqAjI7gjBU4IINfV5JneIwVeNag7Nfcz0c"
    "Dj6uFqe0pP5dz9X/2cP22Ph5+1tokS6BfLo3iry91x4d1GULPu7/Z34Wdf93DAAkqK/oPh3jTDY1qnVfLPsfouWZrQxmkXaXZ7/LudreF1dgys"
    "hXqGGCK/R6dmtD3VC2hQmcnjFdUYG8IFK4k2E8jr610Rjc6IxKU11g8c1qoGiiRi7H61XszVROU+PN4rfA3xmp6tod2B/wB+mrlx9K9BmePX+w"
    "4j/r3L8j8xL613uxxnmvz3E4S7Z+HTgU5LHI6cV5lTAnPKBXmsOOlcNTA2MXArvYFz04I71wVMBczdMiOmkgcdKwWX2exm6YHTN3alPLVLoNUy"
    "RdCivofLniWWM9iOR7g9jXZTyLDYql9Xx1NVIdnuvOL3T9PncfsrmJr3wuuolefTN15Cg3NEB++QfT+Ie4/IV+acVeEGMoqWLyBuvSWrh/y9h/"
    "279tL+aPzig+qzfwa/mcsY2X7wII4OR0r8cdJrSSs0c+omew6VKiIcF5raMRo9P/ZI/Z0vv2ofjVpnhW0lFnby5uNQvWXK2VqmPMkx3OCAo4yz"
    "KMjOa+p4XyOrm2OjgaK31b7Jb/8AA8z1smyueYYpUI6Ldvslv/wD9i/hv8PPD3wM8HWXhzwlZppmiaeoEca8yTtgbpZW/jkbGSx+gwAAP7GyDh"
    "3C5ZhI4TDQSS/q/qftmHp0aFCOHoR5Yx/q77tkHjz4m6d4a09rnU9QstPgB2+bdTrCmfTcxAr3Zzw+Fj7TETUI920l97FOUIK85JLzdvzOGl+M"
    "ej+ILGWbTNY0zUYoh87Wl2k4T6lScV7OUYjB45/7JUjUt/K0/wAmYRq0qibpyTt2af5Hz78TfEP/AAsK/vjLGs9pOphETjKsn09+tfqlHJ6Dwj"
    "wteKaktU/M8HGS9rJxnrF6W8j5b+JXw6vfhJ4vtr7T57i3jd/tFjcRuVkgdSDjcP4lOMHvx71/L/GfBFTJsw5qUm6ctYS6q3Rvuu/VfM+AzHBS"
    "wWIUqTdnrF9Vb9UfQ37Pf7aVj8RPI8P+I5orTxMihIpQAkOo+nssv+z0J6ddtfQcH+JWDr4uOS5jUSr9G9FLyvspeXXp2Pssm4hjioqhiXap+E"
    "v8n5denY9am1fz29q/a4UdLo9qbsQzX3kjIPHetY0rmT1PEP2oP2uLb4UzLpGjwQ3+uvGJJWlbMNmDyAwByzEc4yMAg55xX4b4peL64bqPLMui"
    "p4mybv8ADC+10tXJrXlurKzb6Pwc4zr6lL2VNJz/AAXr5+R8+RftoePTqazy6haXEYOfIe0QRkenADf+PZr+ecN488Y0sT7adeLX8rhHl/Cz/E"
    "+Y/wBZcdzczkvSy/4c+hvgx8frL42aBJIsS2OrWQX7Va7sg56Oh7qSDx1HQ9if7B8K/EvDcWYSV48lenbnje612lF9n96ej6N/RYDM44unfaS3"
    "X6ryNL4h6JF8QPBmoaLebfJvYyquV3eTJ1Rx9Gwa+x404Vw+fZNXyytb34tJ9nvF+qdmdFSlGtTlRn1X3dn8j4S1C1ayvJYJV2yQu0br/dIOCP"
    "zr/KbGUJUqsqU1aUW015p2f4n5xKLi3GXQrEjNedIlB1aoYCDrSADSEGQKljGsMtSEHQUADHIz3pMBMUgFxj3oATFACEYoAKACgA6UAFABQAE5"
    "oAKAADNAC9qAExQAcigAxQAUAFABQAA4oAOlABQAUAGOKACgAoADQAUAHagAoAKACgAoAU8nigBBQA5TzTTAX6YpgKDg0xgDk/SqTC4VaY+oA4"
    "q0xEitmtYspD1fJrojMaZYgn2iu+jWsWpGjpGsT6bdxXEE0sE8LB45I2KsjDoQRyCPWvXwuKakpJ6o3hOzuj7X/Zb/AOCtWs+HrW10D4mQz+Jt"
    "HiURRatCFGq2igYG4nCzqP8AbIf/AGj0r9a4Y4+xGFahiHzx/H/g/n6n2eU8VTpWp4tc0e/Vf5/n6n3D4X8aeHviZ4Qj17wprdh4h0iXgz2r/N"
    "C39yVD80bD0YCv3XJs9wuYQToy17H6Bg8RRxMPaUJcy/r7ijfXZDkDnNfTwgd6iUZLjjJNdEYGnIV3vQOMitOQ0SOT+N9ybj4OeKkBzu0m5H/k"
    "NqivR5qbj3Mccv8AY6/+CX5H563GmYJyO/NfN1cvs9j8YqUinPYgdBXmVcD5HLKkVpLDHUEg1wTwFzJ0iJtPG77tc0sut0IlTIpbDB6c5rGWA8"
    "jN0xY9P59adLL7vVDVMsRaeAcYr0aWX22RapFy0tDFIGQlWByCOCK9vBYJwkqkXaS6rc1hBp3Qmu+A9M8dA/bI/sl8wwt3Cgyx/wCmi9G+vB96"
    "87ijw4yXihc2Mj7HE9KsEtf+vkdFL10l5nRLD0sRpV0f8y/VdfzPMPHXw11PwHMDcwiS1fiO5iy0Un49j7HBr+S+OvDbOeFqtsdT5qT+GpHWEv"
    "n0fk7M8fF5dWw+s1dd1sYMK5I75r4ihG5xI/Qf/gkd4Cg0H4S+KfFO4C71fUE05cjlIYUWQ4P+08vP/XMV/UHgdkVONGtmEleUnyr0ST/G/wCB"
    "+n8E0I0sJUxL3nLl+SSf43/A+mPiP8brP4VeANY1+/DS2+j2klyUB5lKjhB6FmwPxr9rz7FUssy+rmFb4acW38unz2Pqq+JjQoyrz2im3/Xmfk"
    "t8cvjv4k+P3jW41nxBfyXDyuTDbqxFvaJ2SNM4AA79T1JJ5r+FuJuIMbnOLeJxs277Rv7sV2S/Xd9T8YzPNMRjarq1n6LovJI5vw34o1Dwhqcd"
    "7pt5cWVzGcq8TlSfY+o9QeDXLk2a4rLMTHGYGo6dSLunF2+/uvJ6M46OJq0Z89KTT8j7L+DHxIj+Jnw5stVKLHd5MF0q8KJVxkgehBVvbdjtX+"
    "lPhZxq+KMip5hU0qJuM0tuaNrteTTTXa9j9IwON+t4eNe2uz9V/mY/7SoEvwo1C5WMPNpoW4hz/CdwQn8mJ/CuHxolLDcK4jMKMb1KKUo+V2ot"
    "/JNv5HNm9FTwU521jqvvt+TPj5Z3Mvmb28zdu3Z5z1zX+abrVKkvaTk+a979b979z87Td7n2R+x1+0m/xD0abw7rswfWdOhElvcsfmvIhgEN6u"
    "uRyPvA5PIJP9reCPiVWzaH9iZlPmrQV4y6zitNf70dNeq13TZ9/kObzxUfYVn7yW/df5r8T1+81MyqVU5Br+lVSaTZ7XtbM+A/j7JdP8ZvEz3e"
    "7zjqMygH+4HIT/x0LX+Y/iTLEPijMJ4n4vaz+5O0f/JUj81zacpYyo5b3f56fgcgH5r4aM7nnHqP7It3fW/xbH2bf5Rs5RcY6bPlxn/geyv6A+"
    "jbUxb4yjTpfA6U+f8Aw6W/8m5T3eHub61ptZ3/AK9bH1lpzJdOiv3r/QTFXjBn2UFd6Hw78Yo0h+LHicR48sardbcdMea1f5O8eU4U+JMwVP4f"
    "bVbf+ByPz3MLfWqlv5n+ZzPJNfHSONCdDWYCjj60mFgJ9qkBD0+tJgITgetK4hPzxSYCA4BHrQIOTxSGBoASgAxQAUAAoAMUAFABQAUAFABnFA"
    "Cnp0oASgAJzQAUAFABQAGgAPFABQAUAFABQAUAKTke9IBKYBjigAoAKADrQAGgBcUAAOBQAmaAFBP1poBckZ4pgKMU9gFzmgABwfeqTHcKtMaY"
    "ufmzVpgx4PpWykCY9Xwea2jUKuTRS4zXZTq2KTLdtelRxXq0MXY1jM7P4Q/HXxP8EfFKax4X1m80m9A2OYmyk6ZzskQ5WRMj7rAivocsz3E4Sq"
    "q2Hnyy/rfud+CzCvhantcPLlf5+vc+5/2df+CkPhr4tmDTPGS23hPxBLhFvFJGmXbe+STATx94lPccCv3/AIT8UKNdRw+Y+7La/R/5fP7z9OyX"
    "i/DYm1LGWhPv9l/5fPTzPoLVGa0jUnBSRQyOpysgIyGBHBBHpX7PhatOtFTpu6Z9t7PqjHN0ZG64rv8AZ2BwMD4rSib4X+JEzy+mXA+v7tqapX"
    "aT7o58dH/ZKy/uy/I+H73T9pOB3qMVgUmfks6RQl0445HJ5ryKmB8jllS1IX0/nNcssCuxi6RA+n4OMc1zTwRm6RC+n5PTOf0rB4Ah0hU07jpV"
    "xwIKkWIrDoMV20sFctUyaOwww4r0qGEt0LjSLttZe1erSwmzR0Qpm3pdilxG8FxElxbTrslhkG5JFPYivdpZZQxVCeFxkFOnNWlGSumn3R6uEj"
    "b3Wrp6NdGjwH4xeAYvhx49u7KAs1m4We2LdRG4yF99pyuf9mv88/E3g2nwxxFXy6jf2WkoX/klql8tY+dj5DPMvjg8W6UPhaTXo+nyenyPs3/g"
    "m947jg/ZyuLBHAkttZn3jPI3RQkH+f5V/QngDKniskqKO8KjT+cYs+64SmnlbS3jN/ionbftP6bd/Eb4GeKNIsg0t1dWRaJAeZGjZZAoHcnZgD"
    "3r9F8Schr5hwxjMJho3m43SXXlalb52selmkZVsBVow3a/LX9D80ZIzCSDwR61/nnUp8uh+NsjJJrn1Yj6y/ZL8J3GgfCKKe5DRnUrmS6jQ/8A"
    "PMhUU/jsJ+hFf6EfRuybEYHhX29dW9vOU4/4bRin8+VteVj9CyGi6WCjzbybfy0S/I2/2iZIrD4K69LI2N8CwpnuzSKB/OvqvHDFwocF47nfxR"
    "UfnKUUjrzJqOArN9vzaR8aNIN3Ff5pTqxvZH5nc6P4P+OD8PfiVpOrtnybebbOB3icFH/8dY19RwJxJLJOIMLml/dpyXN/hekvwbfyO/LMW8Ni"
    "YVuievo9H+B9zWWpiZUeN0ljZQ6OrblZTyCD3BFf6p4erTxFGNak7xkk011T6/M/Q6u947HjX7TH7PX/AAs7W/7a0QwxavKoW5gkbal0QAAyk8"
    "K2ABg8HA6HOf5p8ZfAzEZ5Wed5Hb27XvwenPbRST2UraO+jSWqe/zma5U8RL21LSXXz/4J4qv7MPjeW+8g6FcRc4MkrokY99xOK/mOl4L8ZVqy"
    "oU8DNPu7KP8A4E3Y8CGU4pu3J/Xqe+/AD4RWfwn0G4aeVLvWL4ATyopCRKOkaZ5IzyTxk444r+1/BvwjfCOElXxslPFVUuZraMVryx+erfV27H"
    "1GXYJYSm1vKW7/AEOk8Z+MYvAvh681Sf8A1NjEZCM4Mh6KgPqTgfjX6PxtxHhsgyTEZtidqcW7d39lLzbsl6noqtGlTlVntFX/AK9T4m1PUJdV"
    "vZrmZi8txI0jn1JOSfzr/JTHYypiq0sRVd5Sbb9W7v8AE/OJzc5OT3ZWHBrz2yQxUtiAYAqAFxigYjEfSpaEIRznmgBrHmkwEHFIBQP0oAQjmg"
    "BSc0AJmgAxQAUAFABQAUAFABQAUAKRnvmkAlMAoAKACgAoAKACgANAARg0AB5NABjNABQAuBigBKACgBQOtACUAHSgAoAKADFAAKADoaAF6jNM"
    "BR0oAUdO1VcAJ5pgHSgBc1omUhelWmDHZrRSBD1bFbRmUSLJiuqFQaZPHcY712U69i1ItW94QetenQxjRrGR7T8Av21/FnwQhjsFn/trw6G+bS"
    "7xyY4x6xN1iP8Au/KT1U1+kcMeIWPylpUnzQ/le3yfT8vI+oyXinGZe1CL54fyv9H0/LyPtn4I/tDeE/j/AKOH0K/MGqopabSbshLqPHUr2kX3"
    "Un3A6V/S3DHHmX5xFKD5Z9Yvf/g/I/WsqzzB5lG9B2l1i916d16fOxu65ANb0q+sWP8Ax9wSW/PbepX+tfoUI2sz0sRRbhKD6pr7z5E1PQzBO0"
    "bKVdSQwPUHvXt4nBJ6o/Mq2GadjNuNK28Yry6mBOCdBlWXTsfw1xzwZzypFd9Oyx4wa5JYPyIdIjOn4PT8qz+pmcqQv9n4xxjFUsGJUiWLTuel"
    "dVLCK5oqRZi07celd9PB3NI0i7aaXn+GvTw+DfU6adBtmrYaW/y4Ukk46V6qw0oq8T0qGHd0oo+ef2kPFUPiX4n3SW7rLDp0aWauvRiuS35MzD"
    "8K/wA6/HLimlnHFld0GnCko0011cb833SbXyPiuKsTGrj3GDuoJR+7f8W0dn+w38YIvAHj650W8kSGy8SCONJXJCxXCE+WT6BtzL9WXsK7/AXj"
    "Knk2fPB4qVqOItHyU18P33tf0OrhPM44evLD1H7tSy+a2/y+4+zLjU5LE4bIcdc8V/fkKUakdD7ttp6HhHxn/ZW8OfErXp9TtZJtDv7gl5jAge"
    "Gdz1YoSMH12kA+mea/EeNvo95VnmIlj8HUeHqy1lZJxk+7jpZ97NX3aufOZjkFDEzdWPuye9tn8jD8D/sXeHvDl4l5qt9c600ZytuYxBCT23AE"
    "sw9sj3zXi8L/AEZMsweJjiM2ruuo68tuWL9dW36XS73OHD8PUaUuao+b8F8/6R6gC1uNoCqqgKiooCqB0AA6Aelf1LQw1KjTVOklGMVZJaJJdE"
    "e+ux8//tifEsXS23hm3lDGCQXN7tPCtjCRn8CWP1Wv4s+k9x1RxNalw3g5X9m+epb+a3uxfmr8z+XU+b4nxfIo4OL13l+if5/ceBuea/kKTPjQ"
    "Vse1KMwTPfv2bf2modF0mLw54hmCQRDZY3rniMdonPZR2bt0PABH9Y+B/jfRyynDIeIJ2pLSnUe0V/LLy/ley2elrfV5PnC5VhsQ7JbP9H+j6e"
    "m30AI8WkdwWRkkG5GVgwYHoQR1r+4MNi6OIgp0JKUXqmtU/R9T6Fxuxkc7XCsOSp45re0UQ4JamP4h1KHw5A91dzQ2drHzJNKwVF/H19uprkzf"
    "PcvyrCTxuY1Y06cVduTsv+D6LUcaWjnN2it29EfNn7Q/x5PxNuU07TzJHo1o+4ZG03TjjeR2A5wD6knrgf52+NnjDLiyusDl944Om7q+jqS25m"
    "uiX2V53eui+UznNI137Gh8C/F9/TseWk4Ffz85HgiYJOenpWbYg6Dk9akAYkDtSYAW9O9LYBpPNK4Bnjmi4CUgDFAC54oAO9ABQAlABQAUAFAB"
    "QAUAHegAoAMUALigBKADtQAvXrQAlABQAUAHWgBc8UAJQACgAxxQAUAHSgBQaAEJzQAUAL2oATNABigAoAOlABmgAoAXOBQAZ96YCg4/GgBQOM"
    "f5NUgFwcUwDGPYU0MOtWAoOKpMY9DkVqhXFDYPHSrjIoerkVvGY7kkc3511U6pSkTx3JB612U8Qy1I0tL8Q3OlXMU9rPLbzwMHjkjcq6MOhBHI"
    "I9a9jCZpVotTpyaa2admjop15wkpQdmuqPpn4Af8FCLzTZYdP8eRzava8KmqQgfbIfeQcCUe/De7dK/cuCvGPF4a2GzX95D+b7S9e/5+p+iZHx"
    "7VppUcy9+P8y+Jevf8/U9I+IOkaV4uc+JPDV9a6vo9+d7yW5z5Mh5Ideqk9cMAQeoHFf1lwzxFgs2wsauFqKS8vyfZ+TPrcXhKOJprG4OSnCW9"
    "uj8+q+ZxV3pXtjHX3r6Ophr7HgVsMUZdMx25riqYQ4p4crPpuB0IrllhDnlQIWsPasHhTJ0RU07J6ULC6gqJNHpp3DiuqlhTRUC7a6TyPlr0qO"
    "ESZ1U8Ma9hoGVeRykUMal5JJGCpGo5JLHgCt8TVoYSlKviZKMI6tvRJd22ethstlUu1st29EvNvoeP/GL9p2DS47nSPCriV3Bjm1M9h0IhH/s5"
    "/Dsa/kHxV+kTzxnlPC0tHdSrfg1TX/t7/wC3Vsz5jOOKKOHUsNl/vS2c+3+Ff+3fd3PAnfc5PXJ5r+OZTd7n5yx0UhRgRjIrWnV5XcD6K+C/7b"
    "klhpEOj+LfPuI4FEdvqSDfLGo6CVerAf3hz6g9a/qXwy+kK8uhHL+Ik501oqi1kl2mt3/iWvk9z7TKuJ0oqjjOn2t/v/zWvkz2bw98RNF8XxCS"
    "x1rS7pGGRtuVVx9UJDD8RX9b5Rx/w5mdJVcFjKc/LmSfzTs180fV0q1GsualNS9Gr/dv+BP4g8W6R4asvNvdX0y3X1kukU/lnJ/CunH8b5FgYO"
    "pjMXTppd5xX4Xu/kKoqcFepOMV5tHjPxS/a/s9LsJrLwuDdXrgqdQkQrHF2yisMsfcgAehr+bPEn6SuG9jPBcLXlJ6e1aso/4YvVvs2kl2Z87j"
    "uJKFCLhg/en/ADPZeie79dPU+cr6/l1C6knnkeWeVi7u5yzMTkknuSa/i7FYuriKsq1aTlKTbberberbfds+GqVJTk5zd2yuT6965HIgKSY0KD"
    "g8VSkI6Lwr8W/Efgm38jTNXvLa2zuMG/dET67Gyufwr7Hh7xA4hyNKOV4udOP8qd4/+Au6/A7cPmOJoLlpzaXbp92x0s37V3jSS2EY1C3j4+8l"
    "pEG/PbX3Nb6QPG04cn1v58kL/wDpJ6D4ixlrXX/gK/yOQ8W+PtZ8dXKy6vqV3fvHwvnSFgn+6Og/AV+a59xXm2dTVXNcROq1tzNtL0Wy+SR5mJ"
    "xleu71pN279PQxi3rXzzkcwnfv7VFxC96gAx+NACZqWAhpAIKADOKADHzUmAA4FACE5NABQAUAFABjNABQAUAFAAetAB1oAUjvQAmKAF6UAJ1o"
    "AKAAUAFAC9qAEoAXGKAEoAO1AAKACgA6UABNAAKADPNACk5NJAJTABQAueR0pADcE+9NAA4FACCgBSPSgAHNAAaABeOtADhx07VfoAdaaAXp70"
    "AA5FMYVVwAVaYxyvjr3q0wHZq+YE2KrbatTGiRXx34rojVHckSf14reNZlXJ4rsovX8K7qOJcTRTOn+HXxW1v4Z60L3RtQls5CNsiD5opl/uuh"
    "+Vh9R9MV9Tw9xVmGVYlYnA1XCX4P1Wz+fyPSyzN8Vga3tsLNxf4Pya2Z9CfDv9ojw58UpVtdXWDw1q74Cvk/YblvqeYyfQ5X37V/XvAvjtgse4"
    "4XOEqVT+b7D+b+F+unmfpWXcTYDMLU8SlRqvr9iX6xfrdeZ1+r+GpNPlKyJjuCOQw9QehFf0FSq0sRFTpu6Z6mKwE6btJGXcaUR2onh1Y8ypQK"
    "radzXO8PqczosWHTfm6VSwxUaBet9IyQSBXQqCSOqnhrkHj3xPo/wq0Jb/WrkRGQZgtY8NcXWP7q9h7nAr4fjfxGybhbDe3x1S838MFrKT7Jfq"
    "7JdzfF1cLl9JV8bKye0V8UvRfq9D5v+Mn7RWs/Flvsvy6Zo0R/d2UDHDe8jdXP6egr+CPEXxbzjiqo6dWXsqF9KcXp5OT+0/uS6Lqfm2d8TYnH"
    "/uorkpLaK/Nvq/w7I87du9fkspHzFyM9eKxbEL06d6SkAbyBVKdgHrOUBwa0VUaY15Wc5JqZVOZ3YXYgck9aTmwEPAqGxCEAtUMAB4x+tJsAXg"
    "UrgLuw3U1VwEzxRcAPWlcYY+Xk8UriF6gdKTATGBxQAFttJsBCR+dSAnQUAHegAxgUgDtQAA/hQAdaAA8UAJ0oAKACgAzxQApoASgAoAX8aAEN"
    "AB2oAKACgBRQADmgBKACgAxQApGDxSATNMA60AFABmgAoAKACgAFAAOtABQAY4oAUn9KAE6UAKODQAY4oAXHPNACLx9KADtTAMYA60AOU4FNMB"
    "TTABwKAFpjEzimmACqTAUEVSYwz71SkAobB5qlINRQ/wCFUpAmPD8etaRmO45ZK2jVHcljnxXVTrNbFKVieK8K45INdlPFNMtTPTfg9+0xrXwz"
    "MVnPt1jRFIzY3LEiMf8ATJuqH6ZHtmv1zgLxXzbh+apxm6lDrCT2X917x9NvI+qyXizF4Bexl+8pfyvp/he8fy8j6H8MfEPwz8W7QTaBdMt0q7"
    "pbCfC3EPrgfxqPUV/aHBPiblHEMOShUSqJawekvu6rzV16H6NgsXgsyhzYOXvdYv4l8uq80TSaZhidpGDX6NuE8NrYlg0gyruVcgfePp706tSn"
    "Ti5TdkjShhXOXLFHm/xa/aj0jwRZSaf4dEOrauOHuz81tbH/AGf+ejf+Oj36V/L3iX9IXDYHnwPDrVSqtHPeEX5fzP8A8lXnsfO5zxThsC3SwL"
    "VSp1f2Y+n8z/D1PmvxP4pv/FurzX2p3c17eTnLyStkn0HsB2A4Hav4pzfN8XmOJljMdUdSpLeUnd/8BdktF0PzHGY2tiqrrYiTlJ7tmYzAc+te"
    "NKRx3GN69KxbEIemDWYAcA0rgIRjvRcAAzTuAE4BxzSuAoBzQ2AhOPTmkmAuO/8AkUmAnbn/APXSuAEnHagA246/zoAU9cUAA4ODQAfXpQAcYp"
    "AIeh70MBtSAdeuaQAeRQAA8igAJz/KgAB4wKAEFAC9TxQAnagBe/egAzxQAlABmgAzQAdqACgAFACjmkAYpgJQAUAK3WgBKACgBcZXNIBKYBnm"
    "gAoAKACgAoAKACgAoAKACgAoAXHFABjmgBSuBQAoAPNABj2oATGMcUALjH+HpQAHpgc0AJjjoKYC96YB2x2oAXPSmgA9KYwNAhc0x3E/CmmAvU"
    "81Q7hnince4dD607hYXfzVpiHZ5qlILjletIzHccslbxqDJFuCDW8a7Ww+Ysafqs2m3sdxbzSwXELbkkjcq6EdwRyD9K7MNj6tKrGtSk4yi7pp"
    "tNPumtUaUq06clUptprZrRo9T0X9sjxXp+mrb3X9m6ps4WS6t/3mPdkK5+pya/bcp+kHxRgqPspzjVa6zjr96av89e7PssPx3mMI2qqNTzktfv"
    "TV/nqc/wCPv2kfFHxA057G5u4rPT3+9bWcflJJ/vH7zD2JI9q+T4q8YOJc+pvD42vy0nvCC5U/Xq/Ru3kebmnFmYY6DpTkowfSKsn69X83Y4B5"
    "jyBX5ZOr0PmbkbN3rmlITYzNYyZImcmoYw+nFSIAaTAAcHnFIAOQaLgBHJ7UegAp9RSAAPegA24PHWgAJyRQAHkGgBAeKAHZ9KAE7mkAZB70AA"
    "JJoAQjnjt+tAAeM0mA09PWkwDbSAMc4oADQAZ4oASgABxQAp6/WgAzx9aAEHNAAaAF6n0oATFACnFABnjtSsAnamAoP/16QABmmAnQ0AGaADpQ"
    "AAZNABQAZoAKAFAoATNABQAGgAoAKADPFABnJoAXHFAAV7UALjgY/OgAPQe9AC5yaLAIf50AKB8wNOwAxz16ikAD5RjuaAFxj2NMBCCeOwoAMZ"
    "HpTAB1wO1CAUA4waYAVzmmAYP50AG31p2ATkNihAKDx1oQAP51Q2FMdwHSmguLzimgFXjmrTsKwqtjPFO4Id1H1qkx3AE9qpSHcNxFWqlgFEn5"
    "Ue0YXAtupOdwGniobuTcYxyazkxCd+ahjCpACcUAAyDSsIMkUrDDqOQcCpEDDPrigAbkUAJ2+lACgYb+VACYGcmgBTnb7UAIc49QaTAXJLelAA"
    "M9qAE6mgAIIGOlACKMH2pALu9MUwEC5yT0pAL0P4UaAG049aVkAbeKAAJzSuA3GKYBg4pAB+lAC4wKAG96AF7ZoATrQAv50AHFABmgBDkmgAoA"
    "MUALjNACYoAO1ABj9KAA0AHSgA6GgAoAKACgAoAX3oAM+/WgAHWgAA59KAFGAKAADrQAY9f1p2AXP0oSAOvPSiwB1NCQCjj8KLMBDRqgFx+RoA"
    "Q560WAOoFOwWFxiqSQ7AeKAAEMKLCDdj1oAUgFfQmq5R2ADjrQkCEZaGAAD6U0gsLgAU7Ba4EfN160JBYOgqrFBjj6U0gbAninYkVV4zimkAE9"
    "gaLgGSKLiFU5P1ppjFJwKbYCbs0rgJ1X60XEIRj3qRgBuqbAJ1pWAXHr/wDqoaATHPPOKmwWDHFAg7+1JjAcVIhCeaADBz2p2AUckUAG0ZosAh"
    "GOM0gA8j6UABOBjvSYADQAp6eooABk0AGMCnYAwDz60rAGOOO1FgDHPWnYAIxznP0pWATp15oaACMH2pNCEx+FIYuOOMUAIRkdaAE/SkAHkUAA"
    "X3oAAO3FAAVwaADAJHNAB37UAGeOlABnigBKAADNAAeKACgA7UAFABQAUAFABQAUAFAC/wAPtSAQUwHD9aVwPpr/AIJ4f8EkvjF/wVDuvEyfCn"
    "TtGuY/CKW7alPqeorZxIZzJ5SqSDuY+VIcAcBfcU0gPqKH/g0b/bAJ50z4ej6+JV/+IqlbqATf8Gjf7YCtxpfw9P08Sp/8RQ7dABf+DRr9r8jP"
    "9l/D3/wpU/8AiKNAGn/g0d/bAz/yC/h7/wCFKn/xFADv+IRz9sDGf7L+Hx/7mVP/AIindANP/Bo9+2Cemk/D8/8AczJ/8RSuBj+Nf+DU/wDbG8"
    "F6TJdp4N8M675aljBpniS1eYgc8LIyZPsCSaaY0fDHx+/Zs8efss+OpvDPxE8I6/4N1+FBI1jq1k9tKUJIDqGA3IcHDLkHB5oYM4QMe/IpXEfe"
    "37JX/Bt7+0x+2l+z/wCHfiZ4M0vwkvhjxVDJPYHUtaW1uJI0leIsYypIBaNiDnkYPen1A9Gk/wCDR39sBBkaX8Pj9PEyf/EU210AQf8ABo/+2B"
    "j/AJBXw+/8KVP/AIii6AX/AIhHf2wDz/Zfw9/8KVP/AIihSQCf8Qj37YBP/IL+H2P+xlT/AOIo5kA6T/g0c/bARc/2X8PT7f8ACTJ/8RVOasO5"
    "Cn/BpP8AthFiDo3gL/wpo/8A4miMrbhc8l/aR/4N4f2tf2ZtAm1XVvhTqWuabbgtLceHLqHWPLUDJZo4GaUKBySUApykn8INnxRNbSW0jRyq0b"
    "qcFWGCDThruNDQhYnFPlfQLHSfCb4OeK/jt45svDPg3w5rXijxBqTFLXTtLs5Lq5nIGTtRAWOACT6AZpRkr2YdT7t+F3/Brf8AthfEzR4b5/Ae"
    "keGop1Dqmua7bW0wB/vRIzup9mUH2q1Uj1C516f8Gi/7Xkp/48/hxj1PiQf/ABqlzrqwuPb/AINE/wBrtBxafDc/TxJ/9qqvaQ7iIx/waK/tek"
    "n/AEH4c/8AhSj/AON0uePcBJv+DRz9r2ED/QPh2c+niUf/ABuq9rG2jAin/wCDSD9r+C3aUaX8P5mX/lmniVAzfTKAfrUwqRv7zA+Xv2yf+CQn"
    "7RH7CWnS6l8Rvhnrml6FCVV9ZtTHf6ahY4UNcQM6ISeAHKkntTc+bZDPmloijcgj2quSyuDR7f8AsD/8E9PiT/wUp+L994I+GFnpd3rem6ZJq9"
    "x/aF4LWCOBJI4id5B53SoAPr6VlObeiEfZMX/BpF+1+V50r4f/AI+Jk/8AiK0hVglaQXEk/wCDSP8Aa/Q8aT8P2+niZP8A4ik6sejC4L/waRft"
    "ftnOk/D8Z9fE0f8A8RSdSIhG/wCDSH9r8cDSfh+f+5mT/wCIpe0iAq/8Gjv7X7cnS/h6P+5mT/4il7SPcBD/AMGkP7YBYgaV8Pv/AApU/wDiKH"
    "UXQDn/AIg/8Gr37Yfw+0aa9TwT4f8AECwoXaLSfEVpLMQPRJGQsfYZJpxnG1luUj4U+NHwD8a/s6+O7rwx478La74Q8QWYDTafq1m9rcIp+621"
    "wCVI5BHBHQ1KTbswt3ORqWrbiG5yfWkxC4OfehIAVSWwOab0Vx2Pv39mH/g2n/an/as+CegfEDw94b8N6foPia2F7p66vrSWdzNA33JfKIJVXX"
    "DKTjKkHoRTjJJ+8FzvJf8Ag0l/a/37RpHw/wD/AApk/wDiKc6iewNnln7ZH/Bu5+0j+wl8A9W+JXjvSfCx8MaI8KXsmma0l3NAJZFiVzGFB273"
    "UE9tw7VEWt2JHwyRg4ola+gMRlGc81NgDuaLAdj8Afghr37S/wAaPC/w/wDCkEN14k8YanBpOmxSyCKN55nCJuY/dXJ5PYZNUmmhpn6Cx/8ABp"
    "J+2ARu/sj4fgf9jNH/APEVC06iJV/4NH/2v3XI0v4e/wDhTJ/8RWjnEdxp/wCDSD9sAD/kE/D8/wDcyx//ABFSmhAv/BpB+2Cf+YT8Px9fE0f/"
    "AMRTuu4Dh/waPftfnrpfw9/8KVP/AIilzeYxD/waOftgFcDS/h6P+5mT/wCIobQhp/4NIf2wIzzpXw+P/cyp/wDEUk7AfIn7eP8AwTn+IX/BOL"
    "4uad4I+JP9gJ4g1LTU1WOHStRW8SOF5JI13sANrFon49MHvWsXG2pStY+oPh7/AMGrv7WnxS8BaH4k03RfBcen6/YQajbJc+IY4pkjmjWRA6bf"
    "lYKwyOxyKyna+gmauqf8Glv7YOmaXPOND8C3BgjZ/Li8SxGSQgZ2qCoGT0GSKLq1hH5v+L/COqeAfFOo6JrVhd6Xq+kXMlpe2d1G0U1rNGxV43"
    "U4KsrAgg8gil1D1M0rSaGJ0zSEFAAwKnBpsD6X/wCCf3/BJj42f8FMtU1KH4XeFReWGjLi+1fULgWWm20hAIhMzjDSkEHYuWxyQBzT6ajR9SXP"
    "/Bo7+1/EPl0v4fMB3HiVP/iKHYVz50/4KE/8EYPjT/wTF8EaB4g+KkPha0svEt8+n2Kadq63c0kiR+YxKBQQoXGT6sB3pAfJ7DB4pAN60AFABQ"
    "AUAL07UAJigBTzQAnagAoAKACgBTx9aAEoAKAFHPY0AGc9KAP6g/8Agz4/Zui+FP8AwTe1Tx1PFi9+JfiK4uUcjlrW0zaxD8JFuT/wKtIrSxUU"
    "frLOd6jAFaRVjRC2y5XoM0S3Exkkm1ugppKw0PHKg8VLQgyM9qdh2GtJ5T54p8txWuNmmGQwxuFEY9GNI+VP+CvP/BNfwr/wUt/Y98T+GdX06y"
    "PirTrKa+8Las0f+kaZqCoTGFYc+XIQI5FPBVicblUiJpLQmSSP44tI8G32p+M7bRre2kl1G5ultIoNvzNKzhFTHqWIGKNOUg/uH/Y/+DFr+zP+"
    "yr8PfAVpEkUXhTw/Y6XtUAZeKBFdvqzhmPuTSjHmZSjc9CJEhJBGa123LCNOfmFErD0FVhupWEu4shCqTxQlcLEcT7m7HNU42Q2hLoBACBzRBX"
    "BDIY0nkBkAIomtLIGtD+eT/g8A/wCCaPhf4L+J/CXx68F6baaMfGl++jeJ7W2j8uK4vfLaaC7CjgSSRxzLJjAJiRsbmcnG7MXc/LL/AIJ1fsL+"
    "LP8Agot+1T4d+GHhJFiudVczX+oSpvh0izQjzrqQAglUBGBkFmKqDlhWvO9kVc/rr/4J/wD/AATJ+Ev/AATe+Dlr4a+HOg29tePCg1TXJ41fVN"
    "akAGZJ5sZOTkiNcRrn5VFKN72a0HG/U92ZRvwxyK3VuhqW4z5UYxjmspK7IYxpPmp8o0h7NsX1zSSFYhx5jZxxWlirEk0hMQXPWs1FXuSkZfiP"
    "wrpfivw5faVrFjaanpmowPb3VrdQrNDcRuMMjowIZSDggggiqlFyehTXNofyN/8ABwV/wTy0n/gnb/wUG1fw94Vg+y+CfFdlF4k0G2yzCxgmeS"
    "N7cFiSRHNFKFyc7NmcnJMqUnLlMXdOx+lP/BlZ+zhBY+BfjD8VLu0H2q8vLTwzY3DDpFGhuLhQfdpLbP8AuComuV2QLfQ/c27uAeAK0hE2jEW3"
    "UyqTRKyExskhjYCnGNxpXHowYZpNCaI5JQH29z0qlEaXUev7ld3FS1fQVrkMrG9bBI2irSUSkuXU+Zf+Cqf/AATP8B/8FMP2aNX8JeIdKs18U2"
    "1pLJ4a1wRAXWj3m3MbK/UxMwAkjPyspPG4KwycLXfQylDqfxp+LfC194K8T6jpGpQPa6hplzJa3MLjDQyIxVlPuCCPwqVGVrshIze/WpaEA6dK"
    "aGj9VP8Ag25/4IjN+3z8WI/in8RtLlPwg8HXY2W0yYTxRfJhhbjPJgjODKw4biPu+0u3sVuf1CwRQafZRW1tDHDBCoRERQqooGAAB0ArWELGkY"
    "2HwR+Ud1OWugNnmn7Z3wEsf2uf2WvH3wy1FlitfG+h3WkmfaGNs8kZEcoz3R9rD3UVLp6XE4aXP4hfHfgq/wDh5401bQ9Utns9S0e8msbuBxho"
    "ZYnKOh9wykfhWcYaXMkjHbg1DELGN3400B+k/wDwaqfs/p8Yf+Ct3hvVbm3Mtp8PdIvvEpYqSokVFtos+hElyrD3ShRd7Irl6I/q6mmW5i7D2r"
    "WMOUtKxHbqF4q5bFOws0vlvjiko3QWQK+6nyoYpB+lJJEjWm8thzimkgsLKguoyVwSBnik3yAny6s/lJ/4KeajP/wUk/4OI9T8JabKbmzv/GOn"
    "eCbQByVjjtWitpyPQb1uHP1Jrmb5pambd3qf1WaFBHpmnw28UaRwwII40UABFAwAB7DiunkVlY0toWLrEoP0pxVtBxPwy/4Okv8Agic3jvRtR/"
    "aZ+GWls2rabCG8c6XaxZ+1wLwNTRVGS6DCzc8oqvgbHLZTikzOasz+fCcbJCpyCOtYsgaME4pALtIYADOaNgP1U/4Ig/8ABt14r/4KDz6b8Q/i"
    "jFqfgz4QBlntcDydR8UDJ4twwJjg4GZiPmBxHk5ZKfmirH9LPwP+AXg/9l/4aaV4M8B6BpnhfwvokYhs9PsYhHFEO7Huzk8s7EsxJJJJJrpjFN"
    "GiWh26yi6G0kHNS48uorWP5vP+Dz748jxJ+1v8M/h5aXSzWvhTw3LqdzEpz5FzeTlcN6N5VtE30cetc8pXdzOTu7n4uuMdcGpENoAWgA7UgExT"
    "AKAFAyaAEoAKAAUAFACk5FAABigAJ5oASgBRxSAt6Nps2q6pBBBE008zhI41GS7E4AAHUkmrS7gf28/sG/ACH9k39iz4Y/DeG3htZPCPh2ysLp"
    "IujXIiU3D+5aYyMT3LGt6UOptBWPXbObdIA3Aq5x0uhyXY/ML/AILEf8HK2n/8Euf2pYPhfpnwzXx5dRaLb6pf3ja9/Z4s5Znl2wbPIk3Hy1jf"
    "dkf60DHGa5lLuZX7nygv/B7e7Jz+z3Fn/sc//uOjnYcwJ/we4yIf+TeoT9fGf/3HQ53ByYl1/wAHtTyYI/Z7jX1/4rL/AO46pVGgU2fd/wDwRl"
    "/4L7+Ef+CueueJfCw8IX3gXxn4bsxqZsXvlv7a7tN6xtIkwSMhld0DIyDh1ILc4cZtu5SlqffMb4PXIrpcTVlD4geLrTwR8Ptb1y+njt7HQ7Gb"
    "ULqRzhY4oUaR2PsFUmsKitoZS2P5FP8Agiv8Ef8Ahsr/AILH/DWzuIUNlD4jk8U30eMoIrMPe7Tx0LxInP8AeFTzaWaIP691kaJAM5A710KKSs"
    "jfToOXVYNMtJ7q5kSKG2jaSRnOAqgZJJ+gqKqstCZ3sfhn42/4PXLTSPEt/aWHwGjv7K3uJI7e6Pi/yzcRhiFcr9kO3KgHGTjPU1jaSM1dGMf+"
    "D2+ReD+zzF/4Wf8A9x0XfYOZjH/4Pa5ZT/yb3EB7eMv/ALjpqT7D5mWrL/g9piiljWb9nsCMsA5TxjlgO+M2eM0Ny6g5M/Yn9iL9r7w/+3d+zF"
    "4S+K/haC/tdB8X2rXEFvfIqXNs6SvDLE4UlcpLG65BIO3I4NbxkmtDRNNaHqVzPtPB6elaRjfcpH48/wDB5L8U7LTf2Ffht4WmkT+0dd8arfwI"
    "T8zRWtjcpIR7BrqIH/eFZVKdpqxnUjZ6GR/wZ1fsh2vg39ljx98Yby3RtW8aawND0+VkG6OxtEV3Kt1AeeZgw/6d19KqNlU1QU9z9l47zyeM4F"
    "bOF9TSx5D+3h+398Nv+CdX7P8AdfEP4kahcW+mJOtpZ2VnEJr7VLlgxWCBCVBchWYlmVQqklhXNNuLsZt2Z+RviP8A4PZ7SHUJ4tN/Z/kmslci"
    "GS58XCOV17FlW0YKSOwY49TWd5J3Ju73M2H/AIPZ5E6/s9xH/ucv/uOm5yfQOZjz/wAHtMjuM/s9xbc/9Dn/APcVCcuwuZnY/CD/AIPS/Cnirx"
    "9pem+MfgvqPhnw/dzLFd6pp3iBdRmslJx5nkNbxb1HUgODgHAJ4Irp3kNPXU/ajStXGsabb3ULiSC5jWWNx0dWGQR7EGuu0XqjdNbluEeY3zHi"
    "pemwmz+cT/g81+Ien6x+298NvDNqImvdC8Hia5dWBK/aLycoh9MLHu/7aVzRqOEuY57+9c/VP/g3Q/Z8u/2a/wDgkx8L7K9tRa6n4ttpfFV3xh"
    "pPtrmWAn3+zG3H4YrenTi1zG0Iq1z7nicA8nJq5LsU2fn1/wAFqP8Agv8A6d/wSG+JPg3wjD8P08eap4p0qXVpwdbGnfYIhL5UXHky797LL6Y8"
    "vvnjkk9bsxb1PiRf+D2cSj5/2e1z7eM//uOmpO2iBTY3/iNmAPH7PiY/7HP/AO46fPLsHOySH/g9ij37m/Z7Ukf9Tn/9x0+adrJBzM+wf+CTX/"
    "ByD4E/4KmfGCT4c3PhDUfh543ktpbzTreW/S/stTjiG6RI5gkbCZVy+wx4KqxDcYp0ZJN3KhLufowztBkd66UkzW5PZ3Chtzn2rGsrR0InsfxJ"
    "/wDBRbxxpfxJ/bx+Mmv6IEGk61401e9stgwphkvZWQj2wRUc2ljK54rjnNZa3EfWv/BHj/glv4l/4Km/tUWPhSxS60/wfo7R3vinW1jzHplpu+"
    "4pI2meXDLGp6kMx+VGp76DWp/Xx8CPgv4W/Zs+Eeg+BfBek2uh+GPDVmljp9nAuFhjUdT/AHmY5ZmPLMzMSSSa2jTtqa8tjZ8WeK9P8E+H77WN"
    "Wv7TTdL0u3ku7y7uZVigtYY1LPI7tgKqqCSScACtNlqVdJalL4f/ABM0r4reCtI8RaBfw6nomv2UWoWF5DnZcwSoHjcZwcFWB5ANOMU1zIem6N"
    "2EohzJyPSlOLkrImWqsfya/wDBzn+yuf2Zv+CrXjO9tLeOHRPiTHF4usPLXC5uMrcg9s/a47g/Rlrmk5K0WYu60Pz0KnIqbCsKpK/ShAj+hz/g"
    "y6/Z8/sj4L/GH4oXlpHv1vVLTw5p87D5wltEbicKf7rG6gz6mIelXF3kmXHe5+2i3Pkt1rqcbo13Plb/AILFf8FSrP8A4JPfsw6V8Q5PDKeMLr"
    "V9eg0O20s6j9hLtJDPM0m/y5OFWA/w9WHIrnm3G6Ik7H5kXn/B7PJcINv7PUIYf9Tkf/kOs4SktkQpNDIf+D2iZBhv2eoDj08ZH/5DpOcgc2D/"
    "APB7XORgfs9wf+Fmf/kOl7Ri5iOP/g9luA3z/s925B/6nI//ACHT9q1uPmIT/wAHr93Ckhi+AECuykIT4xLBT24+xjNOVVyVmhOTZ85/8GxPwx"
    "uf2uf+CyVz8QNZjMsnhiy1TxfcysSQbu4fyF59S907DP8AcNOSStbcEf09SzCAlF6dq6oxvqb76jPte1eSOf1quS4EOpRx6npdzaXEMVza3cbQ"
    "zQyoHjlRgQysp4IIJBB6g0nST3E0noz+WL/g4m/4Iuzf8E5vjrF458FWc0nwf+IFzI9hhSRoF8cvJYMf7pGXiJ5KB15MZY8cqaUtTGSsz83NI0"
    "S98Ra1bafp9rcX19ezLBb29vG0ks8jEKqKqglmJIAAGSTUNWdibH79/wDBET/g11tNBtNH+Kv7S2mQ3OpKUvNI8CzYeO34DJJqI6M+f+XbkDjz"
    "CTujGipyb1LUWfuNpippEKQxKkUUYCKiDaqKOAAB0FdjpprQ2suhfaYTDqKy5WhFW4uDaqWBOB6VVtGPSx/HF/wXE/aLX9p//gqf8ZvE8MvnWE"
    "Wvy6NYsG3K8FiFskYezCDf/wACrge5zs+TJCD0/SpENzigAHX2pMAzTATFAAKADtQAZosAZoAM4NABQAYoAOtAB1oABQA4DFAH1V/wRN/Zub9q"
    "v/gqH8HfCsgVrGLX4tZ1BWXKva2IN5Mh/wB9YSn1cVcFd6jR/Y/a3wVfmbJr0vZWSije1tBl7qwRRtPPtVKk7NlI/kY/4K9aH8WP2w/+Clvxf8"
    "ap8PfHV7YXuvy2GmyQ6BdmNrOzAtLdlPl9GigRuOpYmvMad3dHO9z5pl/Y9+LELH/i2PxB/wDCeu//AI3U8rJsyNv2QfiuvP8AwrH4gj/uXbv/"
    "AON0rMLFjT/2Pfi1eSqifDD4hOzHaqr4dvCST2A8urS7jSP6BP8Ag1Z/4JXeMv2PfAXjn4qfEnw9d+FvEHjmG203RdOv4/Kv7exjLSyyyxn5ov"
    "NkaIBGw37jJABXO9CLUtUXBH66jVBCCrEHNd7pX1NrHxN/wcM/tMH9nH/gk18VLyC6NvqPiyzj8LWG1sM73kixygcg5Fv9oP8AwGuXFJJW6mdS"
    "1j80v+DNT9niPXvjB8WvivqFqQug6bbeHNOmYcGW6kM82D6qltEPpLWOHjKUr2Jp3buf0FPqiIoy1dqpNs1sz5j/AOCyHx3vfgZ/wTS+Lur6LF"
    "f3evX+gy6HpUFlE8txJdXuLWPYqfMWUyluOQEJ7VjXTirWJnsfyORfskfFfUpGZPhn4/IJzx4eu8f+i65Y3taRmr7MSX9j34sRHDfDL4gf+E9d"
    "/wDxuna2wWsLB+x98WXPy/DD4hn/ALl28/8AjdCbBHV/CL/gmv8AHr45+PrHw74c+EfxBvdSv5BHGsmiXFvCmT96SaVVjjUd2dgo7mjW4O5/XF"
    "/wTt/ZqH7C/wCxD8NPhS08Vzd+ENGjgv5o+Y5b2Qma6ZD3QzySlc87cV30MPpzNGsIaHtkGprMwDEZNaSotbDaP5tf+Dwz4/Q/EH/goF4Q8Baf"
    "dm6tvh/4Yje7hUnFtfXkzSyD6m3SzP4ivMqtv3kYSep+13/BHf4Lxfsr/wDBM74OeChbvaXcHh2HU9QjkGJEu70teTK3usk7L7bQO1ehRw0nHm"
    "fU3jDS59LG7Fyp2kZrbksVsfzof8Hin7RVx4p/a3+Hnw2guHNp4J8PvqVzEG4+030vGR6iG3iI/wCuh9a83EVU6l4mE5XldH45li9Z3b1JuIOa"
    "LiF6elCYHp/7FvwKuP2nf2r/AIefD22JWXxj4hsdJLD+BJp0R3+ioWb6Cq5ug12P7bbeW30LSrW0tgqQ2kKQoo/hVQAB+QrtoUXy3ZtGOhE+rv"
    "K2IySewrd0lFXkXZLc/kx/4Kw+L73/AIKD/wDBb3xtpmm3DXf9s+M4PBOkEHcirBJFpyFf9kvGX/4GTXmOg373Q5nuf1eeCtF0z4Z+A9E8OaRh"
    "NN0Cwg060UfwQwxrHGPwVQK76FCVrtWN4R0uy1LrTSA7W611Kikrsuy3P5Q/+Dkb9otf2jP+Cs3xFe3uzd6b4N+zeFrM5ysf2SMC4Qewunufxr"
    "xnBXbOZrU+DWyCcdqlyE2N6j0xU3JFDYqlLWw/I/VD/g0h+An/AAsX/gpRe+MpopTafDfw1d6hHKvCrc3JW0jUn3jluDj/AGD6VrTptz90uC1P"
    "6ajrayLksM16HsGtjex4z/wUO/aJP7Mv7EPxT8fK+yXwx4Yvrq1bt9pMLJbg/Wd4h+NY11yQdyJaRP4sLyR7i8kaRi7FjlvWvNpruYo9B/ZS/Z"
    "c8W/tkfHrw58OvBGnPqXiDxJdC3hX7sUKj5pJpG/hjjQMzN2CnvW0lpoFrn9eP/BNP9gTwZ/wTP/Zf0j4e+FFhubxALrXdY8rZNrd8wAkmbqQg"
    "xtRMnYiqOTuJ7aOG5VdrU3jCx9F6fcG4f731p1Y8quxy0PwJ/wCDo7/gtH/wlmr6j+zZ8LtYJ0uwmC+ONUtJf+PyZDkaajD/AJZxsMzf3nVU6K"
    "4bhm3PYxlK59xf8Gvf7Ulv8cf+CUHhzR7iTfqvw01K68MXBZsu8QIuYGPoPKuFjH/XE1th4yk+Uqmm9D9CJ9YMowGyDXfGjY2SPyC/4O/P2TT8"
    "R/2P/BHxcsLPzb34fau2l6nMg+ZLC9ACM3qq3KRqPQzn3rgxEbVNDGpufzjO+G/nXLN66GTFjTzJAD3NK10M/r1/4IB/AL/hlv8A4JM/CLR59q"
    "6h4h01vE94QMbmv5GuYs+4t3gU/wC5XZhqN43NYLQ+xZ9Q8wcMK6407GiR4p+11+wz8Jv26NN0ax+LPg608Z2Ph6aW40+C5vLmBLaSRVV2xDIm"
    "4kKBls45xjJyTwsZq4OmnqeN6b/wb9fsYKh8z4F6CT7apqP/AMkVzTwTXwsh0uw25/4N+f2MC/HwK0D/AMGmo/8AyRQsC+rBUiu//Bvv+xsrgj"
    "4FeHCPfUtR/wDkirWAh1YeyRej/wCDf39iy6h2P8C/DiPjAJ1PUQB/5MVjWwcoJy6EunZXP5WP2yLrwpcftV/EOTwFpUGieCT4gvV0OwhkaWO1"
    "sxM6woGYlm+QKcknrXBdrYxP3J/4M2PgmPC3wD+LHxHurfbc+I9ZtdAspiORDaQmaXB9Ga7j/GP2rtwlJ1HzM2pRvqftSmpecfmPNd7pWNbEGo"
    "eI9N0zWLTTp9QtItTvoZLi3tHmUTXEcZQSOqZyyoZIwxA43rnqKyUk5coua5JLrCtHhWFbRovqNKx53+1H+z94U/bA+AniP4ceOtOTVfDHia1N"
    "tcR9JIGzmOaJv4ZI3Csrdio6jIpVMNGXqKUEz45/4JE/8G+vww/4Js6ldeMdbvoPiL8TjPMun6vcWYhttEt9zBPs0JLbZ3jK75GYkHKpgbi/HH"
    "BSjL3tjP2dnqfoRYXvlsVDZya7Z07o0aJ77UYILuC3e6t47q5VnigaRRJKqY3FVzkgblyR03DPWuaMo83KJS6DBqhhPLfhW7pXKscb+078dov2"
    "ff2ZviD49kEbr4L8O32tbZPuube3eVVP1KgfjXJiYcsTOasj+ILxJqs+ua7d311I011ezNPNIxyZHY7mJPqSSa81mBQFIBKAFIxj3oAOSTQAnS"
    "gBT1pAJTAKADFABQAZzQAtACGgAoAXv1oAUcnk4oQH7Of8Gd37PSaz8fvif8U72z3w+FtGh0LT52XhLm8k8x2U+ohgKn2m9678BRc6mnQ1pK7P"
    "6AD4kKMQGz+Ne/8AVjosSRa0s3U5NS6LQWsLJq7xSblkcewY0KjFqzQ0iQ+JZZFwZX/76qPqsV0DlQDW5AMmRvzo+rx7ByojuNfkMW3zWx9aqO"
    "Hje9gURbHWRFEQWz+NOdHyBoeutQ3DMWkVAgySxwAPXNZ1I+zjdkvQ/mr/AODmD/gq/p/7anx1034X+ANXTVfhv8O7h5Jr63k3W+taoQUeZCOG"
    "iiQtGjdGLSsCVZTXiV67qT5rHPOV2fq5/wAG1nwPj/Z+/wCCTng2e4iSPUvH97d+KpmxhmSVhDAD3x5NvGw/3/eu3AUG06iNKUep94PrJYj5q9"
    "T2XU2SR+Uv/B1h+2Vr37O/wF+EGieEdavND8Tat4pbxHHd2kvlz2o01F8plPXma5Ujt+6NeRjpN1FAxqt7H0x/wRx/4LGab/wUz/Zqt767lt9O"
    "+JvhhI7TxVpcThVaQ5CXkK5z5M2M9PkfcnICs2mApwqtwl0HSs9GfXb+IZ7qXc80nPbca9ZYaEVZI2UUh51t4oziV8+u6p+rpvYdkNtvEUvmZM"
    "rtj1NVPDRa2BxHtrguGwW5PvS9i0Tynkn7a37bPg/9gv4Aa18RPHV+LTSdLj221sjgXOrXTAmK0gB+9K5U47KAzHCqxHPiKtOEG29SZtJXP5cf"
    "hJrHij/grd/wVn0C78R7brWPir41t5NQReUt7QyqXRR/chtYyo/2YxXgwmpKzOVM/rlnuIrOdlhVY4hwir0QDoB9BX0lGnaCidi2GR+IXt50C/"
    "OzMAAO5rSeHUotjaP5FP8AgtJ+0j/w1Z/wU6+MHiyO6S7sBr82ladIhBRrSzxawMCOMFIg3/AjXy7d73WpxXPljvWdiRQfpRa4BVWGfpF/wa4f"
    "s/D4s/8ABTex8RXNu8lj8OtDvtdMmDtWZ1WzhGfXfdbx/wBcz6V04OjzV0lqaU4+8f0zw66dqqWJ+tfTyoa3R1W1OW/aU/aCtP2bP2aPiB8Qbr"
    "DQ+C/D17rG3IBkeGFmRPqzhVHua8zHR5aeplU0R/NV/wAG7Xwjn/aO/wCCt/hjXdWtpNRtfBqXvi29lYFsXESEW8jH1+1zQNn1FeZhYqpVjTiZ"
    "QV3ZH9PMGq4ABbFfTOkdZX8a/Eqx+GHw68ReKdSbGn+F9KutXujnpFbwvM//AI6hrixacINkTdkfxT/GP4g33xW+K3iTxPqb+ZqHiPU7nVLls5"
    "zLPK0r/wDjzmvnE3q+5x3Ob5ApCAdKLABUkcU3FvVDP6Kf+DQ74Lv8Mf2M/iB48vrTybnx74hSwtJWTBmtLCLhlPdTNdTL9YjXpZbhZT/edjej"
    "D7R+rz68GY84r3VQ0Oix+bH/AAdQ/tRH4Zf8EyofB9tcrFd/ErxHa6bNHkbpLS2zdyn6CSK2B/3/AHrxc0g4SRz11Zn82Phvwxf+NvEVlpOlWd"
    "zqGp6nOlra21vGZJriV2CoiKOWZmIAA5JNcE0rXRjc/qJ/4IWf8EiNO/4JnfAYa54lt4Lj4weM7ZG1ucEONFtyQ6adEw9CFaVh96QAZKxqT6WX"
    "4Rpc8jopQtqfesGotJgA5xXpyp21ZqfnL/wcAf8ABaR/+Cfvwjl+HngPUEPxe8a2bLHJC4aTw1YuCpuyOcTPysSnGOZP4VD+PmOIh8EdTCrJLR"
    "H8yt/cX+t6jNdXJuJ7i5cySSSEs7sTkkk8kk9zXkwqPY5z9f8A/g0O/aQbwL+038Qfhdql6bWw8a6ImrafExwJb2xflQPU288zH2gHpXdgZuNW"
    "y1ubUZWZ/QONZRRw3Fe/7JnS0ePft9/BeH9r/wDYz+JPwwljEsni7Qri1ss/wXqr5lo//AbhIm/CscVguak5dUTKndH8a2raXNpOrXFrcxSQzw"
    "SFJI3Xa0bA4KkHoQa+XjHWxxWOo+Avw2uPi98ZvCfhS0DNdeJ9YtNJhA5O+eZIl/Vq2UdLdykuh/aR4Rt9P8DeFNJ0DSEEOk6FZw6dZRDpHDDG"
    "sca/gqgfhX1NHCOEEnudqjZG7aa8COWGcVM6FtRWPEvEv/BUf9nPwd4k1LR9b+OHwv0vVtJuZLO8tLjxHapNazRsUeN1L5VlYEEHkEGuKWLorR"
    "MhzXQxf+Hvf7M0MxA+PfwnI9f+EmtP/i6v61h7fEVzx7jm/wCCvX7MjN/yXv4Tj/uZ7T/4ul9aofzBzx7jpP8Agrx+zGkWR8ffhMfYeJrX/wCL"
    "pfWqF9yeZdzyb9s//gtL8CfBv7KHxK1Twh8Zfh5rPiu18NXy6LY6dr0E91c3rwPHAI0RiSRIyNwOACe1RisTSdJxpu7FNrl0Z/KbcSG61B3JYi"
    "RycnnrXgWa3OY/rS/4ITfAz/hmX/glT8JNIuY/I1HWtNfxHeAjBZ76RriMn3+zvAP+A17+W0X7O9jporQ+t5fEojQsrcivSjhruzNVE/CX/g5F"
    "/wCClHi74B/8FR/hDdeA9Xax1z4O6Kt+y7t0TzX775IZUB+aOS1SBWU9Vk7da+dxvu17Q6HLUT5tD9d/+Cf/AO3D4U/4KA/sr+H/AIm+FpVhh1"
    "JTbalpzS75dGv0VTNayHAyV3KQ2BuR0bA3Yr1MLiFWjbqbQlzI9luNaVYuG4rrVFtmiRBDrpRTgnArR0F1G1c+Vv8AgqH/AMFh/h9/wS++HK3e"
    "uzLrvjzVLczaH4WtpgtxdjLKs0zc+TbhlIMhBLFSFDEHHm4zFQpJwjqzGc0tD5O/4NzP2uPiT/wUM/aV+N/x6+Kmqy6hd2NhZeF9Bs4VMWm6RB"
    "cytczwWsWSEA+yW245LtkF2Y81wYHDutJvsZ0ots/Wl9WFyc5xn3r21Sa0NrH5/f8ABzH+0NN8FP8Agk94wsba5MF3491Gx8NRBTh5I3kNxOB7"
    "GG3kU+z+9eZmllFJbmVV6H8ssrb3Jz1rwzAZmgAoAKAF5xQAnWgA6UAFAB2oABQAvQ0AJQAUAAoAOpoAUUAKBuPtnFAH9N3/AAbUfB7/AIUJ/w"
    "AEtvDt7PsF98RtUvfE0vGHSPeLSBT6gx2okHtLX1eS4K9J1n1OyjT93mPvs6sWGQa9r2Ztynzj/wAFGf8Agqn4Q/4Jg+AfDfiHxfpGua8vijUZ"
    "LCzstLMInIijDyynzXUbV3RKcHOZV9683MsVHDpRtdszqTUT5Avf+Dvz4My42fDP4mL65ax/+PV5MM3Ud4mKrLqRR/8AB338G0zn4Z/Ew/8AA7"
    "H/AOPU3nK/lG667Dj/AMHf/wAGwv8AyTH4mc/9NLH/AOPUv7YX8oe2XYgm/wCDvj4POfl+GPxJx7y2P/x2qWcx/lH7aPYzNf8A+Dw34baXaOdI"
    "+DvjfUrnadi3ur2tnHn3KLKf0qKmb8y92NhOuuiPz8/4KI/8HDPxv/bx8M3nhi2nsPhx4IvlaK60rQHkWbU4yfuXNyxMjqRwUTZG3dTXnV8XVq"
    "6SehjKblofEHw48L33xF+IGi6BYxPcX+uX0Gn20ajLPLLIsaAfVmArnjJ/ChJt6H9lXww8G6T8E/hj4c8HaCQukeEdLtdFtAvA8q2hWFT+ITP4"
    "195gcLKFCMZK3X7zuhFpJM6G31ozY2sS3pmtp0kimj+cP/g6X/aOb4vf8FK5vDEU8j2Xw30Cx0Xy8nYlzIpvJz/vZuVQ/wDXIDtXxeLq/vpNbH"
    "FN2kz45/YP/bg8Z/sAftIaH8RfBlztutOfyr6xlYi21e0YjzbWYA8o4HXqrBXXDKCOSFVxnzx3IUrO6P6tf2Pf2y/Cn7cP7P8AoXxI8E3LTaNr"
    "SbZreRgbjS7pQPNtJgOBJGSAccMCrDKspP2eArwxFO63W5305KSueny6zhOSK7FS1KsVdT8RXllol7Pp9kNTvoYJJLa0Nwtv9qkCkrH5jZVNzA"
    "LubgZyeKmvBxi5JXYpKyPx6/a1/wCDqvxT8GfF2s+EtF+As/hXxhoVy9nex+MNVeY2sqkgq1vAkbZB5B83BGDyDXzdbNKslaKsczqvofkd+2p+"
    "398U/wBv/wCIo8S/E7xTea9cwBksrQAQ2OmRscmO3gTCRjpkgbmwCxY815M5c3xbmD1Prr/g1j+EsHj3/gqJb63K2JPAnhXVNdtxjIaQ+TZD8h"
    "esfqtb5fTi8RGMy6SXNqf0hXHiXdkFvmFfbxw52qJVi8TtBcJIPvIwZQemQc1pLCqUXF9S+TQ/E/8AaU/4NPtZ8Z/GTxDrngL4seFNO8NaxfzX"
    "tpYa3ZXa3disjlxCXiSRZAmdof5SwGSor5OrkeI53yK5xOhK+hwSf8GjvxOfp8YPhj+MGo//ACPUvJsWt4/iv8wdGQw/8GknxOSTDfF/4ZAevk"
    "aj/wDI9CyTGfy/iv8AMPYSLkX/AAaL/EhtjSfGT4aLESNxS11F3A9gYACfbI+tV/Y+Kfu8tvmg9jJn6hf8Eo/+CXvhD/glv8JtW0zR9Un8S+Kf"
    "FDxSa3rc9utv54iDeXDDGCxjhUuxwWYszZJ4VV9fBZT7D3pu8jaFLlPqL+2fJfr+tet7G5rynwF/wcsftXQfBH/gmrqnhiO8jj1n4p6jBosEG8"
    "eY9pC63N1IB3UbIIz/ANdxXz+dzUIqBz13ZWPmz/g0i+CEug/D74t/FOSIbNWu7TwtYykc7Yl+1XIH4yWn5e1c+R0I1Kzm+n9f5ioRTdz9jjrJ"
    "f5gcV9V7JI6uU8j/AG6/hr4s/aR/ZF8f/Dvwfr2meGtb8aaZ/ZCajqCytbwQSyoLkMI1Zsvb+dGMDguK4sxwU6tHlorUmpBuNkfjkP8Ag0e+Js"
    "ylv+Fw/DEHrgwaj/8AI9fNvJ8WtHH8V/mcnsJIY3/BpD8TVGf+FwfDA/8AbDUv/kej+xcZ/L+K/wAw9hIav/BpF8TWXP8AwuD4YfTyNS/+R6Hk"
    "mM/l/Ff5h9Xkdh8Ff+DSPVz4xtX8efGTw7/YMUga5h8PabczXk6d1R7hY0jJ/vEPj+6elawyXFNWmkkUsPLqfsl8B/hB4W/Zk+E/h/wH4M05NJ"
    "8MeGLQWdhbK25guSzO7fxSO7M7N3Z2PfFfSYbBxpUlTidMYpKx2M2piMbq2jTvoUj+fz/g64/ads/iZ+1p4Q+GulXDXCfDzSGl1NQ2Vjv70pKU"
    "x6rbpb59C7DtXxWc1/aYhpbLQ4a8ryPof/g23/4JFw/Czw7p/wC0P8SdOU+JNSh8zwVpV1ECdPgcY/tNwf8Alo6kiH+6paTq0ZXqyvL5VWqtRe"
    "6aUaT+Jn6+N4kdeGJNfVfV10OrlueA/wDBRb/gpv4Z/wCCbn7Nep+N9bEF/rt1usvDejNJtfV73bkA4IIhjBDysOi4A+Z0B8jOKscPBRW7MKz5"
    "VY/mp+Gn/BR/x54O/bztvj7rd3F4p8U3GrHUNXi1CJZbfVYX+WW2ZCCFjMXyIAP3YCbcbFx8rzcslI5U7an9T37OvxM8C/tA/Azwx4+8J6fo9z"
    "4f8V6fFqFm32KEMiuOY3CjCujBkZezIw7V9ZQw2HrQUoxX3HYoxkro6qEWcd4Ht7KxtnGQGit0Rhng4IGa9CngKFP3lFXNeSK1Lj6uYB94gVoq"
    "SYWuQNqxuJFKsVIIIYHkGr9lZWYH8vP/AAXw/ZhT9l//AIKcfES0tY2TSPF1wvizTzs2r5d8DM6KOm1JzNGP+udfB4ikqdWUGefPSRp/8G83wT"
    "Pxl/4KheCLqWz+16b4Hgu/E95xxEbePZbufpdTW341rl9L22JhBFUlzSSP6bbHVGSEZ4NfdTpK53kreIrmGN5bZoPtMalofOJ8vePuhsAnbnrg"
    "E4rGvh3KDjHdkyjdWR+FPiT/AINXPi7468W6lql78YvhxcX+qXMl3czPFqJaWWRizsT9n5JYk/jXykskxS96UfxON0ZLczrn/g0k+KcL/wDJXv"
    "hkf+2Oo/8AyPWayTEvaP4oXsJEbf8ABpf8UFHPxf8Ahn/351H/AOR6f9hYv+X8UP2EhF/4NMficynPxf8AhmP+2Go//I9P+wcX/L+K/wAw9hIk"
    "P/Bo/wDFOSEtH8X/AIZv7eTqOT/5L1m8kxUen4ofsJHwPqH7Guo+E/8AgoGPgK+p6fr2qw+MYfCEl/p+/wCzTTNcrbs6bwG2hieSB9015rvfkZ"
    "jrex/W7o8Nl4N0Ww0fTMLpmkW0VhaIOAkEKLHGo+iKo/CvvcJheSjGElqjvhGyRo2VxJezJHEQzyMEUepJwKurywi3LoU3ofyZ/wDBYT9oY/tM"
    "/wDBSb4v+LI5BLZzeIrjT7Fg2Va0tD9ltyPYxQofxr8+q1G5uR5zldndf8ETv+CpWp/8E4/2mYm1S4uJvhp4xeKx8UWaEnyUDERX0YwcyQFmOB"
    "y6NInBZSu2DxUqNVVEVTnyu5/UVpnii11vSbG/sbuG+07UoI7q0uoX3xXMMih45EboVZWBBHUEV9xRcKsOeB2ppq6Pz0/4LE/8F+PDv7BcOp+A"
    "PhvNp3ir4vFGhuWOJ7DwqxA5n/hkuRnIh5CkfvOnlt4GYZmlenS+8wqVeiP52fiv8ZPFHx9+Jer+LPGeuaj4j8R67Obi+1C+mMs07n1J6ADACj"
    "AUAAAAAV86m29TmWp/SP8A8G4HwQk+Bv8AwSs8J31zD5N1461W/wDEr5XDtG7rbQ59jHaqw9nr6nIqS9k5dWddBe6fesHiAsQAcV7cqBryn4nf"
    "8Hg/7Qkeqa38GfhrbSv5tjZXvie+QH5WE8gtYMj1H2a4/B/evj83kvbcvY5K3xH4kHPFeQYjRQAvGaAEoAKAFIxjHekgEpgFABQAUAA60AFABQ"
    "AUAFAB0oA1fBfh258Y+KLDSbKCS5vdUuY7W3iQZaSSRgqqPckimld2QJH9gnwe+ENj+z58I/C3gvSQv2Dwdo9nocTD/loLaBId/HdihYnuWNfp"
    "WXR5MNCm+35npQdopHTJ4hWBRvauz2DexR+B/wDwdZftDRfET9s/wj4Gsb5p7T4f+HEluYAflgvr5/Ok/E26WZr4DO6jeIcX0OGs3zWPyz3k9a"
    "8cxELDBoAN3rzQAb/bmgBCc0AKvNAH2v8A8G/nwP8A+F1/8FRPhw0sCzaf4Qmn8UXe4fKv2KF5oc/W4EC/8CruwFL2uIhTXVmlJXmkf0tw35sD"
    "sBO33Nfp0o82p6T1JG8RLpP+kzSCO2hBlmkY4Ecags7E+gUE/hXPiFGNGU5dERJ2Wp/Iv+2X8fLj9qH9qj4hfEK7UpL4y8QXusKhPMSTTvIif8"
    "BRlX/gNfl7au7HmtnmYwKSEfZP/BG7/gq3rP8AwTR/aAEt6LzVPht4raO08TaVG2WVAcJeQKePPh3Egcb1LoSNwZerC4uVCopxNIT5Xc/pf8H/"
    "ABT0f4m+BtI8R+HtUs9a0HX7SO+06+tZA8N3C4yrqf0IPIIIIBBA/Q8LOFaCqQ2Z6MWnqjSh1t4ASWxW8qSbKPhX/gsv/wAEl9K/4KS+CTr3h2"
    "Kw0n4waFb7NNvZAIovEEK9LK5foGx/qpWztICMQjbk8TOckVSm69Ddbr+v6/XCtQ5lzRP5yfH/AMO9b+FHjPVPDviLTL3Rtb0W6ksr6yvITFPa"
    "TRsVeN0bBVlYEEHpiviYqy1OBH09/wAESv23tP8A2Ev2/wDwv4p127ks/CmswT+HtemRd3k2lyABKw67Yp0glbHO2I4BPFdOExEadeM2aQmlK5"
    "/Trp2rrqtrDc280V1b3caTwTwyCSK4idQySI6khkZSGDAkEEEcGv0uMqcoKcHdM9FNdC0115ajd1pWuw0IP7SZ268VooIqxJHelT/9ep5BWQSX"
    "RboRmmoWCwLdNgYOafKh2RJFrHl/KT+FJ0b7EtGH8cvjT4S/Z1+EGqeOvHfiCx8M+GNJXM19dPgO5BKwxL96WZ8ELGgLMe2MkeZiswpYX+I9ex"
    "nKoo7n8xP/AAVh/wCCkGq/8FJP2oLnxG0Vzp3hDRYzpnhnSpXy1paByTI4Bx50zEu+M4JVASqKa+HxOLlXrOpU6nDUm5Sufu5/wRC+DS/s5/8A"
    "BMH4V2XlmG78UWU3ii+U8fvL2VpIj/4Ci1H4V9dw9h0sI521b/r8bnXQVoH1g2rE4IPH1r3VT7nQkI1+XOfSmo2GIbwk9aORCsg+1lxjPejlCy"
    "A3Bx97mjlCwkV6yNkE03BNBYsvctLHvXnFSo20YkfK3/BUn/grr4K/4JwfCO/tXudP1z4salbZ0Pwz5geSFnHyXV4o5it1HzANhpcBV4LOvzWb"
    "5rCk/Z0Hd90c9eqo6I/LX/gif/wTL1r/AIKUftF6v8ePjMbjU/A9hrEt/dteqS/jHVmfzWg7ZgVnDzMMjBWMD5yV+fy3AVMTV1Wi1bMKVNydz9"
    "65737BO3IA6AKAqqOwAHAAHAA4Ffo0KUeRQirJHoXT0Ri/Fn49+F/2ffhT4g8ceMtRTSvDHhiza91C6YbiqghVRV6tI7lURRyzMo968/Mayw1F"
    "1JfLzM6kuWNz+XP/AIKdf8FCvEv/AAUc/aW1Hxnq6vpuh2ubLw/oyy749IsVYlI/QyMSXkcAbnY9AFA/PMTXnWm6k3qzz5ycndnzqGytYtk3P0"
    "p/4Iz/APBeeD/gnh8Hte+HXjnQNd8VeFJ7v+0tE/s6aJZtLmfiePEhAMUmEfhvldW4PmEj0suzKWHe10bUqvLufYUP/B2N8JrS9/5Jr8QmQ/3p"
    "7Mf+1K9efEt1ZwNnio9j9Av2TP2xfD/7bv7OHhf4l+F7a9stL8SpOPsl2VM9pJDcSwPG5Ulc5j3DB+66nvXuZViViaXtDWnNSVz0ZtT+xpljtN"
    "ekoc2xpufkf/wda/Ak+PPhD8OPixZQRtL4YvZfC+puq/vXhuVa4tWP+wkkVyPYzr618hxLgXScaq6nJiYW1MD/AINNvgu2k6P8W/iZcIqpePZe"
    "FbKQjlwC11dr+BWyP41HDOG56kqvZCw0L3Z+yk2oFV9q+wjE60VlvXdsZ61pyosliumjbPSk430BivfMx6/rU8iFZDDc570+ULIY1wR9KpINDJ"
    "8YfFWz+EXhPWfFGpOBpXhjT7nWbzPQw20LzuPxWMiubMEo4Wc3pp/wCJO0Wz+ff/ggt4Ruf2lP+Cutv431uN73/hHYtV8X38jDP+lFWjhc+/2q"
    "6hb8K+Byej7fGQjbr/w/4HFQXNM/ob07Uj5KqTgn1r9IqxTd0dzOP/af/aAH7OP7NXxC8bmVIpPCfhu/1S3LHg3Edu5gX6tN5a/jXk5vaOElNm"
    "dR2ifyFatqkmqX0s07PJLK5Z3Y5Zj3JPrX5y5XPPuVixBBFK4j6E+HP/BVb9oX4TfCmw8FeHPi1410bw1pVs1pZWdrqBjW1iYsSiMBuUAscYPH"
    "bGBWscRUjHlT0KUmjwC5v5r+6lmnlkmmmYu7uxZnYnJJPcn1rK5Nyfw9plxruu2llbRtLc3kyQRIOru7BVH4kihXb0Gf2F/AfwJb/AP4IeD/AI"
    "eW8yTQ+BNFs9AWVeBMbaBIWk/4EyFvxr9HwGD9lh4W6q/3nfCNkkdTPdvHGXTP0rvik9zRM/mb/wCDhf4+H47f8FTviGsbH7H4MNt4UgT/AJ5t"
    "Zwqk4/G6Nwf+BV+aY+qqleUltc8+q05Ox8RHpXGZiCgBccdqADrQAnSgAoAU80AJ0oAM4oAOtAB1NABigA60AHWgAoAKAO8/Zi+Odx+zP+0H4O"
    "+IFppen61d+DdXttYt7K+3fZriWCQSIH2kNt3KDwe1VGXK7oadj9MbT/g7V+JdvbmNvhN8NZM9zc6jn/0dXsvPsU3e/wCC/wAjX28jOvP+DrT4"
    "kXTkn4VfDdcntcah/wDHq3jxNjF1X3IaxEz86/2tv2mtb/bB/aK8WfEjxBBaWuq+K743ktva7vItV2qiRR7iW2IiKoyScLXg1qsqk3OW7MW7u7"
    "PNycmsxBQAUAFABnnigBRx3oA+lP8Agmn/AMFIdZ/4JrfETxF4m0Hwr4e8Tah4h0saS39qyTottF58czbPKdTlmijznsvua68HjJ4aoqtPcqE3"
    "F3R9oXP/AAdg/Ee4tVjPwl+GoKj732nUc/8Ao6vTjxHi073/AAX+Rr9Ykcv8Xv8Ag6B+J3xT+D3ifwlF8P8AwJoZ8S6Vc6UdRs5r5rmzSeJone"
    "PfKV3BXbGQRmssTnmJrQcJPRilXk1Y/MWZhJKSDnJ7145iNzz16UXAM7j6UAfbX/BOT/gub8T/APgnl8OL7wbZaVoHjbwtLcfa7Cx1x7j/AIlE"
    "pz5vkNE6lUkOCyHK7l3AAs270MHmdfDRcaT0ZpCrKOiPoi8/4OwfiTdy5/4VR8NkHYC41H/49XdHiLFrr+CL+sTJF/4OwfiSiDHwk+Gm4dzcaj"
    "/8epviLGd/y/yH7eoj4z/4KV/8FIJf+Ck3jvTPFes/Dzwh4Q8UWUP2W71HRJLnfqsKgCMXCyuwZowMK4w207TuAXb4tas6snOW7MZSvqfM0fDg"
    "9Mc1kkSfYv7D/wDwXF+PH7CPhi38PeH9c0/xL4QtM+RoPiS2N7aWmTkiF1ZJoQTztSQLnnbXdQxtWhfkkaRqNbH03/xFg/Eu4RTN8Kvhu0n8Wy"
    "41BVJ9gZjj869GPEWLSsn+X+Rp9YmD/wDB138SV4T4UfDcD3udQP8A7WpviLGd1+AfWZjx/wAHYPxKK8/Cj4b59rnUB/7Wpf6xYzv+QfWJj4/+"
    "DsT4kqp/4tR8ODn/AKetQ/8AjtL/AFhxj6r7kL6xMan/AAdi/EpH/wCSU/DjB7fab/8A+O0/9YsXtcf1iRy/xF/4OnfjX4i0+WHQvBPwy8Ozyr"
    "hbtbW8vJoT6qJZzHn/AHkYe1TPiLHNWUrB9ZmfD37UP7c3xV/bM8Rw6p8SvGuteKZrQFbWG5kCWtkp6rDAgWKIeyKM9815Mqspy55u7Zi5N6s8"
    "ogn2TrJgEqc49aiT5tST9TPC3/B098QfCXgjRtAs/hJ8NI7DQ9OttMtF8/UBshghSGMcTdkRR+Fephs8xNGCp03ovQ2VeSVkWo/+DrX4mwrj/h"
    "Vnw1x7z6j/APH66XxJjHq7fgV9ZmSL/wAHXvxNXr8K/hr/AN/9R/8Aj1T/AKx4zv8Agg+szHD/AIOwfiUOvwp+Gp/7eNQ/+PUPiPGd19yD6zMG"
    "/wCDsD4mE8fCr4bD/tvqH/x6j/WTF+X4B9ZmNP8AwdffE1x/ySz4bf8Af/UP/j1NcR4sPrMgm/4OvvigIGEXwr+GYcjgvLqLAH6CcfzpPiLGPZ"
    "/kH1mZ458dP+Dkb9pf4u2Utno+taD8OLSZSjf8ItYGG4IIwcXE7yzIfdHU1xYrNMViFarLQmdeUlZnwv4i8X6l4y8SXGr6xf3mq6jeSme5ubqd"
    "ppp3Y5LM7ElifUk15/W6MT9Mvg//AMHO/i34FfCrw94L8LfBb4W6X4e8L2SWNhbRTagBGi8ljib5nZizsx5ZmYnk16eGzfEUIclJ2Xy/yNY1pJ"
    "WRtn/g66+JU8u6X4UfDZ/YXGoj/wBrV1riPGJaNfci1iJny9/wUk/4LM/En/go9oGi6BrOn6J4T8K6NK12dJ0Zp/JvbogqJ5mldmdkQlVHCruc"
    "4yxNebjsxr4qSdZ3sZTqSlufILNketcTZA05pAKrbCKadgHPIGBpuVwbPuX/AIJwf8F1/Hf/AATp+CFx4B0vwj4U8V6K2rTatbnVpbpJLR5o4U"
    "kRTFIo2Ewq2COrN6134PNK+Gg4UnuaQquKsj3rV/8Ag6++JGqNz8J/hvGPRbnUf/j1d1PiPGU9n+C/yLWIkjyn9rf/AIOCPFf7Yv7N3ij4aa/8"
    "M/AlnpniaOFWu7Se8NzZyQzxzxyR75Cu4NGByOQzDvWONzvEYuHs6trBKvKaszG/YD/4LqeKP+Ce37PC/Dzw58OvBWt2Z1W41ia+1Ka8FxNPMs"
    "SNkRSKuAkMajj+H3rLBZriMJFxo6X3JhVlBaHtZ/4OvPiY5GfhX8NzjoPP1H/49XX/AKyYvy+5FfWJEq/8HX/xMU5/4VR8Nv8AwI1D/wCPUnxF"
    "jO4fWJgf+DsT4mP/AM0p+G3/AIEah/8AHqS4ixa6/kCxEwH/AAdg/EwH/klPw2/8CNQ/+PU/9Y8X3/IPrExp/wCDsH4mE/8AJKvhrj08/UP/AI"
    "9QuI8X3/If1mYp/wCDsL4lEEH4U/Dbkf8APxqH/wAeo/1jxff8hfWJHnX7T3/ByJ8Sf2kv2ffGHw/fwN4J0C28Z6c2l3V9Yy3j3MMDspkCeZIV"
    "+dAyHIPyu1Y4zPMRiaXsqmwSrykrHgf/AATQ/wCCn+u/8E0fE3i3VtB8I+GvFF74ss4LGV9Xe4X7NFHL5pVPJdfvuIyc5/1a+9cWCx1TC1PaUt"
    "zOFRwd4n2Dc/8AB198SZ0XHwm+GaFR1W41H/4/XpR4jxa6/gv8jRV5Hkv7a/8AwcL/ABC/bV/Zp8Q/DLUfBPgzw5p3iRrcXV7pkl41z5cM8c/l"
    "jzZWXazRpnjoK5cZm9fEw5Kj0JlWlJWZ+e7HcxPrXlmQlACg549aAAnmgDr/AIB/FU/A740+FPGK6XY63J4W1e11dLC9LC3vGt5klWOTaQ2xig"
    "BwQcE81UJWdxo/Sv8A4isviUsu8fCv4bgk5P8ApGo8/wDkavf/ANZsZa119yNniJlxv+DtL4pxooj+FnwzQpggmXUDyP8AtvWEs+xLvd7i9vI/"
    "LX4nfEPVPi58Rtd8Va1cG71nxJqFxqd/O3WaeaRpJGP1Zifxrxm7u5k3cwqQgoAKADGKAFJJoASgBTxQAnU0AFAAKACgAoAKADtQAUAFAB0oAX"
    "OKADPGKAEzQAUAGKACgAoAAKACgA6UALnBoAQ0AFABQAdKADpQA5TjmmgELbjQ2Ag60gFzxTuAcGi4ClcAd6dgED4xSuA4sKLhcM0AJuyaLgIW"
    "yKLgIP0oAM0XAXODQAufejQBPrQAdaAAH15oQCg8U7gJnPekgAnI702wACkgAmgBQ2frQmANxTYDakBcUAJQAvWgBMYoAUNimmArNn60NgJuJp"
    "XABz3xTQCluKbYAvHakAmSBQAo5oAQ4FIAyMe9AADigAzigAx9aAEoAUnI+lAAfwpAA/OgBMUwFJPI60AIaADOKAAUAAoAO9AB2oAO1ACnp0oA"
    "SgBe/pQAlABQAUAGeKACgAoABQAUAFAB2oAKAFAyPekAmaYBQAUAFABQAUAAoAM4NAC56+9AB7dqADpQAKCenamlcB4t2Y9vzqvZsrlYPbsp6A"
    "fjQ6bQcjGmJgOnSpaaE0NNIQCgBRQAojYrkdKpRdrj5RB71IgPv2oAToKACgBaAFRN5ppN7DSEZdpIPX60NWE0JmkAZoAXOfqaYDhA2e350+Rl"
    "crHNCfUfnVcrFysYY2TGf0qWmgasIRx3qRCUAKeFpgOW3ZvSqUGVysDAR0x+dDg0HKw8pgeg/OlysTixp/lUiEoAXPFADljaQZGOKqMG9hqNx3"
    "2ZjycfnT5Gtx8rGm3Yen50cjDlY1lKnBqWrEiUgFFCAWNC547VUYtjSuOaFunGPrVOD2HysT7O3t+dLkYuViEbeuRUu6E0If1pAA5oAQjBxQAu"
    "7A4oACTketABnigBKAFoAAeetACUABGKAFHJ9KAE70AGaADNABQAvSgBKAFFACZ4oAUHFAAaAEoAKAFxkfSgA6YpAJTAOKADPFABQAUABoAUHa"
    "aQCdTTAKACgBcY7UAJQAZxQAdB9aAFH5UAJQAuMDNACdcCgD7g/wCCev7NnhfTfhyfH3iqzsb2edJZoPtyK8Flbxk5l2tkFjtY5I4AGMZNf1l4"
    "OeH2W0cp/wBY81gpOV3HmV4xjH7Vnpd2bu9lt1P2jgbhnCQwH9qYyKbd2rq6jFdfwevRbHQap/wUW+FWk6nJHZ6Zq8scbFAyaZEqMB3ALg4+oH"
    "0r6Cp418FwbpujJ26qmrfK7TPUn4hZHFcijJ27RVv/AEpG38Kv2zPh/wDG7x5Z6BYWGoW99e7/ACjcWEaRnapcglWJHCntivd4a8T+FM6x0Mtw"
    "dGSnO9uaCtom+jfRHXlPF2V5jiY4TDwlzyva8V0V+7PJf+CpXhbwzov/AAi9/pmn2dhrF408VwbaJYxcxoFIdwAMsGbG7qc9eBj8s+kNkmWYRY"
    "XFYaEY1ajknypK8VbV26pu199fI+S8S8uw9CNGuopVJOWytdK2r+b3Pj8V/MZ+SiY4oAdCnmSgZxk1UI3dhpXZ+jWteHNO/Zy/YRZ5rKzbVLLQ"
    "ADI0Sl1ubjAznGcrJL+lf25icvw/DXALnUpx9pGla9lfnn/9tI/op0IZRw1zyiuaNPt9qen5yPzkflia/iOW5/Or3EqRBQAtABQB9mf8Ervhal"
    "5a+JfE11bwzRb4tPhMqBgMfvJMZ+sdf1J9Hrh2nUpYrMq8VJNqEbrt70vzifsHhjl69nXxc1e9or83+h4D+2R4yg8cftIeKru0SOO0hvDZwqig"
    "LthAiyMdiUJ/GvxfxMzGnjOJMVUopKMZcqtp8Hu/mmz4Pi3GrE5rWqR2Tsv+3dP0PMOlfBnzYoNNAes/sSfDRfib+0h4dtJl3WtjK2oT/LuGIV"
    "LqCPQuEX/gVfpHhRkccz4mw1Korxg+d/8Abuqv5N2TPquCsvWLzijCW0XzP/t3X8XZH3T8av2oPh7+zlqlno3iHS5pby8tvtKLZ2MUm1NzKC2W"
    "XGSp/Kv6w4y404b4cxUMNjqfvSjzWjFPS9tdvM/Zs74iyzK60aOJi7tX0Sel7d0ebS/8FF/hWjMItH1ghjxnSoP/AI5Xy/8AxGvgtf8AMPP5U1"
    "/meK/EDI/+fcv/AAGP/wAkdt8TfC/gz4y/s1azrk+nWf2G40WTUrS4e1WO4tj5RkRlOMq2QOAcHpyDX23FWWZDnXC08c6UVTlSdSMuVKS93mT7"
    "p/8ADHuZrhcuzLKXXlFKLg5J2Sa0uj80ZBhyK/z6lo7H82vcQc/hUgbnw08Hv8QfH+jaJGWEmq3sVopAyV3uFz+Ga9nIMseY5hQwMd6k4x+9pH"
    "dluDeKxVPDR3m0vvZ+mnxb+Lvgn9lTw7p8mvaeTb3jfZbSO1s0lkIReuCRwBgZz1Ir+7+Lc+4e4YwtGeNpaSfKlGKb0Xy0/wAz+jc7zTL8opQn"
    "Xi+VuySSey+XSx5Ncf8ABRj4VzzPnR9ZKnp/xLIM/wDoyvh1428F/wDPif8A4Lj/AJnzj8RMk/59z/8AAY//ACR6b8N/EXgv9ov4ZS39po1tea"
    "XePJbSQX9iisCoGeOQDhhhlPHqCK/SMlr8PcV5S8VQoJ0m3FqUEndb/wDDpn02W1cuzrCutTpXg21aUV/we+6Z+Z/jnTrTSfGeq21g5ksbe8mi"
    "t3JyWjVyFOfpiv8APvO8PQoZhXo4Z3hGclH0TaX4H825hTp08VUp0X7qk0vS+n4GUeteWcY4Rljgd6aTew0rn6Rfsc6LpXwO/ZCsdd1eyjWI2s"
    "2tXzvCruQ2Su0HrmNYwBnrX9veHWV5dkvBcMwx8N4yqS0u9dV/5KlZH9CcL4bDZbkEMViOqc5d9dvwSMu+/wCClXwovZQDo+uLjjjS4B/7Urya"
    "fjNwdHT2E/8AwBf5nL/xELI1pyT/APAV/wDJDb//AIKVfCJLMKuha08i9M6XAP8A2pSl4zcIR19jP5QX+ZjLj7JHvCX/AICv/kj4c+OHxAj+KX"
    "xZ17XoI2ittSvHkt42QIyRZxGpA4BChRx6V/LfF+c081zjEY+jHlhOT5VZK0VpG6XWyV/M/H87x8cZjquJgrRk3bpp0/A5XOM182eUCjPWmgP0"
    "B/4J6eB7H4dfsw3fifVre2ddTuJ74vJGGZLeEbM89v3bn8a/s7wTyPC4PheeZY2Kam5Tu1tGPu/+2tn7nwFg6eGymWLrbSbl8o6fox9h/wAFG/"
    "hOL0vJpOtP6AaZD/8AHKUvGvg2SsqEl/24v8yp8f5K9FGX/gK/zOl+Hf7W/wAPvj144j0TQdOvor2SJ5VW605ERggyeVZscetfUcHeIXC2fY5Z"
    "bhaL9o02uaCtpvrqerknFGV5liVhaMHzNN6xVtPmz5q/4KXeB9C8KfEzRLnSrK00681Sxaa9htoxHGxVyqybQAAzYYHHXbnrk1+E/SCyPLcvzi"
    "jPAwUJVItySVlo7J27vX1sfAeJeW4XC4qlPDpJzi20tNnZO3nr62PmsjjHev5/PzQTGKADpQAuDjocUWAM5FAB3oATHPegBc8fWgBKADPFAAea"
    "ACgBehpAIKYC5zQAlABQAtAABQAlABQAUAFAAelAAeaAFAz3oASgAoAKAAdaAFzQAhOTQACgC3ocNrcazax3s7W1m8yLPKqFzGhI3MAOuBk4rq"
    "wMKM8RCOIlywbXM7Xsr6u3WyNaKg6kVUdo31fl1PrGLxN+ytHAoOla07AYJIucn/x+v6K+u+FS05JfdU/zP0yWI4PW0ZfdL/5IbH4i/ZWdvm0z"
    "XFH0uf8A4up+ueFX8s/uqf5k/WeEP5ZfdL/5Im/4SP8AZSUcaZrR+ouv/i6axnhV/LL7qn+Y1ieEP5X90v8A5IZJ4m/ZVyNul61j6XP/AMXT+u"
    "+FS+xL7qn+YPE8IfyP7pf/ACQkviL9lVlyum60D6AXP/xdJYzwq/kl91T/ADJ+scI/yv7pf/JDV8R/srp10zW2H0uc/wDodW8Z4Ur7Evuqf5ie"
    "I4SW0X90v/kjx79p3WfhhqE+mx/DfTr22iXe15LctJkngIoDsf8AaJPuK/NvEHF8J1I0YcM02n7zm3zLtZJSb82/kfN8Q4jJ5whDK4NPVybv5W"
    "STb8zyavzI+WFJoAm02yk1LUIbeFS8s7iNFHVmJwB+ZrfDUJ1qsaNNXlJpL1eiLpQc5qEd3off37TMq/AH9kGbSbOdA5sYNDjIOA5YBZCPqiyG"
    "v7r8RKkeG/D54Ci9eWNFed9JP7rs/ojiSpHK+G3hoPXlVNfO1/wufn2x3fX+dfwc3fU/nQuaRY39y7PZQ3MjJ1MSMSufpXThaeIlLmw6ba7X/Q"
    "1pRqX5qafyI9Ta6FyY7zzxLHwVlzuX8DU4idZy5azd13v+oqspt2qXv5lauczF6HnmgDt/2dPh6fij8bPDWiGIzQ3t9GJ0H/PJTvk/8cVq+v4G"
    "yX+1c9wuCteMpq/+Fay/BM9nh/A/XMwo4dq6lJX9N3+B9af8FQvHTaN8KNE8Pb9surX5uJEB5MUS8ZHoXdf++a/pn6RmaxoZTh8vg/4krteUF/"
    "m0fr3inj1DB0sNB/G7teUf+C/wPhQ9eK/jc/CAzn3NACYxQAUAKv3hjrTW4H6Jfs9zR/Aj9iNNQKmKePTZ9Yn3cZlkBaMfiPKFf3dwRho8OcAr"
    "HVFyy9nKr85JuP4cqP6G4dpLLeHlXnp7kp/Nq6/RH55ahdSX19LPMzPLM5d2PJZick/nX8K16sqtSVSbu27v1e5/PlSblJye7Ih1rIgAeO1MD6"
    "9/4JVeHG07XPE3iSaEmNIY9NhkI4JdvMfHuNkf/fVf1J9G7h+VWti8zktIpQi/Nvml91o/efrXhfgZOdfFvZJRXzd3+SPUf2mP2Jrb4/fEWXxL"
    "d+I7m0MsEcEdslosghRB0B3DqSx/4FX6Pxn4L0eJMzlmFbFShoklyppJed11u/mfRZ3wM80xbxVSq46JJct7W87/ADOI0T/gmF4fnuU+0eLtS2"
    "KwLItkiFh3AO44+uDXy8fo2YGnJOeMnJdbRiv1Z5S8L6MZLmrN/Jf5s1/25fiZo3wf+B0ngnS7uM3+pQxWMNskoeS2tk25Z8dMqoUZwTuJHQ16"
    "3jRxPl+TcLrh3CSXtJqMFHrGEbXb9UuVX3vpszv44zWhgcpWXUpLnklFLqordvttbXe58Ilt3XrX8TN3PwkKAPev+CdvgqTxB+0HBqXkmSDQbW"
    "S8JxwrkeWn6vn/AIDX7b4CZH9d4ohXl8NGLlfz+Ffnf5H3/hxl/wBYzeNSW1NOXz2X4s+s/wBpr9mW2/aiu9Ilv9futJTR4XjSGK1EodnYEuSW"
    "HYKPwr+mfEPwvp8T1aUqtdwjBOyST1drvVrsfqHFPCjzacHKo4xjeytfV9d/JHk6/wDBLfRY2Bbxff4Ppp6E/wDodfm//EteDWv1yX/gC/8Akj"
    "5aPhZDf27/APAV/mek/FDUPDP7Gv7NB0yw1H/SoLSWDT45HH2m8uJN2ZCo7AsWJ6AKBnOM/bZ1mGWcB8JywFGfvqMlBO3NKUr62XZu7eySsfTY"
    "vF4Xh3J3QhL3kmorrKTvr8r3vtZWPzikcs5JOfrX8Fyd3dn86N3Y2kI0/Bnh648XeK9N0m1Utcalcx2sQHdnYKP516OUYCpjcbSwdL4qkoxXrJ"
    "pfqdODw8q9eFCG8mkvm7H6l+PvH3w++HPwustG8az2lt4fmij02OGeB5kmEaghSqKTgBAemOlf6EcTV8jybKaeEzlxVCaUEmm07Layv2P6Szmr"
    "gMHg40ce0qbSjazd7Ly9DyPUPiV+yrY2/wC7sfDc0h/6gtz/APGq/KP7W8LIu84UkvKnJ/8Atp8asdwcn70Y/wDgEv8AI+fP2xfG3wu8R6PpNt"
    "8PNO0uK4895buW1spLcqgUBVy6jOSxPH90V+XeK2bcG4jDUKHC9OPNzNzkoOLtayWqV7t307Hy3GOLyGpRp08ngr3bk0mtLaLU8Axivw4/Pw60"
    "ASW8RmmVQCSxAAHU1pSi5SUUVBXaSP060X4Ozj9nuDwKLyXTopNJTT5riNAzLlAspAOB82X/AO+q/wBHqPB8P9UYcPqo4P2Sg5JJvVWlp56/ef"
    "0y8nvk8cshLlfIotrXda/fqePw/wDBLvw5HcYk8W6qnv8AY0x/6FX47D6N2Ab0xc2/8Mf8z4heGVG/8Zv5I9D+BH7Mvg79k7W77X5Ndlu5Zbc2"
    "5ur4x28NtESGbHuSq8k9sY5r7bhDwxyng+rUzKriG5OLV5uMUldN/ktT28l4ZwmSTlipVNWrXlZJLdnxp+2X8X7X40/HbVNV013fSYFSzsS2fn"
    "jjHLgHkBnLsPZhX8n+KfFNPPeIKuLw7vTVox9I9fRttryPyfi7N4ZjmMq1L4FZL0XX5u7+Z5V1r85PmBMUAdv+z/N4NtviJDN46FxJoMcMheKF"
    "XLSuRhRlSCAM5/4DjvX2nAn9gLMubiNtUFF6JN3lsl7uvd/I9zh95csVzZpf2aT0Ser6bff8j3uTxD+zChyNL1hlPoLkf+zV+0rF+EyXwy+6p/"
    "mfWyxHCiWkX90v/kirc+IP2aGYmPTtYA9MXHH/AI/QsZ4R9YT+6p/mZfWOFv5X90v/AJIgk179m8A7dP1Yn3Fx/wDFVtHGeEHWE/uqf5mUq/DP"
    "2Yv/AMm/zKs2v/s8Mfk07VR/3/8A/iq1WN8HesJ/dV/zOaVfh/pF/j/mUrrXfgHn93Y6oPqJ/wD4qtljvBn7VOf3Vf8AM5ZVsk6Rf4/5lWTWfg"
    "Wgytlqje2Jv/iqf17wX/kqfdU/zM/aZNfZ/j/meK+ObvTL7xbfSaNA1tpZlIto2JLBBwCcknJ69e9fzjxXiMsr5tXqZNDkw7k+RO9+VaJu93rv"
    "bpex4GMnSlWk6CtHp6GTXzxzAeDQAE0AFAC4NFwEoAKAD6UAFAAOtAAaACgAoAKAFPBoASgAxxQAUAHWgAoAKAFAz7UAIaADFAB0oAKACgAzQA"
    "p/lQAbjjHagBMflQAZoAczZFAkdB8JPEVp4S+Jvh/U79DJZafqMFxMuM/IkisePoK+j4RzDDYHOsLi8Wv3cKkJS66KSb/A9TJcTSw+Po16/wAM"
    "Zxb9E02fod4o8PfD/wDaL8GWtprOs6VqOktKt1E8GoiN43AKgggjnDEEEd+ma/vriejwtxbl0KWJqxnTupRamlrZq+j7N7n9DZvHKM4w8Y1ZqU"
    "L3VpJa/ecXqf7EPwSsyCl9K49taTNfnMfBzgZ/FUt/3FR8v/qbw+3rK3/b6Ot8Lat8Jv2S/h9fNp9/YWMUxM8gF2Lm8vXUcKozknsAMAZPTJNf"
    "TYR8HcE5dUlQqRUXq/eUpyaWiWt35LZavuerQhkeS4aThNKL1+JSk35f1ZH56/FDxxP8SviJrOvXAKy6tdyXO0tu8sMxKrn0VcD8K/hziPOJ5t"
    "mdfMamjqScrdk3ovkrI/CM0x0sZi6mKlo5tv08vlsYArxTgDFAH0f/AME04rTSvjNfa9f3VlbRaVYMkRuJljzLKwUYyeflD/nX734AYLDvOquO"
    "xdSMY04WV2l70nZWv5J/ej9C8OqFJ4+detJRUY9Wlq9Ovlc+q/jf8IPhh+0Ff2mpeI9Xtpruyg+zwrBqqxKF3FugPXJ6+wr+huK+FeF+Iq8K+Z"
    "VFLkVlaokrXvsnY/Q84yzKsxqRnipJ8qsrTS637nnNv+xl8ExPtkusLnqNbX/GvmP+IQcApaVE/wDuL/wTy1wjw7f4l/4H/wAE+Yv2w/BPg74d"
    "/FYaR4LMklhbWkbTyNdC4DysS3DDjAXb+Oa/nHxTybJMqzaOCyTWKgnJ83N7zu9H5Kx+c8X4HL8HjFh8v1Siru99Xfr6WPKO9fmZ8oKD2oA1PB"
    "Hh/wD4SvxfpmmiSOH7fdRW5kkYKqB2CkknoBnNepkmA+u4+jhLpc8oxu3ZatLc68BhvrGJp0E7czS10Wrsfan7c/xV0rw7+zivh/Sb7T7h9UuI"
    "bTy7aZXMcEXz9AeBlEH41/YfjfxFgsNwrHLsFOL55RjaLTtGK5ns9tEvmftPHeZUKGVfVMPJPmcY6NPRa9PRHwu53Nmv4oZ+FiZoAOtCA+8v2P"
    "PE2ifCL9kprq61PTReypdas9ubhBLkKdq4znJWNePev7d8JsZgch4M+tVasVNqdVrmV/JWve9or7z904Oq0MvyZVpzV3zTtdX8la/ZHxnqHxb8"
    "R6jqFxNJrusF55GkO28kABJycDPHWv5GxHFmb1KkpvE1Pebek5W1d+5+PVM4xrm5e1lq7/E/8ynJ8RNfYn/ieaxz/wBPkn+Ncj4izT/oJqf+By"
    "/zMf7Sxf8Az9l/4E/8zLur2a+laSaR5ZHOWZzuZj7mvLrVqlWXPVk231erOSc5TfNJ3ZDWRIA4NAH2l/wTGXSPBngnxHreo6lp1tPqdylokU9w"
    "kbiOJdxOCc4LSY/4DX9bfR7w+FwmX4jMK1SKlUkopNpaQV+rvq5fgfsnhrh6dPDVcTKcU5vlV2lsr9e7f4Hz1+0D8ctX8Z/GbxHqNhrOow2Et9"
    "IlqkN06IIkOxMAHHKqDX4hx3xnj8bn2KxGHxE1BzailNpWTsrJO2qVz4DPs7xNbMK1SnVly8ztZu1loupx7/ErxBIoB1zWcf8AX7Jz/wCPV8nL"
    "iTNWrPE1P/A5f5nkvNcY1Z1Zf+BP/MzNR1m61aYyXVzPdSHgvK5dvzPNeZiMVWry5683J9223+JyVK06jvUbb8yqeTXOZi0AexfsJaJY6n+0bo"
    "91qVxb21npKyXzPNKsYLKpVACe+9lP4V+r+DGBo1+J6NXENKNJOerS1Sst/Np/I+x4FwsKubQnUaUYJyu2ltot/No+2/jh8Kfh38dLfTjr2tW8"
    "qacXaGO31RIly+3JIzyflH61/WvF3D/DvE3slmdbSne1qiS1te+vkfr+dYHLMzcFiqiahe1ppb2/yODX9iv4ITwEPfmNvU60v+NfFS8IOBuk7/"
    "8AcZf5nz0uFOHr6P8A8nR4j+2b8Evhv8GvB2lP4SuHutVvrsrIf7QW4VIVQ7vlHQlmTn2NflHi1wXwzkWAozymTdWc2vj5vdS109Wj5Li3Jcrw"
    "GGhLByvKT/mvZJf52PmxjuYmv5/Pz0SgDuf2bdCt/EHxx8NQ3clvFZx3qT3DTuEj2R/OQSeOduPxr7rw1y6njeJcHSrNKCmpSu0laPvPfTW1j3"
    "uGMNGvmdGFRpR5k3fTRa/ofRX/AAUX+OMd/pfhzRdK1SOVjJJe3DWlxuGANiAlT7vx7V++/SD4rpxo4XAZfW1blOXLLotI6p9bv7j9A8Rs1iqV"
    "Ghhp6ttuz6bLb5nz/wDBD4+ar8Kvifp2sSXd3c2aP5V3A0hbzYG4cAE4zjke6ivwbgjjrG5HnVHMJVJSgnaSbbvF/EtXvbVeaR8FkOf18BjqeJ"
    "lJtJ6q+6e/9dz6u/bQ8J+HfjV8GodU0nW9Im1bSE+2WeLuMNdxMAXjIznOMMo9RgD5jX9Q+MWV5dxLkscbga0HWprmj7y96LV3Hfe2q81bqfqH"
    "GGDw2a4NYjD1IucFde8tV1W+/Vfd1PhB2LE5NfxB5H4extAgzQApoAN5K47UAJigAoAUfnQAlABQAUALnBoASgBew9KAEoAMUAFABQAUALxQAl"
    "ABQAdKAA0AFABQAYzQAUAL35oASgA60AFABQAZ49qACgAoAKAA8mgAoAMYoADQAGgAoAB1oAKAHmdiMZOKrnY+Zh57FQNxo5mPmYhlZhgkkUuZ"
    "i5mNNIQUALyBQAquVPBxmmm1sO4ouHH8Rp8zDmYnnMT940czDmYjMWOSSTSbuDdxKQgoAUdcjtQArSM45JNNyb3G23uNpCAdaADvQA4SMBjJx6"
    "U+Z2sO7Gg4pCFwOaAE7UAFABQA5ZWUcMRTUmthqTQ0nmkIBQAuKAE70ALnmgABKngmmnYL2Hm5cLjJ/OnzMfMxPObHLH86XMwuxGlZupzQ23uF"
    "2IKQhKAFUkHjrRewAzFuScn3ptt7jbb3EJzSEO81guMnFPmY+ZjaQhc0AGf1oASgAxQAUAFAC9DQAA8dKAEoAKAFHJoASgAoAKAAUAHSgAxQAU"
    "AHT8aACgAoAKACgAoAOlABQAUAFABQAUAL2oASgAoAKACgAoAKACgANABQApFABmgBKAAGgAoAKAAUAHegAFAB1oAKACgAoAKACgAHegANABmg"
    "BT165oATv6UAFABQAUAFAC/nQAZ4oASgAHrQAE0AFABmgA60AL2+lIA7UwDjHWgBO1AB0oADQACgBSM0AIRzQAUAGMUABoACKACgAzQApwPWgB"
    "O1ABQAUABGKADvQAUAHagBc8UAIOtAC96AENABQAvagBM0AFABQAdaAA0AFABQAUAFAB2oAKADvQAvWgBKACgAoAWgBO1AB1oAD1oAKACgBR0N"
    "ACdqACgAoAXP4UAJQAYoAKAACgBR1oAOn1oASgAoAO9ABQACgAFACn+dACUAFABQAUAFAAaAF7UAJQAUAKOfrQAY4zQAnSgAoABQADmgAxgUAL"
    "QAlAAKACgAFABQAE0AAoAKAFPBoASgAxg0AFABjH4UAFABQAuc0AJQAdaACgAoAUHHvQAEY/GkAnWmAUAHegA7UAGaAADNABQAUAFAC9OtACDi"
    "gA6UAKOvpQAAUAJQAucDFACUAFAC547UAJQAUAFABnFABQAUAGaAAUAAGTQAUALQAhoAKACgAoABQAGgANAARigANAC5/SgBKADGDSADzTAKAA"
    "0AKee9ACY4oAKAAGgAJzQAUAHegAoAOlAB1oAKACgAoAKACgA7UAL1NACUAFABQAd6ACgAoAKAA0AFABQAUAFABQAUAHWgAoAO9ABmgAHBoAKA"
    "CgA60AFAC9R9KAE7UAFAC9frQAmeKACgA70ABFAB0oAKACgA7UAKKAEPJoAMUAGc0AFABQAUAFABjNABQAUAFABQAHrQAUAFAARzxQAUAFABQA"
    "E5oAXH5UAJQAUAFABQAE8UAKTn1oASgA6UAFABjJoAXFACUAGeKADrQADmgAzxQAdqAAGgAoAKADNABigAoAMYoABQAUAHvQAdKAAcUAHWgAxx"
    "QAHpQAvINACUAFABQAUAB4NAC9aAE6UAFAAKADNABjigA70AB+tABQACgANABmgBc0AGc/jQAmaADrQAdqAAjFAAaACgAoAM8UAFAAaACgAoAO"
    "lAB17UABoAKAA8UAB5oAO9ABQAUAFABQAUAFAC4oASgAoAKAFx70AJ3oAKACgBRyeaAEoAKAAc0AL2oAToaACgANAAKAF6e9ACGgBTwaAEoAKA"
    "CgAAoAD1oAO9ABQAUAFABQAozn3oAAcc0AGaAA0AJQAUABoAM0AFABQAp60AB4FACCgAoAKAFGM80AIaAAdaACgAoAKACgBccUAHUUAJigAPNA"
    "BQAZ4oAKAA0AKck0AJQAUAFAB0oAUEDtmkAmaYADigAAzQAUAKBmgBBQAUAFAB2oAKADqaACgAPSgAoAKACgAzigA60AAoAM4oAUjFACUAHagA"
    "xQAUABNABQAUALn9aQCUwCgAHWgAoAUDJoASgA6HmgANAAKADvQAdTQAo9qACgBBzQAvU98UAAGTQAgoAAM0AFABmgA78UAFAC/WgBKAA0AFAA"
    "aACgA/pQAUAFAC9TQAmcZoAKAF6c0gENMANAB2oAKAFFACUAFABQAUAGKAF3YFKwCUwA80AHagAoAMY60ABOaAAcUAHWgAoADQAUAKR75oASgA"
    "oADQAdqAFHINJgJ1NMBcYoASgAoAKAFI4oAQ0AFABQAUAHU0AFAADz2oAKADHNAB1NABigA60AFABQAUAFABQAUAFABQAo4oAQHFABQApNABig"
    "BKADNABmgA60AFABQAGgAoAO9ABQAAcUAHagAoAOKAFHFAADj0oASgAoAKADNAAKAFPBoAM/SgBM8UAGKACgAoAKACgABxQAUAFAC9aAEoAUni"
    "gBKACgA7+tAB3oAKAFPA7UAJ1oAKAF6CgBKADtQAEYNABQAUABx2oAM8UAFABQAUAA60AKenagA28UAJQAUAFABQAUAGMmgA6UAL1FACUAFABi"
    "gAJzQAdTQAUAFABigA6UAFABQAdqAAcdKACgA7UALmgAHSkAhpgFABQAdDQAdKACgAoAXHNACDrQAuKAExigA6igAHWgAoAOnFABQAZ5oAXnGa"
    "AEoAOtAAPegAoAXP60AJ1oACMUAKOSKAEPWgAAzQAdqADvQAGgBT0oAQnmgAoAXtQAnX1oAUgigBBQAYoAU9aAE7UAGaACgBQM0AJQAdTQAdaA"
    "F7UAJQAUALjNACdB9aACgAoAO9ABQAvakAlMAz7UAFABnFABQAUAGOKACgAHJoAXHFACUAFAAeDQAZ5oAKAADNABQAUAAOKAFY80kAMNpoTAQ8"
    "E0wCgAxQAUAFABng+9AAOTQAUAAGaAAc0ALu5oASgAoACMGgAoAKACgAoAKACgBQMmkwEPBpgA5NACkY/CkgDFMAIwaAEoAM8UAFAAOTQAZoAK"
    "AAHmgAHJoADQAdfwoAKAFPQUgEpgKRikAlMAzQAvUUAIaACgAoAM0AGeKADPFACqOfpSYCdqYCj7tIBKYH/9k="
)


def _logo_src():
    from PIL import Image
    if LOGO_SRC.exists():
        return Image.open(LOGO_SRC).convert("RGB")
    import base64
    import io
    return Image.open(io.BytesIO(base64.b64decode(LOGO_JPG))).convert("RGB")
LOGO_HUE = {"": 0, "US": -55, "DOTA": 120}           # сдвиг оттенка от фиолетового: US синий, DOTA оранжевый


def _hue(im, deg):
    if not deg:
        return im
    from PIL import Image
    h, s, v = im.convert("HSV").split()
    h = h.point(lambda x: (x + int(deg * 256 / 360)) % 256)
    return Image.merge("HSV", (h, s, v)).convert("RGB")


def wordmark(profile):
    """Надпись PRODAEM CLIPPER из логотипа, в цвете профиля. None, если файла логотипа нет."""
    import numpy as np
    from PIL import Image
    rgb = _hue(_logo_src().crop((50, 405, 620, 520)), LOGO_HUE.get((profile or "").upper(), 0))
    a = np.asarray(rgb).astype(float)
    alpha = a.max(axis=2, keepdims=True)                 # чёрный фон становится прозрачным, свечение остаётся
    col = np.where(alpha > 0, a * 255.0 / np.maximum(alpha, 1), 0)
    return Image.fromarray(np.concatenate([col, alpha], axis=2).clip(0, 255).astype("uint8"), "RGBA")


def draw_logo(profile, size=512):
    """Значок: знак P с «плеем» и ножницами на тёмной плитке. Цвет свой у каждого профиля."""
    from PIL import Image, ImageChops, ImageDraw
    if True:
        n = size * 4
        mark = _hue(_logo_src().crop((165, 60, 515, 410)), LOGO_HUE.get((profile or "").upper(), 0))
        mark = mark.resize((int(n * .98), int(n * .98)), Image.LANCZOS)
        layer = Image.new("RGB", (n, n), (0, 0, 0))
        layer.paste(mark, ((n - mark.width) // 2, (n - mark.height) // 2))
        tile = ImageChops.lighter(Image.new("RGB", (n, n), (13, 13, 19)), layer)   # чёрный фон знака исчезает
        rnd = Image.new("L", (n, n), 0)
        ImageDraw.Draw(rnd).rounded_rectangle((0, 0, n - 1, n - 1), int(n * .22), fill=255)
        out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        out.paste(tile, (0, 0), rnd)
        edge = Image.new("L", (n, n), 0)
        ImageDraw.Draw(edge).rounded_rectangle((4, 4, n - 5, n - 5), int(n * .22), outline=40, width=8)
        out.paste((255, 255, 255, 255), (0, 0), edge)
        return out.resize((size, size), Image.LANCZOS)
    return draw_logo_old(profile, size)


def draw_logo_old(profile, size=512):
    """Прежний логотип: тёмная плитка и светящийся знак «плей с разрезом»."""
    from PIL import Image, ImageDraw, ImageFilter
    k = 4
    n = size * k
    c1, c2, glow = PROFILE_COLORS.get((profile or "").upper(), ((40, 200, 120), (0, 120, 255), (20, 160, 90)))

    def pt(*v):
        return [x * n / 512 for x in v]

    g = Image.linear_gradient("L").resize((n * 2, n * 2)).rotate(-45)
    g = g.crop((n // 2, n // 2, n // 2 + n, n // 2 + n))
    grad = Image.composite(Image.new("RGB", (n, n), c2), Image.new("RGB", (n, n), c1), g)
    tile = Image.new("RGBA", (n, n), (14, 15, 20, 255))
    halo = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse(pt(96, 96, 416, 416), fill=glow + (150,))
    tile.alpha_composite(halo.filter(ImageFilter.GaussianBlur(70 * n / 512)))
    mark = Image.new("L", (n, n), 0)
    dm = ImageDraw.Draw(mark)
    dm.polygon([tuple(pt(176, 128)), tuple(pt(176, 384)), tuple(pt(392, 256))], fill=255)
    dm.line([tuple(pt(110, 380)), tuple(pt(410, 132))], fill=0, width=int(26 * n / 512))
    tile.paste(grad, (0, 0), mark)
    rnd = Image.new("L", (n, n), 0)
    ImageDraw.Draw(rnd).rounded_rectangle((0, 0, n - 1, n - 1), int(112 * n / 512), fill=255)
    out = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    out.paste(tile, (0, 0), rnd)
    return out.resize((size, size), Image.LANCZOS)


def make_assets():
    """Раскладывает логотип в файлы: значок окна и картинку для шапки. Возвращает (ico, png) или (None, None).
    У каждого профиля свой файл значка, чтобы ярлыки на рабочем столе различались."""
    try:
        from PIL import Image
        m.CACHE.mkdir(exist_ok=True)
        tag = "_v5" + (("_" + PROFILE.lower()) if PROFILE else "")   # v3: знак P с ножницами
        ico, png = BASE / f"clipper{tag}.ico", m.CACHE / f"logo_44{tag}.png"
        if not ico.exists() or not png.exists():
            im = draw_logo(PROFILE)
            im.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            im.resize((44, 44), Image.LANCZOS).save(png)
        return ico, png
    except Exception:
        return None, None


def make_shortcut(ico):
    """Один раз кладёт на рабочий стол ярлык с логотипом (только Windows)."""
    if sys.platform != "win32" or not ico:
        return
    flag = BASE / "cache" / f"shortcut_done_{Path(ico).stem}.txt"      # новый значок: ярлык пересоздаётся
    if flag.exists():
        return
    target, args = sys.executable, '"' + str(CODE / "review.py") + '"'
    pyw = CODE / "python" / "pythonw.exe"
    if (CODE / "launch.pyw").exists() and pyw.exists():      # готовая сборка: запуск без чёрного окна
        target, args = str(pyw), '"' + str(CODE / "launch.pyw") + '"'
    ps = ("$ErrorActionPreference='Stop';$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '" + APP_NAME + ".lnk'));"
          "$s.TargetPath='" + target.replace("'", "''") + "';"
          "$s.Arguments='" + args.replace("'", "''") + "';"
          "$s.WorkingDirectory='" + str(CODE).replace("'", "''") + "';"
          "$s.IconLocation='" + str(ico).replace("'", "''") + "';"
          "$s.Save();"
          "if (Test-Path (Join-Path $d '" + APP_NAME + ".lnk')) { 'CREATED' }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, timeout=30, text=True, errors="replace")
        if "CREATED" in (r.stdout or ""):            # отметку ставим, только если ярлык действительно появился
            flag.write_text("ok", encoding="utf-8")
            return f"На рабочем столе создан ярлык «{APP_NAME}»."
        return "Ярлык на рабочем столе не создался: " + ((r.stderr or r.stdout or "причина неизвестна").strip()[:200])
    except Exception as err:
        return f"Ярлык на рабочем столе не создался: {err}"


# ---------- основное окно: тёмная тема ----------
BG, CARD, LINE = "#0b0c0f", "#13151a", "#252832"
TXT, MUTED, ACC, ACC_H = "#f2f3f5", "#8b90a0", "#7c5cff", "#6848f0"
BTN, BTN_H, RED, HINT = "#1d2027", "#2a2e38", "#ff5d5d", "#9db2ff"


def gui():
    import tkinter as tk
    from tkinter import filedialog
    import customtkinter as ctk

    cfg = json.loads((BASE / "montage.json").read_text(encoding="utf-8"))
    enc = m.pick_encoder(cfg)
    st = load_state()
    ctk.set_appearance_mode("dark")
    try:                                             # размеры в пикселях: кадр и ползунки совпадают по ширине
        ctk.deactivate_automatic_dpi_awareness()
    except Exception:
        pass
    root = ctk.CTk(fg_color=BG)
    root.title(APP_NAME)
    ico, logo_png = make_assets()
    if ico:
        try:
            root.iconbitmap(str(ico))                # значок в заголовке окна и в панели задач
        except Exception:
            pass
    shortcut_msg = make_shortcut(ico) if ico else "Ярлык не создан: не получилось подготовить значок программы."
    scr_h = root.winfo_screenheight()
    VW = 760 if scr_h >= 1000 else 640 if scr_h >= 860 else 560     # ширина кадра в окне: под высоту экрана
    root.geometry(f"{VW + 760}x{min(scr_h - 80, 980)}")
    logq, jobs = queue.Queue(), queue.Queue()
    S = {"items": [], "cur": None, "info": None, "scale": 1.0, "rects": [], "drag": None, "img": None,
         "busy": None, "done": 0, "failed": 0, "cap": None, "capmode": False,
         "pv_img": None, "pv_busy": False, "pv_dirty": False, "content": None, "contmode": False}

    def F(size=13, bold=False):
        return ctk.CTkFont(family="Segoe UI", size=size, weight="bold" if bold else "normal")

    def card(parent, title):
        fr = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        ctk.CTkLabel(fr, text=title, font=F(15, True), text_color=TXT, anchor="w").pack(fill="x", padx=18, pady=(14, 6))
        return fr

    def button(parent, text, command, primary=False, width=150, height=36):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=height, corner_radius=10,
                             font=F(13, primary), text_color=TXT,
                             fg_color=ACC if primary else BTN, hover_color=ACC_H if primary else BTN_H,
                             border_width=0 if primary else 1, border_color=LINE)

    root.grid_columnconfigure(1, weight=1)
    root.grid_rowconfigure(1, weight=1)

    # --- шапка ---
    top = ctk.CTkFrame(root, fg_color=BG)
    top.grid(row=0, column=0, columnspan=3, sticky="ew", padx=18, pady=(12, 4))
    if logo_png:
        try:
            S["logo"] = tk.PhotoImage(file=str(logo_png))
            tk.Label(top, image=S["logo"], bg=BG, bd=0).pack(side="left", padx=(0, 12))
        except Exception:
            pass
    ctk.CTkLabel(top, text="PRODAEM", font=F(26, True), text_color=TXT).pack(side="left")
    ctk.CTkLabel(top, text=" CLIPPER" + (" · " + PROFILE if PROFILE else ""), font=F(26, True), text_color=ACC).pack(side="left")
    ctk.CTkLabel(top, text="     выбери клип  →  обведи камеру  →  выбери формат  →  смонтируй",
                 font=F(13), text_color=MUTED).pack(side="left", pady=(8, 0))
    chips = {}
    for key, label in (("failed", "С ошибкой"), ("done", "Готово"), ("wait", "Ждут монтажа"), ("busy", "Монтируется")):
        chips[key] = ctk.CTkLabel(top, text=f"{label}: 0", font=F(12), text_color=MUTED, fg_color=CARD,
                                  corner_radius=10, height=30)
        chips[key].pack(side="right", padx=(8, 0), ipadx=10)

    # --- слева: очередь ---
    left = card(root, "01 · Очередь клипов")
    left.grid(row=1, column=0, rowspan=2, sticky="ns", padx=(18, 8), pady=(6, 16))
    count = ctk.CTkLabel(left, text="", font=F(12), text_color=MUTED, anchor="w")
    count.pack(fill="x", padx=18)
    lwrap = ctk.CTkFrame(left, fg_color=CARD)
    lwrap.pack(fill="both", expand=True, padx=(12, 6), pady=6)
    lb = tk.Listbox(lwrap, width=40, exportselection=False, bg=CARD, fg="#d7dae2", font=("Consolas", 10),
                    selectbackground=ACC, selectforeground="white", borderwidth=0, highlightthickness=0,
                    activestyle="none")
    sb = ctk.CTkScrollbar(lwrap, command=lb.yview, fg_color=CARD)
    lb.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    lb.pack(side="left", fill="both", expand=True)

    # --- справа: пример и субтитры ---
    right_wrap = ctk.CTkScrollableFrame(root, fg_color=BG, corner_radius=0, width=312)   # на низком экране прокручивается
    right_wrap.grid(row=1, column=2, rowspan=2, sticky="ns", padx=(8, 6), pady=(6, 12))
    right = card(right_wrap, "04 · Как получится")
    right.pack(fill="x")
    pvc = tk.Canvas(right, width=270, height=480, bg="#000000", highlightthickness=0)
    pvc.pack(padx=18)
    ctk.CTkLabel(right, text="Белая рамка: сюда встанет баннер.\nСубтитры показаны для примера.", font=F(12),
                 text_color=MUTED, justify="left", anchor="w").pack(fill="x", padx=18, pady=(6, 0))
    pv_btn = button(right, "Обновить пример", lambda: preview_now(), width=270)
    pv_btn.pack(padx=18, pady=(8, 0))
    sub_title = ctk.CTkLabel(right, text="Положение субтитров", font=F(14, True), text_color=TXT, anchor="w")
    sub_title.pack(fill="x", padx=18, pady=(16, 2))
    sub_auto = tk.BooleanVar(value=True)
    ctk.CTkCheckBox(right, text="Авто, как задумано в формате", variable=sub_auto, command=lambda: on_sub(),
                    font=F(12), text_color=TXT, fg_color=ACC, hover_color=ACC_H, border_color=LINE,
                    checkbox_width=20, checkbox_height=20).pack(anchor="w", padx=18, pady=(2, 6))
    sub_pos = ctk.CTkSlider(right, from_=15, to=92, number_of_steps=77, width=270, progress_color=ACC,
                            button_color=ACC, button_hover_color=ACC_H, fg_color=BTN)
    sub_pos.set(70)
    sub_pos.pack(padx=18)
    sub_val = ctk.CTkLabel(right, text="высота: 70% от верха экрана", font=F(12), text_color=MUTED, anchor="w")
    sub_val.pack(fill="x", padx=18, pady=(2, 14))

    # --- центр ---
    mid = ctk.CTkScrollableFrame(root, fg_color=BG, corner_radius=0)      # если экран низкий, середина прокручивается колесом
    mid.grid(row=1, column=1, sticky="nsew", pady=(6, 4))
    c_frame = card(mid, "02 · Кадр: обведи камеру стримера")
    c_frame.pack(fill="x")
    head = ctk.CTkLabel(c_frame, text="Выбери клип слева", font=F(13, True), text_color=TXT, anchor="w",
                        justify="left", wraplength=VW)
    head.pack(fill="x", padx=18)
    canvas = tk.Canvas(c_frame, width=VW, height=int(VW * 9 / 16), bg="#000000", highlightthickness=0, cursor="crosshair")
    canvas.pack(padx=18, pady=(6, 4))
    pos = ctk.CTkSlider(c_frame, from_=0, to=100, number_of_steps=100, width=VW, progress_color=ACC,
                        button_color=ACC, button_hover_color=ACC_H, fg_color=BTN)
    pos.set(40)
    pos.pack(padx=18)
    tlabel = ctk.CTkLabel(c_frame, text="0:00 / 0:00", font=("Consolas", 12), text_color=MUTED, anchor="e")
    tlabel.pack(fill="x", padx=18)

    def upd_time():
        if S["info"]:
            d_ = S["info"]["dur"]
            t_ = d_ * pos.get() / 100
            tlabel.configure(text=f"{int(t_ // 60)}:{int(t_ % 60):02d} / {int(d_ // 60)}:{int(d_ % 60):02d}")
    hint = ctk.CTkLabel(c_frame, text="", font=F(12), text_color=HINT, anchor="w", justify="left", wraplength=VW)
    hint.pack(fill="x", padx=18, pady=(4, 0))
    tools = ctk.CTkFrame(c_frame, fg_color=CARD)
    tools.pack(fill="x", padx=18, pady=(6, 14))

    c_fmt = card(mid, "03 · Формат и монтаж")
    c_fmt.pack(fill="x", pady=(10, 0))
    body = ctk.CTkFrame(c_fmt, fg_color=CARD)
    body.pack(fill="x", padx=18, pady=(0, 14))
    fmt = tk.StringVar(value="talk")
    for i, (key, label, _) in enumerate(FORMATS):
        ctk.CTkRadioButton(body, text=label, variable=fmt, value=key, command=lambda: on_fmt(), font=F(13),
                           text_color=TXT, fg_color=ACC, hover_color=ACC_H, border_color="#3a3f4b",
                           radiobutton_width=18, radiobutton_height=18).grid(row=i % 3, column=i // 3, sticky="w",
                                                                            padx=(0, 22), pady=4)
    ctk.CTkLabel(body, text="Ник стримера", font=F(12), text_color=MUTED).grid(row=3, column=0, sticky="w", pady=(10, 0))
    ctk.CTkLabel(body, text="Заголовок (только для своих видео)", font=F(12), text_color=MUTED).grid(
        row=3, column=1, sticky="w", pady=(10, 0))
    e_streamer = ctk.CTkEntry(body, width=220, height=34, corner_radius=10, fg_color=BTN, border_color=LINE,
                              text_color=TXT, font=F(13))
    e_streamer.grid(row=4, column=0, sticky="w", padx=(0, 22))
    e_title = ctk.CTkEntry(body, width=360, height=34, corner_radius=10, fg_color=BTN, border_color=LINE,
                           text_color=TXT, font=F(13))
    e_title.grid(row=4, column=1, sticky="w")
    STYLE_LABELS = ["Случайный"] + list(m.STYLES.values())
    STYLE_KEYS = dict(zip(STYLE_LABELS, ["random"] + list(m.STYLES)))
    FONT_AUTO = "Как в настройках"

    def menu(values, command):
        return ctk.CTkOptionMenu(body, values=values, command=command, width=220, height=34, corner_radius=10,
                                 fg_color=BTN, button_color=BTN_H, button_hover_color=ACC, text_color=TXT,
                                 font=F(13), dropdown_font=F(13), dropdown_fg_color=CARD, dropdown_text_color=TXT,
                                 dropdown_hover_color=BTN_H)

    def cur_style():
        return STYLE_KEYS.get(style_menu.get(), "classic")

    def cur_font():
        return None if font_menu.get() == FONT_AUTO else font_menu.get()

    def on_look(_=None):
        st["style"], st["font"] = style_menu.get(), font_menu.get()
        save_state(st)
        preview_now()

    ctk.CTkLabel(body, text="Стиль монтажа", font=F(12), text_color=MUTED).grid(row=5, column=0, sticky="w", pady=(10, 0))
    ctk.CTkLabel(body, text="Шрифт субтитров", font=F(12), text_color=MUTED).grid(row=5, column=1, sticky="w", pady=(10, 0))
    style_menu = menu(STYLE_LABELS, on_look)
    style_menu.grid(row=6, column=0, sticky="w", padx=(0, 22))
    style_menu.set(st.get("style") if st.get("style") in STYLE_LABELS else "Рамка")
    font_menu = menu([FONT_AUTO] + m.FONTS, on_look)
    font_menu.grid(row=6, column=1, sticky="w")
    font_menu.set(st.get("font") if st.get("font") in [FONT_AUTO] + m.FONTS else FONT_AUTO)
    style_note = ctk.CTkLabel(body, text="", font=F(12), text_color=HINT, anchor="w", justify="left")
    style_note.grid(row=7, column=0, columnspan=2, sticky="w")
    ctk.CTkLabel(body, text="Баннер оффера", font=F(12), text_color=MUTED).grid(row=8, column=0, sticky="w", pady=(10, 0))

    def banner_names():
        return list(m.banner_list(cfg)) or ["нет баннеров"]

    def on_banner(v):
        if v in m.banner_list(cfg):
            save_banners(cfg, m.banner_list(cfg), v)  # выбранный становится основным и для автомонтажа

    def banners_changed():
        names = banner_names()
        banner_menu.configure(values=names)
        banner_menu.set(cfg.get("banner_use") if cfg.get("banner_use") in names else names[0])

    banner_menu = menu(banner_names(), on_banner)
    banner_menu.grid(row=9, column=0, sticky="w", padx=(0, 22))
    banners_changed()
    ctk.CTkButton(body, text="Баннеры офферов…", width=220, height=34, corner_radius=10, fg_color=BTN,
                  hover_color=BTN_H, text_color=TXT, font=F(13),
                  command=lambda: banners_dialog(root, cfg, banners_changed, say)).grid(row=9, column=1, sticky="w")
    acts = ctk.CTkFrame(root, fg_color=BG)           # главные кнопки всегда на виду, под серединой
    acts.grid(row=2, column=1, sticky="w", padx=6, pady=(4, 12))

    c_log = card(mid, "Журнал")
    c_log.pack(fill="x", pady=(10, 0))
    logbox = ctk.CTkTextbox(c_log, height=120, fg_color=CARD, text_color="#c5c9d3", font=("Consolas", 12),
                            border_width=0, state="disabled")
    logbox.pack(fill="both", expand=True, padx=12, pady=(0, 10))

    class Out:
        def write(self, s):
            logq.put(s)

        def flush(self):
            pass

    def say(text):
        logq.put(text + "\n")

    def refresh(keep=None):
        S["items"] = load_queue(st)
        lb.delete(0, "end")
        for p in S["items"]:
            tag = "СВОЁ" if p.get("own") else p.get("streamer", "")
            lb.insert("end", f" {tag[:13]:13s}{int(p.get('twitch_views', 0)):>5d} {int(p.get('duration') or 0):>3d}с  {p.get('title', '')[:24]}")
        count.configure(text=f"В очереди: {len(S['items'])}   ·   ник, просмотры, длина, название")
        if S["items"]:
            i = 0 if keep is None else min(keep, len(S["items"]) - 1)
            lb.selection_clear(0, "end")
            lb.selection_set(i)
            lb.see(i)
            open_clip(i)
        else:
            S["cur"] = None
            canvas.delete("all")
            head.configure(text="Очередь пуста. Нажми «Найти новые клипы» или добавь свой файл.")

    def show_frame(*_):
        p = S["cur"]
        if not p:
            return
        t = S["info"]["dur"] * pos.get() / 100
        png = m.CACHE / f"review_{os.getpid()}.png"
        m.CACHE.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", p["file"], "-frames:v", "1",
                        "-vf", f"scale={VW}:-2", str(png)], capture_output=True)
        if not png.exists():
            return
        S["img"] = tk.PhotoImage(file=str(png))
        canvas.config(height=S["img"].height())
        redraw()
        upd_time()

    def cur_sub():
        return None if sub_auto.get() else int(1920 * sub_pos.get() / 100)

    def preview_now():
        p = S["cur"]
        if not p:
            return
        if S["pv_busy"]:
            S["pv_dirty"] = True
            return
        try:
            build_force(fmt.get(), S["rects"])
        except ValueError:
            return
        S["pv_busy"], S["pv_dirty"] = True, False
        pv_btn.configure(text="Рисую пример…")
        args = (p["file"], fmt.get(), [list(r) for r in S["rects"]], list(S["cap"]) if S["cap"] else False,
                cur_sub(), S["info"]["dur"] * pos.get() / 100, m.CACHE / f"preview_{os.getpid()}.png")
        look = (cur_style(), cur_font(), list(S["content"]) if S["content"] else None)   # читаем в главном потоке

        def work():
            res = None
            try:
                res = make_preview(*args, cfg, style=look[0], font=look[1], content=look[2])
            except Exception as err:
                logq.put(f"  пример не получился: {err}\n")
            logq.put(("\x02", res))

        threading.Thread(target=work, daemon=True).start()

    def on_sub():
        if not sub_auto.get():
            st.setdefault("sub_pos", {})[fmt.get()] = int(sub_pos.get())
        else:
            st.setdefault("sub_pos", {}).pop(fmt.get(), None)
        save_state(st)
        sub_val.configure(text="высота: авто" if sub_auto.get() else f"высота: {int(sub_pos.get())}% от верха экрана")
        preview_now()

    def redraw():
        canvas.delete("all")
        if S["img"]:
            canvas.create_image(0, 0, anchor="nw", image=S["img"])
        k = S["scale"]
        for i, r in enumerate(S["rects"]):
            x0, y0, x1, y1 = [v / k for v in r]
            canvas.create_rectangle(x0, y0, x1, y1, outline="#ffe600", width=3)
            canvas.create_text(x0 + 12, y0 + 12, text=str(i + 1), fill="#ffe600", font=("Segoe UI", 12, "bold"))
        if S["content"]:                             # то, что он смотрит: обведено вручную
            x0, y0, x1, y1 = [v / k for v in S["content"]]
            canvas.create_rectangle(x0, y0, x1, y1, outline="#7cff2b", width=3)
            canvas.create_text(x0 + 8, y0 + 6, anchor="nw", text="контент", fill="#7cff2b", font=("Segoe UI", 10, "bold"))
        if S["cap"]:
            y0, y1 = S["cap"][0] / k, S["cap"][1] / k
            canvas.create_rectangle(0, y0, VW, y1, outline=RED, width=2, stipple="gray25", fill=RED)
            canvas.create_text(8, y0 + 4, anchor="nw", text="субтитры Twitch: эта полоса обрежется", fill="white",
                               font=("Segoe UI", 9, "bold"))
        if S["drag"]:
            canvas.create_rectangle(*S["drag"], outline=RED if S["capmode"] else "#7cff2b" if S["contmode"] else "#00e5ff",
                                    width=2, dash=(4, 3))

    # ---- просмотр клипа прямо в окне: картинка на кадре, звук через ffplay из набора ffmpeg ----
    play = {"on": False, "cap": None, "proc": None, "t0": 0.0, "start": 0.0, "fps": 30.0, "shown": 0,
            "drag": False, "warned": False}

    def stop_play(redraw_still=False):
        was = play["on"]
        play["on"] = False
        if play["cap"] is not None:
            play["cap"].release()
            play["cap"] = None
        if play["proc"] is not None:
            try:
                play["proc"].kill()
            except Exception:
                pass
            play["proc"] = None
        play_btn.configure(text="▶  Смотреть здесь", fg_color=BTN)
        if was and redraw_still:
            show_frame()

    def start_play():
        p = S["cur"]
        if not p:
            return
        stop_play()
        try:
            import cv2
            from PIL import ImageTk  # noqa: F401
        except ImportError:
            say("Для просмотра в окне нужны библиотеки opencv-python и pillow.")
            return
        dur = S["info"]["dur"]
        t = dur * pos.get() / 100
        if t > dur - 0.7:
            t = 0.0
        cap = cv2.VideoCapture(p["file"])
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        play.update(on=True, cap=cap, start=t, fps=cap.get(cv2.CAP_PROP_FPS) or 30.0, shown=0)
        try:
            play["proc"] = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "-ss", f"{t:.2f}", p["file"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            play["proc"] = None
            if not play["warned"]:
                play["warned"] = True
                say("Звук в окне не включился: не найден ffplay. Картинка идёт без звука.")
        play["t0"] = time.time() + 0.25              # ffplay стартует с небольшой задержкой
        play_btn.configure(text="■  Стоп", fg_color="#3a2a66")
        tick()

    def toggle_play():
        if play["on"]:
            stop_play(redraw_still=True)
        else:
            start_play()

    def tick():
        if not play["on"]:
            return
        import cv2
        from PIL import Image, ImageTk
        cap = play["cap"]
        el = max(0.0, time.time() - play["t0"])
        want = int(el * play["fps"])
        frame = None
        while play["shown"] <= want:                 # отстали: пропускаем кадры, показываем последний
            if not cap.grab():
                stop_play()
                pos.set(0)
                show_frame()
                return
            play["shown"] += 1
            if play["shown"] > want:
                ok, frame = cap.retrieve()
                if not ok:
                    frame = None
        if frame is not None:
            hh = int(VW * frame.shape[0] / frame.shape[1])
            rgb = cv2.cvtColor(cv2.resize(frame, (VW, hh)), cv2.COLOR_BGR2RGB)
            S["img"] = ImageTk.PhotoImage(Image.fromarray(rgb))
            redraw()                                 # рамки остаются поверх видео
            if not play["drag"]:
                pos.set(min(100.0, 100 * (play["start"] + el) / max(0.1, S["info"]["dur"])))
                upd_time()
        root.after(15, tick)

    def open_clip(i):
        stop_play()
        p = S["items"][i]
        S["cur"] = p
        S["info"] = m.probe(p["file"])
        S["scale"] = S["info"]["w"] / VW
        S["rects"] = []
        S["cap"], S["capmode"] = None, False
        zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8")) if m.ZONES_FILE.exists() else {}
        z = zones.get(p.get("streamer", ""))
        if z and abs(S["info"]["w"] / S["info"]["h"] - 16 / 9) < 0.06:
            S["cap"] = [int(S["info"]["h"] * z[0] / 100), int(S["info"]["h"] * z[1] / 100)]
        e_streamer.delete(0, "end")
        e_streamer.insert(0, p.get("streamer", ""))
        e_title.delete(0, "end")
        guess = "cs" if p.get("format") == "cs" else "talk"
        S["content"], S["contmode"] = None, False
        last = st.get("last", {}).get(p.get("streamer", ""))
        if last and not p.get("own"):
            guess = last["fmt"] if last["fmt"] in [k for k, _, _ in FORMATS] else guess
            kk = S["info"]["w"] / 1920
            S["rects"] = [[int(v * kk) for v in r] for r in last["rects"]]
            if last.get("content"):
                S["content"] = [int(v * kk) for v in last["content"]]
        fmt.set(guess)
        head.configure(text=f"{p.get('streamer') or 'своё видео'}   ·   {p.get('title', '')}   ·   "
                            f"{int(p.get('duration') or S['info']['dur'])} с   ·   {int(p.get('twitch_views', 0))} просмотров на Twitch")
        pos.set(40)
        show_frame()
        on_fmt(clear=False)

    def on_fmt(clear=True):
        key = fmt.get()
        hint.configure(text=dict((k, h) for k, _, h in FORMATS)[key]
                       + ("  Рамки подставлены как в прошлом клипе этого стримера: проверь и поправь."
                          if S["rects"] and not clear else ""))
        if clear and key != "multi" and len(S["rects"]) > 1:
            S["rects"] = S["rects"][:1]
        if key == "cs_nocam":
            S["rects"] = []
        saved = st.get("sub_pos", {}).get(key)
        sub_auto.set(saved is None)
        if saved is not None:
            sub_pos.set(saved)
        sub_val.configure(text="высота: авто" if saved is None else f"высота: {int(saved)}% от верха экрана")
        redraw()
        preview_now()

    def press(e):
        S["drag"] = [e.x, e.y, e.x, e.y]

    def move(e):
        if S["drag"]:
            S["drag"][2], S["drag"][3] = max(0, min(VW, e.x)), max(0, e.y)
            redraw()

    def release(e):
        d, S["drag"] = S["drag"], None
        if not d or abs(d[2] - d[0]) < 12 or abs(d[3] - d[1]) < 12:
            redraw()
            return
        k = S["scale"]
        r = [int(min(d[0], d[2]) * k), int(min(d[1], d[3]) * k), int(max(d[0], d[2]) * k), int(max(d[1], d[3]) * k)]
        if S["capmode"]:
            S["cap"], S["capmode"] = [r[1], r[3]], False
            cap_btn.configure(text="Отметить субтитры Twitch", fg_color=BTN)
        elif S["contmode"]:                          # обводили то, что он смотрит
            S["content"], S["contmode"] = r, False
            cont_btn.configure(text="Обвести то, что он смотрит", fg_color=BTN)
        elif fmt.get() == "multi":
            if len(S["rects"]) < 4:
                S["rects"].append(r)
        else:
            S["rects"] = [r]
        redraw()
        preview_now()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", release)
    def pos_released(_):
        play["drag"] = False
        if play["on"]:
            start_play()                             # перемотали во время просмотра: продолжаем с нового места
        else:
            show_frame()
        preview_now()

    pos.bind("<ButtonPress-1>", lambda e: play.update(drag=True))
    pos.bind("<ButtonRelease-1>", pos_released)
    sub_pos.bind("<ButtonRelease-1>", lambda e: (sub_auto.set(False), on_sub()))
    lb.bind("<<ListboxSelect>>", lambda e: lb.curselection() and open_clip(lb.curselection()[0]))

    def clear_rects():
        S["rects"], S["content"], S["contmode"] = [], None, False
        cont_btn.configure(text="Обвести то, что он смотрит", fg_color=BTN)
        redraw()
        preview_now()

    def mark_content():
        S["contmode"], S["capmode"] = not S["contmode"], False
        cap_btn.configure(text="Отметить субтитры Twitch", fg_color=BTN)
        cont_btn.configure(text="Обведи область на кадре…" if S["contmode"] else "Обвести то, что он смотрит",
                           fg_color="#1f4a14" if S["contmode"] else BTN)

    def mark_caps():
        S["contmode"] = False
        cont_btn.configure(text="Обвести то, что он смотрит", fg_color=BTN)
        S["capmode"] = not S["capmode"]
        cap_btn.configure(text="Обведи полосу с субтитрами…" if S["capmode"] else "Отметить субтитры Twitch",
                          fg_color="#5a1f24" if S["capmode"] else BTN)

    def clear_caps():
        S["cap"], S["capmode"] = None, False
        cap_btn.configure(text="Отметить субтитры Twitch", fg_color=BTN)
        p = S["cur"]
        if p and m.ZONES_FILE.exists():
            zones = json.loads(m.ZONES_FILE.read_text(encoding="utf-8"))
            if zones.pop(p.get("streamer", ""), None) is not None:
                m.ZONES_FILE.write_text(json.dumps(zones, ensure_ascii=False), encoding="utf-8")
        redraw()
        preview_now()

    def open_player():
        stop_play(redraw_still=True)
        if S["cur"]:
            try:
                os.startfile(S["cur"]["file"])
            except AttributeError:
                subprocess.Popen(["xdg-open", S["cur"]["file"]])

    def cur_index():
        return lb.curselection()[0] if lb.curselection() else 0

    def do_montage():
        p = S["cur"]
        if not p:
            return
        try:
            build_force(fmt.get(), S["rects"])
        except ValueError as err:
            say(str(err))
            return
        streamer = e_streamer.get().strip().lower()
        jobs.put({"clip": p, "fmt_key": fmt.get(), "rects": [list(r) for r in S["rects"]],
                  "streamer": streamer, "title": e_title.get(),
                  "captions": list(S["cap"]) if S["cap"] else False, "sub_y": cur_sub(),
                  "style": cur_style(), "font": cur_font(),
                  "banner": banner_menu.get() if banner_menu.get() in m.banner_list(cfg) else None,
                  "content": list(S["content"]) if S["content"] else None})
        st["clips"][p["clip_id"]] = "queued"
        if streamer:
            kk = 1920 / S["info"]["w"]
            st.setdefault("last", {})[streamer] = {
                "fmt": fmt.get(), "rects": [[int(v * kk) for v in r] for r in S["rects"]],
                "content": [int(v * kk) for v in S["content"]] if S["content"] else None}
        save_state(st)
        refresh(keep=cur_index())

    def do_skip():
        p = S["cur"]
        if not p:
            return
        st["clips"][p["clip_id"]] = "skipped"
        save_state(st)
        refresh(keep=cur_index())

    def add_file():
        got = filedialog.askopenfilenames(title="Выбери видео",
                                          filetypes=[("Видео", "*.mp4 *.mov *.mkv *.webm *.avi"), ("Все файлы", "*.*")])
        for f in got:
            add_own(st, f)
        if got:
            refresh()

    finding = {"on": False}

    def find_clips():
        if finding["on"]:
            return
        finding["on"] = True
        say("Ищу новые клипы…")

        def work():
            try:
                r = subprocess.run([sys.executable, "clipfinder.py"], cwd=str(BASE), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
                tail = (r.stdout or "").strip().splitlines()[-1:] or ["поиск завершён"]
                logq.put(tail[0] + "\n")
            finally:
                finding["on"] = False
                logq.put("\x01")

        threading.Thread(target=work, daemon=True).start()

    cont_btn = button(tools, "Обвести то, что он смотрит", mark_content, width=230)
    cont_btn.grid(row=0, column=0, padx=(0, 8), pady=(0, 8), sticky="w")
    cap_btn = button(tools, "Отметить субтитры Twitch", mark_caps, width=220)
    cap_btn.grid(row=0, column=1, padx=(0, 8), pady=(0, 8), sticky="w")
    button(tools, "Субтитров нет", clear_caps, width=130).grid(row=0, column=2, pady=(0, 8), sticky="w")
    play_btn = button(tools, "▶  Смотреть здесь", toggle_play, width=230)
    play_btn.grid(row=1, column=0, padx=(0, 8), sticky="w")
    button(tools, "Стереть рамки", clear_rects, width=220).grid(row=1, column=1, padx=(0, 8), sticky="w")
    button(tools, "В плеере", open_player, width=130).grid(row=1, column=2, sticky="w")
    button(acts, "Смонтировать и дальше", do_montage, primary=True, width=260, height=44).pack(side="left", padx=(0, 10))
    button(acts, "Пропустить клип", do_skip, width=170, height=44).pack(side="left")
    status = ctk.CTkLabel(acts, text="", font=F(12), text_color=HINT, anchor="w", justify="left", wraplength=330)
    status.pack(side="left", padx=(14, 0))           # последняя строка журнала всегда на виду
    foot = ctk.CTkFrame(left, fg_color=CARD)
    foot.pack(side="bottom", fill="x", padx=14, pady=(2, 14))
    def pool_changed(added):
        if added:
            find_clips()                             # сразу ищем клипы нового стримера
        else:
            refresh(keep=cur_index() if lb.curselection() else None)

    button(foot, "Найти новые клипы", find_clips, width=300).pack(fill="x", pady=(0, 8))
    button(foot, "Добавить свой файл…", add_file, width=300).pack(fill="x", pady=(0, 8))
    button(foot, "Стримеры: добавить или убрать…",
           lambda: streamers_dialog(root, st, True, pool_changed, say), width=300).pack(fill="x", pady=(0, 8))

    # ---- обновления программы ----
    upd = {"info": None, "busy": False}

    def upd_check(quiet=False):
        if upd["busy"]:
            return
        upd["busy"] = True

        def work():
            try:
                import updater
                res = updater.check()
            except Exception as err:
                res = {"error": f"проверка обновлений не сработала: {err}"}
            logq.put(("\x03", res, quiet))

        threading.Thread(target=work, daemon=True).start()

    def upd_click():
        info = upd["info"]
        if not info:
            upd_btn.configure(text="Проверяю…")
            upd_check()
            return
        if S["busy"] or jobs.qsize():
            say("Сначала дождись конца монтажа, потом ставь обновление.")
            return
        try:
            import updater
            done_ = updater.apply(info)
        except Exception as err:
            say(f"Обновление не установлено: {err}")
            return
        say("Обновлено: " + ", ".join(done_) + ". Перезапускаю программу…")
        stop_play()
        subprocess.Popen([sys.executable, str(CODE / "review.py"), "--old"], cwd=str(CODE))
        root.after(600, root.destroy)

    upd_btn = button(foot, "Проверить обновления", upd_click, width=300)
    upd_btn.pack(fill="x", pady=(0, 8))
    button(foot, "Настройки и ключи…", lambda: settings_dialog(root, True, find_clips, say), width=300).pack(fill="x")

    def worker():
        while True:
            job = jobs.get()
            S["busy"] = job["clip"].get("title", "")[:28] or "клип"
            ok = False
            try:
                ok = bool(run_job(job, cfg, enc, log=say))
            except BaseException as err:             # любая ошибка: клип помечается, очередь монтажа идёт дальше
                say(f"  ошибка: {err}")
            st["clips"][job["clip"]["clip_id"]] = "done" if ok else "retry"
            save_state(st)
            S["done" if ok else "failed"] += 1
            S["busy"] = None

    def pump():
        try:
            while True:
                s = logq.get_nowait()
                if s == "\x01":
                    refresh(keep=cur_index() if lb.curselection() else None)
                    continue
                if isinstance(s, tuple) and s[0] == "\x03":      # ответ на проверку обновлений
                    upd["busy"] = False
                    res, quiet = s[1], s[2]
                    if res.get("available"):
                        upd["info"] = res
                        upd_btn.configure(text="Установить обновление", fg_color=ACC, hover_color=ACC_H)
                        logq.put(f"Есть обновление {res['version']}" + (f": {res['notes']}" if res["notes"] else "")
                                 + ". Нажми «Установить обновление» слева внизу.\n")
                    else:
                        if not quiet:                    # ответ пишем и в журнал, и прямо на кнопку
                            logq.put((res.get("error") or ("Обновления не настроены: нет файла update_url.txt."
                                                           if res.get("off") else "У тебя последняя версия.")) + "\n")
                            upd_btn.configure(text="Не удалось проверить" if res.get("error") else
                                              "Обновления не настроены" if res.get("off") else "Последняя версия ✓")
                            root.after(5000, lambda: upd["info"] is None and upd_btn.configure(text="Проверить обновления"))
                        else:
                            upd_btn.configure(text="Проверить обновления")
                    continue
                if isinstance(s, tuple):
                    S["pv_busy"] = False
                    pv_btn.configure(text="Обновить пример")
                    res = s[1]
                    if res and Path(res["png"]).exists():
                        S["pv_img"] = tk.PhotoImage(file=res["png"]).subsample(2)
                        pvc.delete("all")
                        pvc.create_image(0, 0, anchor="nw", image=S["pv_img"])
                        if sub_auto.get():
                            sub_pos.set(int(round(100 * res["sub_auto"] / 1920)))
                        want = cur_style()
                        style_note.configure(text="" if want in ("random", res.get("style")) else
                                             "Этот стиль работает, когда есть камера и игра. Здесь ролик соберётся в «Рамке».")
                    if S["pv_dirty"]:
                        preview_now()
                    continue
                tail_ = s.strip().splitlines()[-1].strip() if s.strip() else ""
                if tail_:
                    status.configure(text=tail_[:110])
                logbox.configure(state="normal")
                logbox.insert("end", s)
                logbox.see("end")
                logbox.configure(state="disabled")
        except queue.Empty:
            pass
        chips["busy"].configure(text=f"Монтируется: {S['busy'] or 'ничего'}",
                                text_color=TXT if S["busy"] else MUTED)
        chips["wait"].configure(text=f"Ждут монтажа: {jobs.qsize()}")
        chips["done"].configure(text=f"Готово: {S['done']}")
        chips["failed"].configure(text=f"С ошибкой: {S['failed']}", text_color=RED if S["failed"] else MUTED)
        root.after(200, pump)

    def auto_find():
        find_clips()
        root.after(int(cfg.get("interval_min", 120)) * 60 * 1000, auto_find)

    root.protocol("WM_DELETE_WINDOW", lambda: (stop_play(), root.destroy()))

    def on_space(e):                                 # пробел: пуск и стоп, как в видеоредакторах
        if "entry" in str(e.widget).lower():         # в полях ввода пробел печатается как обычно
            return
        toggle_play()
        return "break"

    root.bind("<space>", on_space)
    sys.stdout = sys.stderr = Out()                  # с этого момента всё, что печатает монтаж, идёт в журнал
    threading.Thread(target=worker, daemon=True).start()
    refresh()
    pump()
    if shortcut_msg:
        say(shortcut_msg)
    if needs_setup():                                # первый запуск: сначала ключи, поиск начнётся после них
        root.after(800, lambda: settings_dialog(root, True, auto_find, say))
    else:
        root.after(3000, auto_find)
    root.after(6000, lambda: upd_check(quiet=True))  # при запуске тихо проверяем, нет ли новой версии
    try:
        root.after(200, lambda: root.state("zoomed"))  # на весь экран
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    _args = sys.argv[1:]

    def _arg(name, default=None):
        return _args[_args.index(name) + 1] if name in _args else default

    # старый ярлык профиля (папка clipper_us и т.п.): открываем общую программу сразу на этом профиле
    if "--data" not in _args and "--old" not in _args and PROFILE and "_" in CODE.name:
        _root = CODE.parent / CODE.name.rsplit("_", 1)[0]
        if (_root / "review.py").exists() and (_root / "webapp.py").exists() and _root.resolve() != CODE:
            subprocess.Popen([sys.executable, str(_root / "review.py"), "--start", CODE.name.rsplit("_", 1)[1]],
                             cwd=str(_root))
            sys.exit(0)
    if "--old" not in _args:                         # новое окно; старое остаётся запасным
        try:
            import webapp
            if webapp.run(port=int(_arg("--port", 0)), window="--no-window" not in _args, token=_arg("--token"),
                          parent=int(_arg("--parent", 0)), start=_arg("--start", "")):
                sys.exit(0)
        except SystemExit:
            raise
        except Exception:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            import traceback
            traceback.print_exc()
            print("\nНовое окно не запустилось, открываю прежнее. Пришли этот текст ошибки.")
    try:
        import customtkinter  # noqa: F401
        pretty = True
    except ImportError:
        pretty = False
        print("Для красивого окна поставь библиотеку:  python -m pip install customtkinter")
    if pretty:
        try:
            gui()
        except Exception as err:                     # красивое окно не собралось: открываем простое
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
            import traceback
            traceback.print_exc()
            print("\nКрасивое окно не запустилось, открываю простое. Пришли этот текст ошибки.")
            gui_classic()
    else:
        gui_classic()
