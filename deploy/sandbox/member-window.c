// SPDX-License-Identifier: Apache-2.0
/* Trusted driver for the guest observer's member execution windows.
 *
 * The observer itself owns the census and the journal; this program is the
 * only thing allowed to ask it for one. Keeping the request in a second small
 * binary rather than in the observer's own argv means the long-running
 * observer never has to be restarted to start or stop measuring a member, and
 * it means the credential the observer checks is a program the image ships
 * rather than a shell command line the host composes.
 *
 * Every subcommand sends exactly one datagram and reports the acknowledgement
 * as an exit status, so a caller learns whether the request was accepted -
 * not whether the counters inside it were available. Those are different
 * questions, and only the journal answers the second one.
 *
 *   sample   <uid> <measurement_id>   take one more census of an open window
 *   register <uid> <measurement_id>   open a window and take its baseline
 *   close    <uid> <measurement_id>   stop tracking a window
 *   probe    <uid>                    report which counters this guest reads
 */

#define _GNU_SOURCE

#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include "guest-observer.h"

#define REQUEST_TIMEOUT_MS 1500

static int connect_window_socket(void)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    struct timeval timeout = {
        .tv_sec = REQUEST_TIMEOUT_MS / 1000,
        .tv_usec = (REQUEST_TIMEOUT_MS % 1000) * 1000,
    };
    int fd;

    memcpy(address.sun_path, FI_WINDOW_SOCKET, sizeof(FI_WINDOW_SOCKET));
    fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0) {
        perror("open member window socket");
        return -1;
    }
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) < 0) {
        perror("set member window timeout");
        close(fd);
        return -1;
    }
    /* The observer answers every request, and an unbound datagram socket has
     * no address to answer to: it arrives with msg_namelen == 0, which the
     * observer refuses outright. Binding an empty abstract address lets the
     * kernel assign one on the trusted side of the sandbox, so the reply has
     * somewhere to go without naming a path the agent could collide with. */
    {
        struct sockaddr_un local = {.sun_family = AF_UNIX};

        if (bind(fd, (const struct sockaddr *)&local, sizeof(local.sun_family)) < 0) {
            perror("bind member window socket");
            close(fd);
            return -1;
        }
    }
    if (connect(fd, (const struct sockaddr *)&address,
                offsetof(struct sockaddr_un, sun_path) + sizeof(FI_WINDOW_SOCKET)) < 0) {
        perror("connect member window socket");
        close(fd);
        return -1;
    }
    return fd;
}

static const char *status_text(uint32_t status)
{
    switch (status) {
    case FI_WINDOW_OK:
        return "ok";
    case FI_WINDOW_ERR_REQUEST:
        return "malformed request";
    case FI_WINDOW_ERR_UNKNOWN:
        return "no such window";
    case FI_WINDOW_ERR_BUSY:
        return "observer busy";
    case FI_WINDOW_ERR_LIMIT:
        return "too many open windows";
    case FI_WINDOW_ERR_IDENTITY:
        return "measurement does not belong to this member";
    case FI_WINDOW_ERR_JOURNAL:
        return "journal write failed";
    case FI_WINDOW_ERR_INCOMPLETE:
        return "census could not read every address space";
    default:
        return "unknown status";
    }
}

static bool parse_uid(const char *text, uint32_t *uid)
{
    char *end = NULL;
    unsigned long parsed;

    errno = 0;
    parsed = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed > 0xffffffffUL)
        return false;
    *uid = (uint32_t)parsed;
    return true;
}

static bool measurement_id_acceptable(const char *text)
{
    size_t length = strlen(text);

    if (length < 8 || length >= FI_WINDOW_MEASUREMENT_ID_BYTES)
        return false;
    for (size_t index = 0; index < length; ++index) {
        char byte = text[index];

        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
              (byte >= '0' && byte <= '9') || byte == '_' || byte == '-'))
            return false;
    }
    return true;
}

static int send_request(const struct fi_window_request *request)
{
    uint32_t status = 0;
    int fd = connect_window_socket();
    ssize_t answered;

    if (fd < 0)
        return 1;
    if (send(fd, request, sizeof(*request), MSG_NOSIGNAL) != (ssize_t)sizeof(*request)) {
        perror("send member window request");
        close(fd);
        return 1;
    }
    /* The observer answers every accepted request, including a census that
     * found nothing: an absent answer means the request never ran, and a
     * caller that treated silence as success would report an unmeasured
     * window as an empty one. */
    answered = recv(fd, &status, sizeof(status), 0);
    close(fd);
    if (answered != (ssize_t)sizeof(status)) {
        fprintf(stderr, "member window request was not acknowledged\n");
        return 1;
    }
    if (status != FI_WINDOW_OK) {
        fprintf(stderr, "member window request failed: %s\n", status_text(status));
        return 1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    struct fi_window_request request = {.version = FI_WINDOW_REQUEST_VERSION};
    const char *command;
    uint32_t uid = 0;
    uint32_t sequence = 0;

    if (argc < 3 || argc > 5) {
        fprintf(stderr,
                "usage: %s sample|register|close <uid> <measurement-id> [sample-seq]\n"
                "       %s probe <uid>\n",
                argv[0],
                argv[0]);
        return 2;
    }
    command = argv[1];
    if (!parse_uid(argv[2], &uid)) {
        fprintf(stderr, "member window uid is not a number\n");
        return 2;
    }
    request.uid = uid;
    if (strcmp(command, "probe") == 0) {
        request.kind = FI_WINDOW_PROBE;
        return send_request(&request);
    }
    if (argc < 4 || !measurement_id_acceptable(argv[3])) {
        fprintf(stderr, "member window needs a measurement identifier\n");
        return 2;
    }
    memcpy(request.measurement_id, argv[3], strlen(argv[3]));
    if (argc == 5 && !parse_uid(argv[4], &sequence)) {
        fprintf(stderr, "member window sample sequence is not a number\n");
        return 2;
    }
    request.sample_seq = sequence;
    if (strcmp(command, "register") == 0)
        request.kind = FI_WINDOW_REGISTER;
    else if (strcmp(command, "sample") == 0)
        request.kind = FI_WINDOW_SAMPLE;
    else if (strcmp(command, "close") == 0)
        request.kind = FI_WINDOW_CLOSE;
    else {
        fprintf(stderr, "unknown member window command %s\n", command);
        return 2;
    }
    return send_request(&request);
}
