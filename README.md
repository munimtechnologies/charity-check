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
| `review` | In Pub 78 *and* on the revocation list: the files were posted on different days, check by hand |
| `not_found` | In none of the files checked |
| `invalid_ein` | Not a 9-digit EIN |

It also adds the Pub 78 deductibility code (PC, PF, SO, SONFI ...), the foundation code, the
ruling year, the NTEE code, revocation and reinstatement dates, and flags worth a second look
before a grant: `private_foundation`, `type3_non_functionally_integrated`,
`supporting_org_type_unknown`, `foreign_address`, `church`, `group_subordinate`,
`reinstated_after_revocation` and others. Every row is stamped with `irs_checked_on`.

Small foundations, giving circles, donor-advised fund sponsors, fiscal sponsors, corporate
matching programs and donation platforms all have to do this check. The IRS lookup site
(TEOS) does one organization at a time; paid services do it in bulk. This does it in bulk
from the same public IRS data, and your list never leaves your machine.

## Use

Python 3.8+, standard library only.

```
python charity_check.py grantees.csv -o checked.csv
python charity_check.py grantees.csv --bmf -o checked.csv     # also search the Business Master File
python charity_check.py 53-0196605 --json                      # quick lookups
```

- The EIN column is found by name (`EIN`, `FEIN`, `TIN`, `Tax ID`, `Employer Identification
  Number` ...); otherwise pass `--column`. A plain text file with one EIN per line works too.
- EINs can be `53-0196605`, `530196605`, or spreadsheet-mangled `530196605.0` /
  `12345678` (a lost leading zero is restored).
- IRS files are downloaded once and cached in `~/.cache/charity-check` for 7 days (31 for the
  BMF). `--refresh` forces a new download; `--data-dir` moves the cache.
- `--pub78`, `--revocation` and `--bmf-file` use files you already downloaded (zip or text).
- A summary goes to stderr: `charity-check: 240 rows: eligible 231, revoked 3, not_found 6`.

## Data sources

All from the IRS, updated monthly:

- Publication 78 data: https://apps.irs.gov/pub/epostcard/data-download-pub78.zip
- Automatic revocation list: https://apps.irs.gov/pub/epostcard/data-download-revocation.zip
- EO Business Master File extract (with `--bmf`): https://www.irs.gov/pub/irs-soi/eo1.csv to eo4.csv
- Code meanings: [deductibility status codes](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-deductibility-status-codes)
  and the EO BMF [info sheet](https://www.irs.gov/pub/foia/ig/tege/eo-info.pdf)

Rev. Proc. 2011-33 describes when grantors and contributors may rely on Publication 78 or the
Business Master File.

## Limits

- Group-exemption subordinates are not listed separately in Pub 78; churches need not apply
  for recognition at all. `not_found` is a reason to look, not proof of anything.
- The IRS files lag by weeks. For a large or unusual grant, confirm in
  [TEOS](https://apps.irs.gov/app/eos/) and keep the output as your record.
- This is information from public IRS data, not legal or tax advice.

## About

Made by MV2 of Munim, Inc. with AI agents. Issues and pull requests welcome.
MIT license.
