#!/usr/bin/env python3
"""Offline conference portal for a NodeMCU ESP8266 access point."""
from __future__ import annotations

import csv
from contextlib import contextmanager
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CONFERENCE_DB", ROOT / "data" / "conference.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "80"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Baikal2026!")
CAPACITY_DEFAULT = 30
SESSIONS: dict[str, str] = {}  # session token -> CSRF token


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


@contextmanager
def database():
    con = connect_db()
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with database() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS attendees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            organization TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            topic TEXT NOT NULL DEFAULT '',
            seat INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)
        defaults = {
            "title": "Байкальская конференция",
            "subtitle": "Встречаемся у Байкала — даже без интернета",
            "location": "Байкальск, конференц-зал",
            "date": "",
            "capacity": str(CAPACITY_DEFAULT),
            "schedule": "09:30 — Регистрация участников\n10:00 — Открытие конференции\n10:30 — Доклады и обсуждение\n13:00 — Перерыв\n14:00 — Продолжение программы",
        }
        con.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", defaults.items())


def get_settings(con: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}


def esc(value: object) -> str:
    import html
    return html.escape(str(value or ""), quote=True)


def layout(title: str, body: str, active: str = "") -> bytes:
    nav = f'''<nav><a class="brand" href="/">Б<span>·</span></a><div class="nav-links"><a class="{'active' if active == 'home' else ''}" href="/">О конференции</a><a href="/#registration">Регистрация</a><a href="/#schedule">Программа</a></div><a class="admin-link" href="/admin">Для организаторов ↗</a></nav>'''
    page = f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="theme-color" content="#102c2a"><title>{esc(title)}</title><link rel="stylesheet" href="/static/style.css"></head><body>{nav}{body}<footer><span>BAIKAL / LOCAL PORTAL</span><span>Работает локально · интернет не требуется</span></footer></body></html>'''
    return page.encode("utf-8")


def home_page(con: sqlite3.Connection, notice: str = "") -> bytes:
    s = get_settings(con)
    schedule = "".join(f'<li><span>{esc(line.partition(" — ")[0])}</span><p>{esc(line.partition(" — ")[2] or line)}</p></li>' for line in s.get("schedule", "").splitlines() if line.strip())
    notice_html = f'<div class="notice">{esc(notice)}</div>' if notice else ""
    body = f'''<main>
    <section class="hero"><div class="hero-copy"><div class="eyebrow"><i></i> ЛОКАЛЬНАЯ СЕТЬ КОНФЕРЕНЦИИ</div><h1>{esc(s.get('title'))}<em>.</em></h1><p class="hero-sub">{esc(s.get('subtitle'))}</p><div class="hero-actions"><a class="button button-light" href="#registration">Зарегистрироваться <span>↓</span></a><a class="text-link" href="#schedule">Программа конференции</a></div><div class="hero-meta"><div><small>МЕСТО</small><b>{esc(s.get('location'))}</b></div><div><small>ДАТА</small><b>{esc(s.get('date') or 'Уточняется')}</b></div></div></div><div class="hero-art"><div class="sun"></div><div class="mountain mountain-back"></div><div class="mountain mountain-front"></div><div class="water"></div><div class="art-caption">51°50′ N<br>БАЙКАЛ</div></div></section>
    {notice_html}
    <section class="content-grid"><div class="section-intro"><div class="eyebrow dark"><i></i> УЧАСТНИКАМ</div><h2>Всё важное<br>рядом.</h2><p>Портал доступен через Wi‑Fi конференции. Подключение к интернету не требуется.</p><div class="network-pill"><span class="wifi-icon">⌁</span><span><b>Сеть конференции</b><small>Локальный доступ активен</small></span><span class="live-dot"></span></div></div>
    <div class="schedule" id="schedule"><div class="section-head"><h3>Программа</h3><span>ПЛАН ДНЯ</span></div><ol>{schedule or '<li><p>Программа пока не опубликована.</p></li>'}</ol></div></section>
    <section class="registration" id="registration"><div class="registration-heading"><div class="eyebrow"><i></i> ПРИСОЕДИНЯЙТЕСЬ</div><h2>Зарегистрируйтесь<br>на конференцию.</h2><p>Заполните форму — мы закрепим за вами свободное место.</p><div class="seat-note">МЕСТ В ЗАЛЕ <b>{esc(s.get('capacity', CAPACITY_DEFAULT))}</b></div></div>
    <form method="post" action="/register" class="form-card"><label>Имя и фамилия<input name="full_name" autocomplete="name" required maxlength="100" placeholder="Например, Анна Иванова"></label><label>Организация / учебное заведение<input name="organization" required maxlength="120" placeholder="Университет или компания"></label><label>Электронная почта<input name="email" type="email" autocomplete="email" required maxlength="160" placeholder="name@example.com"></label><label>Тема доклада <span class="optional">необязательно</span><input name="topic" maxlength="180" placeholder="Если планируете выступать"></label><button class="button button-dark" type="submit">Отправить заявку <span>↗</span></button><p class="form-privacy">Данные сохраняются локально на компьютере организаторов.</p></form></section>
    </main>'''
    return layout(s.get("title", "Конференция"), body, "home")


def admin_page(con: sqlite3.Connection, error: str = "", csrf_token: str = "") -> bytes:
    attendees = con.execute("SELECT * FROM attendees ORDER BY seat").fetchall()
    s = get_settings(con)
    cap = max(1, int(s.get("capacity", CAPACITY_DEFAULT)))
    rows = "".join(f'''<tr><td class="seat-cell">{r['seat']:02d}</td><td><b>{esc(r['full_name'])}</b><small>{esc(r['organization'])}</small></td><td>{esc(r['email'])}</td><td>{esc(r['topic'] or '—')}</td><td><form class="inline-form" method="post" action="/admin/seat"><input type="hidden" name="csrf" value="{esc(csrf_token)}"><input type="hidden" name="id" value="{r['id']}"><input type="number" name="seat" min="1" max="{cap}" value="{r['seat']}" aria-label="Место"><button class="icon-button" title="Сохранить место">↵</button></form></td><td><form method="post" action="/admin/delete" onsubmit="return confirm('Удалить заявку участника?')"><input type="hidden" name="csrf" value="{esc(csrf_token)}"><input type="hidden" name="id" value="{r['id']}"><button class="delete-button" title="Удалить">×</button></form></td></tr>''' for r in attendees)
    err = f'<div class="notice error">{esc(error)}</div>' if error else ""
    body = f'''<main class="admin-main"><div class="admin-top"><div><div class="eyebrow dark"><i></i> ПАНЕЛЬ ОРГАНИЗАТОРА</div><h1>Управление<br>конференцией.</h1></div><form method="post" action="/logout"><input type="hidden" name="csrf" value="{esc(csrf_token)}"><button class="button button-outline" type="submit">Выйти</button></form></div>{err}
    <div class="stats"><div><small>ЗАРЕГИСТРИРОВАНО</small><b>{len(attendees)} <span>/ {cap}</span></b></div><div><small>СВОБОДНЫХ МЕСТ</small><b>{max(0, cap-len(attendees))}</b></div><a class="button button-dark" href="/admin/export.csv">Скачать CSV <span>↓</span></a></div>
    <section class="admin-settings"><div class="section-head"><h3>Информация о событии</h3><span>НАСТРОЙКИ</span></div><form method="post" action="/admin/settings" class="settings-grid"><input type="hidden" name="csrf" value="{esc(csrf_token)}"><label>Название<input name="title" required maxlength="100" value="{esc(s.get('title'))}"></label><label>Подзаголовок<input name="subtitle" maxlength="160" value="{esc(s.get('subtitle'))}"></label><label>Место проведения<input name="location" maxlength="120" value="{esc(s.get('location'))}"></label><label>Дата<input name="date" maxlength="80" placeholder="Например, 15 июня 2027" value="{esc(s.get('date'))}"></label><label>Вместимость зала<input name="capacity" type="number" min="1" max="200" required value="{cap}"></label><label class="schedule-edit">Расписание <small>каждая строка: время — событие</small><textarea name="schedule" rows="5">{esc(s.get('schedule'))}</textarea></label><button class="button button-dark" type="submit">Сохранить настройки <span>↗</span></button></form></section>
    <section class="attendees-section"><div class="section-head"><h3>Участники</h3><span>{len(attendees):02d} ЗАЯВОК</span></div><div class="table-wrap"><table><thead><tr><th>МЕСТО</th><th>УЧАСТНИК</th><th>ПОЧТА</th><th>ТЕМА ДОКЛАДА</th><th>ИЗМЕНИТЬ МЕСТО</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="6" class="empty">Пока нет заявок. Поделитесь адресом локальной страницы с участниками.</td></tr>'}</tbody></table></div></section></main>'''
    return layout("Панель организатора", body)


class PortalHandler(BaseHTTPRequestHandler):
    server_version = "BaikalConference/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def send_bytes(self, content: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8", extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16000:
            raise ValueError("Форма слишком большая")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] if v else "" for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def session_id(self) -> str:
        raw = self.headers.get("Cookie", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
            return jar["portal_session"].value if "portal_session" in jar else ""
        except Exception:
            return ""

    def is_admin(self) -> bool:
        return self.session_id() in SESSIONS

    def require_admin(self) -> bool:
        if not self.is_admin():
            self.send_response(303)
            self.send_header("Location", "/admin/login")
            self.end_headers()
            return False
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/static/"):
            name = path.removeprefix("/static/")
            if name != "style.css":
                self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")
                return
            self.send_bytes((ROOT / "static" / name).read_bytes(), content_type="text/css; charset=utf-8")
            return
        with database() as con:
            if path == "/" or path in ("/generate_204", "/hotspot-detect.html", "/library/test/success.html", "/success.txt"):
                self.send_bytes(home_page(con))
            elif path == "/health":
                self.send_bytes(b"OK", content_type="text/plain; charset=utf-8")
            elif path == "/admin":
                if self.is_admin():
                    self.send_bytes(self.admin_response(con))
                else:
                    self.send_response(303); self.send_header("Location", "/admin/login"); self.end_headers()
            elif path == "/admin/login":
                self.send_bytes(layout("Вход организатора", f'''<main class="login-main"><div class="eyebrow dark"><i></i> ДОСТУП ОРГАНИЗАТОРА</div><h1>Войти<br>в систему.</h1><p>Управление заявками и программой конференции.</p><form method="post" action="/admin/login" class="form-card login-card"><label>Логин<input name="username" autocomplete="username" required></label><label>Пароль<input type="password" name="password" autocomplete="current-password" required></label><button class="button button-dark" type="submit">Войти <span>↗</span></button></form></main>'''))
            elif path == "/admin/export.csv":
                if not self.require_admin(): return
                out = io.StringIO(); writer = csv.writer(out)
                writer.writerow(["Место", "Имя", "Организация", "Электронная почта", "Тема доклада", "Дата регистрации"])
                for r in con.execute("SELECT * FROM attendees ORDER BY seat"):
                    writer.writerow([r["seat"], r["full_name"], r["organization"], r["email"], r["topic"], r["created_at"]])
                data = ("\ufeff" + out.getvalue()).encode("utf-8")
                self.send_bytes(data, content_type="text/csv; charset=utf-8", extra_headers={"Content-Disposition": "attachment; filename=conference_attendees.csv"})
            elif path == "/favicon.ico":
                self.send_bytes(b"", 204, "image/x-icon")
            else:
                # Captive portal probes often request arbitrary HTTP paths.
                if "." not in path.rsplit("/", 1)[-1]:
                    self.send_bytes(home_page(con))
                else:
                    self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")

    def admin_response(self, con: sqlite3.Connection, error: str = "") -> bytes:
        return admin_page(con, error, SESSIONS.get(self.session_id(), ""))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.form()
        except (ValueError, UnicodeDecodeError):
            self.send_bytes(b"Bad request", 400, "text/plain; charset=utf-8"); return
        if path == "/register":
            name = data.get("full_name", "").strip()
            org = data.get("organization", "").strip()
            email = data.get("email", "").strip().lower()
            topic = data.get("topic", "").strip()
            if not name or not org or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                with database() as con: self.send_bytes(home_page(con, "Проверьте имя, организацию и адрес электронной почты."), 400)
                return
            if len(name) > 100 or len(org) > 120 or len(email) > 160 or len(topic) > 180:
                with database() as con: self.send_bytes(home_page(con, "Одно из полей слишком длинное."), 400)
                return
            with database() as con:
                try:
                    con.execute("BEGIN IMMEDIATE")
                    settings = get_settings(con); cap = max(1, min(200, int(settings.get("capacity", CAPACITY_DEFAULT))))
                    existing = con.execute("SELECT seat FROM attendees WHERE email=?", (email,)).fetchone()
                    if existing:
                        con.rollback(); self.send_bytes(home_page(con, f"Заявка на этот адрес уже есть. Ваше место: {existing['seat']}.")); return
                    used = {r[0] for r in con.execute("SELECT seat FROM attendees")}
                    seat = next((n for n in range(1, cap + 1) if n not in used), None)
                    if seat is None:
                        con.rollback(); self.send_bytes(home_page(con, "Свободных мест пока нет. Обратитесь к организаторам."), 409); return
                    con.execute("INSERT INTO attendees(full_name,organization,email,topic,seat,created_at) VALUES (?,?,?,?,?,?)", (name, org, email, topic, seat, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")))
                    con.commit()
                except sqlite3.IntegrityError:
                    con.rollback(); self.send_bytes(home_page(con, "Не удалось сохранить заявку. Проверьте адрес электронной почты."), 409); return
            self.send_bytes(layout("Заявка принята", f'''<main class="success-main"><div class="success-mark">✓</div><div class="eyebrow dark"><i></i> ЗАЯВКА СОХРАНЕНА</div><h1>До встречи<br>на Байкале.</h1><p>{esc(name)}, вы зарегистрированы. Ваше место в зале:</p><div class="assigned-seat">{seat:02d}</div><a class="button button-dark" href="/">Вернуться на главную <span>↗</span></a></main>'''))
            return
        if path == "/admin/login":
            if hmac.compare_digest(data.get("username", ""), ADMIN_USER) and hmac.compare_digest(data.get("password", ""), ADMIN_PASSWORD):
                token = secrets.token_urlsafe(32); SESSIONS[token] = secrets.token_urlsafe(24)
                self.send_response(303); self.send_header("Location", "/admin"); self.send_header("Set-Cookie", f"portal_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200"); self.end_headers()
            else:
                self.send_bytes(layout("Вход организатора", f'''<main class="login-main"><div class="eyebrow dark"><i></i> ДОСТУП ОРГАНИЗАТОРА</div><h1>Войти<br>в систему.</h1><div class="notice error">Неверный логин или пароль.</div><form method="post" action="/admin/login" class="form-card login-card"><label>Логин<input name="username" required></label><label>Пароль<input type="password" name="password" required></label><button class="button button-dark">Войти <span>↗</span></button></form></main>'''), 401)
            return
        if not self.require_admin(): return
        if not hmac.compare_digest(data.get("csrf", ""), SESSIONS.get(self.session_id(), "")):
            with database() as con: self.send_bytes(self.admin_response(con, "Форма устарела. Обновите страницу и попробуйте ещё раз."), 403)
            return
        with database() as con:
            if path == "/admin/settings":
                title = data.get("title", "").strip()[:100]; subtitle = data.get("subtitle", "").strip()[:160]
                location = data.get("location", "").strip()[:120]; date = data.get("date", "").strip()[:80]
                schedule = "\n".join(line.strip()[:180] for line in data.get("schedule", "").splitlines()[:20] if line.strip())
                try: cap = max(1, min(200, int(data.get("capacity", CAPACITY_DEFAULT))))
                except ValueError: cap = CAPACITY_DEFAULT
                current = con.execute("SELECT COUNT(*) FROM attendees").fetchone()[0]
                if cap < current:
                    self.send_bytes(self.admin_response(con, f"В зале уже зарегистрировано {current} участников. Вместимость нельзя уменьшить ниже этого числа."), 400); return
                values = {"title": title, "subtitle": subtitle, "location": location, "date": date, "capacity": str(cap), "schedule": schedule}
                con.executemany("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())
                self.redirect("/admin")
            elif path == "/admin/seat":
                try: attendee_id, seat = int(data.get("id", "")), int(data.get("seat", ""))
                except ValueError: self.send_bytes(self.admin_response(con, "Укажите корректное место."), 400); return
                cap = int(get_settings(con).get("capacity", CAPACITY_DEFAULT))
                if not 1 <= seat <= cap:
                    self.send_bytes(self.admin_response(con, f"Номер места должен быть от 1 до {cap}."), 400); return
                try:
                    con.execute("UPDATE attendees SET seat=? WHERE id=?", (seat, attendee_id)); con.commit()
                except sqlite3.IntegrityError:
                    self.send_bytes(self.admin_response(con, "Это место уже занято другим участником."), 409); return
                self.redirect("/admin")
            elif path == "/admin/delete":
                try: attendee_id = int(data.get("id", ""))
                except ValueError: self.send_bytes(self.admin_response(con, "Некорректный номер заявки."), 400); return
                con.execute("DELETE FROM attendees WHERE id=?", (attendee_id,)); self.redirect("/admin")
            elif path == "/logout":
                SESSIONS.pop(self.session_id(), None)
                self.send_response(303); self.send_header("Location", "/"); self.send_header("Set-Cookie", "portal_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"); self.end_headers()
            else:
                self.send_bytes(b"Not found", 404, "text/plain; charset=utf-8")

    def redirect(self, path: str) -> None:
        self.send_response(303); self.send_header("Location", path); self.end_headers()


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), PortalHandler)
    print(f"Conference portal is listening at http://{HOST}:{PORT}")
    print(f"Admin login: {ADMIN_USER} / {ADMIN_PASSWORD}")
    print(f"Database: {DB_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
