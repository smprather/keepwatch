"""A stand-in for ssh in tests: drops ssh's options and the host, then runs the remote command here.

Environment: FAKE_SSH_LOG (append {"options", "host", "command"} as one JSON line), FAKE_SSH_FAIL (fail like
an unreachable host), FAKE_SSH_BANNER (print this on stdout first, like a chatty login script).
"""

import json
import os
import shlex
import subprocess
import sys

VALUE_OPTIONS = set("BbcDEeFIiJLlmOoPpQRSWw")


def split(argv):
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        index += 2 if len(arg) == 2 and arg[1] in VALUE_OPTIONS else 1
    return argv[:index], argv[index], " ".join(argv[index + 1 :])


def main():
    options, host, command = split(sys.argv[1:])
    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"options": options, "host": host, "command": command}) + "\n")
    if os.environ.get("FAKE_SSH_FAIL"):
        sys.stderr.write(f"ssh: connect to host {host} port 22: Connection refused\n")
        return 255
    banner = os.environ.get("FAKE_SSH_BANNER")
    if banner:
        print(banner, flush=True)
    return subprocess.call(shlex.split(command))


if __name__ == "__main__":
    sys.exit(main())
