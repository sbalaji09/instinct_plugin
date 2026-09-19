"""Native GUI lane via Cua Driver (last resort). Implemented in build step 7."""

from __future__ import annotations


def doctor_check(cfg):
    from instinct.doctor import Check

    return Check("Cua Driver", None, "GUI lane not integrated yet (build step 7)")


def find_driver(cfg):
    return None


def get_driver(cfg):
    raise RuntimeError("GUI lane not integrated yet (build step 7)")


def ask_claude_desktop(driver, prompt: str) -> dict:
    raise RuntimeError("GUI lane not integrated yet (build step 7)")
