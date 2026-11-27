"""Verklig användning av trädoperationer: filsystemträd.

Denna modul representerar ett filsystem som ett träd och svarar på
frågor om vad mapparna innehåller.
"""

from tree_ops import TreeNode, values_at_level


def create_filesystem_example() -> TreeNode:
    """Skapar filsystemträdet i uppgiftsbeskrivningen.

    Returns:
        Rotnoden (home) i filsystemträdet

    home
    └── user
        ├── documents
        │   ├── report.pdf
        │   └── notes.txt
        ├── code
        │   ├── python
        │   │   └── script.py
        │   └── README.md
        └── tmp
    """

    return TreeNode(
        "home",
        [
            TreeNode(
                "user",
                [
                    TreeNode(
                        "documents",
                        [TreeNode("report.pdf", []), TreeNode("notes.txt", [])],
                    ),
                    TreeNode(
                        "code",
                        [
                            TreeNode("python", [TreeNode("script.py", [])]),
                            TreeNode("README.md", []),
                        ],
                    ),
                    TreeNode("tmp", []),
                ],
            )
        ],
    )


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

    filename_list = None

    if fs.value == folder:
        filename_list = []
        for child in fs.children:
            filename_list.append(child.value)
        return filename_list

    for child in fs.children:
        new_child = folder_contents(child, folder)
        if new_child is not None:
            filename_list = new_child

    if filename_list is not None:
        return filename_list

    return None


def main() -> None:
    """Skriver ut innehållet i några mappar och namnen på nivå 3."""
    """Filsystemträd skapat

        Innehåll i mappar:
        documents: report.pdf, notes.txt
        code: python, README.md
        tmp: (tom mapp)
        music: finns INTE

        På nivå 3: documents, code, tmp
"""
    filesystem = create_filesystem_example()
    folder_list = ["documents", "code", "tmp", "music"]

    output_string = f"Filsystemträd skapat \n \nInnehåll i mappar: \n"
    for file in folder_list:
        content = folder_contents(filesystem, file)
        output_string += f"{file}: "
        if content == []:
            output_string += f"(tom mapp) \n"
        elif content is None:
            output_string += f"finns INTE \n"
        else:
            for i in range(len(content)):
                if i < (len(content) - 1):
                    output_string += f"{content[i]}, "
                else:
                    output_string += f"{content[i]}"
            output_string += f"\n"

    output_string += f"\n"
    output_string += f"På nivå 3: "
    level_values = values_at_level(filesystem, 3)
    for i in range(len(level_values)):
        if i < (len(level_values) - 1):
            output_string += f"{level_values[i]}, "
        else:
            output_string += f"{level_values[i]} "
    print(output_string)


if __name__ == "__main__":
    main()
