# Permissions

> Built in Phase 3. From then on this file is **generated from code** (`make permissions-doc`)
> and must not be edited by hand.

Until then, every route declares itself with `@public` (see `app/authz/declarations.py`),
and the CI test `tests/ci/test_route_rules.py` fails on any route without a declaration.
