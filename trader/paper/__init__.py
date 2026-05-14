"""trader/paper — paper-trading sim.

execute.py — open / close positions (Phase 3 ships open_position; Phase 5
              adds close_position + partial closes for slow-path supersession).
mtm.py     — periodic mark-to-market job (Phase 5).
settle.py  — close at horizon, compute raw + alpha return (Phase 5).
"""
