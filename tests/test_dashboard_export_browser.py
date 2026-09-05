"""Opt-in real Chromium checks: FINREP_BROWSER_TESTS=1 python -m pytest this_file."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
import dash_bootstrap_components as dbc

from src.dashboard import export

pytestmark = pytest.mark.skipif(os.environ.get('FINREP_BROWSER_TESTS') != '1', reason='requires local listeners and installed Chromium')


@pytest.fixture
def servers(monkeypatch, tmp_path):
    received = {'app':[], 'outside':[]}
    redirect_main = [False]

    class Outside(BaseHTTPRequestHandler):
        def do_GET(self):
            received['outside'].append((self.path, self.headers.get('Cookie')))
            self.send_response(200)
            self.send_header('Content-Type', 'text/css')
            self.end_headers()
            self.wfile.write(b'body { background: rgb(240, 240, 240); }')
        def log_message(self, *args):
            pass

    outside = ThreadingHTTPServer(('127.0.0.1', 0), Outside)
    outside_url = f'http://127.0.0.1:{outside.server_port}'

    class App(BaseHTTPRequestHandler):
        def do_GET(self):
            received['app'].append((self.path, self.headers.get('Cookie')))
            if self.path == '/redirect' or redirect_main[0]:
                self.send_response(302)
                self.send_header('Location', outside_url+'/leaked')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(f'''<html><head><link rel="stylesheet" href="{outside_url}/bootstrap.css"></head>
                <body><div id="dashboard-content"><h1>Export security fixture</h1></div>
                <img src="{outside_url}/image"><iframe src="/redirect"></iframe>
                <script>fetch('{outside_url}/fetch').catch(()=>{{}});
                new WebSocket('ws://127.0.0.1:{outside.server_port}/socket');</script></body></html>'''.encode())
        def log_message(self, *args):
            pass

    app = ThreadingHTTPServer(('127.0.0.1', 0), App)
    threads = [Thread(target=s.serve_forever, daemon=True) for s in (app, outside)]
    for thread in threads:
        thread.start()
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.setenv('FINREP_DASH_PORT', str(app.server_port))
    monkeypatch.setattr(export.config, 'REPORTS_PATH', str(tmp_path))
    monkeypatch.setattr(dbc.themes, 'BOOTSTRAP', outside_url+'/bootstrap.css')
    try:
        yield received, redirect_main
    finally:
        for server in (app, outside):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()


@pytest.mark.parametrize('format', ['png', 'pdf'])
def test_real_export_keeps_cookie_local_and_blocks_redirects_resources(servers, format):
    received, _ = servers
    output = export.export_dashboard_page('RUB', 'main', format, session_cookie='synthetic-session')
    assert output.read_bytes().startswith(b'\x89PNG' if format == 'png' else b'%PDF')
    assert received['app']
    assert all(cookie == 'session=synthetic-session' for _, cookie in received['app'])
    assert [path for path, _ in received['outside']] == ['/bootstrap.css']
    assert all(not cookie for _, cookie in received['outside'])


def test_top_level_redirect_cannot_forward_cookie(servers):
    received, redirect_main = servers
    redirect_main[0] = True
    with pytest.raises(RuntimeError):
        export.export_dashboard_page('RUB', 'main', 'png', session_cookie='synthetic-session')
    assert received['outside'] == []
