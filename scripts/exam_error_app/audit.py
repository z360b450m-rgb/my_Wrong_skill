"""Infrastructure-neutral hash-chain service."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from .contracts import JsonObject


@dataclass(frozen=True)
class AuditChainService:
    hash_value: Callable[[Any], str]
    safe_actor_id: Callable[[Any, str], str]
    clock: Callable[[], str]
    event_id_factory: Callable[[], str]

    def state_hash(self, data: JsonObject) -> str:
        """Hash the current document state without the self-referential audit log."""
        state = {key: value for key, value in data.items() if key != "audit_log"}
        return self.hash_value(state)

    def append(
        self,
        data: JsonObject,
        event_type: str,
        payload: Any,
        *,
        actor_ref: str = "system",
        timestamp: str | None = None,
    ) -> JsonObject:
        events = data.setdefault("audit_log", [])
        previous = events[-1]["event_hash"] if events else None
        event = {
            "event_id": self.event_id_factory(),
            "timestamp": timestamp or self.clock(),
            "event_type": event_type,
            "actor_ref": self.safe_actor_id(actor_ref, "system"),
            "payload_hash": self.hash_value(payload),
            "previous_hash": previous,
            "state_hash": self.state_hash(data),
        }
        event["event_hash"] = self.hash_value(event)
        events.append(event)
        return event

    def recompute(
        self,
        data: JsonObject,
        *,
        actor_ref: str = "system",
        timestamp: str | None = None,
    ) -> JsonObject:
        """Rebuild chain links and bind the repaired chain to the current state.

        Recalculation is an explicit administrative operation. Existing payload hashes and
        event metadata are preserved, while chain links and event hashes are rebuilt. A new
        terminal event records that the chain was recalculated.
        """
        result = copy.deepcopy(data)
        events = result.setdefault("audit_log", [])
        if not isinstance(events, list):
            raise ValueError("audit_log must be a list")
        previous = None
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise ValueError(f"audit_log[{index}] must be an object")
            event.pop("state_hash", None)
            event["previous_hash"] = previous
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            event["event_hash"] = self.hash_value(unsigned)
            previous = event["event_hash"]
        self.append(
            result,
            "audit.recomputed",
            {"previous_head": previous, "event_count": len(events)},
            actor_ref=actor_ref,
            timestamp=timestamp,
        )
        return result

    def verify(self, data: JsonObject) -> list[str]:
        errors: list[str] = []
        previous = None
        events = data.get("audit_log", [])
        if not isinstance(events, list):
            return ["audit_log: must be a list"]
        if not events:
            return ["audit_log: must contain at least one event"]
        for index, event in enumerate(events):
            location = f"audit_log[{index}]"
            if not isinstance(event, dict):
                errors.append(f"{location}: must be an object")
                continue
            if event.get("previous_hash") != previous:
                errors.append(f"{location}: previous_hash mismatch")
            stored = event.get("event_hash")
            unsigned = {key: value for key, value in event.items() if key != "event_hash"}
            if stored != self.hash_value(unsigned):
                errors.append(f"{location}: event_hash mismatch")
            previous = stored
        final_state_hash = events[-1].get("state_hash") if events and isinstance(events[-1], dict) else None
        if not final_state_hash:
            errors.append("audit_log: final event is not bound to document state")
        elif final_state_hash != self.state_hash(data):
            errors.append("audit_log: document state hash mismatch")
        return errors
