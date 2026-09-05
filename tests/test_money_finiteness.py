from pathlib import Path

import pandas as pd
import pytest

from src import config
from src.data import assets_editor, crypto, debts, get_finance, investments, staging, validation

BAD = [float('inf'), float('-inf'), float('nan'), 'Infinity', '-Infinity', 'NaN', '1e10000', 'not-a-number']


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_PATH', str(tmp_path))
    get_finance.set_fx_network_enabled(False)
    get_finance._FX_CACHE_DF.clear()
    staging.ensure_transaction_drafts_file()
    debts.ensure_debt_files()
    return tmp_path


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*.csv')}


@pytest.mark.parametrize('bad', BAD)
@pytest.mark.parametrize('action', ['append', 'edit', 'merge', 'month_preview'])
def test_transaction_rejection_preserves_files(data_root, bad, action):
    staging.append_transaction_draft('2026-01-01', 'Прочее', 'RUB', 10, source_id='valid')
    target = staging.monthly_transaction_csv_path('2026', '01')
    target.parent.mkdir(parents=True)
    pd.DataFrame([{'Дата':'01.01.2026', 'Прочее':'5|RUB|previous'}]).to_csv(target, sep=';', index=False)
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        if action == 'append':
            staging.append_transaction_draft('2026-01-02', 'Прочее', 'RUB', bad)
        elif action == 'edit':
            staging.update_transaction_draft('manual', 'valid', {'amount':bad})
        elif action == 'merge':
            row = staging.read_transaction_drafts().iloc[0].to_dict()
            row['amount'] = bad
            staging.merge_transaction_draft_rows([row])
        else:
            staging.export_monthly_transaction_drafts('2026','01', preview_rows=[{'Дата':'01.01.2026','Прочее':f'{bad}|RUB|invalid'}])
    assert snapshot(data_root) == before


@pytest.mark.parametrize('bad', BAD)
@pytest.mark.parametrize('field', ['principal_amount','cash_amount'])
def test_invalid_debt_does_not_create_cash_movement(data_root, bad, field):
    args = dict(debt_type='receivable', counterparty='Synthetic', opened_date='2026-01-01', principal_amount=100, principal_currency='RUB', cash_amount=100)
    args[field] = bad
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        debts.create_debt(**args)
    assert snapshot(data_root) == before


@pytest.mark.parametrize('bad', BAD)
@pytest.mark.parametrize('field', ['amount','cash_amount'])
def test_invalid_payment_preserves_debt_payment_and_draft(data_root, bad, field):
    debt = debts.create_debt('receivable','Synthetic','2026-01-01',100,'RUB')
    args = dict(debt_id=debt['debt_id'], date='2026-01-02', amount=10, cash_amount=10)
    args[field] = bad
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        debts.create_debt_payment(**args)
    assert snapshot(data_root) == before


@pytest.mark.parametrize('bad', BAD)
def test_invalid_asset_does_not_overwrite_or_create_snapshot(data_root, bad):
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        assets_editor.write_asset_snapshot([{'account':'Synthetic','amount':bad,'currency':'RUB'}], '2026','01')
    assert snapshot(data_root) == before


@pytest.mark.parametrize('bad', BAD)
@pytest.mark.parametrize('field', ['quantity','price','fee'])
def test_investment_validator_rejects_nonfinite(data_root, bad, field):
    row = dict(date='2026-01-01', operation='buy', asset_type='stocks', ticker='TEST', quantity=1, price=100, fee=0, currency='USD', account='', comment='')
    row[field] = bad
    issues = investments.validate_investment_transactions(pd.DataFrame([row]))
    assert any(field in issue.message for issue in issues)


@pytest.mark.parametrize('bad', BAD)
@pytest.mark.parametrize('kind', ['price','balance','crypto_quantity','crypto_fee','fx'])
def test_bad_cached_or_crypto_values_preserve_files(data_root, bad, kind):
    if kind == 'price':
        writer = investments.write_price_cache
        row = dict(date='2026-01-01',ticker='TEST',price=10,currency='USD',source='synthetic',fetched_at='2026-01-01')
        field = 'price'
    elif kind == 'balance':
        writer = crypto.write_crypto_balances
        row = dict(fetched_at='2026-01-01', account='Synthetic', chain='bitcoin', asset='BTC', address='synthetic', balance=1, source='synthetic')
        field = 'balance'
    elif kind == 'fx':
        writer = get_finance._write_cache
        row = dict(date='2026-01-01',currency='EUR',usd_rate=1.1,source='synthetic',fetched_at='2026-01-01')
        field = 'usd_rate'
    else:
        writer = crypto.write_crypto_transactions
        row = dict(date='2026-01-01',account='Synthetic',chain='bitcoin',asset='BTC',address='synthetic',tx_id='synthetic',operation='transfer',quantity=1,fee=0,counterparty='',source='synthetic',comment='')
        field = 'quantity' if kind == 'crypto_quantity' else 'fee'
    writer(pd.DataFrame([row]))
    before = snapshot(data_root)
    row[field] = bad
    with pytest.raises(ValueError):
        writer(pd.DataFrame([row]))
    assert snapshot(data_root) == before


@pytest.mark.parametrize('amount', [0,-10.25,10.25,123456789.99])
def test_valid_transaction_amounts_keep_existing_semantics(data_root, amount):
    staging.append_transaction_draft('2026-01-01','Прочее','RUB',amount)
    result = staging.export_monthly_transaction_drafts('2026','01')
    month = pd.read_csv(result['target_path'],sep=';')
    value = str(month.loc[0,'Прочее']).split('|')[0].replace(',','.')
    assert float(value) == amount


def test_optional_crypto_quantity_and_fee_defaults_are_preserved(data_root):
    crypto.write_crypto_transactions(pd.DataFrame([{'quantity':'','fee':'0'}]))
    assert crypto.read_crypto_transactions().iloc[0]['quantity'] == ''
    row = dict(date='2026-01-01',operation='buy',asset_type='stocks',ticker='TEST',quantity=1,price=0,currency='USD',fee='',account='',comment='')
    assert investments.validate_investment_transactions(pd.DataFrame([row])) == []


def test_legacy_csv_literal_nan_fee_is_not_silently_zero(data_root):
    path = data_root/'investments'/'transactions.csv'
    path.parent.mkdir()
    row = dict(date='2026-01-01',operation='buy',asset_type='stocks',ticker='TEST',quantity=1,price=100,currency='USD',fee='NaN',account='',comment='')
    pd.DataFrame([row]).to_csv(path,sep=';',index=False)
    issues = investments.validate_investment_transactions(path=path)
    assert any('fee' in issue.message for issue in issues)


def test_fx_series_drops_unusable_quotes(data_root):
    rates = pd.Series([1.1,float('inf'),float('-inf'),float('nan')],index=pd.date_range('2026-01-01',periods=4))
    assert get_finance._clean_rate_series(rates).tolist() == [1.1]


@pytest.mark.parametrize('bad', ['inf','-inf','NaN','1e10000'])
def test_legacy_file_validation_rejects_nonfinite_money(bad):
    assert validation._parse_money_cell(f'{bad}|RUB|comment', expected_parts=3) is None


def test_preview_preserves_legacy_comments_and_empty_cells(data_root):
    cell = '80,93|EUR|comment # tag|with pipes#1200|KZT|next'
    result = staging.export_monthly_transaction_drafts('2026','01',preview_rows=[{'Дата':'01.01.2026','Прочее':cell,'Доход':None}])
    frame = pd.read_csv(result['target_path'],sep=';')
    assert frame.loc[0,'Прочее'] == cell
    assert frame.loc[0,'Доход'] == 0


@pytest.mark.parametrize('bad', [float('nan'),float('inf'),pd.NA])
def test_raw_nonfinite_preview_does_not_turn_into_zero(data_root, bad):
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        staging.export_monthly_transaction_drafts('2026','01',preview_rows=[{'Дата':'01.01.2026','Прочее':bad}])
    assert snapshot(data_root) == before


def test_finite_debt_and_partial_payment_keep_balance(data_root):
    debt = debts.create_debt('receivable','Synthetic','2026-01-01','100,50','RUB')
    debts.create_debt_payment(debt['debt_id'],'2026-01-02','25,25')
    balance = debts.active_debt_balances('receivable', 'RUB')
    assert balance.iloc[0]['outstanding_amount'] == 75.25


def test_legacy_migration_rejects_infinite_price_before_writes(data_root):
    legacy = data_root/'legacy.csv'
    pd.DataFrame([{'Тип_транзакции':'Покупка','Актив':'Акции','Тикер':'TEST','Количество':1,'Дата':'01.01.2026','Цена':'inf|USD'}]).to_csv(legacy,sep=';',index=False)
    before = snapshot(data_root)
    with pytest.raises(ValueError,match='price'):
        investments.export_legacy_investment_migration(legacy_path=legacy)
    assert snapshot(data_root) == before


def test_fx_nullable_missing_rate_is_rejected(data_root):
    cache = pd.DataFrame({'date':['2026-01-01'],'currency':['EUR'],'usd_rate':pd.Series([pd.NA],dtype='Float64'),'source':['synthetic'],'fetched_at':['2026-01-01']})
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        get_finance._write_cache(cache)
    assert snapshot(data_root) == before


def test_csv_validation_does_not_hide_literal_nan(data_root):
    target = data_root/'transactions_info'/'2026'/'2026_01_.csv'
    target.parent.mkdir(parents=True)
    target.write_text('Дата;Прочее\n01.01.2026;NaN\n')
    assert any('invalid value' in issue.message for issue in validation.validate_transactions())


@pytest.mark.parametrize('bad', BAD)
def test_manual_callback_shows_validation_error_without_saving(data_root, monkeypatch, bad):
    monkeypatch.setenv('FINREP_DASH_PASSWORD','synthetic-password')
    monkeypatch.setenv('FINREP_DASH_SECRET_KEY','synthetic-key')
    from src.dashboard.app import create_app
    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session['authenticated'] = True
        session['data_mode'] = 'live'
    key = next(key for key in app.callback_map if 'transaction-input-message.children' in key)
    callback = app.callback_map[key]
    values = {'transaction-add-button':1, 'transaction-save-grid-button':0, 'transaction-delete-button':0,
              'transaction-input-date':'2026-01-01', 'transaction-input-category':'Прочее',
              'transaction-input-currency':'RUB', 'transaction-input-amount':bad,
              'transaction-input-comment':'Synthetic', 'transaction-drafts-grid':[]}
    payload = {'output':key, 'outputs':[{'id':item.component_id,'property':item.component_property} for item in callback['output']],
               'inputs':[{**item,'value':values.get(item['id'])} for item in callback['inputs']],
               'state':[{**item,'value':values.get(item['id'])} for item in callback['state']],
               'changedPropIds':['transaction-add-button.n_clicks']}
    before = snapshot(data_root)
    response = client.post('/_dash-update-component',json=payload)
    assert response.status_code == 200
    message = response.get_json()['response']['transaction-input-message']
    assert message['color'] == 'danger'
    assert 'amount' in message['children']
    assert snapshot(data_root) == before


def test_existing_infinite_month_cell_is_not_carried_into_new_save(data_root):
    target = staging.monthly_transaction_csv_path('2026','01')
    target.parent.mkdir(parents=True)
    target.write_text('Дата;Прочее\n01.01.2026;inf|RUB|old\n')
    staging.append_transaction_draft('2026-01-02','Прочее','RUB',10)
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        staging.export_monthly_transaction_drafts('2026','01')
    assert snapshot(data_root) == before


def test_invalid_old_staging_is_detected_before_month_write(data_root):
    staging.append_transaction_draft('2026-01-01','Прочее','RUB',10)
    staging.append_transaction_draft('2026-02-01','Прочее','RUB',20)
    path = data_root/'staging'/'transaction_drafts.csv'
    raw = pd.read_csv(path,sep=';',dtype=str)
    raw.loc[1,'amount'] = 'inf'
    raw.to_csv(path,sep=';',index=False)
    before = snapshot(data_root)
    with pytest.raises(ValueError):
        staging.export_monthly_transaction_drafts('2026','01',preview_rows=[{'Дата':'01.01.2026','Прочее':'10|RUB|valid'}])
    assert snapshot(data_root) == before
