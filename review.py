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

import montage as m

BASE = Path(__file__).resolve().parent
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


# ---------- состояние очереди ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"clips": {}, "last": {}, "own": []}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def load_queue(st):
    """Клипы, которые ещё не смонтированы и не отклонены: сначала свои файлы, потом свежие и популярные."""
    items = []
    for own in st.get("own", []):
        if own["clip_id"] not in st["clips"] and Path(own["file"]).exists():
            items.append(own)
    found = []
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
            old = BASE / "clips" / "ready" / p["format"] / f"{p['streamer']}_{p['clip_id'][:16]}.mp4"
            if old.exists():                      # уже смонтирован автоматическим режимом
                continue
            p = dict(p, file=str(raw))
            found.append(p)
    found.sort(key=lambda p: (p.get("found_at", ""), p.get("twitch_views", 0)), reverse=True)
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


def make_preview(clip, fmt_key, rects, captions, sub_y, t, png, cfg, style=None, font=None, content=None):
    """Быстро рисует один кадр: как ролик будет выглядеть с этой камерой, форматом и субтитрами.
    Возвращает {"png", "sub_y", "sub_auto"} или None."""
    fmt, force, want_satis = build_force(fmt_key, rects)
    if captions:
        force["captions"] = captions
    elif captions is False:
        force["captions"] = False
    if sub_y:
        force["sub_y"] = int(sub_y)
    force.update(style=style, font=font, content=content)
    clip = Path(clip)
    sat = m.pick_satis(clip, cfg, force=True) if want_satis else None
    return m.montage(clip, fmt, "", "", m.CACHE / "preview_unused.mp4", cfg, "libx264", satis=sat,
                     show_title=False, force=force, preview={"t": t, "png": str(png)})


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
    force.update(style=job.get("style"), font=job.get("font"), content=job.get("content"))
    sat = m.pick_satis(clip, cfg, force=True) if want_satis else None
    if want_satis and not sat:
        log("  в папке satisfying нет видео: собираю обычный разговорный")
    folder = "manual" if own else fmt
    name = (streamer or "video") + "_" + "".join(ch for ch in p["clip_id"][:16] if ch.isalnum() or ch in "_-")
    out = BASE / "clips" / "ready" / folder / f"{name}.mp4"
    title = (job.get("title") or "").strip()
    show_title = bool(title) if own else bool(cfg.get("clip_titles", False))
    log(f"{streamer or clip.name}: монтаж ({dict((k, v) for k, v, _ in FORMATS)[job['fmt_key']]})")
    res = m.montage(clip, fmt, streamer, title or p.get("title", clip.stem), out, cfg, enc,
                    fixed_title=title or None, satis=sat, show_title=show_title, force=force)
    if not res:
        log("  не получилось смонтировать")
        return None
    p.update(res)
    p["streamer"] = streamer or "стример"
    p["format"] = fmt
    p["source"] = "manual" if own else "review"
    p["potential"] = m.potential(p, res, None)
    hashtags = (f"#{streamer} " if streamer else "") + "#стример #twitch #нарезки"
    out.with_suffix(".txt").write_text((res["description"] or res["final_title"]) + f"\n\n{hashtags}\n", encoding="utf-8")
    p["tg_sent"] = m.tg_send(out, p, hashtags, kind)
    out.with_suffix(".json").write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  готово: {res['duration']} с, потенциал {p['potential']}/100"
        + (", отправлен в Telegram" if p["tg_sent"] else ""))
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
            except Exception as err:
                say(f"  ошибка: {err}")
            st["clips"][job["clip"]["clip_id"]] = "done" if ok else "failed"
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
APP_NAME = "PRODAEM CLIPPER"


def make_assets():
    """Раскладывает логотип в файлы: значок окна и картинку для шапки. Возвращает (ico, png) или (None, None)."""
    try:
        import base64
        import io
        from PIL import Image
        m.CACHE.mkdir(exist_ok=True)
        ico, png = BASE / "clipper.ico", m.CACHE / "logo_44.png"
        if not ico.exists() or not png.exists():
            im = Image.open(io.BytesIO(base64.b64decode(LOGO_B64))).convert("RGBA")
            im.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            im.resize((44, 44), Image.LANCZOS).save(png)
        return ico, png
    except Exception:
        return None, None


def make_shortcut(ico):
    """Один раз кладёт на рабочий стол ярлык с логотипом (только Windows)."""
    if sys.platform != "win32" or not ico:
        return
    flag = BASE / "cache" / "shortcut_done.txt"
    if flag.exists():
        return
    ps = ("$d=[Environment]::GetFolderPath('Desktop');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '" + APP_NAME + ".lnk'));"
          "$s.TargetPath='" + sys.executable.replace("'", "''") + "';"
          "$s.Arguments='review.py';"
          "$s.WorkingDirectory='" + str(BASE).replace("'", "''") + "';"
          "$s.IconLocation='" + str(ico).replace("'", "''") + "';"
          "$s.Save()")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                           capture_output=True, timeout=30)
        if r.returncode == 0:
            flag.write_text("ok", encoding="utf-8")
    except Exception:
        pass


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
    make_shortcut(ico)
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
    ctk.CTkLabel(top, text=" CLIPPER", font=F(26, True), text_color=ACC).pack(side="left")
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
        subprocess.Popen([sys.executable, str(BASE / "review.py")], cwd=str(BASE))
        root.after(600, root.destroy)

    upd_btn = button(foot, "Проверить обновления", upd_click, width=300)
    upd_btn.pack(fill="x")

    def worker():
        while True:
            job = jobs.get()
            S["busy"] = job["clip"].get("title", "")[:28] or "клип"
            ok = False
            try:
                ok = bool(run_job(job, cfg, enc, log=say))
            except Exception as err:
                say(f"  ошибка: {err}")
            st["clips"][job["clip"]["clip_id"]] = "done" if ok else "failed"
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
                    elif not quiet:
                        logq.put((res.get("error") or ("Обновления не настроены: нет файла update_url.txt."
                                                       if res.get("off") else "У тебя последняя версия.")) + "\n")
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
    root.after(3000, auto_find)
    root.after(6000, lambda: upd_check(quiet=True))  # при запуске тихо проверяем, нет ли новой версии
    try:
        root.after(200, lambda: root.state("zoomed"))  # на весь экран
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
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
