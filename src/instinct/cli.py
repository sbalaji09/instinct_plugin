"""`instinct` command: setup helpers. The MCP server itself is `instinct-mcp`."""

from __future__ import annotations

import argparse
import subprocess
import sys

from instinct.config import KEYCHAIN_CANVAS_ACCOUNT, KEYCHAIN_SERVICE, load_config


def cmd_set_canvas_token(_args) -> int:
    # `-w` as the final option makes `security` prompt for the secret itself, so the
    # token never appears in argv / shell history / ps output.
    print("Paste your Canvas access token when prompted (input is hidden).")
    return subprocess.call([
        "security", "add-generic-password", "-U",
        "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_CANVAS_ACCOUNT,
        "-l", "Instinct Canvas token", "-w",
    ])


def cmd_contacts_auth(_args) -> int:
    import threading

    import Contacts

    done = threading.Event()
    result = {}

    def handler(granted, error):
        result["granted"] = bool(granted)
        done.set()

    Contacts.CNContactStore.alloc().init().requestAccessForEntityType_completionHandler_(
        Contacts.CNEntityTypeContacts, handler
    )
    done.wait(120)
    print("Contacts access granted." if result.get("granted") else "Contacts access NOT granted.")
    return 0 if result.get("granted") else 1


def cmd_login(args) -> int:
    from instinct.adapters.browser import interactive_login

    return interactive_login(load_config(), args.site)


def cmd_check_web(_args) -> int:
    import json

    from instinct.adapters.browser import get_lane

    lane = get_lane(load_config())
    try:
        print(json.dumps(lane.check_selectors(), indent=2))
    finally:
        lane.stop()
    return 0


def cmd_doctor(_args) -> int:
    from instinct.doctor import main

    return main()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="instinct")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check permissions and setup").set_defaults(fn=cmd_doctor)
    sub.add_parser("set-canvas-token", help="store the Canvas token in the Keychain").set_defaults(
        fn=cmd_set_canvas_token)
    sub.add_parser("contacts-auth", help="request Contacts permission").set_defaults(fn=cmd_contacts_auth)
    sub.add_parser("check-web", help="show which claude.ai selectors match (after login)").set_defaults(
        fn=cmd_check_web)
    lp = sub.add_parser("login", help="open the background profile visibly once to sign in")
    lp.add_argument("site", help="claude, canvas, or a URL")
    lp.set_defaults(fn=cmd_login)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
