# OpenAPI (drf-spectacular)

Install the extra and add the (otherwise optional) app:

```python
INSTALLED_APPS = [..., "drf_spectacular", "drf_batch"]
```

Every `BatchMixin` viewset is then documented accurately:

- the request bodies are arrays: create rows, update rows with a required pk
  and all other fields optional, and the delete pk array (described in the
  operation, since OpenAPI tooling drops DELETE bodies);
- the success body (`BatchCount` or the data, per `batch_response`) and the
  index-aligned 400/403/404/409 bodies plus 413/504 are included;
- operations not in `batch_actions` are excluded;
- cached fields are typed by their lookup field (`lookup="isin"` is a string,
  a UUID pk a `uuid`).
