"""Generate an original demonstration EPUB without downloading a commercial book."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.epub_factory import write_epub  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("destination", type=Path)
parser.add_argument("--version", choices=["2.0", "3.0"], default="3.0")
args = parser.parse_args()
if args.destination.exists():
    parser.error("Destination already exists; choose a new filename.")
args.destination.parent.mkdir(parents=True, exist_ok=True)
write_epub(args.destination, version=args.version)
print(args.destination)
