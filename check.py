"""Внешний сторож сайтов HOME-PC: проверка с серверов GitHub раз в 10 минут (workflow uptime.yml).

Режимы: check — проверить сайты из targets.txt; report — отчёт за прошлый месяц в reports/; setup — прислать каждому,
кто нажал «Старт» у бота, его id для секрета TG_CHAT; test — пробное сообщение в TG_CHAT.

Тревога — после двух неудачных проверок подряд: сообщение в Telegram и упавший запуск (письмо от GitHub). Пока сайты
недоступны, следующие запуски не падают и не пишут, чтобы не присылать письмо каждые 10 минут; когда всё снова
в порядке — сообщение «снова доступны» с длительностью простоя. Состояние между запусками — state/state.json
(кэш Actions). Репозиторий публичный, поэтому в журнал и сообщения — только адреса сайтов и коды ответов: IP
вырезаются, токен бота и id чата — только из секретов."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).parent
STATE = ROOT / "state" / "state.json"
ATTEMPTS, PAUSE, TIMEOUT = 2, 10, 20  # вторая попытка — через 10 с: единичный обрыв не считается неудачей
UA = "status-watch (+https://github.com/plotnikovsp/status)"
IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b|\[?[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{0,4}){3,7}\]?")


def scrub(text: str) -> str:
    return IP.sub("<ip>", text)


def targets() -> list[tuple[str, int, str]]:
    out = []
    for line in (ROOT / "targets.txt").read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2:
            out.append((parts[0], int(parts[1]), " ".join(parts[2:])))
    return out


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def probe(url: str, expect: int, marker: str) -> str | None:
    """None — в порядке, иначе причина."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    started = time.monotonic()
    try:
        with OPENER.open(req, timeout=TIMEOUT) as r:
            code, body = r.status, r.read(512_000)
    except urllib.error.HTTPError as e:
        code, body = e.code, b""
    except Exception as e:  # обрыв, таймаут, сертификат, DNS
        return scrub(f"{type(e).__name__}: {getattr(e, 'reason', e)}")[:200]
    took = time.monotonic() - started
    if code != expect:
        return f"HTTP {code}, ожидался {expect}"
    if marker and marker.encode("utf-8") not in body:
        return f"HTTP {code}, но на странице нет «{marker}»"
    if took > TIMEOUT * 0.75:
        return f"HTTP {code}, но ответ за {took:.0f} с"
    return None


def check_all() -> dict[str, str]:
    bad = {}
    for url, expect, marker in targets():
        for attempt in range(ATTEMPTS):
            why = probe(url, expect, marker)
            if why is None:
                break
            if attempt + 1 < ATTEMPTS:
                time.sleep(PAUSE)
        if why:
            bad[url] = why
    return bad


def telegram(text: str, chat: str | None = None) -> bool:
    token, chat = os.environ.get("TG_TOKEN", ""), chat or os.environ.get("TG_CHAT", "")
    if not token or not chat:
        print("::warning::Telegram не настроен (секреты TG_TOKEN и TG_CHAT) — только письмо от GitHub")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # текст ошибки может содержать адрес с токеном — не печатаем
        print(f"::warning::сообщение в Telegram не отправлено ({type(e).__name__})")
        return False


def load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%d.%m %H:%M")


def run_check() -> int:
    state, now = load(), time.time()
    bad = check_all()
    month = datetime.fromtimestamp(now, MSK).strftime("%Y-%m")
    m = state.setdefault("months", {}).setdefault(month, {"total": 0, "ok": 0, "incidents": 0})
    m["total"] += 1
    code = 0
    if not bad:
        m["ok"] += 1
        if state.get("alerted"):
            minutes = int((now - state.get("first_bad", now)) // 60)
            telegram(f"HOME-PC: сайты снова доступны снаружи. Простой — около {minutes} мин "
                     f"(с {fmt(state.get('first_bad', now))} МСК).")
        state.update(fails=0, alerted=False)
        state.pop("first_bad", None)
        print(f"::notice::все {len(targets())} адресов в порядке")
    else:
        state["fails"] = state.get("fails", 0) + 1
        state.setdefault("first_bad", now)
        lines = "\n".join(f"- {u}: {w}" for u, w in bad.items())
        for u, w in bad.items():
            print(f"::warning title={u}::{w}")
        if state["fails"] >= 2 and not state.get("alerted"):
            state["alerted"] = True
            m["incidents"] += 1
            telegram(f"Тревога HOME-PC: сайты недоступны снаружи — {state['fails']} проверки подряд, "
                     f"с {fmt(state['first_bad'])} МСК.\n{lines}\n\nПроверка с серверов GitHub.")
            print("::error::две неудачные проверки подряд — тревога отправлена")
            code = 1  # упавший запуск — письмо от GitHub
    # месяцы старше года не храним
    for old in sorted(state["months"])[:-13]:
        del state["months"][old]
    save(state)
    summary(f"Проверено адресов: {len(targets())}, не в порядке: {len(bad)}" + "".join(f"\n- {u}: {w}" for u, w in bad.items()))
    return code


def run_report() -> int:
    state = load()
    first = datetime.now(MSK).replace(day=1)
    prev = (first - timedelta(days=1)).strftime("%Y-%m")
    m = state.get("months", {}).get(prev)
    if not m or not m.get("total"):
        print(f"::warning::за {prev} проверок нет — отчёт не пишется")
        return 0
    pct = 100 * m["ok"] / m["total"]
    down = (m["total"] - m["ok"]) * 10
    text = (f"# Доступность сайтов HOME-PC за {prev}\n\n"
            f"- Доступность снаружи: **{pct:.2f} %** ({m['ok']} из {m['total']} проверок)\n"
            f"- Неудачных проверок: {m['total'] - m['ok']} (примерно {down} мин)\n"
            f"- Тревог (две неудачи подряд): {m['incidents']}\n\n"
            "Проверка раз в 10 минут с серверов GitHub; адреса — targets.txt. Расписание GitHub не точное: "
            "проверок за месяц бывает меньше 4 320.\n")
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"{prev}.md").write_text(text, encoding="utf-8")
    telegram(f"HOME-PC за {prev}: сайты доступны снаружи {pct:.2f} % времени, тревог — {m['incidents']}.")
    print(text)
    return 0


def run_setup() -> int:
    token = os.environ.get("TG_TOKEN", "")
    if not token:
        print("::error::нет секрета TG_TOKEN")
        return 1
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20) as r:
        updates = json.load(r).get("result", [])
    chats = {u["message"]["chat"]["id"] for u in updates if "message" in u}
    for chat in chats:  # id — только в сообщении самому человеку, не в журнал публичного репозитория
        telegram(f"Ваш id: {chat}\nВставьте его в секрет TG_CHAT репозитория status.", str(chat))
    print(f"::notice::отправлено сообщений с id: {len(chats)} (нет — нажмите у бота «Старт» и запустите снова)")
    return 0


def run_test() -> int:
    ok = telegram("Сторож HOME-PC снаружи: пробное сообщение. Тревога придёт сюда после двух неудачных проверок подряд.")
    return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    sys.exit({"check": run_check, "report": run_report, "setup": run_setup, "test": run_test}[mode]())
