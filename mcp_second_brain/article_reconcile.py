"""Bounded, explicit reconciliation of managed article metadata index reservations.

This only repairs existing canonical Markdown, never reconstructs lost content or
replays downloads. Caller supplies a freshly authenticated administrator identity
and bounded metadata callback. No implicit production maintenance is scheduled.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from .article_attribution import _atomic_write, _commit_lock, validate_shared_destination
from .identity import Identity, set_identity, _current
from .note_row import parse_frontmatter
from .vault_paths import resolve_in_vault


def reconcile_articles(vault, identity: Identity, *, index, apply=False, limit=20):
    if identity.role != 'admin':
        raise PermissionError('article reconciliation requires administrator')
    UUID(identity.user_uuid or '')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('reconciliation limit must be 1..100')
    root = Path(vault).resolve()
    registry_path = resolve_in_vault(root, '.article-identities.json', must_exist=False)
    if registry_path != root / '.article-identities.json':
        raise ValueError('article registry must not be a symlink')
    if not registry_path.exists():
        return {'applied': apply, 'records': []}
    with _commit_lock(root, 1):
        if registry_path.stat().st_size > 10*1024*1024:
            raise ValueError('article registry requires offline maintenance')
        registry = json.loads(registry_path.read_text())
        if not isinstance(registry, dict) or not all(
            isinstance(key, str) and isinstance(value, dict)
            and isinstance(value.get('path'), str)
            and value.get('index_status') in {'pending', 'complete'}
            for key, value in registry.items()
        ):
            raise ValueError('invalid article registry')
        paths = sorted({value['path'] for value in registry.values() if value['index_status']=='pending'})[:limit]
        results = []
        for rel in paths:
            validate_shared_destination(rel)
            path = resolve_in_vault(root, rel, must_exist=False)
            validate_shared_destination(path.relative_to(root).as_posix())
            state = 'missing_document'
            if path.is_file():
                if path.stat().st_size > 12*1024*1024:
                    state = 'invalid_document'
                else:
                    metadata = parse_frontmatter(path.read_text())
                    key = metadata.get('article_identity')
                    if (metadata.get('owner_id') not in (None, '', 'null')
                        or registry.get(key, {}).get('path') != rel):
                        state = 'invalid_document'
                    else:
                        state = 'pending'
                        if apply:
                            token = set_identity(identity)
                            try:
                                if index(path) is True:
                                    state = 'complete'
                            except Exception:
                                state = 'pending'
                            finally:
                                _current.reset(token)
                        if state == 'complete':
                            for value in registry.values():
                                if value['path'] == rel:
                                    value['index_status'] = 'complete'
                            _atomic_write(registry_path, json.dumps(registry, sort_keys=True))
            results.append({'path': rel, 'status': state})
        return {'applied': apply, 'records': results}
