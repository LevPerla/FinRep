from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
import dash_bootstrap_components as dbc

from src.dashboard import export


@pytest.mark.parametrize('port_env,expected', [({},8050), ({'FINREP_DASH_PORT':'8067'},8067), ({'PORT':'10000','FINREP_DASH_PORT':'8050'},10000)])
def test_export_origin_uses_server_port(monkeypatch, port_env, expected):
    monkeypatch.delenv('PORT', raising=False)
    monkeypatch.delenv('FINREP_DASH_PORT', raising=False)
    monkeypatch.setenv('FINREP_DASH_HOST', '0.0.0.0')
    for key, value in port_env.items():
        monkeypatch.setenv(key, value)
    url = urlsplit(export.build_dashboard_url('RUB', 'month', '2026', '05'))
    assert url.scheme == 'http'
    assert url.netloc == f'127.0.0.1:{expected}'
    assert parse_qs(url.query) == {'currency':['RUB'], 'tab':['month'], 'year':['2026'], 'month':['05']}


@pytest.mark.parametrize('overrides', [
    {'currency':'../../private'}, {'currency':'http://evil.test'},
    {'tab':'../login'}, {'year':'2026/../x'}, {'year':'0000'},
    {'month':'13'}, {'month':'5&tab=input'}, {'month':[]},
    {'export_format':'../pdf'},
])
def test_invalid_export_parameters_never_launch_or_write(monkeypatch, tmp_path, overrides):
    monkeypatch.setattr(export.config, 'REPORTS_PATH', str(tmp_path / 'reports'))
    launch = Mock()
    monkeypatch.setattr(export, 'sync_playwright', launch)
    args = dict(currency='RUB', tab='month', year='2026', month='05', export_format='pdf')
    args.update(overrides)
    with pytest.raises(ValueError):
        export.export_dashboard_page(**args)
    launch.assert_not_called()
    assert not (tmp_path / 'reports').exists()


def _route(url, resource='document', status=200):
    route = Mock()
    route.request.url = url
    route.request.resource_type = resource
    route.request.method = 'GET'
    route.request.headers = {'cookie':'session=private', 'authorization':'secret', 'referer':'private'}
    route.fetch.return_value.status = status
    return route


@pytest.mark.parametrize('url', ['http://127.0.0.1:8068/', 'https://evil.test/', 'http://127.0.0.1:8050@evil.test/', 'file:///etc/passwd', dbc.themes.BOOTSTRAP+'?secret=1'])
def test_other_origins_and_files_are_blocked_before_request(url):
    route = _route(url)
    export._route_export_request(route, 'http://127.0.0.1:8050')
    route.abort.assert_called_once()
    route.fetch.assert_not_called()


@pytest.mark.parametrize('status', [301,302,303,307,308])
def test_redirects_are_not_followed_or_delivered(status):
    route = _route('http://127.0.0.1:8050/redirect', status=status)
    export._route_export_request(route, 'http://127.0.0.1:8050')
    assert route.fetch.call_args.kwargs['max_redirects'] == 0
    route.abort.assert_called_once()
    route.fulfill.assert_not_called()
    route.fetch.return_value.dispose.assert_called_once()


def test_local_dash_request_and_fixed_stylesheet_policy():
    route = _route('http://127.0.0.1:8050/_dash-layout')
    export._route_export_request(route, 'http://127.0.0.1:8050')
    route.fulfill.assert_called_once()
    css = _route(dbc.themes.BOOTSTRAP, resource='stylesheet')
    export._route_export_request(css, 'http://127.0.0.1:8050')
    headers = css.fetch.call_args.kwargs['headers']
    assert headers.get('cookie', '') == ''
    assert headers.get('authorization', '') == ''
    assert headers.get('referer', '') == ''
    css.fulfill.assert_called_once()
    page = _route(dbc.themes.BOOTSTRAP)
    export._route_export_request(page, 'http://127.0.0.1:8050')
    page.fetch.assert_not_called()


@pytest.mark.parametrize('section,expected_tab', [('overview','main'), ('expenses','expenses'), ('income','income')])
def test_callback_has_no_client_url_and_ignores_host(monkeypatch, tmp_path, section, expected_tab):
    from src.dashboard.app import create_app
    import importlib
    monkeypatch.setenv('FINREP_DASH_PASSWORD', 'synthetic-password')
    monkeypatch.setenv('FINREP_DASH_SECRET_KEY', 'synthetic-key')
    app_module = importlib.import_module('src.dashboard.app')
    app = create_app()
    client = app.server.test_client()
    client.post('/login', data={'password':'synthetic-password', 'data_mode':'live'})
    # Model an authenticated crafted request; the test client's cookie jar is host-scoped.
    client.set_cookie('session', client.get_cookie('session').value, domain='evil.test')
    key = next(key for key in app.callback_map if 'page-export-download.data' in key)
    callback = app.callback_map[key]
    payload = {'output':key, 'outputs':[{'id':item.component_id,'property':item.component_property} for item in callback['output']],
        'inputs':[{'id':'export-png','property':'n_clicks','value':1},{'id':'export-pdf','property':'n_clicks','value':0}],
        'state':[{'id':'dashboard-currency','property':'value','value':'RUB'}, {'id':'dashboard-year','property':'value','value':'2026'}, {'id':'dashboard-month','property':'value','value':'05'}, {'id':'dashboard-tabs','property':'active_tab','value':'main'}, {'id':'main-report-tabs','property':'active_tab','value':section}, {'id':'dashboard-locale','property':'data','value':'ru'}],
        'changedPropIds':['export-png.n_clicks']}
    registered = callback['state']
    assert not any(item['id']=='dashboard-location' for item in registered)
    output = tmp_path/'output.png'
    output.write_bytes(b'png fixture')
    render = Mock(return_value=output)
    monkeypatch.setattr(app_module, 'export_dashboard_page', render)
    response = client.post('/_dash-update-component', json=payload, headers={'Host':'evil.test'})
    assert response.status_code == 200
    assert render.call_args.args == ('RUB',expected_tab,'png')
    assert render.call_args.kwargs['locale'] == 'ru'
    assert 'evil.test' not in str(render.call_args)


def test_local_entrypoint_uses_same_port_as_export(monkeypatch):
    import runpy
    from pathlib import Path
    from dash import Dash
    monkeypatch.setenv('PORT', '10000')
    monkeypatch.setenv('FINREP_DASH_PORT', '8050')
    run = Mock()
    monkeypatch.setattr(Dash, 'run', run)
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'src/dashboard/app.py'), run_name='__main__')
    assert run.call_args.kwargs['port'] == urlsplit(export.build_dashboard_url('RUB','main')).port == 10000
