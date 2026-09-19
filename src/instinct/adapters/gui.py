"""Native GUI lane via Cua Driver (last resort). Implemented in build step 7."""

from __future__ import annotations


def doctor_check(cfg):
    from instinct.doctor import Check

    return Check("Cua Driver", None, "GUI lane not integrated yet (build step 7)")
