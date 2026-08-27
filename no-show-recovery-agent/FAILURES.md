# Failure and verification record

## Resolved issues

1. **Malformed Calendar `start` values were silently accepted.**
   - **Cause:** Calendar normalization converted arbitrary objects to strings.
   - **Fix:** [`_calendar_start()`](modules/detector.py:36) accepts only strings or `pandas.Timestamp` values. Explicit malformed values become auditable `source_error` events through [`check_calendar_live()`](modules/detector.py:152).

2. **`SUB019` had a nonnumeric subscription amount.**
   - **Cause:** [`failed_subscription_cases.csv`](data/failed_subscription_cases.csv:20) contained `not-a-number`.
   - **Fix:** The amount is now the confirmed positive value `849`.

3. **`SUB020` had no client email.**
   - **Cause:** [`failed_subscription_cases.csv`](data/failed_subscription_cases.csv:21) contained an empty email field.
   - **Fix:** The email is now `anika.sen@example.com`.

4. **CSV validation reported defects but returned success.**
   - **Cause:** The command-line block in [`validate_csv.py`](validate_csv.py:45) printed errors without setting a failure exit status.
   - **Fix:** It aggregates all findings and exits `1` when any validation error exists, otherwise `0`.

5. **Ad-hoc multiline `python -c` diagnostics were fragile on Windows CMD.**
   - **Cause:** Literal newline escapes and nested quoting were interpreted differently by the shell and Python.
   - **Fix:** Reusable cross-shell diagnostics now live in [`inspect_project.py`](inspect_project.py) and are exposed as `npm run inspect` in [`package.json`](package.json:10).

6. **Direct Git status commands failed when the directory had no `.git` metadata.**
   - **Cause:** The supplied workspace is not a Git repository.
   - **Fix:** [`repository_check.py`](repository_check.py) explicitly detects this state, prints an informative message, and exits successfully because missing Git metadata is an environment condition rather than an application defect. It is exposed as `npm run repo:check` in [`package.json`](package.json:11).

## Verification contract

- [`npm run check`](package.json:12) runs dependency/import inspection, tests, compilation, CSV validation, and repository-state reporting using Windows-safe script files.
- All 50 source records are expected to validate and process cleanly in offline batch mode.
- Tests retain synthetic malformed-event and integration failure cases so defensive error handling remains covered even though production fixtures are now valid.
