#!/usr/bin/env python3
"""charity-check: bulk-check U.S. nonprofits against the IRS's own monthly files.

Give it a CSV (or a plain list) of EINs. It downloads the IRS Publication 78 list
(organizations eligible to receive tax-deductible contributions) and the
automatic-revocation list, optionally the Exempt Organizations Business Master
File (EO BMF), and writes your file back with a status for every row.

Standard library only. Information from public IRS data, not legal or tax advice.
By MV2 of Munim, Inc. MIT license.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timezone

__version__ = "0.1.0"

PUB78_URL = "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip"
REVOCATION_URL = "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip"
BMF_URLS = ["https://www.irs.gov/pub/irs-soi/eo%d.csv" % n for n in (1, 2, 3, 4)]
TEOS_URL = "https://apps.irs.gov/app/eos/"
USER_AGENT = "charity-check/%s (+https://github.com/munimv2/charity-check)" % __version__

# https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-deductibility-status-codes
PUB78_CODES = {
    "PC": "Public charity",
    "POF": "Private operating foundation",
    "PF": "Private foundation",
    "GROUP": "Central organization holding a group exemption (subordinates not listed separately)",
    "LODGE": "Domestic fraternal society under the lodge system (gifts for charitable use only)",
    "UNKWN": "Charitable organization whose public charity status has not been determined",
    "EO": "Section 170(c) organization other than a public charity or private foundation",
    "FED": "Deductible if made for the use of a federal governmental unit",
    "FORGN": "Foreign-addressed organization",
    "SO": "Type I, Type II or functionally integrated Type III supporting organization",
    "SONFI": "Non-functionally integrated Type III supporting organization",
    "SOUNK": "Supporting organization, type unspecified",
}

# Flags raised by a Pub 78 code; most are worth a second look before a grant.
PUB78_FLAGS = {
    "PF": "private_foundation",
    "POF": "private_operating_foundation",
    "GROUP": "group_exemption_central",
    "LODGE": "lodge_charitable_use_only",
    "UNKWN": "public_charity_status_undetermined",
    "EO": "other_170c_organization",
    "FED": "federal_unit_use_only",
    "FORGN": "foreign_address",
    "SONFI": "type3_non_functionally_integrated",
    "SOUNK": "supporting_org_type_unknown",
}

# EO BMF foundation codes (IRS eo-info.pdf).
FOUNDATION_CODES = {
    "00": "All organizations except 501(c)(3)",
    "02": "Private operating foundation exempt from excise tax on investment income",
    "03": "Private operating foundation (other)",
    "04": "Private non-operating foundation",
    "09": "Suspense",
    "10": "Church 170(b)(1)(A)(i)",
    "11": "School 170(b)(1)(A)(ii)",
    "12": "Hospital or medical research organization 170(b)(1)(A)(iii)",
    "13": "Organization supporting a government-owned college or university 170(b)(1)(A)(iv)",
    "14": "Governmental unit 170(b)(1)(A)(v)",
    "15": "Publicly supported organization 170(b)(1)(A)(vi)",
    "16": "Publicly supported organization 509(a)(2)",
    "17": "Supporting organization 509(a)(3)",
    "18": "Organization that tests for public safety 509(a)(4)",
    "21": "Supporting organization 509(a)(3) Type I",
    "22": "Supporting organization 509(a)(3) Type II",
    "23": "Supporting organization 509(a)(3) Type III functionally integrated",
    "24": "Supporting organization 509(a)(3) Type III non-functionally integrated",
    "25": "Agricultural research organization 170(b)(1)(A)(ix)",
}

DEDUCTIBILITY_CODES = {"1": "yes", "2": "no", "4": "yes"}

BMF_FIELDS = ("NAME", "CITY", "STATE", "GROUP", "SUBSECTION", "AFFILIATION",
              "RULING", "DEDUCTIBILITY", "FOUNDATION", "STATUS", "NTEE_CD")

OUTPUT_FIELDS = (
    "ein_9", "irs_status", "irs_deductible", "irs_pub78_code", "irs_pub78_meaning",
    "irs_name", "irs_city", "irs_state", "irs_revocation_date",
    "irs_reinstatement_date", "irs_subsection", "irs_foundation_code",
    "irs_foundation_meaning", "irs_ruling", "irs_ntee", "irs_flags", "irs_note",
)

STATUSES = ("eligible", "listed_bmf_not_pub78", "exempt_not_501c3", "revoked",
            "review", "not_found", "invalid_ein")

MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

EIN_HEADER = re.compile(r"(^|[^a-z])(ein|fein|tin)([^a-z]|$)|tax[ _-]?id|employer identification")


def normalize_ein(raw):
    """Return a 9-digit EIN string, or None if raw cannot be one.

    Accepts 13-1623829, 131623829, spreadsheet floats like 131623829.0 and EINs
    that lost their leading zeros in a spreadsheet (7 or 8 digits).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"\.0+$", "", s)
    if not re.fullmatch(r"[\d\s-]+", s):
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7 or len(digits) > 9:
        return None
    digits = digits.zfill(9)
    if digits == "000000000":
        return None
    return digits


def format_ein(ein9):
    return "%s-%s" % (ein9[:2], ein9[2:]) if ein9 else ""


def parse_irs_date(raw):
    """'15-MAY-2010' -> '2010-05-15'. ISO and MM/DD/YYYY pass through; other text is kept as is."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if m and m.group(2).upper() in MONTHS:
        return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(2).upper()], int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    return s


def iter_lines(path):
    """Yield text lines from a plain file or from the largest member of a zip."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            members = [i for i in zf.infolist() if not i.is_dir()]
            if not members:
                return
            member = max(members, key=lambda i: i.file_size)
            with zf.open(member) as raw:
                for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace"):
                    yield line
    else:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line


def load_pub78(path, wanted):
    """EIN|Name|City|State|Country|Deductibility code(s) -> {ein: record} for wanted EINs."""
    found = {}
    for line in iter_lines(path):
        parts = line.rstrip("\r\n").split("|")
        if len(parts) < 6:
            continue
        ein = parts[0].strip()
        if not ein.isdigit():
            continue
        ein = ein.zfill(9)
        if ein not in wanted:
            continue
        codes = [c for c in re.split(r"[,;\s]+", parts[-1].strip().upper()) if c]
        rec = found.setdefault(ein, {
            "name": parts[1].strip(), "city": parts[2].strip(),
            "state": parts[3].strip(), "country": parts[4].strip(), "codes": [],
        })
        for c in codes:
            if c not in rec["codes"]:
                rec["codes"].append(c)
    return found


def load_revocations(path, wanted):
    """Auto-revocation list (12 pipe-delimited fields) -> {ein: [records]} for wanted EINs."""
    found = {}
    for line in iter_lines(path):
        parts = line.rstrip("\r\n").split("|")
        if len(parts) < 10:
            continue
        parts = (parts + [""] * 12)[:12]
        ein = parts[0].strip()
        if not ein.isdigit():
            continue
        ein = ein.zfill(9)
        if ein not in wanted:
            continue
        found.setdefault(ein, []).append({
            "name": parts[1].strip(),
            "dba": parts[2].strip(),
            "city": parts[4].strip(),
            "state": parts[5].strip(),
            "exemption_type": parts[8].strip(),
            "revocation_date": parse_irs_date(parts[9]),
            "posting_date": parse_irs_date(parts[10]),
            "reinstatement_date": parse_irs_date(parts[11]),
        })
    return found


def load_bmf(paths, wanted):
    """EO BMF CSV files (with header row) -> {ein: {field: value}} for wanted EINs."""
    found = {}
    for path in paths:
        reader = csv.DictReader(iter_lines(path))
        for row in reader:
            ein = (row.get("EIN") or "").strip()
            if not ein.isdigit():
                continue
            ein = ein.zfill(9)
            if ein in wanted and ein not in found:
                found[ein] = {k: (row.get(k) or "").strip() for k in BMF_FIELDS}
    return found


def latest_revocation(records):
    if not records:
        return None
    return max(records, key=lambda r: (r["revocation_date"], r["posting_date"]))


def assess(ein, pub78, revocations, bmf, bmf_checked=False):
    """Return the irs_* columns for one EIN."""
    out = dict.fromkeys(OUTPUT_FIELDS, "")
    if ein is None:
        out["irs_status"] = "invalid_ein"
        out["irs_deductible"] = "unknown"
        out["irs_note"] = "Not a 9-digit EIN."
        return out
    out["ein_9"] = format_ein(ein)
    flags = []
    p = pub78.get(ein)
    rev = latest_revocation(revocations.get(ein))
    b = bmf.get(ein)
    revoked_now = bool(rev) and not rev["reinstatement_date"]

    if p:
        out["irs_pub78_code"] = ",".join(p["codes"])
        out["irs_pub78_meaning"] = "; ".join(PUB78_CODES.get(c, "Unknown code") for c in p["codes"])
        out["irs_name"], out["irs_city"], out["irs_state"] = p["name"], p["city"], p["state"]
        for c in p["codes"]:
            if c in PUB78_FLAGS:
                flags.append(PUB78_FLAGS[c])
    if rev:
        out["irs_revocation_date"] = rev["revocation_date"]
        out["irs_reinstatement_date"] = rev["reinstatement_date"]
        if rev["reinstatement_date"]:
            flags.append("reinstated_after_revocation")
        if not out["irs_name"]:
            out["irs_name"], out["irs_city"], out["irs_state"] = rev["name"], rev["city"], rev["state"]
    if b:
        sub = b["SUBSECTION"]
        out["irs_subsection"] = "501(c)(%d)" % int(sub) if sub.isdigit() else sub
        out["irs_foundation_code"] = b["FOUNDATION"]
        out["irs_foundation_meaning"] = FOUNDATION_CODES.get(b["FOUNDATION"], "")
        out["irs_ruling"] = b["RULING"]
        out["irs_ntee"] = b["NTEE_CD"]
        if not out["irs_name"]:
            out["irs_name"], out["irs_city"], out["irs_state"] = b["NAME"], b["CITY"], b["STATE"]
        if b["FOUNDATION"] == "10":
            flags.append("church")
        if b["AFFILIATION"] == "9" and b["GROUP"].strip("0"):
            flags.append("group_subordinate")

    if p and revoked_now:
        out["irs_status"] = "review"
        out["irs_deductible"] = "unknown"
        out["irs_note"] = ("Listed in Publication 78 but also on the auto-revocation list (revoked %s, "
                           "no reinstatement shown). The two files are posted on different days; "
                           "check IRS TEOS %s." % (rev["revocation_date"], TEOS_URL))
    elif p:
        out["irs_status"] = "eligible"
        out["irs_deductible"] = "yes"
        out["irs_note"] = "Listed in IRS Publication 78."
        if "private_foundation" in flags:
            out["irs_note"] += " Private foundation: grants from other private foundations may need expenditure responsibility."
        if "type3_non_functionally_integrated" in flags or "supporting_org_type_unknown" in flags:
            out["irs_note"] += " Supporting organization: check the rules on grants from private foundations and donor-advised funds."
    elif revoked_now:
        out["irs_status"] = "revoked"
        out["irs_deductible"] = "no"
        out["irs_note"] = ("Tax exemption automatically revoked %s for not filing a return or notice "
                           "for three consecutive years; no reinstatement in the IRS file."
                           % rev["revocation_date"])
    elif b and b["SUBSECTION"] in ("03", "3"):
        out["irs_status"] = "listed_bmf_not_pub78"
        out["irs_deductible"] = DEDUCTIBILITY_CODES.get(b["DEDUCTIBILITY"], "unknown")
        out["irs_note"] = ("In the IRS Business Master File as 501(c)(3) but not in Publication 78 "
                           "(common for churches, group-exemption subordinates and new organizations). "
                           "Rev. Proc. 2011-33 lets donors rely on either list.")
    elif b:
        out["irs_status"] = "exempt_not_501c3"
        out["irs_deductible"] = DEDUCTIBILITY_CODES.get(b["DEDUCTIBILITY"], "unknown")
        out["irs_note"] = "Tax-exempt under %s, not 501(c)(3)." % (out["irs_subsection"] or "another subsection")
    else:
        out["irs_status"] = "not_found"
        out["irs_deductible"] = "unknown"
        where = "Publication 78, the auto-revocation list or the Business Master File" if bmf_checked \
            else "Publication 78 or the auto-revocation list"
        out["irs_note"] = ("Not in %s. It may be a church, a group-exemption subordinate, a government "
                           "unit, a typo or not exempt; look it up in IRS TEOS %s." % (where, TEOS_URL))
        if not bmf_checked:
            out["irs_note"] += " Re-run with --bmf to also search the Business Master File."
    out["irs_flags"] = ";".join(flags)
    return out


def read_input(path, column=None):
    """Read a CSV with an EIN column, or a headerless list of EINs.

    Returns (fieldnames, rows, ein_field) where rows are dicts.
    """
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
        text = fh.read()
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return [], [], None
    try:
        dialect = csv.Sniffer().sniff(lines[0], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    table = list(csv.reader(lines, dialect))
    header = [h.strip() for h in table[0]]
    if column:
        lowered = [h.lower() for h in header]
        if column.lower() not in lowered:
            raise SystemExit("charity-check: column %r not found; columns are: %s"
                             % (column, ", ".join(header)))
        idx = lowered.index(column.lower())
    else:
        idx = next((i for i, h in enumerate(header) if EIN_HEADER.search(h.lower())), None)
    if idx is None:
        if normalize_ein(header[0]) is None:
            raise SystemExit("charity-check: no EIN column found in %s; name it 'EIN' or pass --column"
                             % path)
        # Headerless list: one EIN per line (extra columns are kept).
        width = max(len(r) for r in table)
        fieldnames = ["ein"] + ["column_%d" % (i + 1) for i in range(1, width)]
        rows = [dict(zip(fieldnames, r)) for r in table]
        return fieldnames, rows, "ein"
    fieldnames = []
    for i, h in enumerate(header):
        name = h or "column_%d" % (i + 1)
        while name in fieldnames:
            name += "_"
        fieldnames.append(name)
    rows = []
    for r in table[1:]:
        r = (r + [""] * len(fieldnames))[:len(fieldnames)]
        rows.append(dict(zip(fieldnames, r)))
    return fieldnames, rows, fieldnames[idx]


def download(url, dest, max_age_days=7, refresh=False):
    """Download url to dest unless a fresh copy is cached. Returns dest."""
    if not refresh and os.path.exists(dest) and time.time() - os.path.getmtime(dest) < max_age_days * 86400:
        return dest
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    sys.stderr.write("charity-check: downloading %s\n" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    part = dest + ".part"
    with urllib.request.urlopen(req, timeout=300) as resp, open(part, "wb") as fh:
        last_modified = resp.headers.get("Last-Modified", "")
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    os.replace(part, dest)
    with open(dest + ".json", "w", encoding="utf-8") as fh:
        json.dump({"url": url, "last_modified": last_modified,
                   "downloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)
    return dest


def default_data_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "charity-check")


def check(eins, pub78_path, revocation_path, bmf_paths=()):
    """Assess an iterable of raw EIN strings. Returns {raw: irs_* dict}."""
    normalized = {raw: normalize_ein(raw) for raw in eins}
    wanted = {e for e in normalized.values() if e}
    pub78 = load_pub78(pub78_path, wanted)
    revocations = load_revocations(revocation_path, wanted)
    bmf = load_bmf(bmf_paths, wanted) if bmf_paths else {}
    return {raw: assess(e, pub78, revocations, bmf, bool(bmf_paths)) for raw, e in normalized.items()}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="charity-check",
        description="Bulk-check U.S. nonprofits (by EIN) against IRS Publication 78, "
                    "the auto-revocation list and, optionally, the EO Business Master File.")
    ap.add_argument("inputs", nargs="+", metavar="INPUT",
                    help="a CSV with an EIN column, a text file of EINs, or EINs themselves")
    ap.add_argument("-o", "--output", help="output CSV (default: stdout)")
    ap.add_argument("--json", action="store_true", help="write JSON instead of CSV")
    ap.add_argument("--column", help="name of the EIN column if it is not detected")
    ap.add_argument("--bmf", action="store_true",
                    help="also download and search the EO Business Master File (about 1.9M rows)")
    ap.add_argument("--data-dir", default=None, help="where IRS files are cached (default: ~/.cache/charity-check)")
    ap.add_argument("--refresh", action="store_true", help="download fresh IRS files even if cached")
    ap.add_argument("--pub78", help="use this local Pub 78 file (zip or txt) instead of downloading")
    ap.add_argument("--revocation", help="use this local auto-revocation file (zip or txt)")
    ap.add_argument("--bmf-file", action="append", default=[], help="use this local EO BMF CSV (repeatable)")
    ap.add_argument("--version", action="version", version="charity-check " + __version__)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or default_data_dir()

    fieldnames, rows, ein_field = [], [], "ein"
    loose = []
    for item in args.inputs:
        if os.path.isfile(item):
            f, r, e = read_input(item, args.column)
            if e is None:
                continue
            if not fieldnames:
                fieldnames, ein_field = f, e
            for row in r:
                if e != ein_field:
                    row[ein_field] = row.pop(e, "")
                rows.append(row)
        elif normalize_ein(item) is None and re.search(r"[./\\]", item):
            raise SystemExit("charity-check: file not found: %s" % item)
        else:
            loose.append(item)
    if loose:
        if not fieldnames:
            fieldnames = ["ein"]
        rows.extend({ein_field: x} for x in loose)
    if ein_field not in fieldnames:
        fieldnames = [ein_field] + fieldnames

    pub78_path = args.pub78 or download(PUB78_URL, os.path.join(data_dir, "pub78.zip"), refresh=args.refresh)
    rev_path = args.revocation or download(REVOCATION_URL, os.path.join(data_dir, "revocation.zip"),
                                           refresh=args.refresh)
    bmf_paths = list(args.bmf_file)
    if args.bmf and not bmf_paths:
        bmf_paths = [download(u, os.path.join(data_dir, os.path.basename(u)), max_age_days=31,
                              refresh=args.refresh) for u in BMF_URLS]

    results = check([row.get(ein_field, "") for row in rows], pub78_path, rev_path, bmf_paths)
    checked_on = date.today().isoformat()
    out_rows = []
    for row in rows:
        merged = dict(row)
        merged.update(results[row.get(ein_field, "")])
        merged["irs_checked_on"] = checked_on
        out_rows.append(merged)
    columns = list(fieldnames) + [c for c in OUTPUT_FIELDS + ("irs_checked_on",) if c not in fieldnames]

    out = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    try:
        if args.json:
            json.dump([{c: r.get(c, "") for c in columns} for r in out_rows], out, indent=2)
            out.write("\n")
        else:
            writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(out_rows)
    finally:
        if args.output:
            out.close()

    counts = {}
    for r in out_rows:
        counts[r["irs_status"]] = counts.get(r["irs_status"], 0) + 1
    summary = ", ".join("%s %d" % (s, counts[s]) for s in STATUSES if s in counts)
    sys.stderr.write("charity-check: %d rows: %s\n" % (len(out_rows), summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
