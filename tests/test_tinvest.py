import copy
import json
import unittest
from pathlib import Path
from unittest import mock

import requests

from jessica.sources import tinvest

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "tinvest" / "responses.json").read_text(encoding="utf-8")
)
TOKEN = "t.SECRET-TEST-TOKEN"


class FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeApi:
    """Подменяет requests.post. failures: метод → список исходов по порядку
    (код HTTP или исключение); когда список кончается, отдаётся фикстура."""

    def __init__(self, fixtures=None, failures=None):
        self.fixtures = fixtures or FIXTURES
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None, verify=None):
        method = url[len(tinvest.API):]
        self.calls.append({"method": method, "payload": json, "headers": headers, "verify": verify})
        queue = self.failures.get(method)
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome, {})
        name = method.split("/")[1]
        data = self.fixtures[name]
        if name in ("BondBy", "ShareBy", "GetAssetBy"):
            data = data[json["id"]]
        return FakeResponse(200, data)

    def count(self, name):
        return sum(1 for call in self.calls if call["method"].endswith("/" + name))


class FetchBondsTest(unittest.TestCase):
    def fetch(self, api, **kwargs):
        with mock.patch.object(tinvest.requests, "post", api), mock.patch.object(tinvest, "PAUSE", 0):
            return tinvest.fetch_positions(TOKEN, **kwargs)

    def test_only_bonds_with_issuer_and_code(self):
        bonds = {b["isin"]: b for b in self.fetch(FakeApi())}
        self.assertEqual(set(bonds), {"RU000A10EA08", "RU000A10D517", "RU000A10FG84", "RU000A10CFK2", "RU0009029540"})
        self.assertEqual(bonds["RU0009029540"]["kind"], "share")
        self.assertEqual(
            bonds["RU000A10EA08"],
            {"uid": "uid-softline", "isin": "RU000A10EA08", "name": "Софтлайн 002Р-02",
             "issuer": 'ПАО "Софтлайн"', "code": "", "kind": "bond", "quantity": 20.0},
        )
        self.assertEqual(bonds["RU000A10D517"]["code"], "SU26253RMFS3")
        self.assertEqual(bonds["RU000A10FG84"]["issuer"], "Алиум")  # borrowName пуст → brand.company
        self.assertEqual(bonds["RU000A10CFK2"]["quantity"], 12.5)  # units + nano

    def test_securitization_issuer_is_the_spv_not_the_bank(self):
        # У СФО в borrowName банк-оригинатор; его отчётность к бумаге не относится.
        bonds = {b["isin"]: b for b in self.fetch(FakeApi())}
        self.assertEqual(bonds["RU000A10CFK2"]["issuer"], "СФО ТБ-4")

    def test_broker_account_is_found_and_iis_ignored(self):
        api = FakeApi()
        self.fetch(api)
        portfolio = next(c for c in api.calls if c["method"].endswith("/GetPortfolio"))
        self.assertEqual(portfolio["payload"], {"accountId": "0000000001"})

    def test_explicit_account_skips_get_accounts(self):
        api = FakeApi()
        self.fetch(api, account="0000000009")
        self.assertEqual(api.count("GetAccounts"), 0)

    def test_two_broker_accounts_without_account_is_none(self):
        fixtures = copy.deepcopy(FIXTURES)
        fixtures["GetAccounts"]["accounts"][1]["type"] = "ACCOUNT_TYPE_TINKOFF"
        self.assertIsNone(self.fetch(FakeApi(fixtures)))

    def test_known_bonds_are_not_described_again(self):
        known = {
            "uid-softline": {"isin": "RU000A10EA08", "name": "Софтлайн 002Р-02", "issuer": 'ПАО "Софтлайн"', "code": "", "kind": "bond"},
            "uid-ofz": {"isin": "RU000A10D517", "name": "ОФЗ 26253", "issuer": "Минфин", "code": "SU26253RMFS3", "kind": "bond"},
        }
        api = FakeApi()
        bonds = self.fetch(api, known=known)
        self.assertEqual(len(bonds), 5)
        self.assertEqual(api.count("BondBy"), 2)
        self.assertEqual(api.count("GetAssetBy"), 3)

    def test_incomplete_known_entry_is_described_again(self):
        api = FakeApi()
        self.fetch(api, known={"uid-softline": {"isin": "RU000A10EA08"}})
        self.assertEqual(api.count("BondBy"), 4)

    def test_failure_on_asset_step_gives_none_not_partial(self):
        api = FakeApi(failures={"InstrumentsService/GetAssetBy": [500] * tinvest.ATTEMPTS})
        self.assertIsNone(self.fetch(api))

    def test_connection_error_is_retried(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [requests.ConnectionError("vpn")]})
        self.assertEqual(len(self.fetch(api)), 5)
        self.assertEqual(api.count("GetAccounts"), 2)

    def test_client_error_is_not_retried(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [401]})
        self.assertIsNone(self.fetch(api))
        self.assertEqual(api.count("GetAccounts"), 1)

    def test_no_token_means_no_requests(self):
        api = FakeApi()
        with mock.patch.object(tinvest.requests, "post", api):
            self.assertIsNone(tinvest.fetch_positions(""))
        self.assertEqual(api.calls, [])

    def test_certificate_is_pinned_for_every_request(self):
        api = FakeApi()
        self.fetch(api)
        self.assertEqual({c["verify"] for c in api.calls}, {str(tinvest.CA_BUNDLE)})
        self.assertEqual({c["headers"]["Authorization"] for c in api.calls}, {f"Bearer {TOKEN}"})

    def test_token_never_reaches_the_log(self):
        api = FakeApi(failures={"UsersService/GetAccounts": [401]})
        with self.assertLogs("jessica.sources.tinvest", level="WARNING") as logs:
            self.fetch(api)
        self.assertNotIn(TOKEN, "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
