"""Permission + setup checker. Prints exact fix instructions for anything missing.

macOS privacy permissions (TCC) are granted to the *responsible process*: the
terminal app when you run this from a shell, or Claude Desktop / Cursor / etc.
when they spawn the MCP server. Grant them to whichever app launches
`instinct-mcp`, and re-run doctor from that same context if you can.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from instinct.config import Config, canvas_token, load_config

PRIVACY_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_"


@dataclass
class Check:
    name: str
    ok: bool | None  # None = skipped / not applicable
    detail: str
    fix: str = ""


def check_full_disk_access(cfg: Config) -> Check:
    db = cfg.messages.db_path
    fix = (
        "System Settings → Privacy & Security → Full Disk Access → enable the app that runs "
        "the server (Terminal/iTerm/Ghostty for `uv run`, or Claude/Cursor if they launch it). "
        f"Then fully quit and reopen that app.\n      open '{PRIVACY_PANE}AllFiles'"
    )
    try:
        with open(db, "rb") as f:
            f.read(16)
    except FileNotFoundError:
        return Check("Full Disk Access", False, f"{db} does not exist (is Messages set up?)",
                     "Open Messages.app once and sign in to iMessage.")
    except PermissionError:
        return Check("Full Disk Access", False, f"cannot open {db}", fix)
    return Check("Full Disk Access", True, f"can read {db}")


def check_accessibility() -> Check:
    fix = (
        "System Settings → Privacy & Security → Accessibility → add/enable the app that runs "
        f"the server (and CuaDriver if installed).\n      open '{PRIVACY_PANE}Accessibility'"
    )
    try:
        from ApplicationServices import AXIsProcessTrusted
    except ImportError:
        return Check("Accessibility", None, "pyobjc not installed", "uv sync")
    ok = bool(AXIsProcessTrusted())
    return Check("Accessibility", ok, "trusted" if ok else "this process is not trusted", "" if ok else fix)


def check_screen_recording() -> Check:
    fix = (
        "System Settings → Privacy & Security → Screen & System Audio Recording → enable the "
        f"app that runs the server (and CuaDriver).\n      open '{PRIVACY_PANE}ScreenCapture'"
    )
    try:
        from Quartz import CGPreflightScreenCaptureAccess
    except ImportError:
        return Check("Screen Recording", None, "pyobjc not installed", "uv sync")
    ok = bool(CGPreflightScreenCaptureAccess())
    return Check("Screen Recording", ok, "granted" if ok else "not granted", "" if ok else fix)


def check_contacts() -> Check:
    try:
        import Contacts
    except ImportError:
        return Check("Contacts", None, "pyobjc not installed", "uv sync")
    status = Contacts.CNContactStore.authorizationStatusForEntityType_(Contacts.CNEntityTypeContacts)
    names = {0: "not determined", 1: "restricted", 2: "denied", 3: "authorized", 4: "limited"}
    label = names.get(int(status), str(status))
    if int(status) in (3, 4):
        return Check("Contacts", True, label)
    fix = (
        "Optional (names instead of raw phone numbers). Run `uv run instinct contacts-auth` "
        "to trigger the prompt, or System Settings → Privacy & Security → Contacts.\n"
        f"      open '{PRIVACY_PANE}Contacts'"
    )
    return Check("Contacts", False, label, fix)


def check_canvas(cfg: Config) -> Check:
    if cfg.canvas.backend == "browser":
        return Check("Canvas token", None, "backend=browser; token not used")
    if cfg.canvas.backend == "auto" and not canvas_token():
        return Check("Canvas token", None, "no token; backend=auto will use the background browser profile")
    if not cfg.canvas.base_url:
        return Check("Canvas token", False, "no [canvas].base_url configured",
                     "Set base_url in ~/.instinct/config.toml (see config.example.toml).")
    token = canvas_token()
    fix = (
        f"Create a token at {cfg.canvas.base_url or '<canvas>'}/profile/settings → "
        "'+ New Access Token', then run `uv run instinct set-canvas-token` (stores it in the "
        "Keychain) or export INSTINCT_CANVAS_TOKEN."
    )
    if not token:
        return Check("Canvas token", False, "no token in env or Keychain", fix)
    import httpx

    try:
        r = httpx.get(f"{cfg.canvas.base_url}/api/v1/users/self",
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except httpx.HTTPError as e:
        return Check("Canvas token", False, f"request failed: {type(e).__name__}", "Check network / base_url.")
    if r.status_code == 200:
        return Check("Canvas token", True, f"valid (user: {r.json().get('name', '?')})")
    return Check("Canvas token", False, f"HTTP {r.status_code} from /users/self", fix)


def check_cua_driver(cfg: Config) -> Check:
    from instinct.adapters import gui

    return gui.doctor_check(cfg)


def _profile_has_cookie(profile: Path, host_suffix: str) -> bool:
    # Chrome ≥96 keeps cookies under Default/Network/; older builds used Default/.
    candidates = [profile / "Default" / "Network" / "Cookies", profile / "Default" / "Cookies"]
    cookies = next((c for c in candidates if c.exists()), None)
    if cookies is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "Cookies"
        shutil.copy2(cookies, dst)
        for side in ("-wal", "-journal"):
            if (cookies.parent / f"Cookies{side}").exists():
                shutil.copy2(cookies.parent / f"Cookies{side}", Path(tmp) / f"Cookies{side}")
        con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key = ? OR host_key LIKE ?",
                (host_suffix, f"%.{host_suffix}"),
            ).fetchone()
        finally:
            con.close()
    return bool(row and row[0])


def check_chrome_profile(cfg: Config) -> list[Check]:
    b = cfg.browser
    out = []
    if not Path(b.chrome_path).exists():
        out.append(Check("Chrome binary", False, f"{b.chrome_path} not found",
                         "Install Google Chrome or set [browser].chrome_path."))
    else:
        out.append(Check("Chrome binary", True, b.chrome_path))
    if not b.profile_dir.exists():
        out.append(Check("Chrome profile", False, f"{b.profile_dir} missing",
                         "Run `uv run instinct login claude` (and `... login canvas`) once."))
        return out
    out.append(Check("Chrome profile", True, str(b.profile_dir)))
    sites = {"claude": "claude.ai"}
    if cfg.canvas.base_url:
        sites["canvas"] = urlparse(cfg.canvas.base_url).hostname or ""
    for site, host in sites.items():
        try:
            ok = _profile_has_cookie(b.profile_dir, host)
        except sqlite3.Error as e:
            out.append(Check(f"Profile login: {site}", None, f"could not read cookies ({e})"))
            continue
        out.append(Check(f"Profile login: {site}", ok,
                         f"cookies for {host} present" if ok else f"no cookies for {host}",
                         "" if ok else f"Run `uv run instinct login {site}` and sign in once."))
    return out


def run_checks(cfg: Config) -> list[Check]:
    checks = [
        check_full_disk_access(cfg),
        check_accessibility(),
        check_screen_recording(),
        check_contacts(),
        check_canvas(cfg),
        check_cua_driver(cfg),
    ]
    checks.extend(check_chrome_profile(cfg))
    return checks


def main() -> int:
    cfg = load_config()
    print(f"instinct doctor (config: {cfg.source or 'defaults, no config file'})\n")
    failed = 0
    for c in run_checks(cfg):
        mark = {True: "✅", False: "❌", None: "➖"}[c.ok]
        print(f"{mark} {c.name}: {c.detail}")
        if c.ok is False:
            failed += 1
            if c.fix:
                print(f"    fix: {c.fix}")
    print(f"\n{failed} problem(s)." if failed else "\nAll checks passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
