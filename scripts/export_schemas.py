"""Export stable, language-neutral schemas, or fail if committed output drifted."""

import argparse
import json
from pathlib import Path

from jrtc_signalling.schema_definitions import SCHEMAS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "src/jrtc_signalling/schemas/v1"
    target.mkdir(parents=True, exist_ok=True)
    mismatches = []
    for name, schema in SCHEMAS.items():
        path = target / f"{name}.schema.json"
        content = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != content:
                mismatches.append(path.name)
        else:
            path.write_text(content)
    if mismatches:
        raise SystemExit("Schema exports differ: " + ", ".join(mismatches))
    print(f"{len(SCHEMAS)} schemas {'verified' if args.check else 'exported'}")


if __name__ == "__main__":
    main()
