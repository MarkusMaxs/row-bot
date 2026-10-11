/*
 * Row-Bot.exe - starts the active version of a per-user Row-Bot install.
 *
 * It sits at the install root R (%LOCALAPPDATA%\Programs\Row-Bot) and:
 *   1. reads R\state.json and applies the loop guard (begin_launch below);
 *   2. starts R\versions\<active>\python\python.exe with
 *      R\versions\<active>\app\launcher.py and its own arguments, with the
 *      environment launch_row_bot.bat used to set plus PYTHONDONTWRITEBYTECODE;
 *   3. starts it again only when it exits with 75 (RELAUNCH_EXIT_CODE), or
 *      after a failed start of a version that is not verified yet.
 *
 * A launch counts as unverified until the Python launcher clears
 * pending_verification (once /readyz answers). After two unverified launches
 * in a row, active goes back to previous and last_rollback records it. A start
 * that is not ready after 240 s counts as failed. The same rules live in
 * src/row_bot/install_layout.py (begin_launch, relaunch_after_exit), the macOS
 * tray host and the Linux row-bot wrapper.
 *
 * python.exe runs with CREATE_NO_WINDOW: a hidden console, as the .vbs/.bat
 * pair gave it, so console programs it starts never open a window.
 */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <shobjidl.h>
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#pragma comment(lib, "shell32.lib")
#pragma comment(lib, "user32.lib")

#define RELAUNCH_EXIT_CODE 75
#define MAX_UNVERIFIED_LAUNCHES 2
#define READY_TIMEOUT_MS (240u * 1000u)
#define QUICK_RELAUNCH_MS (30u * 1000u)
#define MAX_QUICK_RELAUNCHES 3
#define APP_USER_MODEL_ID L"ai.row-bot.desktop"
#define VERSION_MAX 64
#define MAX_FIELDS 48

static wchar_t g_root[MAX_PATH];        /* the install root R */
static wchar_t g_log[MAX_PATH];         /* <data dir>\launcher-host.log */
static wchar_t *g_original_path = NULL; /* PATH before any version was added */

/* -- Logging --------------------------------------------------------------- */

static void log_line(const char *format, ...)
{
    char message[1024];
    char line[1200];
    SYSTEMTIME now;
    va_list args;
    HANDLE file;
    DWORD written;
    int length;

    if (!g_log[0])
        return;
    va_start(args, format);
    vsnprintf(message, sizeof message, format, args);
    va_end(args);
    GetLocalTime(&now);
    length = snprintf(line, sizeof line, "%04u-%02u-%02u %02u:%02u:%02u Row-Bot.exe[%lu] %s\r\n",
                      now.wYear, now.wMonth, now.wDay, now.wHour, now.wMinute, now.wSecond,
                      GetCurrentProcessId(), message);
    if (length <= 0)
        return;
    file = CreateFileW(g_log, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                       NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return;
    WriteFile(file, line, (DWORD)strlen(line), &written, NULL);
    CloseHandle(file);
}

static void open_log(void)
{
    wchar_t dir[MAX_PATH];
    DWORD n = GetEnvironmentVariableW(L"ROW_BOT_DATA_DIR", dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) {
        n = GetEnvironmentVariableW(L"USERPROFILE", dir, MAX_PATH);
        if (n == 0 || n >= MAX_PATH - 10)
            return;
        wcscat_s(dir, MAX_PATH, L"\\.row-bot");
    }
    CreateDirectoryW(dir, NULL);
    if (swprintf(g_log, MAX_PATH, L"%ls\\launcher-host.log", dir) < 0)
        g_log[0] = 0;
}

/* -- A small JSON object reader for state.json ------------------------------
 * The top-level object is kept as (key, raw value text) pairs, so fields this
 * launcher does not know (and nested objects) are written back unchanged. */

typedef struct {
    char key[64];
    char *value;
} Field;

typedef struct {
    Field fields[MAX_FIELDS];
    int count;
    int changed;
} State;

static const char *skip_space(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')
        p++;
    return p;
}

static const char *skip_string(const char *p) /* p is at the opening quote */
{
    for (p++; *p; p++) {
        if (*p == '\\' && p[1])
            p++;
        else if (*p == '"')
            return p + 1;
    }
    return NULL;
}

static const char *skip_value(const char *p)
{
    int depth = 0;
    if (*p == '"')
        return skip_string(p);
    if (*p != '{' && *p != '[') {
        while (*p && *p != ',' && *p != '}' && *p != ']' && *p != ' ' && *p != '\r' && *p != '\n' && *p != '\t')
            p++;
        return p;
    }
    for (; *p; p++) {
        if (*p == '"') {
            p = skip_string(p);
            if (!p)
                return NULL;
            p--;
        } else if (*p == '{' || *p == '[') {
            depth++;
        } else if ((*p == '}' || *p == ']') && --depth == 0) {
            return p + 1;
        }
    }
    return NULL;
}

static void free_state(State *state)
{
    for (int i = 0; i < state->count; i++)
        free(state->fields[i].value);
    state->count = 0;
}

static int parse_object(const char *text, State *state)
{
    const char *p = skip_space(text);
    memset(state, 0, sizeof *state);
    if (*p++ != '{')
        return 0;
    p = skip_space(p);
    if (*p == '}')
        return 1;
    for (;;) {
        const char *key_end, *value_start, *value_end;
        size_t key_length, value_length;
        Field *field;

        if (*p != '"' || state->count >= MAX_FIELDS)
            goto fail;
        key_end = skip_string(p);
        if (!key_end)
            goto fail;
        key_length = (size_t)(key_end - p - 2);
        if (key_length >= sizeof state->fields[0].key || memchr(p + 1, '\\', key_length))
            goto fail;
        field = &state->fields[state->count];
        memcpy(field->key, p + 1, key_length);
        field->key[key_length] = 0;
        p = skip_space(key_end);
        if (*p++ != ':')
            goto fail;
        value_start = skip_space(p);
        value_end = skip_value(value_start);
        if (!value_end || value_end == value_start)
            goto fail;
        value_length = (size_t)(value_end - value_start);
        field->value = (char *)malloc(value_length + 1);
        if (!field->value)
            goto fail;
        memcpy(field->value, value_start, value_length);
        field->value[value_length] = 0;
        state->count++;
        p = skip_space(value_end);
        if (*p == ',') {
            p = skip_space(p + 1);
            continue;
        }
        if (*p == '}')
            return 1;
        goto fail;
    }
fail:
    free_state(state);
    return 0;
}

static const char *get_raw(const State *state, const char *key)
{
    for (int i = 0; i < state->count; i++)
        if (strcmp(state->fields[i].key, key) == 0)
            return state->fields[i].value;
    return NULL;
}

static int set_raw(State *state, const char *key, const char *value)
{
    char *copy;
    const char *old = get_raw(state, key);
    if (old && strcmp(old, value) == 0)
        return 1;
    copy = _strdup(value);
    if (!copy)
        return 0;
    for (int i = 0; i < state->count; i++) {
        if (strcmp(state->fields[i].key, key) == 0) {
            free(state->fields[i].value);
            state->fields[i].value = copy;
            state->changed = 1;
            return 1;
        }
    }
    if (state->count >= MAX_FIELDS) {
        free(copy);
        return 0;
    }
    strcpy_s(state->fields[state->count].key, sizeof state->fields[0].key, key);
    state->fields[state->count++].value = copy;
    state->changed = 1;
    return 1;
}

/* A version folder name: [0-9A-Za-z][0-9A-Za-z._+-]{0,63}, no "..". */
static int is_version_name(const char *name)
{
    size_t length = strlen(name);
    if (length == 0 || length > VERSION_MAX || strstr(name, ".."))
        return 0;
    for (size_t i = 0; i < length; i++) {
        char c = name[i];
        int alnum = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        if (!alnum && (i == 0 || !strchr("._+-", c)))
            return 0;
    }
    return 1;
}

/* Copies a JSON string field that holds a version name; 0 when absent or not one. */
static int get_version(const State *state, const char *key, char *out, size_t size)
{
    const char *raw = get_raw(state, key);
    size_t length;
    if (!raw || raw[0] != '"')
        return 0;
    length = strlen(raw);
    if (length < 3 || raw[length - 1] != '"' || length - 2 >= size)
        return 0;
    memcpy(out, raw + 1, length - 2);
    out[length - 2] = 0;
    return is_version_name(out);
}

static int get_true(const State *state, const char *key)
{
    const char *raw = get_raw(state, key);
    return raw && strcmp(raw, "true") == 0;
}

static int get_count(const State *state, const char *key)
{
    const char *raw = get_raw(state, key);
    int value = raw ? atoi(raw) : 0;
    return value > 0 ? value : 0;
}

/* -- state.json on disk --------------------------------------------------- */

static int path_in_root(wchar_t *out, const wchar_t *relative)
{
    return swprintf(out, MAX_PATH, L"%ls\\%ls", g_root, relative) > 0;
}

static int read_state(State *state)
{
    wchar_t path[MAX_PATH];
    char buffer[65536];
    DWORD size = 0;
    HANDLE file;
    int ok;

    memset(state, 0, sizeof *state);
    if (!path_in_root(path, L"state.json"))
        return 0;
    /* Delete sharing lets a writer rename a new state.json over this one. */
    file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                       OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE)
        return 0;
    ok = ReadFile(file, buffer, sizeof buffer - 1, &size, NULL);
    CloseHandle(file);
    if (!ok || size == 0 || size >= sizeof buffer - 1)
        return 0;
    buffer[size] = 0;
    if (!parse_object(buffer, state))
        return 0;
    if (!get_raw(state, "schema") || strcmp(get_raw(state, "schema"), "1") != 0) {
        free_state(state);
        return 0;
    }
    return 1;
}

/* Writes a temporary file next to state.json, then renames it over it. */
static int write_state(const State *state)
{
    wchar_t path[MAX_PATH], temporary[MAX_PATH];
    size_t capacity = 64, used = 0;
    char *text;
    HANDLE file;
    DWORD written = 0;
    int ok = 0;

    for (int i = 0; i < state->count; i++)
        capacity += strlen(state->fields[i].key) + strlen(state->fields[i].value) + 16;
    text = (char *)malloc(capacity);
    if (!text)
        return 0;
    used += (size_t)sprintf_s(text, capacity, "{\n");
    for (int i = 0; i < state->count; i++)
        used += (size_t)sprintf_s(text + used, capacity - used, "  \"%s\": %s%s\n", state->fields[i].key,
                                  state->fields[i].value, i + 1 < state->count ? "," : "");
    used += (size_t)sprintf_s(text + used, capacity - used, "}\n");

    if (path_in_root(path, L"state.json") &&
        swprintf(temporary, MAX_PATH, L"%ls\\.state.%lu.tmp", g_root, GetCurrentProcessId()) > 0) {
        file = CreateFileW(temporary, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
        if (file != INVALID_HANDLE_VALUE) {
            ok = WriteFile(file, text, (DWORD)used, &written, NULL) && written == used && FlushFileBuffers(file);
            CloseHandle(file);
            for (int attempt = 0; ok && attempt < 40; attempt++) {
                if (MoveFileExW(temporary, path, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
                    break;
                if (attempt == 39)
                    ok = 0;
                Sleep(50);
            }
            if (!ok)
                DeleteFileW(temporary);
        }
    }
    free(text);
    if (!ok)
        log_line("could not write state.json (error %lu)", GetLastError());
    return ok;
}

/* -- Versions ------------------------------------------------------------- */

static int version_root(wchar_t *out, const char *version)
{
    return swprintf(out, MAX_PATH, L"%ls\\versions\\%hs", g_root, version) > 0;
}

static int version_runnable(const char *version)
{
    wchar_t root[MAX_PATH], python[MAX_PATH], launcher[MAX_PATH];
    if (!is_version_name(version) || !version_root(root, version))
        return 0;
    swprintf(python, MAX_PATH, L"%ls\\python\\python.exe", root);
    swprintf(launcher, MAX_PATH, L"%ls\\app\\launcher.py", root);
    return GetFileAttributesW(python) != INVALID_FILE_ATTRIBUTES &&
           GetFileAttributesW(launcher) != INVALID_FILE_ATTRIBUTES;
}

/* 5.2.10 > 5.2.9, and a release beats its pre-releases (5.2.0 > 5.2.0-rc.1). */
static int compare_versions(const char *left, const char *right)
{
    while (*left || *right) {
        if (isdigit((unsigned char)*left) && isdigit((unsigned char)*right)) {
            unsigned long a = strtoul(left, (char **)&left, 10);
            unsigned long b = strtoul(right, (char **)&right, 10);
            if (a != b)
                return a < b ? -1 : 1;
            continue;
        }
        if (*left == *right) {
            left++;
            right++;
            continue;
        }
        if (!*left)
            return (*right == '-' || *right == '+') ? 1 : -1;
        if (!*right)
            return (*left == '-' || *left == '+') ? -1 : 1;
        return *left < *right ? -1 : 1;
    }
    return 0;
}

/* Without a usable state.json: the newest runnable version folder. */
static int newest_version(char *out, size_t size)
{
    wchar_t pattern[MAX_PATH];
    WIN32_FIND_DATAW entry;
    HANDLE find;
    char name[VERSION_MAX + 1];

    out[0] = 0;
    if (!path_in_root(pattern, L"versions\\*"))
        return 0;
    find = FindFirstFileW(pattern, &entry);
    if (find == INVALID_HANDLE_VALUE)
        return 0;
    do {
        if (!(entry.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
            WideCharToMultiByte(CP_UTF8, 0, entry.cFileName, -1, name, sizeof name, NULL, NULL) == 0)
            continue;
        if (version_runnable(name) && (!out[0] || compare_versions(name, out) > 0))
            strcpy_s(out, size, name);
    } while (FindNextFileW(find, &entry));
    FindClose(find);
    return out[0] != 0;
}

static void now_iso(char *out, size_t size)
{
    SYSTEMTIME now;
    GetSystemTime(&now);
    snprintf(out, size, "%04u-%02u-%02uT%02u:%02u:%02uZ", now.wYear, now.wMonth, now.wDay, now.wHour,
             now.wMinute, now.wSecond);
}

/*
 * The loop guard, applied to every launch (install_layout.begin_launch).
 * Fills `active` with the version to start; returns 0 when there is none.
 */
static int begin_launch(char *active, size_t size, int *pending)
{
    State state;
    char previous[VERSION_MAX + 1] = "", ready[VERSION_MAX + 1] = "", raw[512], at[32];
    int has_state = read_state(&state);
    int launches;

    *pending = 0;
    if (!has_state || !get_version(&state, "active", active, size) || !version_runnable(active)) {
        char found[VERSION_MAX + 1];
        if (!newest_version(found, sizeof found)) {
            free_state(&state);
            return 0;
        }
        log_line("state.json is missing or names no installed version; starting %s", found);
        free_state(&state);
        if (!parse_object("{\"schema\": 1}", &state))
            return 0;
        strcpy_s(active, size, found);
        snprintf(raw, sizeof raw, "\"%s\"", found);
        set_raw(&state, "active", raw);
        set_raw(&state, "previous", "null");
        set_raw(&state, "ready", "null");
        set_raw(&state, "pending_verification", "false");
        set_raw(&state, "unverified_launches", "0");
        set_raw(&state, "last_rollback", "null");
        write_state(&state);
        free_state(&state);
        return 1;
    }

    launches = get_count(&state, "unverified_launches");
    if (!get_true(&state, "pending_verification")) {
        if (launches)
            set_raw(&state, "unverified_launches", "0");
    } else if (launches >= MAX_UNVERIFIED_LAUNCHES && get_version(&state, "previous", previous, sizeof previous) &&
               strcmp(previous, active) != 0 && version_runnable(previous)) {
        State ready_state;
        if (get_raw(&state, "ready") && parse_object(get_raw(&state, "ready"), &ready_state)) {
            if (get_version(&ready_state, "version", ready, sizeof ready) && strcmp(ready, active) == 0)
                set_raw(&state, "ready", "null");
            free_state(&ready_state);
        }
        now_iso(at, sizeof at);
        snprintf(raw, sizeof raw,
                 "{\"from\": \"%s\", \"to\": \"%s\", \"at\": \"%s\", "
                 "\"reason\": \"%d launches in a row did not become ready\"}",
                 active, previous, at, MAX_UNVERIFIED_LAUNCHES);
        set_raw(&state, "last_rollback", raw);
        log_line("rolling back from %s to %s after %d unverified launches", active, previous, launches);
        snprintf(raw, sizeof raw, "\"%s\"", previous);
        set_raw(&state, "active", raw);
        set_raw(&state, "previous", "null");
        set_raw(&state, "pending_verification", "false");
        set_raw(&state, "unverified_launches", "0");
        strcpy_s(active, size, previous);
    } else {
        snprintf(raw, sizeof raw, "%d", launches + 1);
        set_raw(&state, "unverified_launches", raw);
        *pending = 1;
    }
    if (state.changed)
        write_state(&state);
    free_state(&state);
    return 1;
}

/* After an exit (install_layout.relaunch_after_exit). */
static int relaunch_after_exit(const char *launched, DWORD code, int timed_out)
{
    State state;
    char active[VERSION_MAX + 1], previous[VERSION_MAX + 1];
    int again = 0;

    if (code == RELAUNCH_EXIT_CODE)
        return 1;
    if (!read_state(&state))
        return 0;
    if (get_true(&state, "pending_verification") && get_version(&state, "active", active, sizeof active) &&
        strcmp(active, launched) == 0 && (timed_out || code != 0))
        again = get_version(&state, "previous", previous, sizeof previous) ||
                get_count(&state, "unverified_launches") < MAX_UNVERIFIED_LAUNCHES;
    free_state(&state);
    return again;
}

static int still_unverified(const char *launched)
{
    State state;
    char active[VERSION_MAX + 1];
    int pending = 0;
    if (read_state(&state)) {
        pending = get_true(&state, "pending_verification") &&
                  get_version(&state, "active", active, sizeof active) && strcmp(active, launched) == 0;
        free_state(&state);
    }
    return pending;
}

/* -- Starting Python ------------------------------------------------------ */

/* The command line after argv[0], passed to launcher.py unchanged. */
static const wchar_t *forwarded_arguments(void)
{
    const wchar_t *p = GetCommandLineW();
    if (*p == L'"') {
        for (p++; *p && *p != L'"'; p++)
            ;
        if (*p)
            p++;
    } else {
        while (*p && *p != L' ' && *p != L'\t')
            p++;
    }
    while (*p == L' ' || *p == L'\t')
        p++;
    return p;
}

static void set_version_environment(const wchar_t *root)
{
    wchar_t value[MAX_PATH * 2];
    size_t path_length = g_original_path ? wcslen(g_original_path) : 0;
    wchar_t *path = (wchar_t *)malloc((path_length + MAX_PATH * 2 + 4) * sizeof(wchar_t));

    SetEnvironmentVariableW(L"ROW_BOT_INSTALL_ROOT", g_root);
    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");
    swprintf(value, MAX_PATH * 2, L"%ls\\python\\tcl\\tcl8.6", root);
    SetEnvironmentVariableW(L"TCL_LIBRARY", value);
    swprintf(value, MAX_PATH * 2, L"%ls\\python\\tcl\\tk8.6", root);
    SetEnvironmentVariableW(L"TK_LIBRARY", value);
    swprintf(value, MAX_PATH * 2, L"%ls\\python\\playwright-browsers", root);
    SetEnvironmentVariableW(L"PLAYWRIGHT_BROWSERS_PATH", value);
    if (path) {
        swprintf(path, path_length + MAX_PATH * 2 + 4, L"%ls\\python\\Scripts;%ls\\python;%ls", root, root,
                 g_original_path ? g_original_path : L"");
        SetEnvironmentVariableW(L"PATH", path);
        free(path);
    }
}

/* Starts launcher.py of `version`; the process and its job come back in *process and *job. */
static int start_version(const char *version, PROCESS_INFORMATION *process, HANDLE *job)
{
    wchar_t root[MAX_PATH], python[MAX_PATH], app[MAX_PATH];
    const wchar_t *arguments = forwarded_arguments();
    size_t length;
    wchar_t *command;
    STARTUPINFOW startup;
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE), out = GetStdHandle(STD_OUTPUT_HANDLE),
           err = GetStdHandle(STD_ERROR_HANDLE);
    BOOL inherit = FALSE;
    BOOL started;

    version_root(root, version);
    swprintf(python, MAX_PATH, L"%ls\\python\\python.exe", root);
    swprintf(app, MAX_PATH, L"%ls\\app", root);
    length = wcslen(python) + wcslen(app) + wcslen(arguments) + 32;
    command = (wchar_t *)malloc(length * sizeof(wchar_t));
    if (!command)
        return 0;
    swprintf(command, length, L"\"%ls\" \"%ls\\launcher.py\"%ls%ls", python, app, arguments[0] ? L" " : L"",
             arguments);
    set_version_environment(root);

    memset(&startup, 0, sizeof startup);
    startup.cb = sizeof startup;
    if (out && out != INVALID_HANDLE_VALUE && err && err != INVALID_HANDLE_VALUE) {
        /* Started with redirected output (a smoke test): pass it on. */
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = in;
        startup.hStdOutput = out;
        startup.hStdError = err;
        SetHandleInformation(out, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        SetHandleInformation(err, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        if (in && in != INVALID_HANDLE_VALUE)
            SetHandleInformation(in, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        inherit = TRUE;
    }
    memset(process, 0, sizeof *process);
    started = CreateProcessW(python, command, NULL, NULL, inherit,
                             CREATE_NO_WINDOW | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT, NULL, app, &startup,
                             process);
    free(command);
    if (!started) {
        log_line("could not start %s (error %lu)", version, GetLastError());
        return 0;
    }
    /* A job without limits, only so a start that never gets ready can be stopped with all it started. */
    *job = CreateJobObjectW(NULL, NULL);
    if (*job && !AssignProcessToJobObject(*job, process->hProcess)) {
        CloseHandle(*job);
        *job = NULL;
    }
    ResumeThread(process->hThread);
    CloseHandle(process->hThread);
    log_line("started %s (pid %lu)", version, process->dwProcessId);
    return 1;
}

/* Waits for the launch to end; a pending start that is not verified in time is stopped. */
static DWORD wait_for_exit(PROCESS_INFORMATION *process, HANDLE job, const char *version, int pending,
                           int *timed_out)
{
    DWORD code = 1;
    *timed_out = 0;
    if (pending && WaitForSingleObject(process->hProcess, READY_TIMEOUT_MS) == WAIT_TIMEOUT &&
        still_unverified(version)) {
        log_line("%s did not become ready in %u s; stopping it", version, READY_TIMEOUT_MS / 1000u);
        *timed_out = 1;
        if (job)
            TerminateJobObject(job, 1);
        else
            TerminateProcess(process->hProcess, 1);
    }
    WaitForSingleObject(process->hProcess, INFINITE);
    GetExitCodeProcess(process->hProcess, &code);
    CloseHandle(process->hProcess);
    if (job)
        CloseHandle(job);
    return code;
}

/* -- Main ----------------------------------------------------------------- */

static void show_error(const wchar_t *message)
{
    HANDLE err = GetStdHandle(STD_ERROR_HANDLE);
    DWORD written;
    if (err && err != INVALID_HANDLE_VALUE) {
        char text[512];
        int length = WideCharToMultiByte(CP_UTF8, 0, message, -1, text, sizeof text - 2, NULL, NULL);
        if (length > 0) {
            text[length - 1] = '\n';
            WriteFile(err, text, (DWORD)length, &written, NULL);
        }
        return;
    }
    MessageBoxW(NULL, message, L"Row-Bot", MB_OK | MB_ICONERROR);
}

/* One supervising launcher per install and data folder; a second start only hands over. */
static HANDLE claim_supervisor(void)
{
    wchar_t key[MAX_PATH * 2], name[96], data[MAX_PATH];
    unsigned long hash = 2166136261u;
    HANDLE mutex;

    if (!GetEnvironmentVariableW(L"ROW_BOT_DATA_DIR", data, MAX_PATH))
        data[0] = 0;
    swprintf(key, MAX_PATH * 2, L"%ls|%ls", g_root, data);
    CharLowerW(key);
    for (const wchar_t *p = key; *p; p++)
        hash = (hash ^ (unsigned long)*p) * 16777619u;
    swprintf(name, 96, L"Local\\RowBotLauncher-%08lx", hash);
    mutex = CreateMutexW(NULL, FALSE, name);
    if (mutex && GetLastError() == ERROR_ALREADY_EXISTS) {
        CloseHandle(mutex);
        return NULL;
    }
    /* A fixed name too, so the uninstaller can ask for Row-Bot to quit first. */
    CreateMutexW(NULL, FALSE, L"RowBotLauncher");
    return mutex;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous_instance, PWSTR command_line, int show)
{
    char version[VERSION_MAX + 1];
    int quick_relaunches = 0;
    DWORD code = 1;
    DWORD path_length;
    HANDLE supervisor;
    wchar_t *slash;

    (void)instance;
    (void)previous_instance;
    (void)command_line;
    (void)show;
    SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID);
    slash = GetModuleFileNameW(NULL, g_root, MAX_PATH) ? wcsrchr(g_root, L'\\') : NULL;
    if (!slash) {
        show_error(L"Row-Bot could not find its install folder.");
        return 1;
    }
    *slash = 0;
    open_log();
    path_length = GetEnvironmentVariableW(L"PATH", NULL, 0);
    if (path_length) {
        g_original_path = (wchar_t *)malloc(path_length * sizeof(wchar_t));
        if (g_original_path)
            GetEnvironmentVariableW(L"PATH", g_original_path, path_length);
    }

    supervisor = claim_supervisor();
    if (!supervisor) {
        /* Row-Bot already runs from here: launcher.py shows it and exits. */
        PROCESS_INFORMATION process;
        HANDLE job = NULL;
        State state;
        int has = read_state(&state), timed_out;
        int ok = has && get_version(&state, "active", version, sizeof version) && version_runnable(version);
        if (has)
            free_state(&state);
        if (!ok && !newest_version(version, sizeof version))
            return 1;
        if (!start_version(version, &process, &job))
            return 1;
        return (int)wait_for_exit(&process, job, version, 0, &timed_out);
    }

    for (;;) {
        PROCESS_INFORMATION process;
        HANDLE job = NULL;
        int pending, timed_out;
        DWORD started_at;

        if (!begin_launch(version, sizeof version, &pending)) {
            show_error(L"Row-Bot could not find an installed version to start. Reinstall Row-Bot from "
                       L"https://row-bot.ai.");
            return 1;
        }
        if (!start_version(version, &process, &job)) {
            show_error(L"Row-Bot could not start. Reinstall Row-Bot from https://row-bot.ai.");
            return 1;
        }
        started_at = GetTickCount();
        code = wait_for_exit(&process, job, version, pending, &timed_out);
        log_line("%s exited with code %lu%s", version, code, timed_out ? " (not ready in time)" : "");
        if (!relaunch_after_exit(version, code, timed_out))
            break;
        if (code == RELAUNCH_EXIT_CODE && GetTickCount() - started_at < QUICK_RELAUNCH_MS) {
            if (++quick_relaunches >= MAX_QUICK_RELAUNCHES) {
                log_line("relaunch asked for %d times within seconds; stopping", quick_relaunches);
                break;
            }
        } else {
            quick_relaunches = 0;
        }
        log_line("starting Row-Bot again");
    }
    CloseHandle(supervisor);
    return code == RELAUNCH_EXIT_CODE ? 1 : (int)code;
}
