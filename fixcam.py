# -*- coding: utf-8 -*-
"""
Исправление камеры в готовом ролике. Выбираешь ролик, обводишь мышкой окно камеры на кадре
из исходника и жмёшь «Пересобрать». Место можно запомнить для стримера.
Запуск:  python fixcam.py     (или кнопка в окне Clipper)
"""
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import montage as m

BASE = Path(__file__).resolve().parent
READY = BASE / "clips" / "ready"
VIEW_W = 960                                     # ширина кадра в окне


def recent(limit=60):
    items = []
    for j in READY.glob("*/*.json"):
        try:
            p = json.loads(j.read_text(encoding="utf-8"))
        except Exception:
            continue
        if p.get("format") not in ("talk", "cs"):
            continue
        raw = Path(p.get("file", ""))
        raw = raw if raw.is_absolute() else BASE / raw
        if raw.exists():
            items.append((j.stat().st_mtime, j, p, raw))
    items.sort(key=lambda x: -x[0])
    return items[:limit]


def frame_png(raw, frac, out):
    info = m.probe(raw)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{info['dur'] * frac:.2f}", "-i", str(raw),
                    "-frames:v", "1", "-vf", f"scale={VIEW_W}:-2", str(out)], capture_output=True)
    return info


def gui():
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Клиппер: исправить камеру")
    items = recent()
    q = queue.Queue()
    state = {"info": None, "rect": None, "start": None, "img": None, "frac": 0.5, "idx": None}

    class Out:
        def write(self, s):
            q.put(s)

        def flush(self):
            pass

    sys.stdout = sys.stderr = Out()

    left = tk.Frame(root, padx=8, pady=8)
    left.pack(side="left", fill="y")
    tk.Label(left, text="Последние ролики (сверху новые):").pack(anchor="w")
    lb = tk.Listbox(left, width=46, height=30, exportselection=False)
    for _, j, p, raw in items:
        lb.insert("end", f"{p.get('streamer', '')}: {p.get('final_title', j.stem)}"[:60])
    lb.pack(fill="y", expand=True)

    right = tk.Frame(root, padx=8, pady=8)
    right.pack(side="left", fill="both", expand=True)
    tk.Label(right, text="Обведи мышкой окно камеры стримера на кадре:").pack(anchor="w")
    canvas = tk.Canvas(right, width=VIEW_W, height=540, bg="#222", cursor="crosshair")
    canvas.pack()

    bar = tk.Frame(right)
    bar.pack(fill="x", pady=4)
    tk.Label(bar, text="Момент клипа:").pack(side="left")
    scale = ttk.Scale(bar, from_=0.05, to=0.95, orient="horizontal", length=420)
    scale.set(0.5)
    scale.pack(side="left", padx=6)

    mode = tk.StringVar(value="cam")
    opts = tk.Frame(right)
    opts.pack(fill="x")
    tk.Radiobutton(opts, text="Камера в обведённом окне", variable=mode, value="cam").pack(anchor="w")
    tk.Radiobutton(opts, text="Стример снят на весь кадр (отдельной камеры нет)", variable=mode, value="full").pack(anchor="w")
    tk.Radiobutton(opts, text="Камеры в клипе нет вообще", variable=mode, value="none").pack(anchor="w")
    remember = tk.BooleanVar(value=True)
    tk.Checkbutton(opts, text="Запомнить это место камеры для стримера", variable=remember).pack(anchor="w")
    send = tk.BooleanVar(value=True)
    tk.Checkbutton(opts, text="Отправить исправленный ролик в Telegram", variable=send).pack(anchor="w")
    btn = tk.Button(right, text="Пересобрать", width=22, height=2)
    btn.pack(pady=6)
    log = tk.Text(right, height=7, state="disabled")
    log.pack(fill="x")

    def show():
        if state["idx"] is None:
            return
        _, j, p, raw = items[state["idx"]]
        png = m.CACHE / "fix_frame.png"
        m.CACHE.mkdir(exist_ok=True)
        state["info"] = frame_png(raw, state["frac"], png)
        if not png.exists():
            return
        state["img"] = tk.PhotoImage(file=str(png))
        canvas.config(height=state["img"].height())
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=state["img"])
        if state["rect"]:
            canvas.create_rectangle(*state["rect"], outline="#ffe600", width=3, tags="box")

    def pick(_=None):
        sel = lb.curselection()
        if sel:
            state["idx"], state["rect"] = sel[0], None
            show()

    def moved(_=None):
        state["frac"] = float(scale.get())
        show()

    def down(e):
        state["start"] = (e.x, e.y)

    def drag(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        state["rect"] = (min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y))
        canvas.delete("box")
        canvas.create_rectangle(*state["rect"], outline="#ffe600", width=3, tags="box")
        mode.set("cam")

    lb.bind("<<ListboxSelect>>", pick)
    scale.bind("<ButtonRelease-1>", moved)
    canvas.bind("<ButtonPress-1>", down)
    canvas.bind("<B1-Motion>", drag)

    def work(jpath, force, rem, snd):
        try:
            cfg = json.loads((BASE / "montage.json").read_text(encoding="utf-8"))
            m.refix(jpath, force, rem, cfg, m.pick_encoder(cfg), snd)
        except Exception as e:
            print(f"  ошибка: {e}")
        finally:
            q.put("\x00")

    def start():
        if state["idx"] is None:
            q.put("Сначала выбери ролик в списке слева.\n")
            return
        force = {"mode": mode.get()}
        if force["mode"] == "cam":
            r = state["rect"]
            if not r or r[2] - r[0] < 20 or r[3] - r[1] < 20:
                q.put("Обведи мышкой окно камеры на кадре.\n")
                return
            k = state["info"]["w"] / VIEW_W
            force["rect"] = [int(r[0] * k), int(r[1] * k), int(r[2] * k), int(r[3] * k)]
        btn.config(state="disabled", text="Идёт пересборка…")
        threading.Thread(target=work, args=(items[state["idx"]][1], force, remember.get(), send.get()), daemon=True).start()

    btn.config(command=start)

    def pump():
        try:
            while True:
                s = q.get_nowait()
                if s == "\x00":
                    btn.config(state="normal", text="Пересобрать")
                    continue
                log.config(state="normal")
                log.insert("end", s)
                log.see("end")
                log.config(state="disabled")
        except queue.Empty:
            pass
        root.after(150, pump)

    pump()
    if not items:
        q.put("Готовых роликов с исходниками пока нет.\n")
    root.mainloop()


if __name__ == "__main__":
    gui()
