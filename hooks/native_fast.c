/*
 * dynos-fast — Native C fast-path filter for dynos-work PreToolUse and
 * SubagentStop hooks (Claude Code).
 *
 * Exits 0 immediately for high-frequency calls guaranteed safe by dynos
 * policy, or exits 10 to delegate to the full Python policy engine
 * (hooks/pre_tool_use.py or hooks/lib_tokens_hook.py --from-stdin).
 */
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define EXIT_ALLOW 0
#define EXIT_DELEGATE 10
#define MAX_STDIN_BYTES (2 * 1024 * 1024)

static char *read_all_stdin(size_t *out_len) {
    size_t cap = 8192;
    size_t len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) return NULL;
    for (;;) {
        if (len + 4096 + 1 > cap) {
            if (cap >= MAX_STDIN_BYTES) {
                free(buf);
                return NULL;
            }
            size_t ncap = cap * 2;
            if (ncap > MAX_STDIN_BYTES) ncap = MAX_STDIN_BYTES;
            char *nbuf = (char *)realloc(buf, ncap);
            if (!nbuf) {
                free(buf);
                return NULL;
            }
            buf = nbuf;
            cap = ncap;
        }
        size_t n = fread(buf + len, 1, cap - len - 1, stdin);
        len += n;
        if (n == 0) break;
    }
    buf[len] = '\0';
    *out_len = len;
    return buf;
}

static const char *skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}

static int json_find_string(const char *json, const char *key, char *out, size_t out_sz) {
    size_t klen = strlen(key);
    const char *p = json;
    while (*p) {
        if (*p == '"') {
            p++;
            const char *kstart = p;
            int has_escape = 0;
            while (*p && *p != '"') {
                if (*p == '\\') {
                    has_escape = 1;
                    if (p[1]) p += 2;
                    else break;
                } else {
                    p++;
                }
            }
            size_t tlen = (size_t)(p - kstart);
            if (*p == '"') p++;
            const char *after = skip_ws(p);
            if (*after == ':' && !has_escape && tlen == klen && memcmp(kstart, key, klen) == 0) {
                const char *val = skip_ws(after + 1);
                if (*val != '"') return 0;
                val++;
                size_t oi = 0;
                while (*val && *val != '"') {
                    if (*val == '\\' && val[1]) {
                        char esc = val[1];
                        if (esc == '\\' || esc == '"' || esc == '/') {
                            if (oi + 1 < out_sz) out[oi++] = esc;
                            val += 2;
                            continue;
                        }
                        if (esc == 'u') {
                            return 0;
                        }
                    }
                    if (oi + 1 < out_sz) out[oi++] = *val;
                    val++;
                }
                if (*val != '"') return 0;
                out[oi] = '\0';
                return 1;
            }
        } else {
            p++;
        }
    }
    return 0;
}

static int file_contains_terminal_stage(const char *manifest_path) {
    FILE *fp = fopen(manifest_path, "rb");
    if (!fp) return 1;
    char buf[16384];
    size_t n = fread(buf, 1, sizeof(buf) - 1, fp);
    fclose(fp);
    buf[n] = '\0';
    char stage[64];
    if (!json_find_string(buf, "stage", stage, sizeof(stage))) return 0;
    if (strcmp(stage, "DONE") == 0 ||
        strcmp(stage, "FAILED") == 0 ||
        strcmp(stage, "CANCELLED") == 0 ||
        strcmp(stage, "CALIBRATED") == 0) {
        return 1;
    }
    return 0;
}

static int check_dynos_state(const char *start_cwd) {
    char cur[PATH_MAX];
    if (!realpath(start_cwd, cur)) {
        snprintf(cur, sizeof(cur), "%s", start_cwd);
    }
    for (;;) {
        char dynos_dir[PATH_MAX];
        int n = snprintf(dynos_dir, sizeof(dynos_dir), "%s/.dynos",
                         strcmp(cur, "/") == 0 ? "" : cur);
        if (n > 0 && n < (int)sizeof(dynos_dir)) {
            struct stat st;
            if (stat(dynos_dir, &st) == 0 && S_ISDIR(st.st_mode)) {
                DIR *d = opendir(dynos_dir);
                if (!d) return 2;
                int has_active = 0;
                struct dirent *ent;
                while ((ent = readdir(d)) != NULL) {
                    if (strncmp(ent->d_name, "task-", 5) != 0) continue;
                    char mpath[PATH_MAX];
                    int mn = snprintf(mpath, sizeof(mpath), "%s/%s/manifest.json",
                                      dynos_dir, ent->d_name);
                    if (mn <= 0 || mn >= (int)sizeof(mpath)) {
                        has_active = 1;
                        break;
                    }
                    struct stat mst;
                    if (stat(mpath, &mst) == 0 && S_ISREG(mst.st_mode)) {
                        if (!file_contains_terminal_stage(mpath)) {
                            has_active = 1;
                            break;
                        }
                    }
                }
                closedir(d);
                return has_active ? 2 : 1;
            }
        }
        if (strcmp(cur, "/") == 0 || cur[0] == '\0') break;
        char *slash = strrchr(cur, '/');
        if (!slash) break;
        if (slash == cur) {
            cur[1] = '\0';
        } else {
            *slash = '\0';
        }
    }
    return 0;
}

static int str_contains(const char *hay, const char *needle) {
    return hay && needle && strstr(hay, needle) != NULL;
}

static int handle_subagent_tokens(const char *json) {
    char tpath[PATH_MAX];
    char cwd[PATH_MAX];
    tpath[0] = '\0';
    cwd[0] = '\0';
    if (!json_find_string(json, "agent_transcript_path", tpath, sizeof(tpath))) {
        json_find_string(json, "transcript_path", tpath, sizeof(tpath));
    }
    json_find_string(json, "cwd", cwd, sizeof(cwd));
    if (tpath[0] == '\0' || cwd[0] == '\0') {
        return EXIT_ALLOW;
    }
    struct stat st;
    if (stat(tpath, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size == 0) {
        return EXIT_ALLOW;
    }
    if (check_dynos_state(cwd) == 0) {
        return EXIT_ALLOW;
    }
    return EXIT_DELEGATE;
}

static int is_safe_read_tool(const char *t) {
    return strcmp(t, "Read") == 0 ||
           strcmp(t, "Glob") == 0 ||
           strcmp(t, "Grep") == 0;
}

static int is_bash_tool(const char *t) {
    return strcmp(t, "Bash") == 0;
}

static int handle_pre_tool_use(const char *json) {
    const char *env_role = getenv("DYNOS_ROLE");
    const char *env_task = getenv("DYNOS_TASK_DIR");
    if ((env_role && env_role[0] != '\0') || (env_task && env_task[0] != '\0')) {
        return EXIT_DELEGATE;
    }

    char tool_name[64];
    if (!json_find_string(json, "tool_name", tool_name, sizeof(tool_name))) {
        return EXIT_DELEGATE;
    }

    int is_read = is_safe_read_tool(tool_name);
    int is_bash = !is_read && is_bash_tool(tool_name);
    if (!is_read && !is_bash) {
        return EXIT_DELEGATE;
    }

    char cwd[PATH_MAX];
    if (!json_find_string(json, "cwd", cwd, sizeof(cwd)) || cwd[0] == '\0') {
        const char *cdir = getenv("CLAUDE_PROJECT_DIR");
        if (cdir && cdir[0]) {
            snprintf(cwd, sizeof(cwd), "%s", cdir);
        } else if (!getcwd(cwd, sizeof(cwd))) {
            return EXIT_DELEGATE;
        }
    }

    if (is_read) {
        char fpath[PATH_MAX];
        char ppath[PATH_MAX];
        fpath[0] = '\0';
        ppath[0] = '\0';
        json_find_string(json, "file_path", fpath, sizeof(fpath));
        json_find_string(json, "path", ppath, sizeof(ppath));
        const char *target = fpath[0] ? fpath : ppath;
        if (str_contains(target, ".dynos") || str_contains(target, "..")) {
            return EXIT_DELEGATE;
        }
        int dynos_state = check_dynos_state(cwd);
        if (dynos_state == 0 || dynos_state == 1) {
            return EXIT_ALLOW;
        }
        return EXIT_DELEGATE;
    }

    char cmd[16384];
    if (!json_find_string(json, "command", cmd, sizeof(cmd))) {
        return EXIT_DELEGATE;
    }
    if (str_contains(cmd, ".dynos") ||
        str_contains(cmd, ".claude") ||
        str_contains(cmd, "settings.json") ||
        str_contains(cmd, "ctl.py") ||
        str_contains(cmd, "dynos")) {
        return EXIT_DELEGATE;
    }
    if (check_dynos_state(cwd) == 0) {
        return EXIT_ALLOW;
    }
    return EXIT_DELEGATE;
}

int main(int argc, char **argv) {
    if (argc < 2) return EXIT_DELEGATE;
    size_t len = 0;
    char *stdin_buf = read_all_stdin(&len);
    if (!stdin_buf) return EXIT_DELEGATE;

    int rc = EXIT_DELEGATE;
    if (strcmp(argv[1], "--subagent-tokens") == 0) {
        rc = handle_subagent_tokens(stdin_buf);
    } else if (strcmp(argv[1], "--pre-tool-use") == 0) {
        rc = handle_pre_tool_use(stdin_buf);
    }
    free(stdin_buf);
    return rc;
}
