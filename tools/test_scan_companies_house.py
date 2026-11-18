"""Companies House reader: matching, the facts kept and the facts never kept, on made-up companies."""
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import credentials  # noqa: E402
import scan_companies_house as ch  # noqa: E402

TODAY = dt.date(2026, 10, 1)


def http_error(code):
    return urllib.error.HTTPError("https://api.example.test", code, "error", {}, io.BytesIO(b""))


class FakeRegister:
    """A made-up register answering the four paths the reader uses."""

    def __init__(self, companies, search=None, refuse=False):
        self.companies, self.search, self.refuse, self.urls = companies, search or {}, refuse, []

    def __call__(self, url):
        self.urls.append(url)
        if self.refuse:
            raise http_error(401)
        path = url.removeprefix(ch.API).split("?")[0]
        parts = path.strip("/").split("/")
        if parts[:2] == ["search", "companies"]:
            q = url.split("q=")[1].split("&")[0].replace("+", " ")
            return {"items": self.search.get(q, [])}
        number = parts[1]
        if number not in self.companies:
            raise http_error(404)
        c = self.companies[number]
        if len(parts) == 2:
            return c["profile"]
        if parts[2] == "officers":
            return {"items": c.get("officers", [])}
        if parts[2] == "filing-history":
            return {"items": c.get("filings", [])}
        raise AssertionError(url)


ZEPHYR = {
    "profile": {"company_name": "ZEPHYR LABS LIMITED", "company_status": "active", "date_of_creation": "2019-04-02",
                "sic_codes": ["62012"]},
    "officers": [
        {"name": "QUILL, Ada", "officer_role": "director", "appointed_on": "2019-04-02",
         "date_of_birth": {"month": 1, "year": 1980}, "address": {"locality": "Nowhere"}, "nationality": "Imaginary",
         "country_of_residence": "Elsewhere", "occupation": "Chief Technology Officer"},
        {"name": "FERN, Bo", "officer_role": "director", "appointed_on": "2020-01-01", "resigned_on": "2024-01-01"},
    ],
    "filings": [
        {"category": "capital", "description": "capital-allotment-shares", "date": "2026-09-09", "type": "SH01",
         "description_values": {"date": "2026-08-28", "capital": [{"figure": "120.5", "currency": "GBP"}]}},
        {"category": "capital", "description": "capital-allotment-shares", "date": "2024-01-10", "type": "SH01",
         "description_values": {"date": "2024-01-02", "capital": [{"figure": "100", "currency": "GBP"}]}},
        {"category": "capital", "description": "capital-cancellation-shares", "date": "2026-09-01", "type": "SH06"},
    ],
}


def search_item(title, number, status="active"):
    return {"title": title, "company_number": number, "company_status": status}


class ProposeTests(unittest.TestCase):
    def test_legal_suffixes_case_and_punctuation_are_set_aside_but_nothing_else(self):
        self.assertEqual(ch.name_key("Zephyr Labs"), ch.name_key("ZEPHYR LABS LIMITED"))
        self.assertEqual(ch.name_key("Kite & Co."), ch.name_key("KITE AND CO LTD"))
        self.assertNotEqual(ch.name_key("Zephyr"), ch.name_key("Zephyr Group Ltd"))

    def test_candidates_are_listed_for_a_person_to_verify_and_none_is_chosen(self):
        register = FakeRegister({}, search={"Zephyr Labs": [search_item("ZEPHYR LABS LIMITED", "01"),
                                                          search_item("ZEPHYR LABS HOLDINGS LIMITED", "02", "dissolved")]})
        out = ch.propose(ch.Client("made-up-key", get=register, pause=0), "Zephyr Labs")
        self.assertEqual([(c["company_number"], c["status"], c["same_name"]) for c in out],
                         [("01", "active", True), ("02", "dissolved", False)])

    def test_numbers_a_page_states_are_listed_with_the_registers_name_and_nothing_is_stored(self):
        page = (b"<footer>Zephyr Labs Limited is registered in England and Wales (company number 00000001)."
                b" VAT no. 123456789. Call 02079460000. Also trading as Kite, registration number: SC123456.</footer>")
        register = FakeRegister({"00000001": ZEPHYR})
        out = ch.numbers_on(ch.Client("made-up-key", get=register, pause=0), "https://zephyr.example/terms",
                            read=lambda url: {"body": page, "charset": "utf-8", "url": url})
        self.assertEqual([(c["company_number"], c["company_name"], c["status"]) for c in out],
                         [("00000001", "ZEPHYR LABS LIMITED", "active"), ("SC123456", "", "not on the register")])
        self.assertIn("company number 00000001", out[0]["stated_as"])
        self.assertEqual(out[0]["url"], "https://zephyr.example/terms")

    def test_the_key_is_sent_as_the_basic_auth_username(self):
        c = ch.Client("made-up-key")
        self.assertEqual(c.headers["Authorization"], "Basic bWFkZS11cC1rZXk6")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_with(self, register, targets, cap=10):
        status = ch.run(targets, self.out, ch.Client("made-up-key", get=register, pause=0, cap=cap), TODAY,
                        observed_at="2026-10-01T05:30:00Z")
        store = json.loads((self.out / "pending" / "observations.json").read_text())
        return status, store

    def test_facts_kept_are_registered_details_current_officers_and_recent_allotments(self):
        register = FakeRegister({"01": ZEPHYR})
        status, store = self.run_with(register, [{"id": "CO-zephyr", "name": "Zephyr Labs", "company_number": "01"}])
        self.assertEqual((status["state"], status["read"], status["requests"]), ("ok", 1, 3))
        item = store["items"][f"{ch.SCOPE}\x1f01"]
        self.assertEqual((item["kind"], item["source_id"]), ("registry", "01"))
        facts = item["current"]
        self.assertEqual((facts["record_id"], facts["company_name"], facts["company_status"], facts["incorporated_on"]),
                         ("CO-zephyr", "ZEPHYR LABS LIMITED", "active", "2019-04-02"))
        self.assertEqual(facts["officers"], [{"name": "QUILL, Ada", "role": "director", "appointed_on": "2019-04-02"}])
        self.assertEqual([a["allotted_on"] for a in facts["allotments"]], ["2026-08-28"])
        self.assertEqual(facts["allotments"][0]["capital"], [{"figure": "120.5", "currency": "GBP"}])
        self.assertEqual(facts["url"], ch.PUBLIC + "01")
        text = json.dumps(store)
        for never in ("date_of_birth", "1980", "Nowhere", "Imaginary", "Elsewhere", "Chief Technology Officer", "made-up-key"):
            self.assertNotIn(never, text)

    def test_the_daily_run_never_searches_and_reports_companies_without_a_recorded_number(self):
        register = FakeRegister({"01": ZEPHYR, "03": ZEPHYR}, search={"Nimbus": [search_item("NIMBUS LIMITED", "03")]})
        status, store = self.run_with(register, [{"id": "CO-zephyr", "name": "Zephyr", "company_number": "01"},
                                                 {"id": "CO-nimbus", "name": "Nimbus", "company_number": ""}])
        self.assertFalse(any("/search/" in u for u in register.urls))
        self.assertEqual(status["no_number"], ["Nimbus"])
        self.assertEqual(list(store["items"]), [f"{ch.SCOPE}\x1f01"])
        self.assertEqual(store["sources"][ch.SCOPE]["state"], "ok")

    def test_a_refused_key_ends_the_run_as_an_error_without_items(self):
        status, store = self.run_with(FakeRegister({}, refuse=True), [{"id": "CO-a", "name": "A", "company_number": "01"},
                                                                       {"id": "CO-b", "name": "B", "company_number": "02"}])
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["requests"], 1)
        self.assertIn("refused the key", store["sources"][ch.SCOPE]["detail"]["error"])
        self.assertFalse(store["sources"][ch.SCOPE]["complete"])

    def test_the_request_cap_keeps_what_was_read_and_marks_the_read_partial(self):
        register = FakeRegister({"01": ZEPHYR, "02": ZEPHYR})
        status, store = self.run_with(register, [{"id": "CO-a", "name": "A", "company_number": "01"},
                                                 {"id": "CO-b", "name": "B", "company_number": "02"}], cap=4)
        self.assertEqual((status["state"], status["read"], status["requests"]), ("partial", 1, 4))
        self.assertEqual(list(store["items"]), [f"{ch.SCOPE}\x1f01"])

    def test_one_company_failing_does_not_stop_the_others(self):
        register = FakeRegister({"01": ZEPHYR})
        real = register.__call__

        def flaky(url):
            if "/company/09" in url:
                raise http_error(500)
            return real(url)
        status, store = self.run_with(flaky, [{"id": "CO-x", "name": "X", "company_number": "09"},
                                              {"id": "CO-a", "name": "A", "company_number": "01"}])
        self.assertEqual((status["read"], status["errors"]), (1, 1))
        self.assertFalse(store["sources"][ch.SCOPE]["complete"])

    def test_without_a_key_the_reader_exits_without_a_request(self):
        with patch.object(credentials, "get", return_value=""):
            self.assertEqual(ch.main(["--out", str(self.out)]), 2)
        self.assertFalse((self.out / "pending" / "observations.json").exists())


class CredentialTests(unittest.TestCase):
    def test_the_environment_first_then_the_service_credential_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "MADE_UP_KEY").write_text("from-file\n")
            with patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": tmp}, clear=True):
                self.assertEqual(credentials.get("MADE_UP_KEY"), "from-file")
                self.assertEqual(credentials.get("ABSENT_KEY"), "")
            with patch.dict("os.environ", {"CREDENTIALS_DIRECTORY": tmp, "MADE_UP_KEY": "from-env"}, clear=True):
                self.assertEqual(credentials.get("MADE_UP_KEY"), "from-env")


if __name__ == "__main__":
    unittest.main()
