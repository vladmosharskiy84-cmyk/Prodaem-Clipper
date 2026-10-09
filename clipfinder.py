# -*- coding: utf-8 -*-
"""
Шаг 1: поиск и скачивание свежих клипов.
Берёт топ-N клипов за последние H часов по каждому стримеру из streamers.txt,
оставляет только нужные категории и скачивает их в папку clips/raw/<стример>/.
Запуск:  python clipfinder.py
"""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
STREAMERS_FILE = BASE / "streamers.txt"
SEEN_FILE = BASE / "seen.json"
PASSPORTS_FILE = BASE / "passports.jsonl"
API = "https://api.twitch.tv/helix"


def load_config():
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if "ВСТАВЬ" in cfg["client_id"] or "ВСТАВЬ" in cfg["client_secret"]:
        sys.exit("Открой config.json и вставь свои Client ID и Client Secret.")
    return cfg


def load_streamers():
    logins = []
    for line in STREAMERS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip().lower()
        if line:
            logins.append(line)
    return list(dict.fromkeys(logins))


def get_token(cfg):
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Twitch не принял ключи ({r.status_code}): {r.text}")
    return r.json()["access_token"]


def api_get(path, headers, params):
    r = requests.get(f"{API}/{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def get_user_ids(logins, headers):
    ids = {}
    for part in chunks(logins, 100):
        for u in api_get("users", headers, [("login", x) for x in part]):
            ids[u["login"].lower()] = u["id"]
    return ids


def get_game_names(game_ids, headers):
    names = {}
    for part in chunks([g for g in game_ids if g], 100):
        for g in api_get("games", headers, [("id", x) for x in part]):
            names[g["id"]] = g["name"]
    return names


def safe_name(text, limit=60):
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text).strip()
    return text[:limit].strip() or "clip"


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--quiet",
           "-o", str(target), url]
    return subprocess.run(cmd).returncode == 0 and target.exists()


def main():
    cfg = load_config()
    logins = load_streamers()
    headers = {"Client-Id": cfg["client_id"],
               "Authorization": f"Bearer {get_token(cfg)}"}

    seen = set(json.loads(SEEN_FILE.read_text(encoding="utf-8"))) if SEEN_FILE.exists() else set()
    talk = {c.lower() for c in cfg["talk_categories"]}
    cs = {c.lower() for c in cfg["cs_categories"]}

    user_ids = get_user_ids(logins, headers)
    missing = [x for x in logins if x not in user_ids]
    if missing:
        print("Не найдены на Twitch (проверь написание):", ", ".join(missing))

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=cfg["hours"])
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    out_dir = BASE / cfg["out_dir"] / "raw"
    total_new = 0

    for login in logins:
        if login not in user_ids:
            continue
        try:
            clips = api_get("clips", headers, {
                "broadcaster_id": user_ids[login],
                "started_at": start.strftime(fmt),
                "ended_at": now.strftime(fmt),
                "first": 100,
            })
        except requests.RequestException as e:
            print(f"{login}: ошибка запроса клипов: {e}")
            continue

        games = get_game_names(sorted({c.get("game_id", "") for c in clips}), headers)
        picked = []
        for c in sorted(clips, key=lambda x: x["view_count"], reverse=True):
            cat = games.get(c.get("game_id", ""), "")
            if cat.lower() in talk:
                kind = "talk"
            elif cat.lower() in cs:
                kind = "cs"
            else:
                continue
            if c["view_count"] < cfg["min_views"]:
                continue
            if (c.get("duration") or 0) < cfg.get("min_duration", 10):
                continue
            picked.append((c, cat, kind))
            if len(picked) >= cfg["top_n"]:
                break

        new = 0
        for c, cat, kind in picked:
            if c["id"] in seen:
                continue
            stamp = c["created_at"][:10]
            name = f"{stamp}_{c['view_count']:06d}_{safe_name(c['title'], 40)}_{c['id'][:12]}.mp4"
            target = out_dir / login / name
            if not download(c["url"], target):
                print(f"{login}: не скачался клип {c['url']}")
                continue
            passport = {
                "clip_id": c["id"], "streamer": login, "title": c["title"],
                "category": cat, "format": kind, "twitch_views": c["view_count"],
                "duration": c.get("duration"), "created_at": c["created_at"],
                "url": c["url"], "file": str(target.relative_to(BASE)),
                "found_at": now.strftime(fmt),
            }
            target.with_suffix(".json").write_text(
                json.dumps(passport, ensure_ascii=False, indent=2), encoding="utf-8")
            with PASSPORTS_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(passport, ensure_ascii=False) + "\n")
            seen.add(c["id"])
            SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")
            new += 1
        total_new += new
        print(f"{login}: клипов за {cfg['hours']} ч: {len(clips)}, "
              f"подошло: {len(picked)}, скачано новых: {new}")

    print(f"\nГотово. Новых клипов: {total_new}. Папка: {out_dir}")


if __name__ == "__main__":
    main()
