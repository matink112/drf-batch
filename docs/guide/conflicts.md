# Cross-row conflicts (409)

The conflict pass checks uniqueness for the **whole batch** before writing:
duplicates inside the payload and clashes with existing rows are a 409, not a
per-row query or a 500 at commit time. Rules come from two places:

1. **The model, automatically.** Non-pk `unique=True` fields,
   `Meta.unique_together`, and unconditional field-only `UniqueConstraint`s
   become rules whenever all their fields are writable on the serializer.
   DRF's per-row `UniqueValidator`/`UniqueTogetherValidator` for those rules
   are removed, so validation does no per-row queries. Opt out with
   `Meta.batch_model_unique = False`.
2. **Explicitly**, for rules the model does not declare:

```python
# quickstart_app/models.py: give Order a client-supplied reference
class Order(models.Model):
    ...
    reference_id = models.CharField(max_length=64)


# quickstart_app/serializers.py
class ConflictOrderSerializer(OrderSerializer):
    class Meta(OrderSerializer.Meta):
        fields = (*OrderSerializer.Meta.fields, "reference_id")
        # one rule per tuple entry: a single field name, or a composite
        # (tuple of field names). Every name must be a concrete field, an
        # FK, or the pk on Meta.model (ImproperlyConfigured otherwise).
        batch_unique_together = ("reference_id",)
```

```bash
curl -X POST http://localhost:8000/conflict-orders/batch/ \
  -H 'Content-Type: application/json' \
  -d '[
    {"ticker": "1101", "customer": "112002", "volume": 1, "reference_id": "DUP"},
    {"ticker": "1102", "customer": "112003", "volume": 2, "reference_id": "DUP"}
  ]'
```

```json
[
  {"reference_id": ["Conflicts with another row in this batch."]},
  {"reference_id": ["Conflicts with another row in this batch."]}
]
```

- Two rows in the same payload with the same rule value are a **409** on
  every row of the group ("Conflicts with another row in this batch.").
- A row whose rule value matches an existing row (other than its own target,
  for updates) is a **409** ("Conflicts with an existing record.").
- `NULL` values never conflict, matching SQL `UNIQUE`.
- The check is one query per rule, against the whole table (uniqueness is not
  tenant-scoped).
- An update row that omits a rule field is checked against the field's
  current value, so a row keeping its own value never conflicts with itself.
- The status is overridable per view: `batch_conflict_status = 422`.
