"""The guest's per-cycle bit choice, compiled from its real source.

``window_counter_bits`` decides what one census cycle claims about itself, and
the host gates every per-instance range on one of those bits. A rule that
important should not be checked by reading it: this module extracts the
function from ``guest-observer.c`` at test time, compiles it with the host's own
compiler, and runs it over the input combinations that matter.

It follows ``guest_serializer``, and for the same reason -- a
fixture that mirrors the producer agrees with whatever the mirror says, which is
how a record the host could not read once survived a fully green suite.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import textwrap

from guest_serializer import _function_body

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OBSERVER_C = REPO_ROOT / "deploy" / "sandbox" / "guest-observer.c"
OBSERVER_H = REPO_ROOT / "deploy" / "sandbox" / "guest-observer.h"


def _compiler() -> str | None:
    return shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")


#: The cycles the harness reports on, as ``(name, census fields, cpu, io)``.
#: Each one is a state a real guest reaches.
CASES: tuple[tuple[str, str, str, str], ...] = (
    ("everything_read", "2, 1, 0, true, true, false", "true", "true"),
    ("io_unaccounted", "2, 1, 0, true, true, false", "true", "false"),
    ("cpu_unread", "2, 1, 0, true, true, false", "false", "true"),
    ("empty_member", "0, 0, 0, true, false, false", "true", "true"),
    ("pss_failed", "2, 1, 1, false, true, false", "true", "true"),
    ("truncated", "2, 1, 0, true, true, true", "true", "true"),
)


def build_harness(tmp_path: pathlib.Path) -> pathlib.Path:
    """Compile the real bit chooser into a program that prints its answers."""
    compiler = _compiler()
    if compiler is None:
        raise AssertionError("no C compiler is available to build the guest bit chooser")

    source = OBSERVER_C.read_text(encoding="utf-8")
    census = source[
        source.index("struct window_census {") : source.index("static void census_task_dir")
    ]
    chooser = _function_body(source, "static uint8_t window_counter_bits(")
    header = OBSERVER_H.read_text(encoding="utf-8")
    counter_enum = header[
        header.index("enum fi_window_counter {") : header.index(
            "};", header.index("enum fi_window_counter {")
        )
        + 2
    ]

    printers = "\n".join(
        f'            report("{name}", window_counter_bits('
        f"&(struct window_census){{.tasks = NULL, .tasks_capacity = 0, "
        f".tasks_count = {fields.split(', ')[0]}, .pss_bytes = 0, "
        f".address_spaces = {fields.split(', ')[1]}, "
        f".pss_failed = {fields.split(', ')[2]}, "
        f".pss_complete = {fields.split(', ')[3]}, "
        f".deduped = {fields.split(', ')[4]}, "
        f".truncated = {fields.split(', ')[5]}}}, {cpu}, {io}));"
        for name, fields, cpu, io in CASES
    )

    harness = textwrap.dedent(
        f"""
        #include <stdbool.h>
        #include <stddef.h>
        #include <stdint.h>
        #include <stdio.h>

        {counter_enum}

        struct window_task {{
            uint64_t cpu_ns;
            uint64_t read_bytes;
            uint64_t write_bytes;
            uint64_t starttime_ticks;
            uint64_t pss_bytes;
            uint32_t uid;
            uint32_t tid;
            uint32_t tgid;
            uint8_t counters;
            bool pss_counted;
        }};

        {census}

        {chooser}

        static bool first = true;

        static void report(const char *name, uint8_t counters)
        {{
            printf("%s{{\\"case\\":\\"%s\\",\\"counters\\":%u}}", first ? "" : ",", name,
                   (unsigned int)counters);
            first = false;
        }}

        int main(void)
        {{
            printf("[");
        {printers}
            printf("]\\n");
            return 0;
        }}
        """
    ).strip()

    path = tmp_path / "window-counters-harness.c"
    path.write_text(harness, encoding="utf-8")
    binary = tmp_path / "window-counters-harness"
    compiled = subprocess.run(
        [compiler, "-O1", "-Wall", "-Wextra", "-Werror", "-o", str(binary), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if compiled.returncode != 0:
        raise AssertionError(
            "the guest bit chooser no longer compiles standalone:\n" + compiled.stderr[-2000:]
        )
    return binary


def run_chooser(tmp_path: pathlib.Path) -> dict[str, int]:
    """Each named cycle's counter bits, as the guest's own code computes them."""
    binary = build_harness(tmp_path)
    completed = subprocess.run([str(binary)], capture_output=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"the guest bit chooser failed with exit code {completed.returncode}")
    return {entry["case"]: entry["counters"] for entry in json.loads(completed.stdout)}
