"""Quick-entry YAML quoting (spec 037) checked against the server's YAML loader.

The extension writes picked values into item files (src/quickEntry.ts,
``yamlValue``). yaml_quoting.json holds value/quoted pairs; the extension test
(src/test/quickEntry.test.ts) checks ``yamlValue(value) == quoted``, this test
checks that each ``quoted`` loads back as exactly ``value``.
"""

import json
from pathlib import Path

import pytest
import yaml

PAIRS = json.loads((Path(__file__).parent / "yaml_quoting.json").read_text(encoding="utf-8"))
# Doorstop reads items with libyaml where available (doorstop_tree.use_libyaml).
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


# Spec 037 FR-005
@pytest.mark.parametrize("pair", PAIRS, ids=[repr(pair["value"]) for pair in PAIRS])
def test_quoted_value_loads_as_the_same_text(pair):
    """A quoted (or deliberately unquoted) value is read back as that exact text."""
    loaded = yaml.load("k: " + pair["quoted"], Loader=LOADER)["k"]
    assert isinstance(loaded, str)
    assert loaded == pair["value"]
