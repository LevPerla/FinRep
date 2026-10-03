from decimal import Decimal
from io import BytesIO
import json

from openpyxl import Workbook
import pytest

from src.data import inflation
from src.data.sqlite_store import (
    cpi_observations,
    cpi_series,
    connect_database,
    initialize_database,
    save_cpi_observations,
)


class FakeResponse:
    def __init__(self, payload: bytes, *, url="https://official.example/data"):
        self.content = payload
        self.url = url
        self.status_code = 200

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)

    def get(self, _url, **_kwargs):
        return self.responses.pop(0)


def _workbook_bytes(rows, *, title="ИПЦ"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_cpi_schema_seeds_one_official_series_per_supported_currency(tmp_path):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)

    registry = {row["currency_code"]: row for row in cpi_series(database)}

    assert set(registry) == {"RUB", "KZT", "USD", "GBP", "EUR"}
    assert registry["RUB"]["provider_id"] == "world_bank_gem"
    assert registry["RUB"]["series_code"] == "CPTOTNSXN"
    assert registry["RUB"]["index_method"] == "published_index"
    assert registry["KZT"]["provider_id"] == "world_bank_gem+stat_kz"
    assert registry["EUR"]["territory_code"] == "EA"
    assert registry["EUR"]["series_code"] == "prc_hicp_minr.I25.TOTAL.EA"
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA table_list('cpi_observations')").fetchone()["strict"] == 1


def test_cpi_versions_are_idempotent_and_latest_release_wins(tmp_path):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)
    common = {
        "currency": "RUB",
        "observations": [{"period": "2026-01", "index_value": "101.2"}],
        "payload_sha256": "a" * 64,
        "fetched_at": "2026-02-10T00:00:00Z",
        "source_version": "release-1",
        "published_on": "2026-02-10",
    }

    assert save_cpi_observations(database, **common) == {
        "submitted": 1, "inserted": 1, "unchanged": 0}
    assert save_cpi_observations(database, **common) == {
        "submitted": 1, "inserted": 0, "unchanged": 1}
    save_cpi_observations(
        database,
        **{**common, "observations": [{"period": "2026-01", "index_value": "101.3"}],
           "payload_sha256": "b" * 64, "source_version": "release-2",
           "fetched_at": "2026-03-10T00:00:00Z"},
    )

    rows = cpi_observations(database, currency="RUB")
    assert rows[0]["index_value"] == Decimal("101.3")
    with connect_database(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM cpi_observations").fetchone()[0] == 2


def test_cpi_batch_rejects_duplicates_and_rolls_back(tmp_path):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)

    with pytest.raises(ValueError, match="duplicate CPI period"):
        save_cpi_observations(
            database,
            currency="USD",
            observations=[
                {"period": "2026-01", "index_value": "100"},
                {"period": "2026-01", "index_value": "101"},
            ],
            source_version="duplicate",
            payload_sha256="c" * 64,
            fetched_at="2026-02-01T00:00:00Z",
        )
    assert cpi_observations(database, currency="USD") == []


def test_bls_parser_keeps_months_and_skips_unpublished_values():
    payload = json.dumps({
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": [
            {"year": "2026", "period": "M02", "value": "326.785"},
            {"year": "2026", "period": "M01", "value": "-"},
            {"year": "2025", "period": "M13", "value": "320.0"},
        ]}]},
    }).encode()

    empty_payload = json.dumps({
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUUR0000SA0", "data": []}]},
    }).encode()
    release = inflation.fetch_bls_cpi(
        FakeSession([FakeResponse(empty_payload), FakeResponse(payload)]), end_year=2026)

    assert release.currency == "USD"
    assert release.observations == (
        {"period": "2026-02", "index_value": "326.785"},)


def test_ons_parser_uses_release_date_and_monthly_rows():
    payload = b'"Title","CPI INDEX 00: ALL ITEMS 2015=100"\n"CDID","D7BT"\n' \
              b'"Release date","16-09-2026"\n"2014 DEC","100.1"\n' \
              b'"2026 JUL","142.9"\n'

    release = inflation.fetch_ons_cpi(FakeSession([FakeResponse(payload)]))

    assert release.published_on == "2026-09-16"
    assert release.observations == (
        {"period": "2026-07", "index_value": "142.9"},)


def test_eurostat_parser_reads_sparse_json_stat_values():
    payload = json.dumps({
        "id": ["freq", "unit", "coicop18", "geo", "time"],
        "updated": "2026-10-02T11:00:00+0200",
        "dimension": {"time": {"category": {"index": {
            "2026-07": 0, "2026-08": 1, "2026-09": 2}}}},
        "value": {"0": 103.24, "2": 104.3},
    }).encode()

    release = inflation.fetch_eurostat_cpi(FakeSession([FakeResponse(payload)]))

    assert release.published_on == "2026-10-02"
    assert release.observations == (
        {"period": "2026-07", "index_value": "103.24"},
        {"period": "2026-09", "index_value": "104.3"},
    )


def test_world_bank_gem_parser_reads_monthly_russia_cpi_and_release_date():
    payload = json.dumps([{
        "page": 1, "pages": 1, "sourceid": "15", "lastupdated": "2026-09-08",
    }, [
        {"indicator": {"id": "CPTOTNSXN"}, "country": {"id": "RUS"},
         "date": "2026M07", "value": 311.35433314398},
        {"indicator": {"id": "CPTOTNSXN"}, "country": {"id": "RUS"},
         "date": "2026M06", "value": 309.68205071434},
        {"indicator": {"id": "CPTOTNSXN"}, "country": {"id": "RUS"},
         "date": "2026M08", "value": None},
    ]]).encode()

    release = inflation.fetch_world_bank_russia_cpi(
        FakeSession([FakeResponse(payload)]), end_year=2026)

    assert release.currency == "RUB"
    assert release.published_on == "2026-09-08"
    assert release.observations == (
        {"period": "2026-06", "index_value": "309.68205071434"},
        {"period": "2026-07", "index_value": "311.35433314398"},
    )


def test_kazakhstan_parser_chains_monthly_rates_without_inventing_future_months():
    payload = _workbook_bytes([
        ["Индекс потребительских цен"], ["в процентах"], [None, "К предыдущему месяцу"],
        [None, "товары и услуги"], [None], [2022],
        ["Январь", 110], ["Февраль", 120], ["Март", None],
    ])

    page = b'''<div class="divTableRow"><div class="divTableCell">
      \xd0\x98\xd0\xbd\xd0\xb4\xd0\xb5\xd0\xba\xd1\x81 \xd0\xbf\xd0\xbe\xd1\x82\xd1\x80\xd0\xb5\xd0\xb1\xd0\xb8\xd1\x82\xd0\xb5\xd0\xbb\xd1\x8c\xd1\x81\xd0\xba\xd0\xb8\xd1\x85 \xd1\x86\xd0\xb5\xd0\xbd \xd0\xb8 \xd0\xb5\xd0\xb3\xd0\xbe \xd1\x81\xd0\xbe\xd1\x81\xd1\x82\xd0\xb0\xd0\xb2\xd0\xbb\xd1\x8f\xd1\x8e\xd1\x89\xd0\xb8\xd0\xb5
      </div><div>01.10.2026</div><a href="/api/cpi.xlsx">xlsx</a></div>'''
    release = inflation.fetch_kazakhstan_cpi(FakeSession([
        FakeResponse(page, url="https://stat.gov.kz/ru/dynamic-tables/"),
        FakeResponse(payload, url="https://stat.gov.kz/api/cpi.xlsx"),
    ]))

    assert release.observations == (
        {"period": "2022-01", "index_value": "110"},
        {"period": "2022-02", "index_value": "132"},
    )
    assert release.published_on == "2026-10-01"


def test_kazakhstan_hybrid_splices_official_rates_onto_world_bank_history():
    world_bank = json.dumps([{
        "page": 1, "pages": 1, "sourceid": "15", "lastupdated": "2026-09-08",
    }, [
        {"indicator": {"id": "CPTOTNSXN"}, "country": {"id": "KAZ"},
         "date": "2021M12", "value": 200},
        {"indicator": {"id": "CPTOTNSXN"}, "country": {"id": "KAZ"},
         "date": "2016M01", "value": 100},
    ]]).encode()
    workbook = _workbook_bytes([
        ["Индекс потребительских цен"], ["в процентах"],
        [None, "К предыдущему месяцу"], [None, "товары и услуги"], [None],
        [2022], ["Январь", 110], ["Февраль", 120],
    ])
    page = b'''<div class="divTableRow"><div class="divTableCell">
      \xd0\x98\xd0\xbd\xd0\xb4\xd0\xb5\xd0\xba\xd1\x81 \xd0\xbf\xd0\xbe\xd1\x82\xd1\x80\xd0\xb5\xd0\xb1\xd0\xb8\xd1\x82\xd0\xb5\xd0\xbb\xd1\x8c\xd1\x81\xd0\xba\xd0\xb8\xd1\x85 \xd1\x86\xd0\xb5\xd0\xbd \xd0\xb8 \xd0\xb5\xd0\xb3\xd0\xbe \xd1\x81\xd0\xbe\xd1\x81\xd1\x82\xd0\xb0\xd0\xb2\xd0\xbb\xd1\x8f\xd1\x8e\xd1\x89\xd0\xb8\xd0\xb5
      </div><div>01.10.2026</div><a href="/api/cpi.xlsx">xlsx</a></div>'''

    release = inflation.fetch_kazakhstan_hybrid_cpi(FakeSession([
        FakeResponse(world_bank),
        FakeResponse(page, url="https://stat.gov.kz/ru/dynamic-tables/"),
        FakeResponse(workbook, url="https://stat.gov.kz/api/cpi.xlsx"),
    ]), end_year=2026)

    assert [(item["period"], item["index_value"], item["provider_id"])
            for item in release.observations] == [
        ("2016-01", "100", "world_bank_gem"),
        ("2021-12", "200", "world_bank_gem"),
        ("2022-01", "220", "stat_kz"),
        ("2022-02", "264", "stat_kz"),
    ]
    assert release.published_on == "2026-10-01"


def test_cpi_observations_keep_point_level_source_provenance(tmp_path):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)
    observations = [
        {"period": "2021-12", "index_value": "200",
         "provider_id": "world_bank_gem", "source_name": "World Bank GEM",
         "source_url": "https://example.com/world-bank"},
        {"period": "2022-01", "index_value": "220",
         "provider_id": "stat_kz", "source_name": "Kazakhstan statistics",
         "source_url": "https://example.com/kazakhstan"},
    ]

    save_cpi_observations(
        database, currency="KZT", observations=observations,
        source_version="hybrid-1", payload_sha256="f" * 64,
        fetched_at="2026-10-03T00:00:00Z")

    rows = cpi_observations(database, currency="KZT")
    assert [(row["period"], row["provider_id"]) for row in rows] == [
        ("2021-12", "world_bank_gem"), ("2022-01", "stat_kz")]


def test_rosstat_parser_follows_current_official_workbook_link():
    rows = [[None] * 3 for _ in range(17)]
    rows[3] = [None, 2015, 2016]
    rows[5] = ["январь", 110, 120]
    rows[6] = ["февраль", 105, None]
    workbook = _workbook_bytes(rows, title="01")
    page = b'<a href="/storage/mediabank/ipc_mes_08-2026.xlsx">XLSX</a>'

    release = inflation.fetch_rosstat_cpi(FakeSession([
        FakeResponse(page, url="https://rosstat.gov.ru/statistics/price?print=1"),
        FakeResponse(workbook, url="https://rosstat.gov.ru/storage/mediabank/ipc_mes_08-2026.xlsx"),
    ]))

    assert release.observations == (
        {"period": "2015-01", "index_value": "110"},
        {"period": "2015-02", "index_value": "115.5"},
        {"period": "2016-01", "index_value": "138.6"},
    )


def test_real_value_uses_selected_price_month_and_never_fills_missing_cpi():
    indexes = {"2026-01": Decimal("100"), "2026-02": Decimal("110")}

    assert inflation.real_value(
        Decimal("5000000"), observation_period="2026-01",
        base_period="2026-02", indexes=indexes) == Decimal("5500000")
    with pytest.raises(inflation.CPIUnavailableError, match="2025-12"):
        inflation.real_value(
            1, observation_period="2025-12", base_period="2026-02", indexes=indexes)


def test_refresh_commits_successful_sources_and_reports_partial_failure(tmp_path, monkeypatch):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)
    release = inflation.CPIRelease(
        "USD", ({"period": "2026-01", "index_value": "100"},),
        "version", "d" * 64, "2026-02-01")
    monkeypatch.setitem(inflation._FETCHERS, "USD", lambda _session: release)
    monkeypatch.setitem(
        inflation._FETCHERS, "GBP", lambda _session: (_ for _ in ()).throw(ValueError("offline")))

    result = inflation.refresh_official_cpi(
        database, currencies=["USD", "GBP"], session=object(),
        fetched_at="2026-02-01T00:00:00Z")

    assert result["status"] == "partial"
    assert [item["status"] for item in result["results"]] == ["updated", "error"]
    assert cpi_observations(database, currency="USD")[0]["index_value"] == Decimal("100")


def test_official_workbook_import_is_idempotent_fallback_for_rub(tmp_path):
    database = tmp_path / "cpi.sqlite3"
    initialize_database(database)
    rows = [[None] * 2 for _ in range(17)]
    rows[3] = [None, 2026]
    rows[5] = ["январь", 110]
    payload = _workbook_bytes(rows, title="01")

    first = inflation.import_official_cpi_workbook(
        database, currency="RUB", payload=payload,
        fetched_at="2026-02-01T00:00:00Z")
    second = inflation.import_official_cpi_workbook(
        database, currency="RUB", payload=payload,
        fetched_at="2026-02-02T00:00:00Z")

    assert first["results"][0]["inserted"] == 1
    assert second["results"][0]["unchanged"] == 1
    stored = cpi_observations(database, currency="RUB")[0]
    assert stored["index_value"] == Decimal("110")
    assert stored["provider_id"] == "rosstat"
