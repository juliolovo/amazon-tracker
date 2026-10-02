"""Servidor local para el dashboard: python serve.py [puerto] [--no-open]  ->  http://localhost:8765"""

import http.server
import posixpath
import re
import sys
import webbrowser
from functools import partial
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8765)
OPEN_BROWSER = "--no-open" not in sys.argv


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = posixpath.normpath(unquote(self.path.split("?")[0]))
        # Solo exponer el dashboard y los JSON de datos (no el perfil del navegador)
        if path in ("/", "/index.html"):
            self.path = "/web/index.html"
        elif not (path.startswith("/web/") or path == "/data/products.json"
                  or re.fullmatch(r"/data/history/[A-Za-z0-9_-]+\.json", path)):
            self.send_error(404)
            return
        else:
            self.path = path
        super().do_GET()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    # Multi-hilo: los navegadores abren conexiones especulativas que bloquean un servidor de un solo hilo
    with http.server.ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(ROOT))) as httpd:
        url = f"http://localhost:{PORT}"
        print(f"Dashboard en {url}  (Ctrl+C para salir)")
        if OPEN_BROWSER:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
