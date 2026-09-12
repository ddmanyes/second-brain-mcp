"""Trusted adapter for the optional managed intake queue; no activation at import."""

from __future__ import annotations

import re
import uuid
from .identity import Identity, get_current_identity


class IntakeIdentityBridge:
    """Recheck the submitted credential on every admission and worker checkpoint.

    ``lookup`` accepts an opaque SHA-256 credential reference, not a raw key.
    The caller wires the store belonging to this service, never a caller DSN.
    """

    def __init__(self, lookup):
        self.lookup = lookup

    def verify(self, actor) -> bool:
        return self.resolve(actor) is not None

    def resolve(self, actor) -> Identity | None:
        try:
            if str(uuid.UUID(actor.user_id)) != actor.user_id:
                return None
            if not re.fullmatch(r"[0-9a-f]{64}", actor.credential_id):
                return None
            current = self.lookup(actor.credential_id)
            valid = (
                isinstance(current, Identity)
                and current.user_uuid == actor.user_id
                and current.role in {"member", "writer", "admin"}
                and (not actor.admin or current.is_admin())
            )
            return current if valid else None
        except Exception:
            return None

    def actor(self):
        from lcdda_ingest.queue import QueueActor, QueueDenied

        identity = get_current_identity()
        if identity is None or not identity.user_uuid or not identity.credential_id:
            raise QueueDenied("actor is not authorized")
        actor = QueueActor(
            identity.user_uuid, identity.credential_id, identity.is_admin()
        )
        if not self.verify(actor):
            raise QueueDenied("actor is not authorized")
        return actor
