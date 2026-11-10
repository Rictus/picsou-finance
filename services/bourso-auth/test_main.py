"""The sidecar's own glue: page extractors and the accounts flow end to end.

`_collect_accounts` is driven through an `httpx.MockTransport` so the whole
chain -- home page, dashboard, trading board, response model -- is exercised
without touching BoursoBank.
"""

import asyncio
import json
import time
import unittest
from decimal import Decimal
from unittest import mock

import httpx
from fastapi import HTTPException

from accounts_parser import AccountsFormatError
from fixtures import DASHBOARD_HTML, MOVEMENTS_HTML, RIB_HTML
from main import (
    BASE_URL,
    AccountsRequest,
    InitiateRequest,
    _collect_accounts,
    _json_success,
    _pending,
    _strong_auth_params,
    describe_securisation_page,
    extract_brs_config,
    extract_form_token,
    initiate,
    is_fraud_education_page,
    lands_on_securisation,
    redirects_to_identity_selector,
    redirects_to_securisation,
    requires_app_push,
    restore_cookies,
    serialize_cookies,
)
from test_accounts_parser import (
    BUSINESS_TOKEN,
    PERSONAL_AND_BUSINESS,
    PERSONAL_TOKEN,
    identity_card,
    identity_selector_html,
)

API_URL = "https://api.boursobank.com/services/api/v1.7"
USER_HASH = "61d55b52615fbdf"
PEA_ID = "9651d8edd5975de1b9eff3865505f15f"

HOME_HTML = (
    '<html><head><script>window.BRS_CONFIG = {"API_HOST": "api.boursobank.com",'
    '"API_URL": "https:\\/\\/api.boursobank.com\\/services\\/api\\/v1.7",'
    '"USER_HASH": "61d55b52615fbdf"};</script></head>'
    '<body><a href="/se-deconnecter">Se déconnecter</a></body></html>'
)

# The regression: BoursoBank's second-factor page also carries the logout link.
SECURISATION_HTML = (
    '<html><body><a href="/se-deconnecter">Se déconnecter</a>'
    "<div>/securisation/validation</div></body></html>"
)


def securisation_response(path="/securisation"):
    return httpx.Response(
        200,
        request=httpx.Request("GET", BASE_URL + path),
        text=SECURISATION_HTML,
    )


def money(value, currency="EUR"):
    return {"value": value, "decimals": 2, "currency": currency}


# Two view sections, as BoursoBank actually serves it: the account summary in
# one, the position rows in another, each line carrying its own ISIN.
TRADING_SUMMARY = [
    {
        "id": "summary",
        "label": "Synthèse",
        "account": {
            "name": "PEA DOE",
            "currency": "EUR",
            "cash": money("3088.89"),
            "valuation": money("140000.00"),
            "total": money("143088.89"),
            "gainLoss": money("12000.00"),
        },
        "actions": [],
    },
    {
        "id": "positions",
        "label": "Positions",
        "actions": [],
        "count": 1,
        "positions": [
            {
                "symbol": "1rTCW8",
                "label": "Amundi MSCI World UCITS ETF",
                "isin": "IE00B4L5Y983",
                "permalink": "/cours/1rTCW8/",
                "exchangeCode": "XPAR",
                "currency": "EUR",
                "quantity": {"value": "1000", "decimals": 4, "currency": None},
                "buyingPrice": money("128.00"),
                "amount": money("140000.00"),
                "last": money("140.00"),
            }
        ],
    },
]


def build_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL,
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )


def default_handler(
    *, home=HOME_HTML, dashboard=DASHBOARD_HTML, trading=None, trading_status=200, movement_status=200
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "clients.boursobank.com" and path == "/":
            return httpx.Response(200, text=home)
        if path == "/dashboard/liste-comptes":
            return httpx.Response(200, text=dashboard)
        if path.endswith("/rib"):
            return httpx.Response(200, text=RIB_HTML)
        if "/trading/accounts/summary/" in path:
            if trading_status != 200:
                return httpx.Response(trading_status, json={})
            return httpx.Response(200, json=trading if trading is not None else TRADING_SUMMARY)
        if path.endswith("/mouvements"):
            if movement_status != 200:
                return httpx.Response(movement_status, text="temporary upstream error")
            return httpx.Response(
                200,
                text=MOVEMENTS_HTML.replace(' data-operations-next-pagination="fixture-next"', ""),
            )
        # No instrument-quote call: the trading board ships the ISIN itself, so
        # any extra upstream request here is a regression.
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


class ExtractorTest(unittest.TestCase):
    def test_reads_the_form_token_whichever_order_the_attributes_come_in(self):
        self.assertEqual(
            extract_form_token('<input type="hidden" name="form[_token]" value="abc.def" >'),
            "abc.def",
        )
        self.assertEqual(
            extract_form_token('<input value="abc.def" name="form[_token]">'), "abc.def"
        )

    def test_a_login_page_without_a_token_is_refused(self):
        with self.assertRaises(AccountsFormatError) as raised:
            extract_form_token("<html></html>")
        self.assertEqual(raised.exception.code, "UPSTREAM_FORMAT_CHANGED")

    def test_unescapes_the_api_url_boursobank_embeds(self):
        api_url, user_hash = extract_brs_config(HOME_HTML)
        self.assertEqual(api_url, API_URL)
        self.assertEqual(user_hash, USER_HASH)

    def test_a_page_without_brs_config_is_refused(self):
        with self.assertRaises(AccountsFormatError) as raised:
            extract_brs_config("<html></html>")
        self.assertEqual(raised.exception.code, "UPSTREAM_FORMAT_CHANGED")

    def test_reads_the_html_escaped_strong_authentication_payload(self):
        payload = {
            "challenges": [
                {
                    "parameters": {
                        "formScreen": {
                            "actions": {
                                "check": {
                                    "api": {
                                        "params": {
                                            "resourceId": "otp-42",
                                            "formState": "state-xyz",
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            ]
        }
        escaped = json.dumps(payload).replace('"', "&quot;")
        markup = f'<div data-strong-authentication-payload="{escaped}"></div>'
        self.assertEqual(_strong_auth_params(markup), ("otp-42", "state-xyz"))

    def test_an_unknown_challenge_shape_is_refused(self):
        markup = '<div data-strong-authentication-payload="{&quot;challenges&quot;: []}"></div>'
        with self.assertRaises(AccountsFormatError) as raised:
            _strong_auth_params(markup)
        self.assertEqual(raised.exception.code, "UPSTREAM_FORMAT_CHANGED")

    def test_only_an_explicit_success_counts_as_success(self):
        self.assertTrue(_json_success(httpx.Response(200, json={"success": True})))
        self.assertFalse(_json_success(httpx.Response(200, json={"success": False})))
        self.assertFalse(_json_success(httpx.Response(200, json={})))
        self.assertFalse(_json_success(httpx.Response(200, text="<html>")))


class CookieStateTest(unittest.TestCase):
    def test_round_trips_cookies_with_their_own_domain(self):
        # The trading board is on api.boursobank.com while the login is on
        # clients.boursobank.com; collapsing the domain would stop
        # authenticating half the calls.
        source = httpx.Client(base_url=BASE_URL)
        source.cookies.set("brsxds_x", "jwt", domain=".boursobank.com", path="/")
        source.cookies.set("navSessionId", "nav", domain="clients.boursobank.com", path="/")

        restored = httpx.Client(base_url=BASE_URL)
        restore_cookies(restored, serialize_cookies(source))

        by_name = {cookie.name: cookie for cookie in restored.cookies.jar}
        self.assertEqual(by_name["brsxds_x"].domain, ".boursobank.com")
        self.assertEqual(by_name["navSessionId"].domain, "clients.boursobank.com")

    def test_a_malformed_session_is_refused(self):
        for broken in ("not-json", "[]", "{}", '{"cookies": []}', '{"cookies": [1]}'):
            with self.assertRaises(HTTPException) as raised:
                restore_cookies(httpx.Client(), broken)
            self.assertEqual(raised.exception.detail, "INVALID_DATA")

    def test_the_session_blob_satisfies_the_request_model(self):
        source = httpx.Client(base_url=BASE_URL)
        source.cookies.set("brsxds_x", "jwt", domain=".boursobank.com", path="/")
        AccountsRequest(sessionState=serialize_cookies(source))


class SecurisationLandingTest(unittest.TestCase):
    def test_a_securisation_landing_needs_app_push_even_with_the_logout_link(self):
        self.assertTrue(requires_app_push(securisation_response(), SECURISATION_HTML))

    def test_a_dashboard_with_a_securisation_link_is_not_a_second_factor(self):
        home = HOME_HTML.replace("</body>", '<a href="/securisation">Sécurité</a></body>')
        response = httpx.Response(
            200,
            request=httpx.Request("GET", BASE_URL + "/"),
            text=home,
        )
        self.assertFalse(requires_app_push(response, home))

    def test_a_securisation_page_without_the_logout_link_still_needs_app_push(self):
        home = "<html><div>/securisation/validation</div></html>"
        response = httpx.Response(
            200,
            request=httpx.Request("GET", BASE_URL + "/"),
            text=home,
        )
        self.assertTrue(requires_app_push(response, home))

    def test_lands_on_securisation_reads_the_final_path(self):
        home = httpx.Response(
            200,
            request=httpx.Request("GET", BASE_URL + "/"),
            text=HOME_HTML,
        )
        self.assertTrue(lands_on_securisation(securisation_response()))
        self.assertTrue(lands_on_securisation(securisation_response("/securisation/validation")))
        self.assertFalse(lands_on_securisation(home))

    def test_only_a_redirect_to_securisation_counts(self):
        def redirect(location):
            return httpx.Response(302, headers={"Location": location})

        self.assertTrue(redirects_to_securisation(redirect("/securisation")))
        self.assertTrue(redirects_to_securisation(redirect("/securisation/validation")))
        self.assertFalse(redirects_to_securisation(redirect("/connexion/lister-identites")))
        self.assertFalse(redirects_to_securisation(redirect("https://evil.example/securisation")))
        self.assertFalse(redirects_to_securisation(httpx.Response(200, text="<html></html>")))

    def test_describe_securisation_page_is_structural_only(self):
        page = (
            '<html><body><a href="/connexion/changer-identite/'
            'abcdef1234567890abcdef1234567890">Tentative de connexion bloquée</a>'
            '<form action="/securisation/validation?token=secret"></form>'
            '<div data-strong-authentication-payload="payload"></div>'
            '<span data-new-widget="1"></span></body></html>'
        )
        response = httpx.Response(
            200,
            request=httpx.Request("GET", BASE_URL + "/securisation/validation"),
            text=page,
        )
        shape = describe_securisation_page(response, page)
        self.assertIn("path=/securisation/validation", shape)
        self.assertIn("data-new-widget", shape)
        self.assertIn("BLOQU", shape)
        self.assertIn(":token", shape)
        self.assertNotIn("abcdef1234567890", shape)
        self.assertNotIn("secret", shape)


class InitiateMfaTest(unittest.IsolatedAsyncioTestCase):
    async def test_initiate_starts_the_app_push_when_home_lands_on_securisation(self):
        client = build_client(default_handler())
        state = {
            "apiUrl": API_URL,
            "userHash": USER_HASH,
            "resourceId": "resource",
            "formState": "state",
            "validationToken": "token",
        }
        with mock.patch("main._new_client", return_value=client), mock.patch(
            "main._login", new_callable=mock.AsyncMock
        ), mock.patch(
            "main._home_response",
            new_callable=mock.AsyncMock,
            return_value=securisation_response(),
        ), mock.patch(
            "main._start_app_push",
            new_callable=mock.AsyncMock,
            return_value=state,
        ):
            result = await initiate(InitiateRequest(customerId="123456", password="1234"))
        try:
            self.assertTrue(result["mfaRequired"])
            self.assertIsNotNone(result["processId"])
            self.assertEqual(result["mfaType"], "APP_PUSH")
            pending = await _pending.take(result["processId"])
            self.assertIsNotNone(pending)
        finally:
            await client.aclose()

    async def test_initiate_keeps_the_direct_session_when_home_is_the_dashboard(self):
        client = build_client(default_handler())
        home = httpx.Response(
            200,
            request=httpx.Request("GET", BASE_URL + "/"),
            text=HOME_HTML,
        )
        with mock.patch("main._new_client", return_value=client), mock.patch(
            "main._login", new_callable=mock.AsyncMock
        ), mock.patch("main._home_response", new_callable=mock.AsyncMock, return_value=home):
            result = await initiate(InitiateRequest(customerId="123456", password="1234"))
        try:
            self.assertFalse(result["mfaRequired"])
            self.assertIsNone(result["processId"])
            self.assertIn("cookies", result["sessionState"])
        finally:
            await client.aclose()


class CollectAccountsTest(unittest.IsolatedAsyncioTestCase):
    async def test_builds_the_full_account_payload(self):
        async with build_client(default_handler()) as client:
            accounts = await _collect_accounts(client)

        self.assertEqual(len(accounts), 3)
        by_type = {account.type: account for account in accounts}

        checking = by_type["CHECKING"]
        self.assertEqual(checking.externalId, "bourso_e2f509c466f5294f15abd873dbbf8a62")
        self.assertEqual(checking.balanceEur, Decimal("20810.50"))
        self.assertIsNone(checking.cashBalance)
        self.assertEqual(checking.positions, [])
        self.assertEqual(checking.iban, "FR7630006000011234567890189")
        self.assertEqual([tx["externalId"] for tx in checking.transactions], ["fixture-op-1", "fixture-op-2"])
        self.assertIsNone(checking.transactionWarning)

        # The fixture's passbook is a real LDDS, so it must arrive typed as one
        # rather than as the generic SAVINGS the sidecar used to send.
        self.assertEqual(by_type["LDDS"].balanceEur, Decimal("11010.00"))

        pea = by_type["PEA"]
        self.assertEqual(pea.externalId, f"bourso_{PEA_ID}")
        # The trading board is authoritative over the dashboard tile.
        self.assertEqual(pea.balanceEur, Decimal("143088.89"))
        self.assertEqual(pea.cashBalance, Decimal("3088.89"))
        self.assertEqual(len(pea.positions), 1)
        self.assertEqual(pea.transactions, [])
        self.assertEqual(pea.positions[0].isin, "IE00B4L5Y983")
        self.assertEqual(pea.positions[0].currentValueEur, Decimal("140000.00"))
        self.assertTrue(pea.snapshotComplete)

    async def test_lep_transactions_are_harvested_but_holdings_are_not(self):
        lep_dashboard = DASHBOARD_HTML.replace(
            "LIVRET DEVELOPPEMENT DURABLE SOLIDAIRE", "LIVRET D'ÉPARGNE POPULAIRE"
        )
        async with build_client(default_handler(dashboard=lep_dashboard)) as client:
            accounts = await _collect_accounts(client)

        lep = next(account for account in accounts if account.type == "LEP")
        self.assertEqual(len(lep.transactions), 2)
        self.assertEqual(lep.positions, [])

    async def test_every_livret_has_its_transactions_and_iban_harvested(self):
        # Issue #167: only CHECKING and LEP used to read /mouvements, so a
        # Livret A, an LDDS or a Livret Bourso+ synced its balance and no history.
        for label, kind in (
            ("LIVRET DEVELOPPEMENT DURABLE SOLIDAIRE", "LDDS"),
            ("LIVRET A", "LIVRET_A"),
            ("LIVRET D'EPARGNE POPULAIRE", "LEP"),
            ("LIVRET JEUNE", "LIVRET_JEUNE"),
            ("PLAN EPARGNE LOGEMENT", "PEL"),
            ("COMPTE EPARGNE LOGEMENT", "CEL"),
            ("LIVRET BOURSO+", "SAVINGS"),
        ):
            with self.subTest(kind=kind):
                dashboard = DASHBOARD_HTML.replace("LIVRET DEVELOPPEMENT DURABLE SOLIDAIRE", label)
                async with build_client(default_handler(dashboard=dashboard)) as client:
                    accounts = await _collect_accounts(client)

                livret = next(account for account in accounts if account.type == kind)
                self.assertEqual(
                    [tx["externalId"] for tx in livret.transactions], ["fixture-op-1", "fixture-op-2"]
                )
                self.assertIsNone(livret.transactionWarning)
                self.assertEqual(livret.iban, "FR7630006000011234567890189")
                self.assertEqual(livret.positions, [])

    async def test_history_past_the_deadline_never_costs_a_balance(self):
        requested = []
        normal_handler = default_handler()

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path.endswith("/mouvements"):
                await asyncio.sleep(10)
            return normal_handler(request)

        started = time.monotonic()
        with (
            mock.patch("main.ACCOUNTS_HISTORY_DEADLINE_SECONDS", 0.2),
            self.assertLogs("bourso-auth", level="WARNING") as captured,
        ):
            async with build_client(handler) as client:
                accounts = await _collect_accounts(client)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.5)
        by_type = {account.type: account for account in accounts}

        checking = by_type["CHECKING"]
        self.assertEqual(checking.balanceEur, Decimal("20810.50"))
        self.assertEqual(checking.transactions, [])
        self.assertEqual(checking.transactionWarning, "TRANSACTIONS_UNAVAILABLE")
        # The IBAN was read before the movements stalled: nothing to discard.
        self.assertEqual(checking.iban, "FR7630006000011234567890189")

        # The deadline had passed by the time the livret came up: it got no
        # request of its own, only the warning and its dashboard balance.
        ldds = by_type["LDDS"]
        self.assertEqual(ldds.balanceEur, Decimal("11010.00"))
        self.assertEqual(ldds.transactions, [])
        self.assertIsNone(ldds.iban)
        self.assertEqual(ldds.transactionWarning, "TRANSACTIONS_UNAVAILABLE")
        self.assertFalse(any("/compte/epargne/" in path for path in requested))

        # Holdings come from the first pass, before any history read.
        pea = by_type["PEA"]
        self.assertEqual(pea.balanceEur, Decimal("143088.89"))
        self.assertEqual(len(pea.positions), 1)
        self.assertIsNone(pea.transactionWarning)

        self.assertEqual(len(captured.output), 1)
        self.assertIn("deadline reached, 2 account(s) without history", captured.output[0])
        for leak in ("20810.50", "11010.00", "FR763", "/mouvements", "/rib", "http"):
            self.assertNotIn(leak, captured.output[0])

    async def test_deadline_counts_from_the_start_of_the_accounts_read(self):
        requested = []
        normal_handler = default_handler()

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path == "/dashboard/liste-comptes":
                await asyncio.sleep(0.3)
            return normal_handler(request)

        with (
            mock.patch("main.ACCOUNTS_HISTORY_DEADLINE_SECONDS", 0.2),
            self.assertLogs("bourso-auth", level="WARNING"),
        ):
            async with build_client(handler) as client:
                accounts = await _collect_accounts(client)

        # The slow first pass used the whole budget: no history request at all,
        # yet every balance and the PEA holdings are intact.
        self.assertFalse(any(p.endswith(("/rib", "/mouvements")) for p in requested))
        by_type = {account.type: account for account in accounts}
        self.assertEqual(by_type["CHECKING"].balanceEur, Decimal("20810.50"))
        self.assertEqual(by_type["CHECKING"].transactionWarning, "TRANSACTIONS_UNAVAILABLE")
        self.assertEqual(by_type["LDDS"].transactionWarning, "TRANSACTIONS_UNAVAILABLE")
        self.assertEqual(len(by_type["PEA"].positions), 1)

    async def test_movement_outage_keeps_the_balance_and_emits_a_safe_warning(self):
        with self.assertLogs("bourso-auth", level="WARNING") as captured:
            async with build_client(default_handler(movement_status=503)) as client:
                accounts = await _collect_accounts(client)

        self.assertTrue(any("TRANSACTIONS_UNAVAILABLE" in line for line in captured.output))
        self.assertFalse(any("20810.50" in line or "FR763" in line for line in captured.output))

        checking = next(account for account in accounts if account.type == "CHECKING")
        self.assertEqual(checking.balanceEur, Decimal("20810.50"))
        self.assertEqual(checking.transactions, [])
        self.assertEqual(checking.transactionWarning, "TRANSACTIONS_UNAVAILABLE")

    async def test_repeated_movement_token_keeps_the_balance_and_warns_incomplete(self):
        movement_requests = []
        normal_handler = default_handler()

        def handler(request):
            if request.url.path.endswith("/mouvements"):
                movement_requests.append(request)
                return httpx.Response(
                    200,
                    text='<ul class="list__movement"><li class="list__movement__range-summary" '
                    'data-operations-next-pagination="loop"></li></ul>',
                )
            return normal_handler(request)

        with self.assertLogs("bourso-auth", level="WARNING") as captured:
            async with httpx.AsyncClient(
                base_url=BASE_URL, transport=httpx.MockTransport(handler), follow_redirects=True
            ) as client:
                accounts = await _collect_accounts(client)

        self.assertTrue(any("TRANSACTIONS_INCOMPLETE" in line for line in captured.output))
        self.assertFalse(any("loop" in line or "/mouvements" in line for line in captured.output))
        checking = next(account for account in accounts if account.type == "CHECKING")
        self.assertEqual(checking.balanceEur, Decimal("20810.50"))
        self.assertEqual(checking.transactionWarning, "TRANSACTIONS_INCOMPLETE")
        checking_requests = [r for r in movement_requests if "/compte/cav/" in r.url.path]
        self.assertEqual(len(checking_requests), 2)

    async def test_cross_origin_redirect_is_not_followed_and_warns_unavailable(self):
        foreign_requests = []
        normal_handler = default_handler()

        def handler(request):
            if request.url.path.endswith(("/rib", "/mouvements")):
                return httpx.Response(302, headers={"Location": "https://evil.example/collect"})
            if request.url.host == "evil.example":
                foreign_requests.append(request)
                return httpx.Response(200, text="unexpected")
            return normal_handler(request)

        async with httpx.AsyncClient(
            base_url=BASE_URL, transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            accounts = await _collect_accounts(client)

        checking = next(account for account in accounts if account.type == "CHECKING")
        self.assertEqual(checking.balanceEur, Decimal("20810.50"))
        self.assertEqual(checking.transactionWarning, "TRANSACTIONS_UNAVAILABLE")
        self.assertEqual(foreign_requests, [])

    async def test_a_line_without_an_isin_still_syncs(self):
        stripped = json.loads(json.dumps(TRADING_SUMMARY))
        del stripped[1]["positions"][0]["isin"]
        async with build_client(default_handler(trading=stripped)) as client:
            accounts = await _collect_accounts(client)

        pea = next(account for account in accounts if account.type == "PEA")
        self.assertIsNone(pea.positions[0].isin)
        self.assertEqual(pea.positions[0].symbol, "1rTCW8")
        # Losing the ISIN must not lose the money.
        self.assertEqual(pea.positions[0].currentValueEur, Decimal("140000.00"))

    async def test_a_single_fund_contract_passes_the_sidecar_contract(self):
        single_fund = [
            {
                "account": {
                    "name": "CONTRAT DOE",
                    "currency": "EUR",
                    "balance": money("350.00"),
                    "gainLoss": money("50.00"),
                }
            },
            {
                "fund": {
                    "isin": "FR0010315770",
                    "label": "Lyxor MSCI World",
                    "quantity": "3.5",
                    "price": money("100.00"),
                }
            },
        ]
        async with build_client(default_handler(trading=single_fund)) as client:
            accounts = await _collect_accounts(client)

        contract = next(account for account in accounts if account.type == "PEA")
        # A zero cash leg, not null: the backend refuses a securities account
        # without one, which is how this contract first failed (#154).
        self.assertEqual(contract.cashBalance, Decimal("0"))
        self.assertEqual(contract.balanceEur, Decimal("350.00"))
        self.assertEqual(len(contract.positions), 1)
        self.assertEqual(contract.positions[0].symbol, "FR0010315770")
        self.assertEqual(contract.positions[0].currentValueEur, Decimal("350.00"))

    async def test_a_logged_out_home_page_reports_an_expired_session(self):
        async with build_client(default_handler(home="<html>Connexion</html>")) as client:
            with self.assertRaises(HTTPException) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "SESSION_EXPIRED")

    async def test_a_home_page_landing_on_securisation_expires_the_session(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(302, headers={"Location": "/securisation"})
            if request.url.path == "/securisation":
                return httpx.Response(200, text=SECURISATION_HTML)
            raise AssertionError(f"unexpected request: {request.url}")

        async with build_client(handler) as client:
            with self.assertRaises(HTTPException) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "SESSION_EXPIRED")

    async def test_a_dashboard_redirect_to_securisation_expires_the_session(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/":
                return httpx.Response(200, text=HOME_HTML)
            if request.url.path == "/dashboard/liste-comptes":
                return httpx.Response(302, headers={"Location": "/securisation"})
            raise AssertionError(f"unexpected request: {request.url}")

        async with build_client(handler) as client:
            with self.assertRaises(HTTPException) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "SESSION_EXPIRED")

    async def test_a_trading_board_401_expires_the_session_rather_than_emptying_the_pea(self):
        async with build_client(default_handler(trading_status=401)) as client:
            with self.assertRaises(HTTPException) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.detail, "SESSION_EXPIRED")

    async def test_a_trading_board_outage_fails_the_whole_sync(self):
        # Not "the PEA is worth its cash now": that would write a false loss
        # into the net-worth series.
        async with build_client(default_handler(trading_status=503)) as client:
            with self.assertRaises(HTTPException) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.detail, "UPSTREAM_UNAVAILABLE")

    async def test_a_portfolio_that_does_not_reconcile_is_refused(self):
        truncated = json.loads(json.dumps(TRADING_SUMMARY))
        truncated[1]["positions"] = []
        async with build_client(default_handler(trading=truncated)) as client:
            with self.assertRaises(AccountsFormatError) as raised:
                await _collect_accounts(client)
        self.assertEqual(raised.exception.code, "PORTFOLIO_INCOMPLETE")


def multi_identity_handler(selector, requested, *, switch_sticks=True):
    """The #153 flow: the dashboard 302s to the selector until the switch link,
    which chains through /feature-redirect, has picked an identity."""
    chosen = []
    fallback = default_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested.append(path)
        if path == "/dashboard/liste-comptes" and not chosen:
            return httpx.Response(302, headers={"Location": "/connexion/lister-identites"})
        if path == "/connexion/lister-identites":
            return httpx.Response(200, text=selector)
        if path.startswith("/connexion/changer-identite/"):
            if switch_sticks:
                chosen.append(path)
            return httpx.Response(
                302, headers={"Location": "/feature-redirect?featureId=customer.dashboard_home"}
            )
        if path == "/feature-redirect":
            return httpx.Response(302, headers={"Location": "/"})
        return fallback(request)

    return handler


class IdentitySelectorFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_switches_to_the_personal_identity_then_reads_the_dashboard(self):
        requested = []
        with self.assertLogs(level="INFO") as logs:
            async with build_client(multi_identity_handler(PERSONAL_AND_BUSINESS, requested)) as client:
                accounts = await _collect_accounts(client)

        self.assertEqual(len(accounts), 3)
        self.assertEqual(
            requested[:7],
            [
                "/",
                "/dashboard/liste-comptes",
                "/connexion/lister-identites",
                f"/connexion/changer-identite/{PERSONAL_TOKEN}",
                "/feature-redirect",
                "/",
                "/dashboard/liste-comptes",
            ],
        )
        self.assertNotIn(f"/connexion/changer-identite/{BUSINESS_TOKEN}", requested)
        self.assertNotIn(PERSONAL_TOKEN, "\n".join(logs.output))

    async def test_a_business_only_selector_is_refused_without_switching(self):
        requested = []
        selector = identity_selector_html(identity_card(BUSINESS_TOKEN, "Exemple SARL"))
        async with build_client(multi_identity_handler(selector, requested)) as client:
            with self.assertRaises(AccountsFormatError) as raised:
                await _collect_accounts(client)

        self.assertEqual(raised.exception.code, "IDENTITY_SELECTION_UNSUPPORTED")
        self.assertFalse(any(path.startswith("/connexion/changer-identite/") for path in requested))

    async def test_a_selector_that_comes_back_after_the_switch_is_not_retried(self):
        requested = []
        handler = multi_identity_handler(PERSONAL_AND_BUSINESS, requested, switch_sticks=False)
        async with build_client(handler) as client:
            with self.assertRaises(AccountsFormatError) as raised:
                await _collect_accounts(client)

        self.assertEqual(raised.exception.code, "UPSTREAM_FORMAT_CHANGED")
        switches = [path for path in requested if path.startswith("/connexion/changer-identite/")]
        self.assertEqual(switches, [f"/connexion/changer-identite/{PERSONAL_TOKEN}"])

    async def test_a_single_identity_access_never_sees_the_selector(self):
        requested = []
        handler = default_handler()

        def recording(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return handler(request)

        async with build_client(recording) as client:
            await _collect_accounts(client)

        self.assertEqual(
            requested,
            [
                "/",
                "/dashboard/liste-comptes",
                f"/services/api/v1.7/_user_/_{USER_HASH}_/trading/accounts/summary/{PEA_ID}",
                f"/compte/cav/{'e2f509c466f5294f15abd873dbbf8a62'}/rib",
                f"/compte/cav/{'e2f509c466f5294f15abd873dbbf8a62'}/mouvements",
                f"/compte/epargne/ldd/{'a8a23172b7e7c91c538831578242112e'}/rib",
                f"/compte/epargne/ldd/{'a8a23172b7e7c91c538831578242112e'}/mouvements",
            ],
        )

    def test_only_a_redirect_to_the_selector_counts_as_one(self):
        def redirect(location):
            return httpx.Response(302, headers={"Location": location})

        self.assertTrue(redirects_to_identity_selector(redirect("/connexion/lister-identites")))
        self.assertTrue(
            redirects_to_identity_selector(
                redirect("https://clients.boursobank.com/connexion/lister-identites/")
            )
        )
        self.assertFalse(redirects_to_identity_selector(redirect("/connexion/")))
        self.assertFalse(
            redirects_to_identity_selector(redirect("https://evil.example/connexion/lister-identites"))
        )
        self.assertFalse(redirects_to_identity_selector(httpx.Response(200, text="<html></html>")))


class FraudEducationPageTest(unittest.TestCase):
    def test_a_fraud_education_landing_is_not_a_wrong_password(self):
        home = (
            '<html><body><a href="/infos-profil/pedagogie-fraude/abc123/1">'
            "J'ai pris connaissance des consignes</a></body></html>"
        )
        self.assertTrue(is_fraud_education_page(home))

    def test_the_dashboard_is_not_a_fraud_landing(self):
        self.assertFalse(is_fraud_education_page(HOME_HTML))

    def test_the_securisation_page_is_not_a_fraud_landing(self):
        self.assertFalse(is_fraud_education_page("<html>/securisation/validation</html>"))

    def test_a_wrong_password_page_is_not_a_fraud_landing(self):
        self.assertFalse(
            is_fraud_education_page("<html>Identifiant ou mot de passe invalide</html>")
        )


if __name__ == "__main__":
    unittest.main()
