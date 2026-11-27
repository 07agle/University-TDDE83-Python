import pytest

from tree_ops import TreeNode, mirror, values_at_level
from filesystem import main, folder_contents, create_filesystem_example


@pytest.fixture
def filesystem_example() -> TreeNode:
    """Skapar ett djupt träd genom create_filesystem_example funktionen"""
    return create_filesystem_example()


@pytest.fixture
def simple_tree() -> TreeNode:
    """Ett enkelt träd med två nivåer."""
    return TreeNode(
        "root",
        [
            TreeNode("a", []),
            TreeNode("b", []),
        ],
    )

def test_folder_contents_right_order(simple_tree: TreeNode) -> None:
    """En mapp med innehåll ger namnen i rätt ordning."""
    assert folder_contents(simple_tree, "root") == ["a", "b"]


def test_empty_folder_return_empty_list(filesystem_example: TreeNode) -> None:
    """Den tomma mappen tmp ger [] - och alltså inte None"""
    assert folder_contents(filesystem_example, "tmp") == []
    assert folder_contents(filesystem_example, "tmp") is not None


def test_map_not_exist_returns_none(filesystem_example: TreeNode) -> None:
    """En mapp som inte finns ger None."""
    assert folder_contents(filesystem_example, "music") is None
