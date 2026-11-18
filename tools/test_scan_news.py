"""Funding news: headline parsing and the adapter's store, on real-shaped synthetic RSS."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_news as sn  # noqa: E402

PORTALS = """
signals:
  funding_news:
    - {id: uktn, name: UKTN, url: https://example.test/uktn, region: UK, min_amount_millions: 1, enabled: true}
    - {id: dormant, name: Dormant, url: https://example.test/off, region: Europe, enabled: false}
  descriptors: [fintech, cloud, startup, london, ai]
  exclude_terms: [" in debt", "headcount cut"]
"""


def item(title, tags=(), guid=None, date="Wed, 23 Sep 2026 10:00:00 +0000"):
    cats = "".join(f"<category><![CDATA[{t}]]></category>" for t in tags)
    g = guid or "https://example.test/" + str(abs(hash(title)))
    return (f"<item><title>{title}</title><link>{g}</link><guid>{g}</guid><pubDate>{date}</pubDate>{cats}"
            "<description><![CDATA[<p>The company has raised new funding.</p>]]></description></item>")


RSS = "<rss><channel>" + "".join([
    item("Stasher secures £3m to expand luggage storage network", ["Travel", "funding", "Stasher"]),
    item("London traveltech Stasher secures £3M for global expansion", guid="https://example.test/stasher-2"),
    item("AI platform Reply Next secures €400K pre-seed funding"),
    item("Magic AI raises £8m to take its AI-powered fitness mirror to the US"),
    item("Cloud startup Verda raises $189m in funding round led by Emergence Capital"),
    item("First Table raises $12 million NZD to fuel expansion across the UK and Ireland"),
    item("Scotland&#8217;s HexSeed secures £2m to cool AI data centre chips"),
    item("Fintech Duqu raises €1.5M to give businesses faster access to working capital"),
    item("365 Finance appoints George Visser as new Chief Risk Officer", ["Funding"]),
    item("UNDP and FLock.io strengthen relationship to advance public sector digital innovation"),
    item("Growth investor DTCP raises €455m first close for defence fund"),
    item("Connect Ventures announces first close of $80M Fund V"),
    item("Robotics funding for 2026 reaches almost £190m as Cambridge leads the way"),
    item("Scotland's HexSeed secures investment to cool AI data centre chips"),
    item("Bird raises $450M in debt, headcount cut from 1,000 peak to 120"),
]) + "</channel></rss>"


class HeadlineTests(unittest.TestCase):
    def parsed(self):
        feed = {"id": "uktn", "region": "UK", "min_amount_millions": 1}
        out = [sn.parse_item(i, feed, {"fintech", "cloud", "startup", "london", "ai"}, (" in debt", "headcount cut"))
               for i in sn.read_rss(RSS)]
        return {x["headline"]: x for x in out if x}

    def test_company_raises_are_read_from_the_headline_only(self):
        names = sorted(x["company"] for x in self.parsed().values())
        self.assertEqual(names, ["Duqu", "First Table", "HexSeed", "Magic AI", "Reply Next", "Stasher", "Stasher", "Verda"])

    def test_appointments_partnerships_funds_and_roundups_are_not_raises(self):
        headlines = " ".join(self.parsed())
        for skipped in ("365 Finance", "UNDP", "DTCP", "Connect Ventures", "Robotics funding", "secures investment", "Bird"):
            self.assertNotIn(skipped, headlines)

    def test_amount_currency_and_floor_are_stated_facts(self):
        p = {x["company"]: x for x in self.parsed().values()}
        self.assertEqual((p["Verda"]["currency"], p["Verda"]["amount_millions"]), ("USD", "189"))
        self.assertEqual((p["First Table"]["currency"], p["First Table"]["amount"]), ("NZD", "$12 million NZD"))
        self.assertEqual(p["Reply Next"]["amount_millions"], "0.4")
        self.assertFalse(p["Reply Next"]["eligible"])
        self.assertTrue(p["Magic AI"]["eligible"])
        self.assertEqual(p["Magic AI"]["published"], "2026-09-23")


class RunTests(unittest.TestCase):
    def test_run_keeps_eligible_stories_once_presence_only(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "portals.yml").write_text(PORTALS)
            (d / "uktn.xml").write_text(RSS)
            first = sn.run(d / "portals.yml", d, "2026-09-25", {"uktn": d / "uktn.xml"})
            again = sn.run(d / "portals.yml", d, "2026-09-25", {"uktn": d / "uktn.xml"})
            self.assertEqual(first["feeds"]["uktn"]["state"], "ok")
            self.assertEqual(first["feeds"]["dormant"]["state"], "disabled")
            self.assertEqual(first["new"], 7)
            self.assertEqual(again["new"], 0)
            obs = json.loads((d / "pending/observations.json").read_text())
            src = obs["sources"]["news:uktn"]
            self.assertEqual((src["source_type"], src["complete"]), ("signal", True))
            kinds = {v["kind"] for v in obs["items"].values()}
            self.assertEqual(kinds, {"signal"})
            self.assertEqual(len(obs["items"]), 7)

    def test_a_failing_feed_is_recorded_and_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "portals.yml").write_text(PORTALS)
            (d / "bad.xml").write_text("<rss><channel><item>")
            status = sn.run(d / "portals.yml", d, "2026-09-25", {"uktn": d / "bad.xml"})
            self.assertEqual(status["feeds"]["uktn"]["state"], "error")
            obs = json.loads((d / "pending/observations.json").read_text())
            self.assertFalse(obs["sources"]["news:uktn"]["complete"])


if __name__ == "__main__":
    unittest.main()
