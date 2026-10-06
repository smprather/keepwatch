"""SSH_ASKPASS helper: `python askpass.py VAR [prompt]` prints $VAR for ssh/scp. Stdlib only.

keepwatch points SSH_ASKPASS at a small launcher that runs this file with the *name* of the environment
variable holding the password, so the secret is never written to a file, passed as an argument or quoted
for a shell. The file rather than `-m keepwatch.askpass`: the launcher's interpreter (for example the
console python beside a Windows pythonw.exe) is not always one that can import keepwatch, and this file
needs nothing but the standard library.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: python askpass.py VAR [prompt]\n")
        return 2
    name, prompt = argv[0], " ".join(argv[1:])
    if "yes/no" in prompt:
        sys.stderr.write(f"keepwatch askpass: not answering {prompt.strip()!r}: host keys must already be known\n")
        return 1
    value = os.environ.get(name)
    if value is None:
        sys.stderr.write(f"keepwatch askpass: the environment variable {name} is not set\n")
        return 1
    os.write(1, (value + "\n").encode("utf-8"))  # fd 1 also works under pythonw, where sys.stdout is None
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
