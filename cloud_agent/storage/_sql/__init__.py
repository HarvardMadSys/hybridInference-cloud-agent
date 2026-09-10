"""Connection-level SQL shared by more than one repository.

Nothing here acquires a connection. A caller passes the one its
transaction already holds, which is what keeps a cross-table operation a
single commit.
"""
