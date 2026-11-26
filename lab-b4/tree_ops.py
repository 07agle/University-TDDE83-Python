"""Operationer på trädstrukturer.

Varje nod är ett TreeNode-objekt med ett value och en lista children.
"""

from dataclasses import dataclass


@dataclass
class TreeNode:
    """En nod i ett träd: ett värde och en lista med barnnoder."""

    value: str | int
    children: list[TreeNode]


def values_at_level(tree: TreeNode, level: int) -> list[str | int]:
    """Returnerar värdena på en viss nivå i trädet.

    Roten ligger på nivå 1, dess barn på nivå 2, och så vidare.
    Värdena kommer i ordning från vänster till höger.

    Args:
        tree: Trädet att läsa ur
        level: Nivån att hämta värden från (minst 1)

    Returns:
        Lista med värdena på nivån, tom om trädet inte är så djupt

    Examples:
        >>> t = TreeNode("a", [TreeNode("b", [TreeNode("d", [])]), TreeNode("c", [])])
        >>> values_at_level(t, 1)
        ['a']
        >>> values_at_level(t, 2)
        ['b', 'c']
        >>> values_at_level(t, 3)
        ['d']
        >>> values_at_level(t, 4)
        []
    """

    tree_list = []
    if level == 1:
        return [tree.value]

    for child in tree.children:
        tree_list.extend(values_at_level(child, level -1))
    return tree_list


def mirror(tree: TreeNode) -> TreeNode:
    """Returnerar ett nytt träd som är spegelbilden av trädet.

    På varje nivå står barnen i omvänd ordning. Originalträdet ändras inte.

    Args:
        tree: Trädet att spegla

    Returns:
        Ett nytt träd med samma värden men omvänd barnordning överallt

    Examples:
        >>> t = TreeNode(
        ...     "a",
        ...     [TreeNode("b", [TreeNode("d", []), TreeNode("e", [])]), TreeNode("c", [])],
        ... )
        >>> m = mirror(t)
        >>> values_at_level(m, 2)
        ['c', 'b']
        >>> values_at_level(m, 3)
        ['e', 'd']
        >>> values_at_level(t, 2)
        ['b', 'c']
    """

    reversed_children_list = []

    if not tree.children:
        return TreeNode(tree.value, children=[])

    for child in reversed(tree.children):
        reversed_children_list.append(mirror(child))

    return TreeNode(tree.value, reversed_children_list)
