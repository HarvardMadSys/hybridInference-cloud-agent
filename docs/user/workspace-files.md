# Workspace files

Open **Files** in the conversation's workspace pane to browse the retained
sandbox's current repository. The folder tree loads directories as you expand
them; **Changes** lists files changed by the run.

## Open a known path

Use **Open file or folder by path** to enter a repository-relative path such as
`src/app.ts`, then press Enter or choose **Open**. This opens the preview without
requiring you to navigate every parent folder first. Invalid or unavailable paths
show an error beside the preview.

Opening a folder shows its current entries directly in the tree. Use
**Workspace root** to return to the full tree, where its parent folders are
expanded. This also lets you open a folder created after the original listing
loaded, or one omitted from a large directory's bounded listing.

Returning to **Workspace root** cancels an unfinished path read so a late
response cannot take you back into another folder. An already loaded file
preview stays open beside the root tree.

Paths use forward slashes and must stay within the repository. Absolute paths,
parent traversal, and `.git` paths are refused. Symlinks are shown without
following them.

## Retry a failed read

Use **Try again** beside a failed directory listing or file preview to retry that
read. **Refresh files** reloads the tree's expanded directories and the selected
file. Reading another file while a request is pending keeps the newer selection.

## Copy and download

**Copy path** copies the selected repository-relative path. **Copy contents**
copies the complete text returned for a file, including an empty file. Binary,
deleted, truncated, and symlink previews do not offer Copy contents. Clipboard
failures show an error so you can retry.

**Download file** saves complete file contents available within the preview size
limit. Supported raster images also have a preview. HTML and SVG remain escaped
text; downloads do not execute them in the application. Oversized files show the
preview limit instead of offering a partial download or copy.

A conversation started with **Start from scratch** has no linked repository and
does not automatically publish its files. **Download workspace** is offered in the
**Start from scratch** label beside the conversation title and on each turn's
result, and saves the workspace files as one `.tar.gz`, excluding `.git`
directories and metadata files. Git data stays in the cloud workspace. Other
hidden files, including `.gitignore` and `.github`, are included; `.gitignore`
does not filter the download. A workspace larger than the server's download
limit is refused rather than saved in part.

The pane is read-only. Ask the agent in the conversation to change workspace
files.
