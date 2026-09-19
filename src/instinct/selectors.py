"""Every web selector Instinct depends on, in one place.

Each entry is an ordered list of Playwright selector strings; the first that
matches a visible element wins. Role/ARIA-based selectors come first because
they survive restyling; CSS fallbacks follow. When claude.ai changes its
markup, update this file and run `uv run instinct check-web` to see which
candidates match on the live page.
"""

CLAUDE_WEB = {
    "new_chat_url": "https://claude.ai/new",
    "login_url_markers": ["/login", "/logout", "/oauth"],
    "composer": [
        "role=textbox[name=/(write|type|talk|message|reply|prompt|chat|ask)/i]",
        'div[contenteditable="true"][role="textbox"]',
        'div.ProseMirror[contenteditable="true"]',
        'fieldset [contenteditable="true"]',
        "textarea",
    ],
    "send_button": [
        "role=button[name=/^send( message)?$/i]",
        'button[aria-label*="send" i]',
    ],
    "stop_button": [
        "role=button[name=/stop( response| generating)?/i]",
        'button[aria-label*="stop" i]',
    ],
    # Containers of Claude's replies, in page order.
    "assistant_message": [
        "[data-is-streaming]",
        '[data-testid="assistant-message"]',
        "div.font-claude-response",
        "div.font-claude-message",
    ],
    "streaming_marker": ['[data-is-streaming="true"]'],
}

# Canvas fallback uses cookie-authenticated API calls from the profile, so it
# needs no selectors. Listed here only for the login check.
CANVAS_WEB = {
    "login_url_markers": ["/login", "/saml", "/cas", "/sso"],
}
