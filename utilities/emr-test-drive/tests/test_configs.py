#!/usr/bin/env python3
"""Every shipped config must parse.

Added because two example configs were published with `workloads` written as a
mapping when the loader expects a list, so both would have crashed on first use.
Nothing validated them, so nothing caught it.

Template placeholders are substituted first: the loader deliberately refuses a
config that still contains them, which is correct behaviour and would otherwise
mask real schema errors.
"""
from __future__ import annotations

import glob
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from etd.spec import ConfigError, load_spec  # noqa: E402

SUBS = {"111122223333": "444455556666", "my-etd-bucket": "real-bucket-name"}


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    targets = sorted(glob.glob(str(root / "examples/configs/*.yaml")))
    targets.append(str(root / "config.template.yaml"))
    failed = 0
    for cfg in targets:
        text = pathlib.Path(cfg).read_text()
        for old, new in SUBS.items():
            text = text.replace(old, new)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(text)
            tmp = fh.name
        rel = pathlib.Path(cfg).relative_to(root)
        try:
            spec = load_spec(tmp)
            wl = [(w.workload_id, w.kind) for w in spec.workloads]
            print(f"  ok   {rel}  variants={len(spec.variants)} workloads={wl}")
        except ConfigError as exc:
            failed += 1
            print(f"  FAIL {rel}")
            for line in str(exc).splitlines():
                print(f"         {line}")
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)
    print(f"\n{len(targets) - failed} valid, {failed} invalid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
