/*
 * Row-Bot Server.exe - runs the Row-Bot server with this version's Python.
 *
 * It sits beside python.exe in R\versions\<version>\python and does what
 * python.exe does: Py_Main from python3.dll in its own folder, so Python finds
 * the same python3XX._pth and standard library. It exists so the program that
 * listens for other devices (LAN and phone access) carries Row-Bot's name,
 * icon and signature: Windows Firewall names that program when it asks (B355).
 * src/row_bot/runtime_paths.py starts the server with it, and points
 * sys.executable back at python.exe for anything the server starts.
 */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

typedef int (*py_main_fn)(int argc, wchar_t **argv);

int wmain(int argc, wchar_t **argv)
{
    wchar_t path[MAX_PATH];
    DWORD length = GetModuleFileNameW(NULL, path, MAX_PATH);
    wchar_t *slash;
    HMODULE python;
    py_main_fn py_main;

    if (length == 0 || length >= MAX_PATH)
        return 1;
    slash = wcsrchr(path, L'\\');
    if (slash == NULL)
        return 1;
    if (wcscpy_s(slash + 1, MAX_PATH - (size_t)(slash + 1 - path), L"python3.dll") != 0)
        return 1;
    /* Only the DLL beside this program, and its dependencies from the same folder. */
    python = LoadLibraryExW(path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (python == NULL) {
        fwprintf(stderr, L"Row-Bot Server: could not load %ls (%lu)\n", path, GetLastError());
        return 1;
    }
    py_main = (py_main_fn)(void *)GetProcAddress(python, "Py_Main");
    if (py_main == NULL) {
        fwprintf(stderr, L"Row-Bot Server: %ls has no Py_Main\n", path);
        return 1;
    }
    return py_main(argc, argv);
}
