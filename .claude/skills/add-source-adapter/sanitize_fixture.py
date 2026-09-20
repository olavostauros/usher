#!/usr/bin/env python3
"""Strip personal data and third-party scripts from a recorded HTML fixture, in place.

Usage: sanitize_fixture.py FILE [FILE...]

Keeps the scripts that carry page data (__NEXT_DATA__, self.__next_f flight chunks,
JSON-LD); drops external <script src> tags and inline analytics; replaces every
e-mail address with redacted@example.org (AGENTS.md §6 step 6, §10).
"""

import re
import sys
from pathlib import Path

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SCRIPT = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)
KEEP_MARKERS = ("__NEXT_DATA__", "__next_f", "application/ld+json", "__NUXT")


def sanitize(text: str) -> tuple[str, int, int]:
    dropped = 0

    def keep_or_drop(m: re.Match[str]) -> str:
        nonlocal dropped
        block = m.group(0)
        if any(k in block for k in KEEP_MARKERS):
            return block
        dropped += 1
        return ""

    text = SCRIPT.sub(keep_or_drop, text)
    text, emails = EMAIL.subn("redacted@example.org", text)
    return text, dropped, emails


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    for name in argv:
        p = Path(name)
        out, dropped, emails = sanitize(p.read_text(encoding="utf-8"))
        p.write_text(out, encoding="utf-8")
        print(
            f"{p}: dropped {dropped} scripts, redacted {emails} e-mails, {p.stat().st_size} bytes"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
