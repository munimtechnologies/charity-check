import csv
import json
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import charity_check as cc  # noqa: E402

PUB78 = """530196605|American National Red Cross|Washington|DC|United States|PC
131623829|Example Family Foundation Inc|New York|NY|United States|PF
012345678|Small Supporting Org|Boston|MA|United States|SONFI
222222222|Both Lists Org|Austin|TX|United States|PC
333333333|Reinstated Org|Denver|CO|United States|PC
not-an-ein|Broken Line|X|XX|United States|PC
"""

REVOCATION = """222222222|Both Lists Org||1 Main St|Austin|TX|78701|US|501(c)(3)|15-MAY-2025|10-JUL-2025|
444444444|Gone Charity|GC|2 Elm St|Reno|NV|89501|US|501(c)(3)|15-NOV-2019|10-FEB-2020|
333333333|Reinstated Org||3 Oak St|Denver|CO|80202|US|501(c)(3)|15-MAY-2018|10-AUG-2018|15-MAY-2018
"""

BMF = """EIN,NAME,ICO,STREET,CITY,STATE,ZIP,GROUP,SUBSECTION,AFFILIATION,CLASSIFICATION,RULING,DEDUCTIBILITY,FOUNDATION,ACTIVITY,ORGANIZATION,STATUS,TAX_PERIOD,ASSET_CD,INCOME_CD,FILING_REQ_CD,PF_FILING_REQ_CD,ACCT_PD,ASSET_AMT,INCOME_AMT,REVENUE_AMT,NTEE_CD,SORT_NAME
555555555,GRACE COMMUNITY CHURCH,,9 PINE ST,SALEM,OR,97301-0000,1234,03,9,7000,195501,1,10,001000000,1,01,,0,0,06,0,12,,,,X20,
666666666,NEIGHBORS FOR PARKS,,4 BIRCH AVE,TOPEKA,KS,66603-0000,0000,04,3,1000,200301,2,00,000000000,1,01,202312,2,2,01,0,12,30000,40000,40000,S20,
530196605,AMERICAN NATIONAL RED CROSS,,431 18TH ST NW,WASHINGTON,DC,20006-5310,0000,03,3,1000,194602,1,13,000000000,1,01,202306,9,9,01,0,06,1,1,1,P20,
"""


def epostcard_line(ein, year, name, terminated):
    """One 26-field 990-N row in the IRS data dictionary's order."""
    return "|".join([
        ein, year, name, "T", terminated, "01-01-%s" % year, "12-31-%s" % year, "",
        "Jo Officer", "1 Main St", "", "Austin", "", "TX", "78701", "US",
        "1 Main St", "", "Austin", "", "TX", "78701", "US", "", "", ""])


EPOSTCARD = "\n".join([
    epostcard_line("333333333", "2022", "Reinstated Org", "F"),
    epostcard_line("333333333", "2024", "Reinstated Org", "T"),
    epostcard_line("777777777", "2024", "Smith & Jones <Club>", "F"),
    epostcard_line("444444444", "2019", "Gone Charity", "T"),
]) + "\n"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.pub78 = os.path.join(d, "pub78.zip")
        with zipfile.ZipFile(self.pub78, "w") as zf:
            zf.writestr("data-download-pub78.txt", PUB78)
        self.revocation = os.path.join(d, "revocation.txt")
        with open(self.revocation, "w", encoding="utf-8") as fh:
            fh.write(REVOCATION)
        self.bmf = os.path.join(d, "eo1.csv")
        with open(self.bmf, "w", encoding="utf-8") as fh:
            fh.write(BMF)
        self.epostcard = os.path.join(d, "epostcard.zip")
        with zipfile.ZipFile(self.epostcard, "w") as zf:
            zf.writestr("data-download-epostcard.txt", EPOSTCARD)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, text):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        return p

    def local(self):
        return ["--pub78", self.pub78, "--revocation", self.revocation]


class TestNormalize(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(cc.normalize_ein("13-1623829"), "131623829")
        self.assertEqual(cc.normalize_ein(" 131623829 "), "131623829")
        self.assertEqual(cc.normalize_ein("131623829.0"), "131623829")
        self.assertEqual(cc.normalize_ein("12345678"), "012345678")
        self.assertEqual(cc.normalize_ein("1234567"), "001234567")

    def test_rejects(self):
        for bad in ("", None, "abc", "12-34", "1234567890", "000000000", "13-162382X"):
            self.assertIsNone(cc.normalize_ein(bad), bad)

    def test_format(self):
        self.assertEqual(cc.format_ein("131623829"), "13-1623829")


class TestDates(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cc.parse_irs_date("15-MAY-2010"), "2010-05-15")
        self.assertEqual(cc.parse_irs_date("5-jan-2021"), "2021-01-05")
        self.assertEqual(cc.parse_irs_date("2010-05-15"), "2010-05-15")
        self.assertEqual(cc.parse_irs_date("07/15/2020"), "2020-07-15")
        self.assertEqual(cc.parse_irs_date("12-31-2024"), "2024-12-31")
        self.assertEqual(cc.parse_irs_date(""), "")
        self.assertEqual(cc.parse_irs_date("sometime"), "sometime")


class TestLoaders(Fixture):
    def test_pub78_from_zip(self):
        got = cc.load_pub78(self.pub78, {"530196605", "012345678", "999999999"})
        self.assertEqual(set(got), {"530196605", "012345678"})
        self.assertEqual(got["530196605"]["codes"], ["PC"])
        self.assertEqual(got["530196605"]["state"], "DC")

    def test_pub78_multiple_codes(self):
        p = self.path("multi.txt", "777777777|Multi|Town|ST|United States|PC, SO\n")
        self.assertEqual(cc.load_pub78(p, {"777777777"})["777777777"]["codes"], ["PC", "SO"])

    def test_revocations(self):
        got = cc.load_revocations(self.revocation, {"444444444", "333333333"})
        self.assertEqual(got["444444444"][0]["revocation_date"], "2019-11-15")
        self.assertEqual(got["444444444"][0]["reinstatement_date"], "")
        self.assertEqual(got["333333333"][0]["reinstatement_date"], "2018-05-15")

    def test_bmf(self):
        got = cc.load_bmf([self.bmf], {"555555555", "666666666"})
        self.assertEqual(got["555555555"]["FOUNDATION"], "10")
        self.assertEqual(got["666666666"]["SUBSECTION"], "04")

    def test_epostcard_keeps_latest_year(self):
        got = cc.load_epostcard(self.epostcard, {"333333333", "777777777", "999999999"})
        self.assertEqual(set(got), {"333333333", "777777777"})
        self.assertEqual(got["333333333"]["tax_year"], "2024")
        self.assertTrue(got["333333333"]["terminated"])
        self.assertEqual(got["333333333"]["period_end"], "2024-12-31")
        self.assertFalse(got["777777777"]["terminated"])
        self.assertEqual(got["777777777"]["city"], "Austin")
        self.assertEqual(got["777777777"]["state"], "TX")


class TestAssess(Fixture):
    def run_check(self, eins, bmf=True, epostcard=False):
        return cc.check(eins, self.pub78, self.revocation, [self.bmf] if bmf else [],
                        self.epostcard if epostcard else None)

    def test_statuses(self):
        r = self.run_check(["53-0196605", "131623829", "12345678", "222222222", "333333333",
                            "444444444", "555555555", "666666666", "999999999", "garbage"])
        self.assertEqual(r["53-0196605"]["irs_status"], "eligible")
        self.assertEqual(r["53-0196605"]["irs_deductible"], "yes")
        self.assertEqual(r["53-0196605"]["ein_9"], "53-0196605")
        self.assertEqual(r["53-0196605"]["irs_foundation_code"], "13")
        self.assertEqual(r["53-0196605"]["irs_subsection"], "501(c)(3)")
        self.assertEqual(r["131623829"]["irs_status"], "eligible")
        self.assertIn("private_foundation", r["131623829"]["irs_flags"])
        self.assertIn("expenditure responsibility", r["131623829"]["irs_note"])
        self.assertIn("type3_non_functionally_integrated", r["12345678"]["irs_flags"])
        self.assertEqual(r["222222222"]["irs_status"], "review")
        self.assertEqual(r["333333333"]["irs_status"], "eligible")
        self.assertIn("reinstated_after_revocation", r["333333333"]["irs_flags"])
        self.assertEqual(r["444444444"]["irs_status"], "revoked")
        self.assertEqual(r["444444444"]["irs_deductible"], "no")
        self.assertEqual(r["444444444"]["irs_revocation_date"], "2019-11-15")
        self.assertEqual(r["444444444"]["irs_name"], "Gone Charity")
        self.assertEqual(r["555555555"]["irs_status"], "listed_bmf_not_pub78")
        self.assertEqual(r["555555555"]["irs_deductible"], "yes")
        self.assertIn("church", r["555555555"]["irs_flags"])
        self.assertIn("group_subordinate", r["555555555"]["irs_flags"])
        self.assertEqual(r["666666666"]["irs_status"], "exempt_not_501c3")
        self.assertEqual(r["666666666"]["irs_deductible"], "no")
        self.assertIn("501(c)(4)", r["666666666"]["irs_note"])
        self.assertEqual(r["999999999"]["irs_status"], "not_found")
        self.assertIn("Business Master File", r["999999999"]["irs_note"])
        self.assertEqual(r["garbage"]["irs_status"], "invalid_ein")

    def test_not_found_without_bmf_suggests_flag(self):
        r = self.run_check(["555555555"], bmf=False)
        self.assertEqual(r["555555555"]["irs_status"], "not_found")
        self.assertIn("--bmf", r["555555555"]["irs_note"])

    def test_terminated_990n_turns_eligible_into_review(self):
        r = self.run_check(["333333333"], epostcard=True)["333333333"]
        self.assertEqual(r["irs_status"], "review")
        self.assertEqual(r["irs_deductible"], "unknown")
        self.assertEqual(r["irs_990n_tax_year"], "2024")
        self.assertEqual(r["irs_990n_terminated"], "yes")
        self.assertIn("terminated_990n", r["irs_flags"])
        self.assertIn("reinstated_after_revocation", r["irs_flags"])
        self.assertIn("has terminated", r["irs_note"])

    def test_terminated_990n_keeps_revoked(self):
        r = self.run_check(["444444444"], epostcard=True)["444444444"]
        self.assertEqual(r["irs_status"], "revoked")
        self.assertIn("terminated_990n", r["irs_flags"])
        self.assertIn("tax year 2019", r["irs_note"])

    def test_990n_only_filer(self):
        r = self.run_check(["777777777"], epostcard=True)["777777777"]
        self.assertEqual(r["irs_status"], "not_found")
        self.assertEqual(r["irs_name"], "Smith & Jones <Club>")
        self.assertEqual(r["irs_990n_terminated"], "no")
        self.assertEqual(r["irs_flags"], "")
        self.assertIn("990-N e-Postcard for tax year 2024", r["irs_note"])

    def test_without_990n_columns_stay_empty(self):
        r = self.run_check(["333333333"])["333333333"]
        self.assertEqual(r["irs_status"], "eligible")
        self.assertEqual(r["irs_990n_tax_year"], "")


class TestReadInput(Fixture):
    def test_detects_tax_id_column(self):
        p = self.path("grantees.csv", "Grantee,Tax ID,Amount\nRed Cross,53-0196605,500\n")
        fields, rows, ein_field = cc.read_input(p)
        self.assertEqual(ein_field, "Tax ID")
        self.assertEqual(rows[0]["Amount"], "500")

    def test_headerless_list(self):
        p = self.path("list.txt", "53-0196605\n\n444444444\n")
        fields, rows, ein_field = cc.read_input(p)
        self.assertEqual(ein_field, "ein")
        self.assertEqual([r["ein"] for r in rows], ["53-0196605", "444444444"])

    def test_bom_and_semicolons(self):
        p = self.path("semi.csv", "﻿Name;EIN\nRed Cross;530196605\n")
        fields, rows, ein_field = cc.read_input(p)
        self.assertEqual(ein_field, "EIN")
        self.assertEqual(rows[0]["EIN"], "530196605")

    def test_explicit_column(self):
        p = self.path("odd.csv", "org,number\nRed Cross,530196605\n")
        self.assertEqual(cc.read_input(p, column="number")[2], "number")
        with self.assertRaises(SystemExit):
            cc.read_input(p)


class TestFailOn(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cc.parse_fail_on("revoked, review;terminated_990n"),
                         ["revoked", "review", "terminated_990n"])
        self.assertEqual(cc.parse_fail_on(""), [])
        with self.assertRaises(SystemExit):
            cc.parse_fail_on("revoked,bogus")

    def test_matches_status_or_flag(self):
        row = {"irs_status": "eligible", "irs_flags": "private_foundation;church"}
        self.assertTrue(cc.matches_any(row, ["church"]))
        self.assertFalse(cc.matches_any(row, ["revoked"]))
        self.assertTrue(cc.matches_any({"irs_status": "revoked", "irs_flags": ""}, ["revoked"]))


class TestMain(Fixture):
    def test_csv_end_to_end(self):
        src = self.path("grantees.csv", "Grantee,EIN,Amount\nRed Cross,53-0196605,500\nGone,444444444,250\n")
        out = os.path.join(self.tmp.name, "out.csv")
        rc = cc.main([src, "-o", out, "--bmf-file", self.bmf] + self.local())
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["irs_status"] for r in rows], ["eligible", "revoked"])
        self.assertEqual(rows[0]["Amount"], "500")
        self.assertTrue(rows[0]["irs_checked_on"])
        self.assertIn("irs_990n_terminated", rows[0])

    def test_loose_eins_json(self):
        out = os.path.join(self.tmp.name, "out.json")
        cc.main(["53-0196605", "999999999", "--json", "-o", out] + self.local())
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual([d["irs_status"] for d in data], ["eligible", "not_found"])
        self.assertEqual(data[0]["ein"], "53-0196605")

    def test_missing_file(self):
        with self.assertRaises(SystemExit):
            cc.main(["no-such-file.csv"] + self.local())

    def test_fail_on_exit_codes(self):
        out = os.path.join(self.tmp.name, "out.csv")
        self.assertEqual(cc.main(["53-0196605", "444444444", "-o", out, "--fail-on", "revoked"]
                                 + self.local()), 1)
        self.assertEqual(cc.main(["53-0196605", "-o", out, "--fail-on", "revoked,review"]
                                 + self.local()), 0)
        self.assertEqual(cc.main(["333333333", "-o", out, "--fail-on", "terminated_990n",
                                  "--epostcard-file", self.epostcard] + self.local()), 1)
        with self.assertRaises(SystemExit):
            cc.main(["53-0196605", "-o", out, "--fail-on", "nope"] + self.local())

    def test_html_report(self):
        out = os.path.join(self.tmp.name, "out.csv")
        report = os.path.join(self.tmp.name, "report.html")
        rc = cc.main(["53-0196605", "777777777", "444444444", "-o", out, "--html", report,
                      "--epostcard-file", self.epostcard] + self.local())
        self.assertEqual(rc, 0)
        with open(report, encoding="utf-8") as fh:
            page = fh.read()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("Smith &amp; Jones &lt;Club&gt;", page)
        self.assertNotIn("<Club>", page)
        self.assertIn("Rows that need a look (2)", page)
        self.assertIn("Form 990-N e-Postcard", page)
        self.assertIn("charity-check " + cc.__version__, page)

    def test_markdown_summary_appends(self):
        out = os.path.join(self.tmp.name, "out.csv")
        summary = self.path("summary.md", "earlier step\n")
        cc.main(["53-0196605", "444444444", "-o", out, "--summary", summary] + self.local())
        with open(summary, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("earlier step\n"))
        self.assertIn("### charity-check: 2 rows", text)
        self.assertIn("| revoked | 1 |", text)
        self.assertIn("| 2 | 44-4444444 | Gone Charity | revoked |", text)


@unittest.skipUnless(os.environ.get("CHARITY_CHECK_LIVE"), "set CHARITY_CHECK_LIVE=1 to test against the live IRS files")
class TestLiveIRS(unittest.TestCase):
    """Downloads the real IRS files (tens of MB) and checks that the formats still parse."""

    def test_red_cross(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.json")
            cc.main(["53-0196605", "--json", "-o", out, "--990n", "--data-dir", d])
            with open(out, encoding="utf-8") as fh:
                r = json.load(fh)[0]
        self.assertEqual(r["irs_status"], "eligible")
        self.assertIn("PC", r["irs_pub78_code"])
        self.assertIn("RED CROSS", r["irs_name"].upper())

    def test_revocation_file_has_dates(self):
        with tempfile.TemporaryDirectory() as d:
            path = cc.download(cc.REVOCATION_URL, os.path.join(d, "revocation.zip"))
            seen = 0
            for line in cc.iter_lines(path):
                parts = line.rstrip("\r\n").split("|")
                self.assertGreaterEqual(len(parts), 10, line)
                self.assertRegex(cc.parse_irs_date(parts[9]), r"^\d{4}-\d{2}-\d{2}$", line)
                seen += 1
                if seen >= 200:
                    break
        self.assertEqual(seen, 200)

    def test_epostcard_file_layout(self):
        with tempfile.TemporaryDirectory() as d:
            path = cc.download(cc.EPOSTCARD_URL, os.path.join(d, "epostcard.zip"))
            seen = 0
            for line in cc.iter_lines(path):
                parts = line.rstrip("\r\n").split("|")
                if not parts[0].strip().isdigit():
                    continue
                self.assertGreaterEqual(len(parts), 23, line)
                self.assertRegex(parts[1].strip(), r"^\d{4}", line)
                self.assertIn(parts[4].strip().upper(), ("T", "F", "Y", "N", ""), line)
                seen += 1
                if seen >= 200:
                    break
        self.assertEqual(seen, 200)


if __name__ == "__main__":
    unittest.main()
