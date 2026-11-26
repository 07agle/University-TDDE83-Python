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

    return TreeNode("home", 
                        [TreeNode("user", 
                                [TreeNode("documents", 
                                            [TreeNode("report.pdf", []), TreeNode("notes.txt", [])])
                                ,TreeNode("code", 
                                            [TreeNode("python", 
                                                    [TreeNode("script.py",[])])
                                        , TreeNode("README.md", [])])
                                ,TreeNode("tmp", [])
                                            ])])
    

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

    filename_list =  []
    if fs.value == folder and fs.children:
        for child in fs.children:
            filename_list.append(child.value)
        return filename_list 
    
    for child in fs.children:
        if folder_contents(child, folder) is not None:
           filename_list.extend(folder_contents(child, folder))
           return filename_list
        else:
            return None

    if filename_list is not None:
        if fs.value != folder:
            return None
        else:
            return filename_list
    
    return None


  
print(folder_contents(create_filesystem_example(), "music"))

def main() -> None:
    """Skriver ut innehållet i några mappar och namnen på nivå 3."""
    ...


if __name__ == "__main__":
    main()


fs = create_filesystem_example()
folder_contents(fs, "tmp") #[]


"""

    filename_list =  []
    if fs.value == folder:
        if fs.children:
            for child in fs.children:
                filename_list.append(child.value)
        else:
            return ["saknas"]
        return filename_list

    if not fs.children:
        return filename_list


    for child in fs.children:
        
        child_list = folder_contents(child, folder)
        
        if child_list is not None:
            filename_list.extend(child_list)

    
    if filename_list == ["saknas"] :
        return []
    if filename_list:
        return filename_list
    else:
        return None


        



    filename_list =  []
    if fs.value == folder and fs.children:
        for child in fs.children:
            filename_list.append(child.value)
        return filename_list 
    
    for child in fs.children:
        if folder_contents(child, folder):
           filename_list.extend(folder_contents(child, folder))
           return filename_list
        else:
            return []
        
    return None



        filename_list =  []
    if fs.value == folder:
        if fs.children:
            for child in fs.children:
                filename_list.append(child.value)
        else:
            return ["saknas"]
        return filename_list

    if not fs.children:
        return filename_list


    for child in fs.children:
        
        child_list = folder_contents(child, folder)
        
        if child_list is not None:
            filename_list.extend(child_list)

        if filename_list is None:
            return []
        if filename_list == ["saknas"] :
                return filename_list

    
    if filename_list == ["saknas"] :
        return []
    if filename_list:
        return filename_list
    else:
        return None




    filename_list =  []
    if fs.value == folder:
        if fs.children:
            for child in fs.children:
                filename_list.append(child.value)
            return filename_list 
        else:
            return []
    
    for child in fs.children:
        if folder_contents(child, folder):
            filename_list.extend(folder_contents(child, folder))
            return filename_list
        else:
            return None
    
    return []

  """