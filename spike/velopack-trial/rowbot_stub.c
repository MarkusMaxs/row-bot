/*
 * Velopack trial main executable (Phase 0 spike, throwaway).
 *
 * Velopack needs a real main executable: Setup.exe and Update.exe start it
 * for hooks (--veloapp-install/updated/obsolete/uninstall <version>) and to
 * relaunch the app after an update. This stub sets the environment that
 * installer/launch_row_bot.bat sets today and starts
 * python\pythonw.exe app\launcher.py with the stub's own arguments.
 *
 * For hook invocations it waits for Python and returns its exit code:
 * Velopack waits on the stub's PID with a timeout and then force-stops every
 * process under the install root, so a stub that returned at once would get
 * its Python child killed before the hook ran. Normal launches return at once.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#define BUF 32768

static wchar_t root[BUF], scratch[BUF], oldpath[BUF], cmd[BUF], pyw[BUF], appdir[BUF];

static void set_env_under_root(const wchar_t *name, const wchar_t *relative) {
    swprintf(scratch, BUF, L"%ls\\%ls", root, relative);
    SetEnvironmentVariableW(name, scratch);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR args, int show) {
    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    DWORD length, code = 0;
    wchar_t *slash;
    int hook;
    (void)instance;
    (void)previous;
    (void)show;

    length = GetModuleFileNameW(NULL, root, BUF);
    if (length == 0 || length >= BUF) {
        return 10;
    }
    slash = wcsrchr(root, L'\\');
    if (slash == NULL) {
        return 11;
    }
    *slash = L'\0';

    swprintf(pyw, BUF, L"%ls\\python\\pythonw.exe", root);
    swprintf(appdir, BUF, L"%ls\\app", root);

    SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
    set_env_under_root(L"TCL_LIBRARY", L"python\\tcl\\tcl8.6");
    set_env_under_root(L"TK_LIBRARY", L"python\\tcl\\tk8.6");
    set_env_under_root(L"PLAYWRIGHT_BROWSERS_PATH", L"python\\playwright-browsers");
    length = GetEnvironmentVariableW(L"PATH", oldpath, BUF);
    if (length >= BUF) {
        length = 0;
    }
    oldpath[length] = L'\0';
    swprintf(scratch, BUF, L"%ls\\python\\Scripts;%ls\\python;%ls", root, root, oldpath);
    SetEnvironmentVariableW(L"PATH", scratch);

    if (args == NULL) {
        args = L"";
    }
    swprintf(cmd, BUF, L"\"%ls\" \"%ls\\launcher.py\"%ls%ls", pyw, appdir, args[0] ? L" " : L"", args);

    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    ZeroMemory(&pi, sizeof(pi));
    if (!CreateProcessW(pyw, cmd, NULL, NULL, FALSE, 0, NULL, appdir, &si, &pi)) {
        return 12;
    }
    CloseHandle(pi.hThread);

    hook = wcsstr(args, L"--veloapp-") != NULL;
    if (hook) {
        WaitForSingleObject(pi.hProcess, INFINITE);
        GetExitCodeProcess(pi.hProcess, &code);
    }
    CloseHandle(pi.hProcess);
    return (int)code;
}
