"""Verklig användning av trädoperationer: filsystemträd.

Denna modul representerar ett filsystem som ett träd och svarar på
frågor om vad mapparna innehåller.
"""

from tree_ops import TreeNode, values_at_level


def create_filesystem_example() -> TreeNode:
    """Skapar filsystemträdet i uppgiftsbeskrivningen.

    Returns:
        Rotnoden (home) i filsystemträdet
    """
    ...


def folder_contents(fs: TreeNode, folder: str) -> list[str | int] | None:
    """Returnerar namnen på det som ligger direkt i en mapp.

    folder ska vara namnet på en mapp. Namnen i filsystemet antas
    vara unika.

    Args:
        fs: Filsystemträdet
        folder: Namnet på mappen

    Returns:
        Lista med namnen i mappen, [] om mappen är tom,
        eller None om mappen inte finns

    Examples:
        >>> fs = create_filesystem_example()
        >>> folder_contents(fs, "documents")
        ['report.pdf', 'notes.txt']
        >>> folder_contents(fs, "tmp")
        []
        >>> folder_contents(fs, "music") is None
        True
    """
    ...


def main() -> None:
    """Skriver ut innehållet i några mappar och namnen på nivå 3."""
    ...


if __name__ == "__main__":
    main()
