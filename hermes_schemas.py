#!/usr/bin/env python3
"""Dump the tool schemas a Hermes profile sends on every model call, as JSON.

Run it with Hermes' own interpreter from the hermes-agent checkout, so the list is exactly
what the model receives (tool_search collapsing included):

    cd ~/.hermes/hermes-agent && HERMES_HOME=~/.hermes/profiles/<profile> \\
        venv/bin/python /path/to/hermes_schemas.py --toolsets hermes-cli --disable tts > schemas.json
"""
import argparse
import contextlib
import io
import json
import sys

p = argparse.ArgumentParser()
p.add_argument("--toolsets", nargs="+", default=["hermes-cli"])
p.add_argument("--disable", nargs="*", default=[])
a = p.parse_args()

sys.path.insert(0, ".")
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import model_tools
    defs = model_tools.get_tool_definitions(enabled_toolsets=a.toolsets, disabled_toolsets=a.disable or None,
                                            quiet_mode=True)
json.dump(defs, sys.stdout, ensure_ascii=False)
