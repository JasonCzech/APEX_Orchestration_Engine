"""Hardened YAML loading for documents an operator or a repository supplies.

PyYAML's ``SafeLoader`` refuses arbitrary Python objects but still expands
aliases (a small "billion laughs" document can allocate gigabytes), recurses
without bound and silently keeps the last of two duplicate keys. This loader
streams parser events first to reject aliases and cap depth and node count,
then constructs the tree with a loader that rejects duplicate and non-scalar
mapping keys. Errors name the document kind, never its content.
"""

from __future__ import annotations

from typing import Any

import yaml

from apex.domain.diagnostics import safe_type_name


class SafeYamlError(ValueError):
    """The document is not acceptable YAML; the message is safe to show."""


def load_yaml_safely(raw: str, *, subject: str, max_depth: int, max_nodes: int) -> Any:
    """Parse ``raw`` without aliases, beyond ``max_depth`` or past ``max_nodes`` nodes."""

    error_kind: str | None = None
    try:
        depth = 0
        nodes = 0
        for event in yaml.parse(raw, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.events.AliasEvent):
                raise SafeYamlError(f"{subject} YAML aliases are not allowed")
            if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
                depth += 1
                nodes += 1
                if depth > max_depth:
                    raise SafeYamlError(f"{subject} document exceeds maximum depth {max_depth}")
            elif isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
                depth -= 1
            elif isinstance(event, yaml.events.ScalarEvent):
                nodes += 1
            if nodes > max_nodes:
                raise SafeYamlError(f"{subject} document exceeds the {max_nodes}-node limit")

        class UniqueKeyLoader(yaml.SafeLoader):
            pass

        def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
            loader.flatten_mapping(node)
            result: dict[Any, Any] = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                invalid_key = False
                try:
                    duplicate = key in result
                except TypeError:
                    invalid_key = True
                    duplicate = False
                if invalid_key:
                    raise SafeYamlError(f"{subject} YAML mapping keys must be scalar")
                if duplicate:
                    raise SafeYamlError(f"{subject} document contains a duplicate key")
                result[key] = loader.construct_object(value_node, deep=deep)
            return result

        UniqueKeyLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
            construct_mapping,
        )
        return yaml.load(raw, Loader=UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except SafeYamlError:
        raise
    except (yaml.YAMLError, RecursionError) as exc:
        error_kind = safe_type_name(exc)
    raise SafeYamlError(f"invalid YAML {subject} document ({error_kind})")
