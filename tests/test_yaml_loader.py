from __future__ import annotations

import pytest
import yaml

from esmvaltool_language_server import yaml_loader


def test_parse_once() -> None:
    text = "base: &base {a: 1}\nother:\n  <<: *base\n"
    data = yaml_loader.load(text)
    root = yaml_loader.compose(text)
    assert data == {"base": {"a": 1}, "other": {"a": 1}}
    assert yaml_loader.load(text) is data
    assert yaml_loader.compose(text) is root
    # Building the data did not merge the keys in the nodes.
    assert isinstance(root, yaml.MappingNode)
    [(key, _)] = root.value[1][1].value
    assert key.value == "<<"


def test_parse_empty() -> None:
    assert yaml_loader.load("") is None
    assert yaml_loader.compose("") is None


def test_parse_invalid() -> None:
    for _ in range(2):
        with pytest.raises(yaml.MarkedYAMLError) as info:
            yaml_loader.load("a: [\n")
        # The traceback of the remembered error does not grow.
        assert len(list(info.traceback)) < 5
    with pytest.raises(yaml.YAMLError):
        yaml_loader.compose("a: [\n")
