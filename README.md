# ghl-custom-values-automation

Current milestone: a **read-only agency Private Integration connectivity test**. Live Custom Value writes are disabled until the token context and accessible locations have been verified.

## Read-only connectivity test

The current official HighLevel v3 documentation says an **Agency Private Integration Token** can call:

```text
GET https://services.leadconnectorhq.com/locations/search
```

The Private Integration must have the `locations.readonly` scope. The endpoint accepts both OAuth and Private Integration authentication, and specifically supports Agency tokens. This application does not treat `GHL_AGENCY_TOKEN` as an OAuth token, does not refresh or exchange it, and sends it only as:

```text
Authorization: Bearer <token>
```

Configure `.env`:

```text
GHL_AGENCY_TOKEN=your-agency-private-integration-token
GHL_COMPANY_ID=
```

`GHL_COMPANY_ID` is optional because the official location-search schema marks `companyId` optional. Supply it only if your agency requires the explicit filter.

Run:

```bash
python -m src.connectivity
```

Successful stdout contains only one sub-account per line:

```text
Sub-account name<TAB>Location ID
```

The command performs paginated `GET` requests only. It never logs response bodies, prints the token, exchanges the token, or modifies HighLevel. HTTP 401 reports an invalid/wrong-context token; HTTP 403 specifically asks for `locations.readonly`.

Each run also creates a timestamped processing log under `logs/`, for example:

```text
logs/connectivity-20260818T120000Z.log
```

The log records start time, pages fetched, retry events, each processed sub-account name and Location ID, the completion total, and safe error messages. Authorization headers, tokens, and API response bodies are never logged. Set `GHL_CONNECTIVITY_LOG_DIR` in `.env` to use a different directory.

Official references: [Search Sub-Accounts](https://marketplace.gohighlevel.com/docs/ghl/locations/search-locations/), [Private Integrations](https://marketplace.gohighlevel.com/docs/Authorization/PrivateIntegrationsToken), and [HighLevel scopes](https://marketplace.gohighlevel.com/docs/Authorization/Scopes/).

## Export all Business Profile Settings for Excel

To read the Business Profile Settings for every HighLevel sub-account and save
one row per advisor, run:

```bash
python -m src.export_business_profiles
```

The command creates `output/ghl-business-profiles-<timestamp>.csv`, encoded for
Excel and with phone numbers preserved as text. Columns match the Business
Profile screen: Location ID, Friendly Business Name, Legal Business Name,
Business Email, Business Phone, Branded Domain, Business Website, Business
Niche, Business Currency, and Business Logo URL.

This export is read-only. It uses only `GET /locations/search` and
`GET /locations/:locationId`; it never changes a sub-account. HighLevel's
documented v3 response does not guarantee Business Niche or Business Currency,
so those cells remain blank when the account does not return them.

## Future-phase scaffolding

The repository contains earlier CSV, exact-match, OAuth, reporting, and mocked updater scaffolding for a later phase. The production CLI refuses `--apply`, so no Custom Value write can be initiated while Private Integration connectivity is being validated.

The connectivity implementation is isolated in `src/connectivity.py` and does not import the OAuth or Custom Values clients.

## Setup

Python 3.10 or newer is recommended.

```bash
cd ghl-custom-values-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` locally. Never commit or share it. The Agency Private Integration token is stored only as `GHL_AGENCY_TOKEN`.

```text
GHL_AGENCY_TOKEN=
GHL_COMPANY_ID=
```

Do not configure the reserved OAuth variables or run the older dry-run workflow for this connectivity milestone.

## Tests

Tests mock HighLevel and verify GET-only behavior, v3 headers, pagination, permission errors, retries, and token non-disclosure. They never contact a live account:

```bash
pytest -q
```

## Read-only Vimeo connectivity test

Set `VIMEO_ACCESS_TOKEN` in `.env` to a Vimeo Personal Access Token with the
`public` and `private` scopes, then run:

```bash
python -m src.vimeo_connectivity
```

The command performs only `GET` requests. It calls `GET /me`, follows the
advertised personal-folder and team connections, checks each discovered role at
`GET /users/{team_owner_user_id}/team/role`, and reads Team Library folders at
`GET /users/{team_owner_user_id}/folders`. Every collection follows
`paging.next`. The token and response bodies are never printed or logged.

Vimeo now calls projects "folders," while parts of the API retain the original
`project` nomenclature. Team-owned folders must be addressed with the team
owner's Vimeo user ID, not the authenticated team member's ID. Vimeo can
advertise a nonzero `metadata.connections.teams.total` while returning `404`
for the advertised teams collection. In that case the test reports the
inconsistency without calling the token invalid. If the owner ID is already
known, set the optional comma-separated `VIMEO_TEAM_OWNER_USER_IDS` value and
rerun; the test then performs the documented owner-scoped, read-only lookup
with the same token.

Each run writes a private processing log named
`logs/vimeo-connectivity-<timestamp>.log`. The log contains account/folder
progress but no authorization header or token.

Official references: [Vimeo API Reference](https://developer.vimeo.com/api/reference),
[Teams](https://developer.vimeo.com/api/reference/teams),
[Working with Folders](https://developer.vimeo.com/api/guides/folders), and
[Project response](https://developer.vimeo.com/api/reference/response/project).

### Vimeo-to-GHL Custom Value sync

Named Vimeo title aliases are configured in `config/vimeo_mappings.yml`.
Titles beginning with `NN.` map dynamically to `advisorvimeovideoNN`. Every
candidate target must exist as an exact live GHL Custom Value `fieldKey`; the
sync never creates a missing value and never falls back to a display-name
match. The default mode is read-only:

```bash
python -m src.main --advisor "Barry Goldwater" --sync-vimeo
```

Live writes require the explicit `--apply` flag. The sync reads nested Vimeo
folders, reports every unmapped video, shows the current and proposed GHL
values for confirmation, and writes a private text report under `logs/`.

## Read-only WebPrez Playwright proof of concept

The WebPrez proof of concept uses browser automation and an authenticated
Playwright storage-state file only. No WebPrez credentials belong in source or
`.env`. The storage state is gitignored, must have mode `0600`, and should be
captured through a manual headed login:

```bash
python -m playwright install chromium
python -m src.webprez_poc --capture-storage-state
```

Run exactly one advisor after the storage state has been captured:

```bash
python -m src.webprez_poc --advisor "Barry Goldwater" --headed
```

Targets and exact title aliases are stored in `config/webprez_targets.yml`.
The browser lists the visible category/title catalog and extracts only **Video
Page Hypertext Link WITH Viewing Notice** URLs. It never selects the WITHOUT
Viewing Notice or WITH Lead Capture options. Non-GET/HEAD/OPTIONS requests and
downloads are blocked. Selector failures produce a private screenshot and a
sanitized HTML excerpt under `logs/webprez-debug/`. This proof of concept does
not import the GHL client and performs no GHL writes.

## Troubleshooting

- `401`: the token is invalid, expired/rotated, or not accepted in Agency context.
- `403`: edit the Agency Private Integration to grant `locations.readonly`; copy a newly rotated token if HighLevel requires it after scope changes.
- `422`: verify the token was created under Agency settings and, if provided, `GHL_COMPANY_ID` is correct.
- `429/5xx`: the test retries a bounded number of times, then exits without modifying anything.
