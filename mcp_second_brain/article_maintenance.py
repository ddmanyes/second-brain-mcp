"""Explicit administrator preview/apply for pending shared article metadata."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .article_reconcile import reconcile_articles
from .identity import Identity, hash_key
from .query_event_report import _SafeArgumentParser, _bounded_int, _read_key, _suppress_pool_logs
from .store.postgres_store import PostgresStore
from .visibility import multiuser_enabled


def main(argv=None, *, stdin=None, stdout=None, stderr=None, store_factory=PostgresStore):
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument('--key-stdin', required=True, action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', default=20, type=_bounded_int(1, 100))
    args = parser.parse_args(argv)
    store = None
    with _suppress_pool_logs():
        try:
            root = os.environ.get('SECOND_BRAIN_PATH', '')
            dsn = os.environ.get('SB_PG_DSN', '')
            if not multiuser_enabled() or not root or not Path(root).is_absolute() or not dsn:
                raise ValueError('explicit multiuser configuration required')
            credential = hash_key(_read_key(stdin))
            store = store_factory(dsn, min_size=0, max_size=1)
            identity = store.get_identity_for_key(credential)
            if not isinstance(identity, Identity) or identity.role != 'admin' or not identity.user_uuid:
                raise PermissionError()

            def index(path):
                current = store.get_identity_for_key(credential)
                if current != identity or current.role != 'admin':
                    raise PermissionError()
                return store.index_shared_article_metadata(Path(root), path)

            report = reconcile_articles(root, identity, index=index, apply=args.apply, limit=args.limit)
            print(json.dumps(report, sort_keys=True), file=stdout)
            allowed = {'complete'} if args.apply else {'complete', 'pending'}
            return 0 if all(row['status'] in allowed for row in report['records']) else 1
        except PermissionError:
            print('article maintenance denied', file=stderr)
            return 3
        except Exception:
            print('article maintenance unavailable', file=stderr)
            return 1
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    print('article maintenance cleanup unavailable', file=stderr)


if __name__ == '__main__':
    raise SystemExit(main())
