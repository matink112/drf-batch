# Security policy

## Supported versions

`drf-batch` is pre-1.0: only the latest released minor version receives
security fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

<!-- TODO(maintainer): replace with a monitored address before publishing. -->
Report privately to **security@REPLACE-ME.example** or through GitHub's
"Report a vulnerability" (private security advisories) on the repository.
Include a minimal reproduction and the affected versions. You will get an
acknowledgement within 7 days.

Areas of particular interest: authorization (queryset scoping and object
permissions on batch update/delete), injection through lookup keys, and
resource exhaustion (batch size, collection timeouts).
