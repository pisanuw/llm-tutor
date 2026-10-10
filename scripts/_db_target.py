"""Compatibility shim: the production-DB fence lives in tutor/db/target.py."""
from tutor.db.target import (  # noqa: F401
    ALLOW_ENV,
    DBTarget,
    describe_engine,
    prepare_schema,
    print_target,
    require_prod_ok,
)
