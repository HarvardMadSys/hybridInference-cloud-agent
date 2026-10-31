"""Turn one recorded event into the file operations it proves.

The module reads only what a stored row already carries: a tool call's
arguments, the result paired to it, and — for Codex — the ``fileChange``
notification the host stored as a raw row. It never reads a file, runs a
command, or asks a runtime anything.

Four rules shape every branch:

* **Evidence only.** A shell command is never scanned for paths. A search that
  names a pattern, a glob or a directory attaches to no file, and matches a
  result lists are never turned into files. An image is an image only when the
  tool or the recorded result says so — an extension is a label, not proof.
* **A missing number is None.** A write over a file whose old content was not
  recorded, a deleted file's removed lines, a clipped patch: None, never zero.
  Zero appears only where the record proves it (an added file removes nothing).
* **Paths are data.** They are joined lexically to the cwd the turn recorded,
  keeping roots and case; nothing is resolved against a disk.
* **One section of a multi-file patch is one operation**, so add, delete,
  rename and multi-file patches keep their own identities and statuses.

The payload keys read here are the ones
:mod:`cloud_agent.storage._sql.file_activity` computes in Postgres, where every
supported runtime's spelling is folded into one name and the exact line counts
of the principal text fields are computed on the whole recorded text.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cloud_agent.file_activity.models import (
    FileKind,
    FileOperation,
    OperationKind,
    OperationStatus,
    PathBasis,
)

#: Exact tool names, after the final ``__``/``.`` segment, that are a file action.
READ_TOOLS = frozenset({"read", "read_file", "readfile", "read_text_file", "view_file"})
WRITE_TOOLS = frozenset({"write", "write_file", "writefile", "create_file", "createfile"})
PATCH_TOOLS = frozenset({"apply_patch", "applypatch", "patch", "patch_file"})
EDIT_TOOLS = frozenset(
    {
        "edit",
        "edit_file",
        "editfile",
        "multiedit",
        "multi_edit",
        "str_replace",
        "str_replace_editor",
        "notebookedit",
        "notebook_edit",
    }
)
SEARCH_TOOLS = frozenset(
    {
        "grep",
        "search",
        "search_files",
        "text_search",
        "find",
        "find_files",
        "glob",
        "list",
        "ls",
        "list_files",
        "file_search",
    }
)
IMAGE_TOOLS = frozenset({"view_image", "read_image", "image", "images", "screenshot"})

#: Every exact name the classifier accepts, as a sorted tuple. The SQL
#: prefilter keeps its own copy of these names; a test asserts the two sets are
#: equal, so a name added to one without the other fails rather than silently
#: reading nothing.
ACTION_TOOL_NAMES = tuple(
    sorted(READ_TOOLS | WRITE_TOOLS | PATCH_TOOLS | EDIT_TOOLS | SEARCH_TOOLS | IMAGE_TOOLS)
)

#: Extension labels. The badge names a kind of file from its name alone; it is
#: never evidence that the file was read, written, searched or viewed.
_EXTENSIONS: dict[str, tuple[str, FileKind]] = {
    "ts": ("TS", "code"),
    "tsx": ("TSX", "code"),
    "js": ("JS", "code"),
    "jsx": ("JSX", "code"),
    "mjs": ("JS", "code"),
    "cjs": ("JS", "code"),
    "py": ("PY", "code"),
    "rs": ("RS", "code"),
    "go": ("GO", "code"),
    "java": ("JAVA", "code"),
    "kt": ("KT", "code"),
    "rb": ("RB", "code"),
    "php": ("PHP", "code"),
    "c": ("C", "code"),
    "h": ("H", "code"),
    "cc": ("C++", "code"),
    "cpp": ("C++", "code"),
    "cs": ("C#", "code"),
    "swift": ("SWIFT", "code"),
    "lua": ("LUA", "code"),
    "sql": ("SQL", "data"),
    "json": ("JSON", "data"),
    "jsonl": ("JSONL", "data"),
    "yaml": ("YAML", "data"),
    "yml": ("YAML", "data"),
    "toml": ("TOML", "data"),
    "ini": ("INI", "data"),
    "csv": ("CSV", "data"),
    "lock": ("LOCK", "data"),
    "md": ("MD", "markup"),
    "mdx": ("MDX", "markup"),
    "html": ("HTML", "markup"),
    "css": ("CSS", "markup"),
    "scss": ("SCSS", "markup"),
    "vue": ("VUE", "markup"),
    "svg": ("SVG", "image"),
    "png": ("PNG", "image"),
    "jpg": ("JPG", "image"),
    "jpeg": ("JPG", "image"),
    "gif": ("GIF", "image"),
    "webp": ("WEBP", "image"),
    "ico": ("ICO", "image"),
    "bmp": ("BMP", "image"),
    "sh": ("SH", "shell"),
    "bash": ("SH", "shell"),
    "zsh": ("SH", "shell"),
    "fish": ("SH", "shell"),
    "ps1": ("PS1", "shell"),
    "txt": ("TXT", "text"),
    "log": ("LOG", "text"),
    "rst": ("RST", "text"),
    "pdf": ("PDF", "other"),
}

#: A Codex ``fileChange`` change kind, as the app-server names it.
_CHANGE_KIND: dict[str, tuple[OperationKind, str]] = {
    "add": ("write", "write"),
    "create": ("write", "write"),
    "delete": ("edit", "delete"),
    "remove": ("edit", "delete"),
    "rename": ("edit", "rename"),
    "update": ("edit", "edit"),
    "edit": ("edit", "edit"),
}

_PATCH_HEADER = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")
_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


class PatchSection:
    """One file's section of an ``apply_patch`` body, before it is a model."""

    __slots__ = ("action", "added", "change", "path", "removed", "rename_to")

    def __init__(self, change: str, path: str) -> None:
        self.change = change
        self.path = path
        self.rename_to: str | None = None
        self.added = 0
        self.removed: int | None = 0
        self.action = "apply_patch"


def tool_key(tool: Any) -> str:
    """The final, lower-cased segment of a recorded tool name."""
    if not isinstance(tool, str):
        return ""
    name = tool.strip().lower()
    if not name:
        return ""
    for separator in ("__", "."):
        name = name.rsplit(separator, 1)[-1]
    return name


def classify(tool: Any) -> tuple[OperationKind | None, str]:
    """The operation a tool name records, and the action it is shown as."""
    key = tool_key(tool)
    if key in IMAGE_TOOLS:
        return "image", "image"
    if key in READ_TOOLS:
        return "read", "read"
    if key in WRITE_TOOLS:
        return "write", "write"
    if key in PATCH_TOOLS:
        return "edit", "apply_patch"
    if key in EDIT_TOOLS:
        return "edit", "edit"
    if key in SEARCH_TOOLS:
        return "search", "search"
    return None, "unknown"


def line_count(text: str) -> int:
    r"""The file lines a recorded string holds.

    ``""`` holds none. A trailing newline ends the last line instead of
    starting an empty one, so ``"a\\n"`` and ``"a"`` both count one.
    """
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def normalize_path(raw: str, cwd: str | None) -> tuple[str, PathBasis]:
    """Resolve ``raw`` lexically, as recorded, without touching a filesystem.

    An absolute path keeps its root and its case. A relative path joins the
    cwd the turn recorded — and only when that cwd is absolute, so a relative
    path can never merge with an absolute path that ends the same way. ``.``
    and ``..`` are resolved in the string; nothing is stat'ed.
    """
    if _WINDOWS_PATH.match(raw):
        return posixpath.normpath(raw.replace("\\", "/")), "absolute"
    if raw.startswith("/"):
        return posixpath.normpath(raw), "absolute"
    if cwd and cwd.startswith("/"):
        return posixpath.normpath(posixpath.join(cwd, raw)), "cwd_relative"
    return posixpath.normpath(raw), "relative"


def file_labels(path: str) -> tuple[str, FileKind]:
    """A badge and a kind from the path's own extension."""
    name = posixpath.basename(path)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXTENSIONS.get(extension, (extension.upper()[:6] or "FILE", "other"))


def is_image_result(content: Any) -> bool:
    """Whether a recorded tool result is an image content block.

    The adapters flatten a result's blocks into text, so the only structure
    left is JSON. A result is an image when that JSON is itself an image block,
    or a list whose blocks include one. A document that happens to contain the
    word ``image`` is not: it does not parse into that shape.
    """
    if not isinstance(content, str) or not content.strip():
        return False
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return False
    return _has_image_block(parsed)


def _has_image_block(value: Any) -> bool:
    """An image content block, at the top level or in a list of blocks."""
    if isinstance(value, list):
        return any(_has_image_block(item) for item in value)
    if not isinstance(value, dict):
        return False
    if value.get("type") != "image":
        return False
    source = value.get("source")
    if isinstance(source, dict):
        media = source.get("media_type") or source.get("mediaType")
        return isinstance(media, str) and media.startswith("image/")
    # A URL-bearing image block is still explicit image evidence.
    return isinstance(value.get("url"), str) or isinstance(value.get("data"), str)


def _usable_path(value: Any) -> str | None:
    """A recorded path string that names something, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "\x00" in text or len(text) > 4096:
        return None
    return text


def _usable_id(value: Any) -> str | None:
    return value if isinstance(value, str) and value and len(value) <= 512 else None


def _fields(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """The whitelisted recorded arguments of one tool call."""
    value = payload.get("input")
    return value if isinstance(value, Mapping) else {}


def _explicit_file(payload: Mapping[str, Any]) -> str | None:
    """The path of one file, from a field a runtime records as one.

    A generic ``path`` counts only when the call carries no search-shaped
    argument: for a search it usually names a directory, and calling that a
    file would invent one. ``file_path``/``filePath``/``notebook_path`` are
    always a file.
    """
    fields = _fields(payload)
    for key in ("file_path", "filePath", "file_path_alt", "notebook_path"):
        raw = _usable_path(fields.get(key))
        if raw is not None:
            return raw
    if _is_search_shaped(fields):
        return None
    return _usable_path(fields.get("path")) or _usable_path(fields.get("file"))


def _is_search_shaped(fields: Mapping[str, Any]) -> bool:
    """Whether the arguments describe a search rather than one file."""
    return any(
        isinstance(fields.get(key), str) and fields.get(key)
        for key in ("pattern", "glob", "query", "directory")
    )


def parse_patch(text: str) -> list[PatchSection]:
    """Split an ``apply_patch`` body into its files, counting hunk lines.

    A section's header decides what is proven: an added file removes nothing;
    a deleted file's removed side is unknown, because the body does not carry
    its content; a moved file also names its destination.
    """
    sections: list[PatchSection] = []
    current: PatchSection | None = None
    for line in text.splitlines():
        header = _PATCH_HEADER.match(line)
        if header is not None:
            current = PatchSection(header.group(1).lower(), header.group(2).strip())
            sections.append(current)
            continue
        if current is None:
            continue
        move = _PATCH_MOVE.match(line)
        if move is not None:
            current.rename_to = move.group(1).strip()
            current.action = "rename"
            continue
        # An ``apply_patch`` body marks hunks with ``@@`` and files with
        # ``***``. A line beginning ``+++`` or ``---`` here is hunk content —
        # an added line whose text starts with ``++``, or a removed one whose
        # text starts with ``--`` — not a file header, so it is counted.
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            current.added += 1
        elif line.startswith("-"):
            current.removed = (current.removed or 0) + 1
    for section in sections:
        if section.change == "add":
            section.removed = 0
        elif section.change == "delete":
            section.removed = None
    return sections


def _multi_edit(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Sum a MultiEdit's pairs; a replace-all or a clipped side is unknown."""
    pairs = payload.get("edits")
    if not isinstance(pairs, list) or not pairs:
        return None, None
    added = removed = 0
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("replace_all"):
            return None, None
        new = pair.get("new_string")
        old = pair.get("old_string")
        # A side clipped by the projection cannot be counted: its length is
        # shorter than the recorded text it came from.
        if not isinstance(new, str) or len(new) < int(pair.get("new_chars") or 0):
            return None, None
        if old is not None and len(old) < int(pair.get("old_chars") or 0):
            return None, None
        added += line_count(new)
        removed += line_count(old) if isinstance(old, str) else 0
    return added, removed


def _write_edit_deltas(
    payload: Mapping[str, Any], fields: Mapping[str, Any], kind: OperationKind
) -> tuple[int | None, int | None]:
    """The line change a write or edit's own recorded content proves."""
    if kind == "write":
        if payload.get("content_lines") is None:
            return None, None
        # Overwriting a file whose previous lines were not recorded: the added
        # side is known, the removed side is not.
        return int(payload["content_lines"]), None
    if payload.get("edits") is not None:
        # The projection kept each pair, with the whole side's length so a
        # clipped pair is recognized rather than summed short.
        return _multi_edit(payload)
    if fields.get("replace_all"):
        # How many occurrences were replaced is not recorded.
        return None, None
    if payload.get("edit_added_lines") is None:
        return None, None
    added = int(payload["edit_added_lines"])
    removed = (
        int(payload["edit_removed_lines"])
        if payload.get("edit_removed_lines") is not None
        else None
    )
    return added, removed


def _operation_status(payload: Mapping[str, Any]) -> OperationStatus:
    """Whether the recorded pair proves the call finished, failed or never ended."""
    if payload.get("result_id") is not None:
        return "failed" if payload.get("result_error") else "succeeded"
    if payload.get("attempt_running"):
        return "pending"
    return "not_recorded"


def _base(
    payload: Mapping[str, Any],
    *,
    tool: str,
    action: str,
    kind: OperationKind,
    status: OperationStatus,
) -> dict[str, Any]:
    """The identity fields every operation carries."""
    result_id = payload.get("result_id")
    return {
        "call_id": _usable_id(payload.get("tool_id")),
        "event_id": int(payload["id"]),
        "result_event_id": int(result_id) if result_id is not None else None,
        "attempt_id": int(payload["attempt_id"]),
        "attempt_no": int(payload.get("attempt_no") or 0),
        "job_id": str(payload["job_id"]),
        "turn_no": int(payload.get("turn_no") or 0),
        "seq": int(payload.get("seq") or 0),
        "at": payload.get("created_at"),
        "tool": tool,
        "action": action,
        "kind": kind,
        "status": status,
        "subagent": bool(payload.get("subagent")),
        "superseded": bool(payload.get("superseded")),
    }


def _file_operation(
    payload: Mapping[str, Any],
    cwd: str | None,
    kind: OperationKind,
    action: str,
) -> FileOperation:
    """One read, write, edit or image operation on the file the call names."""
    fields = _fields(payload)
    raw = _explicit_file(payload)
    path: str | None = None
    basis: PathBasis = "unknown"
    if raw is not None:
        path, basis = normalize_path(raw, cwd)
    status = _operation_status(payload)
    added = removed = None
    if status == "succeeded" and kind in {"write", "edit"}:
        added, removed = _write_edit_deltas(payload, fields, kind)
    if kind == "read" and (
        payload.get("result_image") or is_image_result(payload.get("result_content"))
    ):
        # The recorded result carried an image content block: this read is
        # image activity. ``result_image`` is the projection's structural
        # check, which still holds for a base64 body too large to return; the
        # parsed check covers a small complete body. A source file that merely
        # contains the text ``"type": "image"`` is neither.
        kind, action = "image", "image"
    return FileOperation(
        **_base(
            payload,
            tool=str(payload.get("tool") or ""),
            action=action,
            kind=kind,
            status=status,
        ),
        path=path,
        name=posixpath.basename(path) if path else None,
        path_basis=basis,
        added=added,
        removed=removed,
    )


def _patch_operations(
    payload: Mapping[str, Any], cwd: str | None, sections: list[PatchSection]
) -> list[FileOperation]:
    """One operation per file a multi-file patch names."""
    status = _operation_status(payload)
    total = len(sections)
    built: list[FileOperation] = []
    for index, section in enumerate(sections, start=1):
        raw = _usable_path(section.path)
        if raw is None:
            continue
        path, basis = normalize_path(raw, cwd)
        renamed = _usable_path(section.rename_to)
        built.append(
            FileOperation(
                **_base(
                    payload,
                    tool=str(payload.get("tool") or "apply_patch"),
                    action=section.action,
                    kind="write" if section.change == "add" else "edit",
                    status=status,
                ),
                path=path,
                name=posixpath.basename(path),
                path_basis=basis,
                added=section.added if status == "succeeded" else None,
                removed=section.removed if status == "succeeded" else None,
                patch_index=index,
                patch_files=total,
                renamed_to=normalize_path(renamed, cwd)[0] if renamed is not None else None,
            )
        )
    return built


def _search_operation(payload: Mapping[str, Any], cwd: str | None) -> FileOperation:
    """A search: attached to a file only when its input names one explicitly.

    A pattern, a glob or a directory is not a file, so such a search is
    recorded with no path and attaches to nothing. Matches a result lists are
    never turned into files: a match proves the pattern found it, not that the
    search read every matching file.
    """
    fields = _fields(payload)
    raw = None
    for key in ("file_path", "filePath"):
        raw = _usable_path(fields.get(key))
        if raw is not None:
            break
    if raw is None and not _is_search_shaped(fields):
        raw = _usable_path(fields.get("path"))
    path: str | None = None
    basis: PathBasis = "unknown"
    if raw is not None:
        path, basis = normalize_path(raw, cwd)
    return FileOperation(
        **_base(
            payload,
            tool=str(payload.get("tool") or ""),
            action="search",
            kind="search",
            status=_operation_status(payload),
        ),
        path=path,
        name=posixpath.basename(path) if path else None,
        path_basis=basis,
    )


def _patch_clipped(payload: Mapping[str, Any]) -> bool:
    text = payload.get("patch")
    source_chars = payload.get("patch_source_chars")
    return isinstance(text, str) and isinstance(source_chars, int) and len(text) < source_chars


def tool_operations(payload: Mapping[str, Any], cwd: str | None) -> list[FileOperation]:
    """Every file operation one recorded tool call stands for."""
    tool = str(payload.get("tool") or "")
    kind, action = classify(tool)
    if kind is None:
        return []
    if kind == "search":
        return [_search_operation(payload, cwd)]
    if action == "apply_patch":
        text = payload.get("patch")
        if isinstance(text, str) and not _patch_clipped(payload):
            sections = parse_patch(text)
            if sections:
                return _patch_operations(payload, cwd, sections)
        # A patch that names no file (clipped or empty) is still a real edit,
        # but there is no path to attribute it to.
        return [
            FileOperation(
                **_base(
                    payload,
                    tool=tool,
                    action=action,
                    kind="edit",
                    status=_operation_status(payload),
                ),
                clipped=_patch_clipped(payload),
            )
        ]
    return [_file_operation(payload, cwd, kind, action)]


def _diff_deltas(diff: str) -> tuple[int, int]:
    """Count a unified diff's own added and removed lines.

    ``diff --git``, ``index``, ``---``, ``+++`` and ``@@`` are headers: they are
    read only before the first hunk, because inside a hunk a line beginning
    ``+++``/``---`` is content. Once a hunk starts, only a hunk header ends it,
    so ``+++x`` counts as one added line and ``---x`` as one removed.
    """
    added = removed = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            # Before any hunk header: ``diff --git``, ``index``, ``---``/``+++``
            # file headers and metadata are not content.
            if line.startswith(("--- ", "+++ ")):
                continue
            # A bare body without a hunk header is still content once a
            # +/- line appears; Codex's fileChange diff can be exactly that.
            if line.startswith(("+", "-,", "-")):
                in_hunk = True
            else:
                continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _change_kind(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    return str(value or "update")


@dataclass
class CodexChange:
    """One native ``fileChange`` item as a notification recorded it.

    Codex's app-server announces the same item at ``item/started``, may update
    it, and ends it at ``item/completed``. The platform stores each
    notification as its own raw row, so the item's own id — not the ledger
    event id — is what makes those rows one change.
    """

    item_id: str
    event_id: int
    seq: int
    at: Any
    status: OperationStatus
    changes: list[Any]
    #: The bounded body was clipped, so ``changes`` is incomplete, not empty.
    clipped: bool = False


def _codex_native(payload: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """The item's own id, status and type, read from the projection.

    These are projected separately from the bounded notification body, so a
    long change list clipped out of ``file_change`` still leaves the item's
    identity and outcome readable.
    """

    def text(key: str) -> str | None:
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    return (
        text("file_change_item_id"),
        text("file_change_item_status"),
        text("file_change_item_type"),
    )


def _codex_item(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """The native item one raw notification row carries, if it carries one."""
    raw = payload.get("file_change")
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    params = parsed.get("params")
    item = params.get("item") if isinstance(params, dict) else None
    if not isinstance(item, dict):
        return None
    return item


def _codex_status(method: str, item_status: Any) -> OperationStatus | None:
    """What one notification proves about the item's outcome."""
    if method.endswith("item/completed"):
        return "failed" if item_status in {"failed", "declined"} else "succeeded"
    if method.endswith(("item/started", "item/updated")):
        return "pending"
    return None


def _codex_item_id(item: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """The native item's identity, scoped to its attempt by the caller.

    A pinned app-server names every item. A shape that somehow does not falls
    back to its call id, then to its own ledger event, so it is still one
    change rather than none.
    """
    for value in (item.get("id"), payload.get("tool_id")):
        if isinstance(value, str) and value:
            return value[:512]
    return f"event:{payload['id']}"


def codex_changes(payload: Mapping[str, Any]) -> tuple[str, str, CodexChange] | None:
    """The native item a raw row records: its id, method, and this row's state.

    ``None`` when the row is not a ``fileChange`` notification. The item's id,
    status and type come from the projection's own small fields, so a
    notification whose change body was clipped still keeps one identity and an
    honest outcome. ``clipped`` on the returned record says the change list is
    incomplete rather than empty.
    """
    method = str(payload.get("file_change_method") or "")
    if _codex_status(method, None) is None:
        return None
    native_id, native_status, native_type = _codex_native(payload)
    if native_type is not None and native_type != "fileChange":
        return None
    item = _codex_item(payload)
    status = _codex_status(method, native_status or (item or {}).get("status"))
    if status is None:
        return None
    item_id = native_id or _codex_item_id(item or {}, payload)
    changes: list[Any] = []
    if item is not None:
        raw_changes = item.get("changes")
        changes = raw_changes if isinstance(raw_changes, list) else []
    return (
        item_id,
        method,
        CodexChange(
            item_id=item_id,
            event_id=int(payload["id"]),
            seq=int(payload.get("seq") or 0),
            at=payload.get("created_at"),
            status=status,
            changes=changes,
            clipped=bool(payload.get("file_change_clipped")),
        ),
    )


def _change_without_diff(
    action: str, added: int | None, removed: int | None
) -> tuple[int | None, int | None]:
    """A change's line counts, keeping what its kind proves without a diff."""
    if action == "write":
        # An added file removes nothing; its added side needs the diff.
        return added, 0
    if action == "delete":
        # A delete removes the whole file; its removed lines need the diff.
        return 0, removed
    return added, removed


def codex_operations(payload: Mapping[str, Any], cwd: str | None) -> list[FileOperation]:
    """The file changes one recorded ``fileChange`` notification proves.

    Codex's app-server reports file changes outside its tool-call items, so the
    host stores each notification as a raw row. Only that item is read; a shell
    command's text is never scanned for paths.

    A missing ``diff`` stays unknown: only a diff the notification really
    carried — including an explicitly empty one — is counted as zero.
    """
    parsed = codex_changes(payload)
    if parsed is None:
        return []
    _item_id, _method, record = parsed
    if not record.changes and record.clipped:
        # The notification was clipped: its item is known, its files are not.
        return [
            FileOperation(
                event_id=record.event_id,
                attempt_id=int(payload["attempt_id"]),
                attempt_no=int(payload.get("attempt_no") or 0),
                job_id=str(payload["job_id"]),
                turn_no=int(payload.get("turn_no") or 0),
                seq=record.seq,
                at=record.at,
                tool="fileChange",
                action="edit",
                kind="edit",
                status=record.status,
                path=None,
                path_basis="unknown",
                subagent=bool(payload.get("subagent")),
                superseded=bool(payload.get("superseded")),
                clipped=True,
            )
        ]
    if not record.changes:
        # A well-formed item that changed no file: nothing to record.
        return []
    total = len(record.changes)
    built: list[FileOperation] = []
    for index, change in enumerate(record.changes, start=1):
        if not isinstance(change, dict):
            continue
        raw = _usable_path(change.get("path"))
        if raw is None:
            continue
        path, basis = normalize_path(raw, cwd)
        kind, action = _CHANGE_KIND.get(_change_kind(change.get("kind")), ("edit", "edit"))
        raw_diff = change.get("diff")
        if isinstance(raw_diff, str):
            added, removed = _diff_deltas(raw_diff)
        else:
            # No diff was recorded: neither side is known.
            added, removed = None, None
        added, removed = _change_without_diff(action, added, removed)
        if record.status != "succeeded":
            added = removed = None
        built.append(
            FileOperation(
                event_id=record.event_id,
                attempt_id=int(payload["attempt_id"]),
                attempt_no=int(payload.get("attempt_no") or 0),
                job_id=str(payload["job_id"]),
                turn_no=int(payload.get("turn_no") or 0),
                seq=record.seq,
                at=record.at,
                tool="fileChange",
                action=action,
                kind=kind,
                status=record.status,
                path=path,
                name=posixpath.basename(path),
                path_basis=basis,
                added=added,
                removed=removed,
                patch_index=index,
                patch_files=total,
                subagent=bool(payload.get("subagent")),
                superseded=bool(payload.get("superseded")),
            )
        )
    return built
