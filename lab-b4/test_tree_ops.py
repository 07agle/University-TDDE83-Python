"""Enhetstester för trädoperationer.

Kör med: uv run pytest test_tree_ops.py -v
"""

import pytest

from tree_ops import TreeNode, mirror, values_at_level

# Testdata: Olika träd att använda i tester


@pytest.fixture
def single_node() -> TreeNode:
    """Ett träd med endast en nod."""
    return TreeNode("root", [])


@pytest.fixture
def simple_tree() -> TreeNode:
    """Ett enkelt träd med två nivåer."""
    return TreeNode(
        "root",
        [
            TreeNode("child1", []),
            TreeNode("child2", []),
        ],
    )


@pytest.fixture
def deep_tree() -> TreeNode:
    """Ett djupare träd med flera nivåer."""
    return TreeNode(
        "root",
        [
            TreeNode(
                "a",
                [
                    TreeNode("aa", []),
                    TreeNode("ab", []),
                ],
            ),
            TreeNode(
                "b",
                [
                    TreeNode(
                        "ba",
                        [
                            TreeNode("baa", []),
                        ],
                    ),
                ],
            ),
        ],
    )


def test_level_root(deep_tree: TreeNode) -> None:
    """Test: Nivå 1 är bara roten."""
    assert values_at_level(deep_tree, 1) == ["root"]


def test_mirror_single_node(single_node: TreeNode) -> None:
    """Test: Ett löv speglas till ett nytt, likadant löv."""
    result = mirror(single_node)
    assert result == TreeNode("root", [])
    assert result is not single_node
