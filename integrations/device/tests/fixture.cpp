#include <Windows.h>
// Main can succeed only when the profile and hooks were installed before it ran.
int main() {
    Sleep(300); // Let the test owner acquire its wait handle.
    const auto module = GetModuleHandleW(L"SteamSDADevice.dll");
    if (!module) return 10;
    const auto probe = reinterpret_cast<DWORD(WINAPI*)()>(GetProcAddress(module, "SteamSDA_Probe"));
    return probe && probe() == 0 ? 42 : 11;
}
