#!/usr/bin/env python3
"""charity-check: bulk-check U.S. nonprofits against the IRS's own monthly files.

Give it a CSV (or a plain list) of EINs. It downloads the IRS Publication 78 list
(organizations eligible to receive tax-deductible contributions) and the
automatic-revocation list, optionally the Exempt Organizations Business Master
File (EO BMF) and the Form 990-N (e-Postcard) file, and writes your file back
with a status for every row. With --baseline it also says what changed since an
earlier check.

Standard library only. Information from public IRS data, not legal or tax advice.
By MV2 of Munim, Inc. Apache-2.0 license.
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
from html import escape

__version__ = "0.4.0"

PUB78_URL = "https://apps.irs.gov/pub/epostcard/data-download-pub78.zip"
REVOCATION_URL = "https://apps.irs.gov/pub/epostcard/data-download-revocation.zip"
EPOSTCARD_URL = "https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip"
BMF_URLS = ["https://www.irs.gov/pub/irs-soi/eo%d.csv" % n for n in (1, 2, 3, 4)]
TEOS_URL = "https://apps.irs.gov/app/eos/"
REPO_URL = "https://github.com/munimtechnologies/charity-check"
USER_AGENT = "charity-check/%s (+%s)" % (__version__, REPO_URL)

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

OTHER_FLAGS = ("reinstated_after_revocation", "church", "group_subordinate", "terminated_990n",
               "group_central_unconfirmed")

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

# EO BMF affiliation codes: 6 and 8 hold a group exemption, 7 and 9 are covered by one.
GROUP_PARENT_CODES = ("6", "8")
GROUP_MEMBER_CODES = ("7", "9")

BMF_FIELDS = ("NAME", "CITY", "STATE", "GROUP", "SUBSECTION", "AFFILIATION",
              "RULING", "DEDUCTIBILITY", "FOUNDATION", "STATUS", "NTEE_CD")

OUTPUT_FIELDS = (
    "ein_9", "irs_status", "irs_deductible", "irs_pub78_code", "irs_pub78_meaning",
    "irs_name", "irs_city", "irs_state", "irs_revocation_date",
    "irs_reinstatement_date", "irs_subsection", "irs_foundation_code",
    "irs_foundation_meaning", "irs_ruling", "irs_ntee", "irs_990n_tax_year",
    "irs_990n_terminated", "irs_group_exemption", "irs_group_central_ein",
    "irs_group_central_name", "irs_flags", "irs_note",
)

STATUSES = ("eligible", "listed_bmf_not_pub78", "exempt_not_501c3", "revoked",
            "review", "not_found", "invalid_ein")

KNOWN_FLAGS = tuple(PUB78_FLAGS.values()) + OTHER_FLAGS

# --fail-on value for rows that changed since the --baseline check.
CHANGE_TOKEN = "changed"

# Columns compared with --baseline, and the words used to describe a change.
CHANGE_FIELDS = (("irs_status", "status"), ("irs_pub78_code", "Pub 78 code"),
                 ("irs_revocation_date", "revocation date"),
                 ("irs_reinstatement_date", "reinstatement date"),
                 ("irs_990n_terminated", "990-N terminated"))

MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}

EIN_HEADER = re.compile(r"(^|[^a-z])(ein|fein|tin)([^a-z]|$)|tax[ _-]?id|employer identification")

TRUE_VALUES = ("T", "Y", "TRUE", "YES", "1")


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


def group_key(gen):
    """Group exemption number as a lookup key: '0927' -> '927'; '' and '0000' -> ''."""
    return (gen or "").strip().lstrip("0")


def parse_irs_date(raw):
    """'15-MAY-2010' -> '2010-05-15'. ISO, MM/DD/YYYY and MM-DD-YYYY too; other text is kept as is."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", s)
    if m and m.group(2).upper() in MONTHS:
        return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(2).upper()], int(m.group(1)))
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
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


def load_epostcard(path, wanted):
    """Form 990-N file (26 pipe-delimited fields) -> {ein: latest filing} for wanted EINs.

    Fields used: 0 EIN, 1 tax year, 2 name, 4 terminated (T/F), 6 tax period end,
    18 organization city, 20 organization state (IRS 990-N data dictionary).
    """
    found = {}
    for line in iter_lines(path):
        parts = line.rstrip("\r\n").split("|")
        if len(parts) < 7:
            continue
        parts = (parts + [""] * 26)[:26]
        ein = parts[0].strip()
        if not ein.isdigit():
            continue
        ein = ein.zfill(9)
        if ein not in wanted:
            continue
        year = re.sub(r"\D", "", parts[1])[:4]
        rec = {
            "tax_year": year,
            "name": parts[2].strip(),
            "terminated": parts[4].strip().upper() in TRUE_VALUES,
            "period_end": parse_irs_date(parts[6]),
            "city": parts[18].strip(),
            "state": parts[20].strip(),
        }
        old = found.get(ein)
        if old is None or (rec["tax_year"], rec["period_end"]) >= (old["tax_year"], old["period_end"]):
            found[ein] = rec
    return found


def load_bmf(paths, wanted, centrals=None):
    """EO BMF CSV files (with header row) -> {ein: {field: value}} for wanted EINs.

    If centrals is a dict, it is filled with {group key: {"ein", "name"}} for every
    organization that holds a group exemption (affiliation code 6 or 8).
    """
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
            if centrals is not None and (row.get("AFFILIATION") or "").strip() in GROUP_PARENT_CODES:
                gen = group_key(row.get("GROUP"))
                if gen and gen not in centrals:
                    centrals[gen] = {"ein": ein, "name": (row.get("NAME") or "").strip()}
    return found


def latest_revocation(records):
    if not records:
        return None
    return max(records, key=lambda r: (r["revocation_date"], r["posting_date"]))


def group_central_note(b, central, pub78, revocations, flags):
    """Name the organization that holds a subordinate's group exemption and say how it is listed."""
    if not central:
        return ("No organization holding group exemption %s was found in the Business Master File "
                "searched." % b["GROUP"])
    held = "Group exemption %s is held by %s (EIN %s)" % (b["GROUP"], central["name"],
                                                          format_ein(central["ein"]))
    cp = pub78.get(central["ein"])
    crev = latest_revocation(revocations.get(central["ein"]))
    if crev and not crev["reinstatement_date"]:
        flags.append("group_central_unconfirmed")
        return ("%s, whose own exemption was automatically revoked %s; ask the organization for a "
                "current group exemption letter." % (held, crev["revocation_date"]))
    if cp:
        return "%s, which is listed in Publication 78 (code %s)." % (held, ",".join(cp["codes"]))
    flags.append("group_central_unconfirmed")
    return ("%s, which is not in Publication 78; ask the organization for the central organization's "
            "group exemption letter and proof that it is included in the group." % held)


def assess(ein, pub78, revocations, bmf, bmf_checked=False, epostcard=None, centrals=None):
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
    n = (epostcard or {}).get(ein)
    revoked_now = bool(rev) and not rev["reinstatement_date"]
    group_note = ""

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
        gen = group_key(b["GROUP"])
        if b["AFFILIATION"] in GROUP_MEMBER_CODES and gen:
            flags.append("group_subordinate")
            out["irs_group_exemption"] = b["GROUP"]
            central = (centrals or {}).get(gen)
            if central:
                out["irs_group_central_ein"] = format_ein(central["ein"])
                out["irs_group_central_name"] = central["name"]
            group_note = group_central_note(b, central, pub78, revocations, flags)
    if n:
        out["irs_990n_tax_year"] = n["tax_year"]
        out["irs_990n_terminated"] = "yes" if n["terminated"] else "no"
        if n["terminated"]:
            flags.append("terminated_990n")
        if not out["irs_name"]:
            out["irs_name"], out["irs_city"], out["irs_state"] = n["name"], n["city"], n["state"]

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
        if n:
            out["irs_note"] += " It did file a 990-N e-Postcard for tax year %s." % (n["tax_year"] or "unknown")
        if not bmf_checked:
            out["irs_note"] += " Re-run with --bmf to also search the Business Master File."
    if group_note:
        out["irs_note"] += " " + group_note

    if n and n["terminated"]:
        said = ("Its latest 990-N e-Postcard (tax year %s) says the organization has terminated."
                % (n["tax_year"] or "unknown"))
        if out["irs_status"] in ("eligible", "listed_bmf_not_pub78"):
            out["irs_status"] = "review"
            out["irs_deductible"] = "unknown"
            out["irs_note"] = ("%s %s IRS lists can lag a termination; confirm the organization still "
                               "operates before a grant." % (out["irs_note"], said))
        else:
            out["irs_note"] += " " + said
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


def load_baseline(path):
    """An earlier charity-check output (CSV or JSON) -> {ein: row}."""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8-sig") as fh:
            try:
                rows = json.load(fh)
            except ValueError:
                raise SystemExit("charity-check: --baseline %s is not valid JSON" % path)
        rows = rows if isinstance(rows, list) else []
        ein_field = "ein"
    else:
        _, rows, ein_field = read_input(path)
    rows = [r for r in rows if isinstance(r, dict)]
    if rows and not any("irs_status" in r for r in rows):
        raise SystemExit("charity-check: --baseline %s is not a charity-check output (no irs_status column)"
                         % path)
    found = {}
    for r in rows:
        if "irs_status" not in r:
            continue
        ein = normalize_ein(r.get("ein_9")) or normalize_ein(r.get(ein_field))
        if ein and ein not in found:
            found[ein] = r
    return found


def flag_set(text):
    return {f for f in str(text or "").split(";") if f}


def describe_change(old, row):
    """What changed between a baseline row and a new row: 'new', '' (nothing) or a short description.

    Only columns the baseline has are compared, so an older output compares what it can.
    """
    if old is None:
        return "new"
    parts = []
    for field, label in CHANGE_FIELDS:
        if field not in old:
            continue
        a, b = str(old.get(field) or "").strip(), str(row.get(field) or "").strip()
        if a != b:
            parts.append("%s %s -> %s" % (label, a or "(none)", b or "(none)"))
    if "irs_flags" in old:
        a, b = flag_set(old.get("irs_flags")), flag_set(row.get("irs_flags"))
        diff = ["+" + f for f in sorted(b - a)] + ["-" + f for f in sorted(a - b)]
        if diff:
            parts.append("flags " + " ".join(diff))
    return "; ".join(parts)


def is_changed(row):
    return row.get("irs_change") not in (None, "", "new")


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


def source_date(path):
    """Best known date of a data file: the server's Last-Modified, else the download or file time."""
    meta = path + ".json"
    if os.path.exists(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                info = json.load(fh)
            if info.get("last_modified"):
                return info["last_modified"]
            if info.get("downloaded_at"):
                return "downloaded " + info["downloaded_at"]
        except (OSError, ValueError):
            pass
    try:
        return "file dated " + datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return ""


def default_data_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "charity-check")


def check(eins, pub78_path, revocation_path, bmf_paths=(), epostcard_path=None):
    """Assess an iterable of raw EIN strings. Returns {raw: irs_* dict}."""
    normalized = {raw: normalize_ein(raw) for raw in eins}
    wanted = {e for e in normalized.values() if e}
    centrals = {}
    bmf = load_bmf(bmf_paths, wanted, centrals) if bmf_paths else {}
    # Also look up the organization holding each subordinate's group exemption.
    lookup = set(wanted)
    for b in bmf.values():
        gen = group_key(b["GROUP"])
        if b["AFFILIATION"] in GROUP_MEMBER_CODES and gen in centrals:
            lookup.add(centrals[gen]["ein"])
    pub78 = load_pub78(pub78_path, lookup)
    revocations = load_revocations(revocation_path, lookup)
    epostcard = load_epostcard(epostcard_path, wanted) if epostcard_path else {}
    return {raw: assess(e, pub78, revocations, bmf, bool(bmf_paths), epostcard, centrals)
            for raw, e in normalized.items()}


def parse_fail_on(text):
    """'revoked, review' -> ['revoked', 'review']; unknown names are an error."""
    names = [t.strip() for t in re.split(r"[,;\s]+", text or "") if t.strip()]
    unknown = [t for t in names if t not in STATUSES and t not in KNOWN_FLAGS and t != CHANGE_TOKEN]
    if unknown:
        raise SystemExit("charity-check: unknown --fail-on value(s): %s; use statuses (%s), flags (%s) or %s"
                         % (", ".join(unknown), ", ".join(STATUSES), ", ".join(KNOWN_FLAGS), CHANGE_TOKEN))
    return names


def matches_any(row, names):
    flags = row["irs_flags"].split(";") if row["irs_flags"] else []
    if CHANGE_TOKEN in names and is_changed(row):
        return True
    return row["irs_status"] in names or any(f in names for f in flags)


def needs_look(row):
    return row["irs_status"] != "eligible" or bool(row["irs_flags"])


def count_statuses(rows):
    counts = {}
    for r in rows:
        counts[r["irs_status"]] = counts.get(r["irs_status"], 0) + 1
    return counts


def compared(rows):
    """True if the rows were checked against a --baseline."""
    return any("irs_change" in r for r in rows)


def render_html(rows, ein_field, sources, checked_on):
    """A self-contained HTML report: totals, changes, the rows that need a look, then every row."""
    counts = count_statuses(rows)
    look = [(i, r) for i, r in enumerate(rows, 1) if needs_look(r)]

    def table(items):
        out = ["<table><thead><tr><th>Row</th><th>EIN</th><th>IRS name</th><th>Location</th>"
               "<th>Status</th><th>Code</th><th>Flags</th><th>Note</th></tr></thead><tbody>"]
        for i, r in items:
            place = ", ".join(x for x in (r["irs_city"], r["irs_state"]) if x)
            out.append(
                '<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td><span class="s %s">%s</span></td>'
                "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                    i, escape(r["ein_9"] or r.get(ein_field, "")), escape(r["irs_name"]), escape(place),
                    escape(r["irs_status"]), escape(r["irs_status"]), escape(r["irs_pub78_code"]),
                    escape(r["irs_flags"].replace(";", ", ")), escape(r["irs_note"])))
        out.append("</tbody></table>")
        return "\n".join(out)

    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>charity-check report %s</title>" % escape(checked_on),
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:2em;color:#222}"
        "table{border-collapse:collapse;margin:1em 0}th,td{border:1px solid #ccc;padding:4px 8px;"
        "text-align:left;vertical-align:top}th{background:#f3f3f3}.s{padding:1px 6px;border-radius:3px}"
        ".eligible{background:#dff3df}.revoked,.invalid_ein{background:#f8d7d7}.review{background:#fde9c4}"
        ".not_found,.listed_bmf_not_pub78,.exempt_not_501c3{background:#e6ecf5}small{color:#666}</style>",
        "</head><body>",
        "<h1>charity-check report</h1>",
        "<p>%d rows checked on %s with charity-check %s.</p>" % (len(rows), escape(checked_on), __version__),
        "<h2>Totals</h2><table><thead><tr><th>Status</th><th>Rows</th></tr></thead><tbody>",
    ]
    for s in STATUSES:
        if s in counts:
            parts.append('<tr><td><span class="s %s">%s</span></td><td>%d</td></tr>' % (s, s, counts[s]))
    parts.append("</tbody></table>")
    if compared(rows):
        changed = [(i, r) for i, r in enumerate(rows, 1) if is_changed(r)]
        added = sum(1 for r in rows if r.get("irs_change") == "new")
        parts.append("<h2>Changed since the last check (%d)</h2>" % len(changed))
        if changed:
            parts.append("<table><thead><tr><th>Row</th><th>EIN</th><th>IRS name</th><th>Status</th>"
                         "<th>Change</th></tr></thead><tbody>")
            for i, r in changed:
                parts.append('<tr><td>%d</td><td>%s</td><td>%s</td><td><span class="s %s">%s</span></td>'
                             "<td>%s</td></tr>" % (i, escape(r["ein_9"]), escape(r["irs_name"]),
                                                   escape(r["irs_status"]), escape(r["irs_status"]),
                                                   escape(r["irs_change"])))
            parts.append("</tbody></table>")
        else:
            parts.append("<p>None of the statuses, codes, dates or flags changed.</p>")
        if added:
            parts.append("<p>%d rows were not in the earlier check.</p>" % added)
    parts.append("<h2>Rows that need a look (%d)</h2>" % len(look))
    parts.append(table(look) if look else "<p>None: every row is in Publication 78 with no flags.</p>")
    parts.append("<details><summary>All %d rows</summary>" % len(rows))
    parts.append(table(list(enumerate(rows, 1))))
    parts.append("</details>")
    parts.append("<h2>IRS data used</h2><ul>")
    for label, path in sources:
        parts.append("<li>%s: %s</li>" % (escape(label), escape(source_date(path) or "date unknown")))
    parts.append("</ul>")
    parts.append("<p><small>Information from public IRS data, not legal or tax advice. IRS files lag by "
                 "weeks; confirm large or unusual grants in <a href=\"%s\">IRS TEOS</a>. Made with "
                 "<a href=\"%s\">charity-check</a> by MV2 of Munim, Inc.</small></p>" % (TEOS_URL, REPO_URL))
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"


def render_markdown(rows, ein_field, limit=50):
    """A short Markdown summary, e.g. for a GitHub Actions job summary."""
    counts = count_statuses(rows)
    lines = ["### charity-check: %d rows" % len(rows), "", "| status | rows |", "|---|---|"]
    lines += ["| %s | %d |" % (s, counts[s]) for s in STATUSES if s in counts]
    if compared(rows):
        changed = [(i, r) for i, r in enumerate(rows, 1) if is_changed(r)]
        lines += ["", "**Changed since the last check: %d**" % len(changed)]
        if changed:
            lines += ["", "| row | EIN | IRS name | status | change |", "|---|---|---|---|---|"]
            for i, r in changed[:limit]:
                cells = (r["ein_9"], r["irs_name"], r["irs_status"], r["irs_change"])
                lines.append("| %d | %s |" % (i, " | ".join(c.replace("|", "/") for c in cells)))
    look = [(i, r) for i, r in enumerate(rows, 1) if needs_look(r)]
    if look:
        lines += ["", "| row | EIN | IRS name | status | flags |", "|---|---|---|---|---|"]
        for i, r in look[:limit]:
            cells = (r["ein_9"] or r.get(ein_field, ""), r["irs_name"], r["irs_status"],
                     r["irs_flags"].replace(";", ", "))
            lines.append("| %d | %s |" % (i, " | ".join(c.replace("|", "/") for c in cells)))
        if len(look) > limit:
            lines.append("")
            lines.append("...and %d more rows that need a look." % (len(look) - limit))
    return "\n".join(lines) + "\n"


def build_parser():
    ap = argparse.ArgumentParser(
        prog="charity-check",
        description="Bulk-check U.S. nonprofits (by EIN) against IRS Publication 78, the "
                    "auto-revocation list and, optionally, the EO Business Master File and the "
                    "990-N e-Postcard file.")
    ap.add_argument("inputs", nargs="+", metavar="INPUT",
                    help="a CSV with an EIN column, a text file of EINs, or EINs themselves")
    ap.add_argument("-o", "--output", help="output CSV (default: stdout)")
    ap.add_argument("--json", action="store_true", help="write JSON instead of CSV")
    ap.add_argument("--html", help="also write a self-contained HTML report to this file")
    ap.add_argument("--summary", help="append a Markdown summary to this file (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--baseline",
                    help="an earlier charity-check output (CSV or JSON); adds an irs_change column saying "
                         "what changed since then (it may be the same file as --output)")
    ap.add_argument("--fail-on", default="",
                    help="exit with status 1 if any row has one of these statuses or flags "
                         "(comma-separated, e.g. revoked,review,terminated_990n), or 'changed' "
                         "for rows that changed since --baseline")
    ap.add_argument("--column", help="name of the EIN column if it is not detected")
    ap.add_argument("--bmf", action="store_true",
                    help="also download and search the EO Business Master File (about 1.9M rows); "
                         "names the organization holding each subordinate's group exemption")
    ap.add_argument("--990n", dest="use_990n", action="store_true",
                    help="also download the Form 990-N e-Postcard file and flag organizations "
                         "whose latest e-Postcard says they have terminated")
    ap.add_argument("--data-dir", default=None, help="where IRS files are cached (default: ~/.cache/charity-check)")
    ap.add_argument("--refresh", action="store_true", help="download fresh IRS files even if cached")
    ap.add_argument("--pub78", help="use this local Pub 78 file (zip or txt) instead of downloading")
    ap.add_argument("--revocation", help="use this local auto-revocation file (zip or txt)")
    ap.add_argument("--bmf-file", action="append", default=[], help="use this local EO BMF CSV (repeatable)")
    ap.add_argument("--epostcard-file", help="use this local 990-N e-Postcard file (zip or txt)")
    ap.add_argument("--version", action="version", version="charity-check " + __version__)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or default_data_dir()
    fail_on = parse_fail_on(args.fail_on)
    # Read the baseline first: it may be the file --output is about to replace.
    baseline = load_baseline(args.baseline) if args.baseline else None
    if CHANGE_TOKEN in fail_on and baseline is None:
        raise SystemExit("charity-check: --fail-on %s needs --baseline" % CHANGE_TOKEN)

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
    sources = [("Publication 78", pub78_path), ("Automatic revocation list", rev_path)]
    bmf_paths = list(args.bmf_file)
    if args.bmf and not bmf_paths:
        bmf_paths = [download(u, os.path.join(data_dir, os.path.basename(u)), max_age_days=31,
                              refresh=args.refresh) for u in BMF_URLS]
    sources += [("EO Business Master File (%s)" % os.path.basename(p), p) for p in bmf_paths]
    epostcard_path = args.epostcard_file
    if args.use_990n and not epostcard_path:
        epostcard_path = download(EPOSTCARD_URL, os.path.join(data_dir, "epostcard.zip"), refresh=args.refresh)
    if epostcard_path:
        sources.append(("Form 990-N e-Postcard", epostcard_path))

    results = check([row.get(ein_field, "") for row in rows], pub78_path, rev_path, bmf_paths, epostcard_path)
    checked_on = date.today().isoformat()
    out_rows = []
    for row in rows:
        merged = dict(row)
        merged.update(results[row.get(ein_field, "")])
        if baseline is not None:
            e = normalize_ein(merged["ein_9"])
            merged["irs_change"] = describe_change(baseline.get(e), merged) if e else ""
        else:
            merged.pop("irs_change", None)  # stale, if the input is an earlier output
        merged["irs_checked_on"] = checked_on
        out_rows.append(merged)
    extra = ("irs_change",) if baseline is not None else ()
    columns = list(fieldnames) + [c for c in OUTPUT_FIELDS + extra + ("irs_checked_on",) if c not in fieldnames]

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

    if args.html:
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(render_html(out_rows, ein_field, sources, checked_on))
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(render_markdown(out_rows, ein_field))

    counts = count_statuses(out_rows)
    summary = ", ".join("%s %d" % (s, counts[s]) for s in STATUSES if s in counts)
    sys.stderr.write("charity-check: %d rows: %s\n" % (len(out_rows), summary))
    if baseline is not None:
        changed = sum(1 for r in out_rows if is_changed(r))
        added = sum(1 for r in out_rows if r.get("irs_change") == "new")
        sys.stderr.write("charity-check: %d rows changed since %s, %d new\n" % (changed, args.baseline, added))
    if fail_on:
        hits = [r for r in out_rows if matches_any(r, fail_on)]
        if hits:
            sys.stderr.write("charity-check: %d rows match --fail-on %s\n" % (len(hits), ",".join(fail_on)))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
