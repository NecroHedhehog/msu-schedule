"""
Мини-копия сайта соцфака на localhost — для тестов без интернета.

Отдаёт ровно ту разметку, за которую цепляется SocioParser:
ссылки ?f= / ?sp= / ?gr=, select name="yr", таблицы с td.TmTblC и div#LESS.
Умеет по команде падать (503) заданное число раз — чтобы проверять ретраи.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


PAGE = """<html><head><title>t</title></head><body>{body}</body></html>"""


def lesson_div(subject, abbr, room, kind, groups, teacher):
    return (
        f'<div id="LESS" title="Лекция по \'{subject}\'">'
        f'<font color="#004000"><b>{abbr}</b></font>'
        f'<b>{room}</b><font>{kind}</font>'
        f'[{groups}]{teacher}</div>'
    )


def broken_div(title):
    """Блок, который парсер не разберёт: title не ложится на регулярку."""
    return f'<div id="LESS" title="{title}"><b>???</b></div>'


def day_table(date_str, pairs):
    """pairs: список списков html-блоков, индекс = номер пары - 1."""
    cells = ''.join(f'<td class="TmTblC">{"".join(blocks)}</td>' for blocks in pairs)
    return f'<table><tr><td>{date_str}</td></tr><tr>{cells}</tr></table>'


class FakeSite:
    """Состояние фейкового сайта: сколько раз какой путь должен упасть."""

    def __init__(self):
        self.fail_times = {}      # 'ключ пути' -> сколько раз ещё отдать 503
        self.status_for = {}      # 'ключ пути' -> постоянный код ответа
        self.hits = {}            # 'ключ пути' -> сколько раз запросили
        self.groups = {}          # site_id -> код группы
        self.departments = {}     # f_id -> название

    @staticmethod
    def key(qs):
        """Ключ страницы: все узнанные параметры, а не первый попавшийся.
        Иначе ?gr=101&pMns=9.2026 неотличим от ?gr=101."""
        parts = [f"{name}={qs[name][0]}"
                 for name in ('mnu', 'f', 'sp', 'yr', 'gr', 'prr', 'selst', 'pMns')
                 if name in qs]
        return '&'.join(parts) if parts else 'index'


def make_handler(site: FakeSite, body_for):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            key = site.key(qs)
            site.hits[key] = site.hits.get(key, 0) + 1

            if site.status_for.get(key):
                self.send_error(site.status_for[key])
                return

            left = site.fail_times.get(key, 0)
            if left > 0:
                site.fail_times[key] = left - 1
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'busy')
                return

            body = body_for(qs, key)
            if body is None:
                self.send_error(404)
                return

            data = PAGE.format(body=body).encode('cp1251', errors='replace')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=windows-1251')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def start(site: FakeSite, body_for):
    server = HTTPServer(('127.0.0.1', 0), make_handler(site, body_for))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://127.0.0.1:{port}"


def stop(server):
    """
    Остановить сервер и закрыть слушающий сокет.

    Одного shutdown() мало: он останавливает цикл обработки, но сокет
    остаётся открытым, и Python сыплет ResourceWarning на каждый тест.
    """
    server.shutdown()
    server.server_close()
