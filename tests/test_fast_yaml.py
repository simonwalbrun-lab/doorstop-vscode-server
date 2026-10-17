"""libyaml for Doorstop's YAML (doorstop_tree.use_libyaml): same data, byte-identical files.

Real Doorstop items from the regression fixture plus one with unicode and a
multi-line text, each loaded and dumped with PyYAML's Python and C code.
"""

import shutil
from pathlib import Path

import doorstop
import pytest
import yaml
from doorstop import common

from doorstop_server.doorstop_tree import use_libyaml

REGRESSION = Path(__file__).resolve().parents[2] / "testdata" / "regression"

pytestmark = pytest.mark.skipif(not yaml.__with_libyaml__, reason="PyYAML built without libyaml")


@pytest.fixture
def items(tmp_path):
    root = tmp_path / "regression"
    shutil.copytree(REGRESSION, root)
    use_libyaml()
    tree = doorstop.build(cwd=str(root), root=str(root), request_next_number=None)
    extra = tree.find_document("REQ").add_item()
    extra.text = "Ünïcödé — a long line " + "x" * 120 + "\n\nand a second paragraph"
    return [item for document in tree for item in document if item.itemformat == "yaml"]


def test_doorstop_reads_with_the_c_loader():
    use_libyaml()
    assert common.load_yaml.__defaults__ == (yaml.CSafeLoader,)


def test_c_loader_reads_the_same_data(items):
    for item in items:
        text = Path(item.path).read_text(encoding="utf-8")
        assert yaml.load(text, Loader=yaml.CSafeLoader) == yaml.load(text, Loader=yaml.SafeLoader), item.path


def test_c_dumper_writes_byte_identical_files(items):
    for item in items:
        data, _ = item._yaml_data()  # pylint: disable=protected-access
        python = yaml.dump(data, Dumper=yaml.Dumper, default_flow_style=False, allow_unicode=True)
        assert item._dump(data) == python, item.path  # pylint: disable=protected-access
