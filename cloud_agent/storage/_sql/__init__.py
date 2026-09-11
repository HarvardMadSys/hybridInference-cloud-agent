"""SQL fragments shared by more than one statement.

A fragment lives here when a second reader asks a different question of the
same definition; duplicating the text would be a second algorithm free to
drift from the first.
"""
