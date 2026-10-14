"""Stage 1 — Target definition (spec 3).

Rule: only flyers STARTING this week are extracted.
  Included if valid_from is: today, tomorrow, Wednesday, or Thursday of run week.
  Duration does not matter.

Carry-over exception: flyer started last week Mon/Tue/Wed AND covers the
WHOLE run week (valid_to >= Sunday) is KEPT, flagged CHECK WITH ELLIE.
If a store has both a carry-over and a new flyer, BOTH are taken.
"""
from datetime import date, timedelta

from models import Flyer, TargetSelection


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def select_target_flyers(
    flyers: list[Flyer], run_date: date, region: str
) -> list[TargetSelection]:
    mon = _monday_of(run_date)
    sun = mon + timedelta(days=6)
    wed = mon + timedelta(days=2)
    thu = mon + timedelta(days=3)
    today = run_date
    tomorrow = run_date + timedelta(days=1)

    allowed_starts = {today, tomorrow, wed, thu}

    last_mon = mon - timedelta(days=7)
    carryover_starts = {last_mon, last_mon + timedelta(days=1), last_mon + timedelta(days=2)}

    selected: list[TargetSelection] = []
    for f in flyers:
        if f.region != region:
            continue
        # Normal this-week rule
        if f.valid_from in allowed_starts:
            selected.append(
                TargetSelection(
                    flyer=f, is_carryover=False, reason=f"starts-this-week:{f.valid_from}"
                )
            )
            continue
        # Carry-over exception
        if f.valid_from in carryover_starts and f.valid_to >= sun:
            selected.append(
                TargetSelection(
                    flyer=f,
                    is_carryover=True,
                    reason=f"carry-over CHECK WITH ELLIE: {f.valid_from}->{f.valid_to} covers week {mon}->{sun}",
                )
            )
            continue
        # Otherwise excluded (last week's stale flyer, other region already skipped)
    return selected
