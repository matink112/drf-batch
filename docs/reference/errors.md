# Error contract

Row errors are a single list aligned to the request array, using `{}` for
rows without errors. A row's non-field error is reported under DRF's
`NON_FIELD_ERRORS_KEY` (`non_field_errors` by default). Field errors include
malformed keys (`Invalid value for 'ticker': expected a string, got list.`,
raised before any collection runs) and lookup misses (`Ticker 'XYZ' not
found.`: the field's `error=` template or `Object '{value}' not found.`, with
`{value}` formatted from the **coerced** key).

| Status | Code | When | Body |
|---|---|---|---|
| 400 | `invalid` | malformed rows, validation errors | index-aligned list |
| 403 | `batch_permission_denied` | a target fails the object permissions | index-aligned list |
| 404 | `batch_target_not_found` | a target pk is missing or out of scope | index-aligned list |
| 409 | `batch_conflict` | a uniqueness rule is violated | index-aligned list |
| 409 | `batch_integrity_conflict` | the database rejected the write | `{"detail": ...}` |
| 413 | `batch_size_limit_exceeded` | more than `batch_max_size` rows | `{"detail": ...}` |
| 504 | `batch_collection_timeout` | async collection timed out | `{"detail": ...}` |

The checks run in this order, and the first failing one answers: malformed
rows (400) → not found (404) → permission (403) → validation (400) →
conflicts (409). Any error writes nothing. This format is stable and
independent of DRF's own `LIST_SERIALIZER_ERRORS_AS_DICT` transition.
