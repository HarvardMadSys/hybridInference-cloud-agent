// SPDX-License-Identifier: Apache-2.0
#ifndef FREEINFERENCE_TOOL_MARK_H
#define FREEINFERENCE_TOOL_MARK_H

#include <stdint.h>

#define FI_TOOL_MARK_VERSION 1U
#define FI_TOOL_MARK_PRE 1U
#define FI_TOOL_MARK_POST 2U
#define FI_TOOL_MARK_ID_BYTES 201U

#define FI_TOOL_MARK_SOCKET "/run/freeinference-tool-mark.sock"
#define FI_TOOL_MARK_HELPER "/usr/local/libexec/freeinference-tool-mark"

struct fi_tool_mark {
    uint32_t version;
    uint32_t phase;
    uint64_t mono_ns;
    char tool_use_id[FI_TOOL_MARK_ID_BYTES];
};

#endif
