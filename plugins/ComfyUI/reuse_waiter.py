"""Keep Deadline's task process alive until its queued prompt finishes."""
import json
import os
import sys
import time


def main(marker):
    while not os.path.exists(marker):
        time.sleep(1)
    with open(marker, encoding="utf-8") as handle:
        result = json.load(handle)
    os.remove(marker)
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
