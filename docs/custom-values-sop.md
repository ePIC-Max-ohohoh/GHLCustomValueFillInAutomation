# Updating Custom Values with this application

1. Fill `data/advisors.csv` with one row per advisor and an exact GHL Location ID.
2. Leave a cell blank to preserve the current GHL value.
3. Run a single-advisor dry run and review every proposed change.
4. Resolve all `MISSING`, `AMBIGUOUS`, `LEGACY`, `DISABLED`, and API errors manually.
5. Apply to that advisor only after review.
6. Confirm affected campaigns in GHL and send the normal test emails.

The application updates existing Custom Values only. It cannot create, delete, rename, fuzzy-match, or repair campaign references. It sends the live GHL Custom Value ID and the live unchanged name in every update request.

## Safe commands

```bash
python -m src.main --advisor "John Smith"
python -m src.main --advisor "John Smith" --apply
```

Do not begin with a batch `--apply`.

