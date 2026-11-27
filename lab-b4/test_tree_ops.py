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


def test_only_root(single_node: TreeNode) -> None:
    """Ett träd med bara roten har ett värde på nivå 1 och inga på nivå 2."""
    assert values_at_level(single_node, 1) == [single_node.value]
    assert values_at_level(single_node, 2) == []


def test_deep_tree_level3_left_to_right(deep_tree: TreeNode) -> None:
    """Nivå 3 i deep_tree samlar värden från båda grenarna, i ordning från vänster till höger."""

    assert values_at_level(deep_tree, 3) == ["aa", "ab", "ba"]


def test_deep_tree_deepest(deep_tree: TreeNode) -> None:
    """Den djupaste nivån i deep_tree ger rätt värde, och nivån under den ger []."""
    assert values_at_level(deep_tree, 4) == ["baa"]
    assert values_at_level(deep_tree, 5) == []


def test_deep_tree_mirror(deep_tree: TreeNode) -> None:
    """Efter spegling står barnen i omvänd ordning på nivå 2 och nivå 3."""
    assert values_at_level(mirror(deep_tree), 2) == ["b", "a"]
    assert values_at_level(mirror(deep_tree), 3) == ["ba", "ab", "aa"]


def test_deep_tree_original_not_changed(deep_tree: TreeNode) -> None:
    """Originalträdet ser likadant ut efter anropet som innan."""

    new_tree = TreeNode(
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

    mirror(new_tree)
    for i in range(1, 5):
        assert values_at_level(deep_tree, i) == values_at_level(new_tree, i)


def test_simple_tree_double_mirror(simple_tree: TreeNode) -> None:
    """Två speglingar efter varandra ger ett träd som är lika med (==) originalet"""
    new_tree = TreeNode(
        "root",
        [
            TreeNode("child1", []),
            TreeNode("child2", []),
        ],
    )

    assert mirror(mirror(simple_tree)) == new_tree
