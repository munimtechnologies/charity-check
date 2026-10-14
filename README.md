# charity-check

Bulk-check U.S. nonprofits against the IRS's own files, for free, on your own computer.

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
`church`, `group_subordinate`, `reinstated_after_revocation`, `terminated_990n` and others.
Every row is stamped with `irs_checked_on`.

Small foundations, giving circles, donor-advised fund sponsors, fiscal sponsors, corporate
matching programs and donation platforms all have to do this check. The IRS lookup site
(TEOS) does one organization at a time; paid services do it in bulk. This does it in bulk
from the same public IRS data, and your list never leaves your machine.

## Use

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
- `--bmf` also searches the 1.9M-row Business Master File (finds churches, subordinates and
  non-501(c)(3) exempt groups). `--990n` also reads the Form 990-N e-Postcard file that small
  organizations file each year; an organization whose latest e-Postcard says it has terminated
  is flagged `terminated_990n` and moved to `review` even if it is still in Pub 78.
- `--html report.html` writes a single-file report you can open, print or attach to a grant
  file: totals, the rows that need a look, every row, and the date of each IRS file used.
- `--fail-on revoked,review` exits with status 1 if any row has one of those statuses or flags,
  so a nightly job or a donation platform's pipeline can stop on a bad recipient.
  `--summary FILE` appends a short Markdown table (for example to `$GITHUB_STEP_SUMMARY`).
- IRS files are downloaded once and cached in `~/.cache/charity-check` for 7 days (31 for the
  BMF). `--refresh` forces a new download; `--data-dir` moves the cache.
- `--pub78`, `--revocation`, `--bmf-file` and `--epostcard-file` use files you already
  downloaded (zip or text).
- A summary goes to stderr: `charity-check: 240 rows: eligible 231, revoked 3, not_found 6`.

## As a GitHub Action

Keep your recipient list in a repository and check it on a schedule:

```yaml
name: check grantees
on:
  schedule:
    - cron: "0 7 * * 1"
  workflow_dispatch:
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: munimv2/charity-check@v0.2.0
        with:
          file: grantees.csv
          bmf: "true"
          epostcard: "true"
          fail-on: revoked,review
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: charity-check
          path: |
            charity-check.csv
            charity-check.html
```

Inputs: `file` (required), `fail-on` (default `revoked,review,invalid_ein`; empty never fails),
`bmf`, `epostcard`, `column`, `output` (default `charity-check.csv`) and `html` (default
`charity-check.html`). The job summary shows the totals and the rows that need a look. Only
use this with a private repository if your list is private.

## Data sources

All from the IRS, updated monthly:

- Publication 78 data: https://apps.irs.gov/pub/epostcard/data-download-pub78.zip
- Automatic revocation list: https://apps.irs.gov/pub/epostcard/data-download-revocation.zip
- Form 990-N e-Postcard (with `--990n`): https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip
  (26 pipe-delimited fields; see the [990-N data dictionary](https://www.irs.gov/pub/irs-tege/990n-data-dictionary.pdf))
- EO Business Master File extract (with `--bmf`): https://www.irs.gov/pub/irs-soi/eo1.csv to eo4.csv
- Code meanings: [deductibility status codes](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-deductibility-status-codes)
  and the EO BMF [info sheet](https://www.irs.gov/pub/foia/ig/tege/eo-info.pdf)

Rev. Proc. 2011-33 describes when grantors and contributors may rely on Publication 78 or the
Business Master File.

## Tests

```
python -m unittest -v                          # offline, uses small fixtures
CHARITY_CHECK_LIVE=1 python -m unittest -v     # also downloads the live IRS files and checks their layout
```

## Limits

- Group-exemption subordinates are not listed separately in Pub 78; churches need not apply
  for recognition at all. `not_found` is a reason to look, not proof of anything.
- Only small organizations file the 990-N, so `terminated_990n` catches only those that said so
  on an e-Postcard; larger organizations report termination on Form 990 or 990-EZ.
- The IRS files lag by weeks. For a large or unusual grant, confirm in
  [TEOS](https://apps.irs.gov/app/eos/) and keep the output as your record.
- This is information from public IRS data, not legal or tax advice.

## About

Made by MV2 of Munim, Inc. with AI agents. Issues and pull requests welcome; if your
organization uses it, an issue saying so (and what would make it more useful) helps a lot.
MIT license.
