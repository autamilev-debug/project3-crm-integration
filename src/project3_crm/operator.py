"""Non-public commands for deliberate operator recovery actions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence

from sqlalchemy import Engine, select, update

from project3_crm.db.database import get_database_engine
from project3_crm.db.schema import integration_runtime_state


class OperatorStateError(RuntimeError):
    """Required durable operator state is missing or inconsistent."""


@dataclass(frozen=True)
class ClearCrmPauseResult:
    was_paused: bool
    previous_reason: str | None


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Operator timestamps must be timezone-aware")
    return value.astimezone(UTC)


def clear_crm_pause(
    engine: Engine,
    *,
    now: datetime | None = None,
) -> ClearCrmPauseResult:
    """Clear one existing CRM pause without changing any lead state."""

    current_time = _require_aware_utc(now or datetime.now(UTC))
    with engine.begin() as connection:
        runtime = connection.execute(
            select(
                integration_runtime_state.c.crm_delivery_paused,
                integration_runtime_state.c.pause_reason,
            )
            .where(integration_runtime_state.c.id == 1)
            .with_for_update()
        ).mappings().one_or_none()
        if runtime is None:
            raise OperatorStateError(
                "integration runtime singleton id=1 is missing"
            )

        paused = bool(runtime["crm_delivery_paused"])
        reason = runtime["pause_reason"]
        if not paused:
            return ClearCrmPauseResult(was_paused=False, previous_reason=None)

        connection.execute(
            update(integration_runtime_state)
            .where(
                integration_runtime_state.c.id == 1,
                integration_runtime_state.c.crm_delivery_paused.is_(True),
            )
            .values(
                crm_delivery_paused=False,
                pause_reason=None,
                paused_at=None,
                pause_alert_sent_at=None,
                pause_alert_last_error=None,
                updated_at=current_time,
            )
        )
        return ClearCrmPauseResult(
            was_paused=True,
            previous_reason=str(reason),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m project3_crm.operator")
    parser.add_subparsers(dest="command", required=True).add_parser(
        "clear-crm-pause",
        help="clear a human-resolved global CRM pause",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one requested operator command."""

    arguments = _parser().parse_args(argv)
    if arguments.command != "clear-crm-pause":
        raise OperatorStateError("unsupported operator command")

    engine = get_database_engine()
    try:
        result = clear_crm_pause(engine)
    finally:
        engine.dispose()

    if result.was_paused:
        print(f"CRM pause cleared; previous reason: {result.previous_reason}")
    else:
        print("CRM delivery is not paused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
