# charity-check

Bulk-check U.S. nonprofits against the IRS's own files, for free, on your own computer.

**No install: [use it in your browser](https://munimv2.github.io/charity-check/).** Paste
EINs or choose a CSV, choose the IRS files you downloaded, and get the same statuses as the
command-line tool, a CSV to download and a printable report. The page is not allowed to make
any network requests (its Content-Security-Policy is `default-src 'none'`), so your list and the
IRS files stay on your computer. It is one HTML file ([docs/index.html](docs/index.html)) that you
can also save and open offline.

Give it a spreadsheet of grantees, matching-gift requests or donation recipients with an
EIN column. It writes the same spreadsheet back with a status for every row:

| irs_status | meaning |
|---|---|
| `eligible` | Listed in IRS Publication 78 (eligible to receive tax-deductible contributions) |
| `listed_bmf_not_pub78` | In the IRS Business Master File as 501(c)(3) but not in Pub 78 (common for churches, group-exemption subordinates, new organizations) |
| `exempt_not_501c3` | Tax-exempt, but under another 501(c) subsection (gifts usually not deductible) |
| `revoked` | On the automatic-revocation list with no reinstatement |
| `review` | Check by hand: in Pub 78 *and* on the revocation list (the files are posted on different days), or listed but its latest 990-N e-Postcard says it has terminated |
| `not_found` | In none of the files checked |
| `invalid_ein` | Not a 9-digit EIN |

It also adds the Pub 78 deductibility code (PC, PF, SO, SONFI ...), the foundation code, the
ruling year, the NTEE code, revocation and reinstatement dates, the latest 990-N tax year, and
flags worth a second look before a grant: `private_foundation`,
`type3_non_functionally_integrated`, `supporting_org_type_unknown`, `foreign_address`,
`church`, `group_subordinate`, `group_central_unconfirmed`, `reinstated_after_revocation`,
`terminated_990n` and others. Every row is stamped with `irs_checked_on`.

Small foundations, giving circles, donor-advised fund sponsors, fiscal sponsors, corporate
matching programs and donation platforms all have to do this check. The IRS lookup site
(TEOS) does one organization at a time; paid services do it in bulk. This does it in bulk
from the same public IRS data, and your list never leaves your machine.

## What changed since last month

A grantee that was fine when you approved it can be revoked, or file a final e-Postcard, a few
months later. Keep the CSV from your last check and pass it as `--baseline` (or choose it in
step 3 of the browser page):

```
python charity_check.py grantees.csv --baseline last-check.csv -o last-check.csv --html report.html
```

Every row gets an `irs_change` column: empty if nothing that matters changed, `new` if the EIN
was not in the earlier check, or a short description such as
`status eligible -> revoked; Pub 78 code PC -> (none); revocation date (none) -> 2026-05-15` or
`flags +terminated_990n`. The status, Pub 78 code, revocation and reinstatement dates, 990-N
termination and flags are compared; names and notes are not. The report and the job summary
list the changed rows first, and `--fail-on changed` exits with status 1 when there are any.
The baseline may be the same file as `--output`; it is read before it is replaced. Use the same
options (`--bmf`, `--990n`) each time, or rows will look changed only because a file was left out.

## Group exemptions

Thousands of congregations, chapters, posts and local units are tax-exempt only through a
central organization's group exemption. They are not listed in Publication 78 or TEOS on their
own, which is why a plain lookup says "not found". With the Business Master File (`--bmf`, or
the eo1-eo4 files in the browser), charity-check finds each subordinate's group exemption number
and names the organization that holds it:

- `irs_group_exemption`, `irs_group_central_ein`, `irs_group_central_name`
- a note saying whether that central organization is itself in Publication 78
- the flag `group_central_unconfirmed` if the central organization is not in Publication 78 or
  its own exemption was revoked: ask the grantee for the central organization's group exemption
  letter and proof that it is included in the group.

## Use (command line)

Python 3.8+, standard library only.

```
python charity_check.py grantees.csv -o checked.csv
python charity_check.py grantees.csv --bmf --990n -o checked.csv --html report.html   # the full check
python charity_check.py 53-0196605 --json                                              # quick lookups
```

- The EIN column is found by name (`EIN`, `FEIN`, `TIN`, `Tax ID`, `Employer Identification
  Number` ...); otherwise pass `--column`. A plain text file with one EIN per line works too.
- EINs can be `53-0196605`, `530196605`, or spreadsheet-mangled `530196605.0` /
  `12345678` (a lost leading zero is restored).
- `--bmf` also searches the 1.9M-row Business Master File (finds churches, group-exemption
  subordinates and their central organization, and non-501(c)(3) exempt groups). `--990n` also
  reads the Form 990-N e-Postcard file that small organizations file each year; an organization
  whose latest e-Postcard says it has terminated is flagged `terminated_990n` and moved to
  `review` even if it is still in Pub 78.
- `--html report.html` writes a single-file report you can open, print or attach to a grant
  file: totals, changes since the baseline, the rows that need a look, every row, and the date
  of each IRS file used.
- `--baseline last.csv` compares with an earlier output (CSV or JSON); see above.
- `--fail-on revoked,review` exits with status 1 if any row has one of those statuses or flags
  (or, with `changed`, if any row changed since the baseline), so a nightly job or a donation
  platform's pipeline can stop on a bad recipient. `--summary FILE` appends a short Markdown
  table (for example to `$GITHUB_STEP_SUMMARY`).
- IRS files are downloaded once and cached in `~/.cache/charity-check` for 7 days (31 for the
  BMF). `--refresh` forces a new download; `--data-dir` moves the cache.
- `--pub78`, `--revocation`, `--bmf-file` and `--epostcard-file` use files you already
  downloaded (zip or text).
- A summary goes to stderr: `charity-check: 240 rows: eligible 231, revoked 3, not_found 6`.

## Use from Python

An intake or grants pipeline can call it directly; it is one file with no dependencies:

```python
import charity_check as cc

pub78 = cc.download(cc.PUB78_URL, "irs/pub78.zip")             # re-downloaded after 7 days
revocations = cc.download(cc.REVOCATION_URL, "irs/revocation.zip")
results = cc.check(["53-0196605", "58-1766061"], pub78, revocations)
for ein, r in results.items():
    print(ein, r["irs_status"], r["irs_flags"], r["irs_note"])
```

`check()` also takes `bmf_paths` (the four `eo*.csv` files, from `cc.BMF_URLS`) and
`epostcard_path` (`cc.EPOSTCARD_URL`). Each result has the `irs_*` columns described above.

## Why not just look the EIN up on ProPublica?

ProPublica's Nonprofit Explorer is a great place to read 990s, but its organization record is
not a status check. On 2026-10-14 its API still returned Cancer Fund of America
(EIN 58-1766061), one of the sham cancer charities ordered dissolved in a 2016 settlement with
the FTC and all 50 states, with `subsection_code` 3, which is what a "is it a 501(c)(3)?" test
usually looks at. Its `data_source` was `xml_backfill`, not a current Business Master File
snapshot such as `current_2026_09_16`. charity-check reads the IRS's current Publication 78,
revocation list and BMF instead, so an organization that has dropped out of them is never
reported as `eligible`.

If you already use the ProPublica API, a cheap guard is to treat a record whose
`organization.data_source` is not a recent `current_YYYY_MM_DD` snapshot as unverified.

## As a GitHub Action

Keep your recipient list in a repository and check it on a schedule. This workflow keeps the
last result in the Actions cache, fails when a recipient is revoked or needs review, and also
fails once when anything changed since the previous run:

```yaml
name: check grantees
on:
  schedule:
    - cron: "0 7 * * 1,4"   # twice a week: Actions deletes caches unused for 7 days
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache/restore@v4
        with:
          path: last-check.csv
          key: charity-check-${{ github.run_id }}
          restore-keys: charity-check-
      - uses: munimv2/charity-check@v0.4.0
        with:
          file: grantees.csv
          bmf: "true"
          epostcard: "true"
          baseline: last-check.csv
          fail-on: revoked,review,changed
      - if: always()
        run: cp charity-check.csv last-check.csv || true
      - if: always()
        uses: actions/cache/save@v4
        with:
          path: last-check.csv
          key: charity-check-${{ github.run_id }}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: charity-check
          path: |
            charity-check.csv
            charity-check.html
```

Inputs: `file` (required), `fail-on` (default `revoked,review,invalid_ein`; empty never fails),
`bmf`, `epostcard`, `column`, `baseline` (skipped, along with `changed`, on the first run when
the file does not exist yet), `output` (default `charity-check.csv`) and `html` (default
`charity-check.html`). The job summary shows the totals, the changes and the rows that need a
look. Only use this with a private repository if your list is private.

## Data sources

All from the IRS, updated monthly:

- Publication 78 data: https://apps.irs.gov/pub/epostcard/data-download-pub78.zip
- Automatic revocation list: https://apps.irs.gov/pub/epostcard/data-download-revocation.zip
- Form 990-N e-Postcard (with `--990n`): https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip
  (26 pipe-delimited fields; see the [990-N data dictionary](https://www.irs.gov/pub/irs-tege/990n-data-dictionary.pdf))
- EO Business Master File extract (with `--bmf`): https://www.irs.gov/pub/irs-soi/eo1.csv to eo4.csv
- Code meanings: [deductibility status codes](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-deductibility-status-codes)
  and the EO BMF [info sheet](https://www.irs.gov/pub/foia/ig/tege/eo-info.pdf) (affiliation
  codes 6 and 8 hold a group exemption; 7 and 9 are covered by one)

Rev. Proc. 2011-33 describes when grantors and contributors may rely on Publication 78 or the
Business Master File.

## Tests

```
python -m unittest -v                          # offline, uses small fixtures
CHARITY_CHECK_LIVE=1 python -m unittest -v     # also downloads the live IRS files and checks their layout
```

The offline tests also check that the browser page uses the same codes, statuses, flags,
columns and change wording as the Python code, and that it contains no network calls.

## Limits

- Group-exemption subordinates are not listed separately in Pub 78; churches need not apply
  for recognition at all. `not_found` is a reason to look, not proof of anything.
- The BMF names a subordinate's group exemption, not whether the central organization still
  includes it; for a large grant, ask for the central organization's confirmation.
- Only small organizations file the 990-N, so `terminated_990n` catches only those that said so
  on an e-Postcard; larger organizations report termination on Form 990 or 990-EZ.
- The IRS files lag by weeks. For a large or unusual grant, confirm in
  [TEOS](https://apps.irs.gov/app/eos/) and keep the output as your record.
- This is information from public IRS data, not legal or tax advice.

## About

Made by MV2 of Munim, Inc. with AI agents. Issues and pull requests welcome; if your
organization uses it, an issue saying so (and what would make it more useful) helps a lot.
MIT license.
