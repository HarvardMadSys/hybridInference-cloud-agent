"""The guest observer loader's request and journal rules, compiled from its source.

The loader is one long-lived root process inside the Kata guest, so what it
keeps and what it lets an untrusted sender spend lasts for the guest's whole
life. These tests extract the functions that decide it from
``guest-observer.c`` at test time, compile them with the host's own compiler
against counting stand-ins for what they call, and run them -- the same
approach as ``guest_serializer``, and for the same reason: a copy of the rule
in the test tree would agree with itself whatever the loader does.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from guest_serializer import _compiler, _function_body

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SANDBOX = REPO_ROOT / "deploy" / "sandbox"
OBSERVER_C = SANDBOX / "guest-observer.c"


def _build(tmp_path: pathlib.Path, name: str, source: str) -> pathlib.Path:
    """Compile one harness with the loader's own warnings, failing loudly."""
    compiler = _compiler()
    if compiler is None:
        raise AssertionError("no C compiler is available to build the guest loader harness")
    path = tmp_path / f"{name}.c"
    path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
    binary = tmp_path / name
    compiled = subprocess.run(
        [
            compiler,
            "-O1",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(SANDBOX),
            "-o",
            str(binary),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if compiled.returncode != 0:
        raise AssertionError(
            f"the guest loader's {name} no longer compiles standalone:\n" + compiled.stderr[-2000:]
        )
    return binary


def _run(binary: pathlib.Path) -> dict[str, int]:
    completed = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_a_probe_gives_its_census_back_and_names_an_incomplete_one(tmp_path):
    """A probe must not grow the guest, nor blame a member for an unread PSS.

    The census array is allocated for every cycle and is the caller's to free.
    Only a probe ever skipped that, so each one kept a few hundred KiB in the
    observer for good; and an incomplete PSS read answered with the status
    the driver prints as "measurement does not belong to this member".
    """
    source = OBSERVER_C.read_text(encoding="utf-8")
    handler = _function_body(source, "static uint32_t handle_window_request(")
    member_window = source[
        source.index("struct member_window {") : source.index("struct observer {")
    ]
    census = source[
        source.index("struct window_census {") : source.index("static void census_task_dir")
    ]
    binary = _build(
        tmp_path,
        "probe-harness",
        f"""
        #include <stdbool.h>
        #include <stddef.h>
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>

        #include "guest-observer.h"

        /* Whether the handler calls every stand-in is what is being tested. */
        #pragma GCC diagnostic ignored "-Wunused-function"

        struct observer {{
            int unused;
        }};

        struct window_task {{
            uint64_t cpu_ns;
        }};

        {member_window}

        {census}

        /* Census arrays handed out and not yet given back. */
        static int outstanding;
        static bool next_pss_complete;

        static int run_census(struct observer *observer,
                              uint32_t uid,
                              struct window_census *census,
                              struct fi_window_sample *sample)
        {{
            (void)observer;
            (void)uid;
            (void)sample;
            census->tasks = calloc(4, sizeof(*census->tasks));
            if (census->tasks == NULL)
                return FI_WINDOW_ERR_BUSY;
            outstanding += 1;
            census->pss_complete = next_pss_complete;
            return FI_WINDOW_OK;
        }}

        static void free_census(struct window_census *census)
        {{
            if (census->tasks != NULL)
                outstanding -= 1;
            free(census->tasks);
            census->tasks = NULL;
            census->tasks_count = 0;
        }}

        static bool measurement_id_valid(const char *measurement_id)
        {{
            return measurement_id[0] != '\\0';
        }}

        static struct member_window *find_window(struct observer *observer, const char *id)
        {{
            (void)observer;
            (void)id;
            return NULL;
        }}

        static struct member_window *open_window(struct observer *observer,
                                                 uint32_t uid,
                                                 const char *id)
        {{
            (void)observer;
            (void)uid;
            (void)id;
            return NULL;
        }}

        static int emit_window_sample(struct observer *observer,
                                      struct member_window *window,
                                      uint64_t sample_seq)
        {{
            (void)observer;
            (void)window;
            (void)sample_seq;
            return FI_WINDOW_OK;
        }}

        {handler}

        int main(void)
        {{
            struct observer observer = {{0}};
            struct fi_window_request request = {{
                .version = FI_WINDOW_REQUEST_VERSION,
                .kind = FI_WINDOW_PROBE,
                .uid = 11001,
            }};
            uint32_t complete;
            uint32_t incomplete;

            next_pss_complete = true;
            complete = handle_window_request(&observer, &request);
            next_pss_complete = false;
            incomplete = handle_window_request(&observer, &request);
            printf("{{\\"complete\\":%u,\\"incomplete\\":%u,\\"outstanding\\":%d,"
                   "\\"ok\\":%u,\\"incomplete_status\\":%u}}\\n",
                   complete, incomplete, outstanding,
                   (unsigned int)FI_WINDOW_OK, (unsigned int)FI_WINDOW_ERR_INCOMPLETE);
            return 0;
        }}
        """,
    )

    answered = _run(binary)

    assert answered["outstanding"] == 0
    assert answered["complete"] == answered["ok"]
    assert answered["incomplete"] == answered["incomplete_status"]
