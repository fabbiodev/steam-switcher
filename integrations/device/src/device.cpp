// Device collector/replay for the explicitly supported Steam x64 build.
// All work runs in exported functions, outside DllMain and the loader lock.
#include <Windows.h>
#include <objbase.h>
#include <MinHook.h>
#include <array>
#include <cstdint>
#include <cstring>
#include <string>

#pragma pack(push, 1)
struct DeviceData {
    DWORD version;
    char guid[128];
    unsigned char network[16];
    char disk[512];
    char hostname[256];
    char fqdn[512];
    wchar_t root[1024];
};
#pragma pack(pop)
static_assert(sizeof(DeviceData) == 3476);
static DeviceData saved{};
static HMODULE client{}, tier{};

template<size_t N> bool valid(const char (&s)[N], bool empty = false) {
    const auto end = static_cast<const char*>(std::memchr(s, 0, N));
    return end && (empty || end != s);
}
static bool valid_profile(const DeviceData& d) {
    return d.version == 1 && valid(d.guid) && valid(d.disk) && valid(d.hostname) && valid(d.fqdn, true);
}
static bool load(const DeviceData& d) {
    if (!std::wmemchr(d.root, 0, 1024) || !d.root[0]) return false;
    const std::wstring root(d.root);
    tier = LoadLibraryExW((root + L"\\tier0_s64.dll").c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!tier) return false;
    client = LoadLibraryExW((root + L"\\steamclient64.dll").c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    return client != nullptr;
}
template<class T> static T at(std::uintptr_t rva) {
    return reinterpret_cast<T>(reinterpret_cast<std::uintptr_t>(client) + rva);
}
using Guid = char(__fastcall*)(unsigned char*, DWORD);
using Network = DWORD(__fastcall*)(unsigned long long*);
using Fallback = DWORD(__fastcall*)(unsigned long long*, char);
using Disk = bool(__fastcall*)(unsigned char*, int);
using Hostname = const char*(__fastcall*)();
using Fqdn = bool(__fastcall*)(char*, size_t);

// RVAs are pinned by full-file SHA256 in Device.cs before either export is called.
static constexpr auto guid_rva = 0x8f8b20;
static constexpr auto network_rva = 0xe94e50;
static constexpr auto fallback_rva = 0xe95120;
static constexpr auto disk_rva = 0xe93ad0;

extern "C" __declspec(dllexport) DWORD WINAPI SteamSDA_Capture(DeviceData* d) {
    if (!d || d->version != 1 || !load(*d)) return 1;
    const auto com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    std::array<unsigned long long, 2> network{};
    const auto hostname = reinterpret_cast<Hostname>(GetProcAddress(tier, "GetLocalHostname"));
    const auto fqdn = reinterpret_cast<Fqdn>(GetProcAddress(tier, "BGetLocalFQDN"));
    bool ok = hostname && fqdn;
    if (ok) {
        ok = at<Guid>(guid_rva)(reinterpret_cast<unsigned char*>(d->guid), sizeof(d->guid)) != 0;
        DWORD code = at<Network>(network_rva)(network.data());
        if (code) code = at<Fallback>(fallback_rva)(network.data(), 1);
        ok = ok && code == ERROR_SUCCESS;
        std::memcpy(d->network, network.data(), sizeof(d->network));
        ok = at<Disk>(disk_rva)(reinterpret_cast<unsigned char*>(d->disk), sizeof(d->disk)) && ok;
        const auto name = hostname();
        ok = name && strnlen_s(name, sizeof(d->hostname)) < sizeof(d->hostname) && ok;
        if (name && strnlen_s(name, sizeof(d->hostname)) < sizeof(d->hostname))
            strcpy_s(d->hostname, name);
        if (!fqdn(d->fqdn, sizeof(d->fqdn))) d->fqdn[0] = 0;
    }
    if (SUCCEEDED(com)) CoUninitialize();
    return ok && valid_profile(*d) ? 0 : 2;
}

static char __fastcall get_guid(unsigned char* out, DWORD capacity) {
    if (!out || capacity <= std::strlen(saved.guid)) return 0;
    std::memcpy(out, saved.guid, std::strlen(saved.guid) + 1);
    return 1;
}
static DWORD __fastcall get_network(unsigned long long* out) {
    if (!out) return ERROR_INVALID_PARAMETER;
    std::memcpy(out, saved.network, sizeof(saved.network));
    return ERROR_SUCCESS;
}
static DWORD __fastcall get_fallback(unsigned long long* out, char) { return get_network(out); }
static bool __fastcall get_disk(unsigned char* out, int capacity) {
    if (!out || capacity <= 0 || static_cast<size_t>(capacity) <= std::strlen(saved.disk)) return false;
    std::memcpy(out, saved.disk, std::strlen(saved.disk) + 1);
    return true;
}
static const char* __fastcall get_hostname() { return saved.hostname; }
static bool __fastcall get_fqdn(char* out, size_t capacity) {
    if (!out || !capacity) return false;
    out[0] = 0;
    if (!saved.fqdn[0] || capacity <= std::strlen(saved.fqdn)) return false;
    std::memcpy(out, saved.fqdn, std::strlen(saved.fqdn) + 1);
    return true;
}

static DWORD check_outputs() {
    char guid[128]{}, disk[512]{}, fqdn[512]{};
    std::array<unsigned long long, 3> network{0, 0, 0x123456789abcdef0ull};
    if (!get_guid(reinterpret_cast<unsigned char*>(guid), sizeof(guid)) || std::strcmp(guid, saved.guid)) return 10;
    if (!get_disk(reinterpret_cast<unsigned char*>(disk), sizeof(disk)) || std::strcmp(disk, saved.disk)) return 11;
    if (get_fallback(network.data(), 1) || std::memcmp(network.data(), saved.network, 16) || network[2] != 0x123456789abcdef0ull) return 12;
    if (std::strcmp(get_hostname(), saved.hostname)) return 13;
    if (get_fqdn(fqdn, sizeof(fqdn)) != (saved.fqdn[0] != 0) || std::strcmp(fqdn, saved.fqdn)) return 14;
    if (get_guid(reinterpret_cast<unsigned char*>(guid), 1) || get_disk(reinterpret_cast<unsigned char*>(disk), 1) || get_fqdn(fqdn, 1)) return 15;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI SteamSDA_Test(DeviceData* d) {
    if (!d || !valid_profile(*d)) return 1;
    saved = *d;
    return check_outputs();
}

// Synthetic functions used by the suspended-process integration test. They never
// load Steam DLLs, inspect this computer, or contact a server.
__declspec(noinline) static char __fastcall fake_guid(unsigned char* out, DWORD size) { return out && size ? (out[0] = 0, 0) : 0; }
__declspec(noinline) static DWORD __fastcall fake_network(unsigned long long* out) { return out ? (out[0] = 0, ERROR_NOT_FOUND) : ERROR_INVALID_PARAMETER; }
__declspec(noinline) static DWORD __fastcall fake_fallback(unsigned long long* out, char pci) { return fake_network(out) + pci; }
__declspec(noinline) static bool __fastcall fake_disk(unsigned char* out, int size) { return out && size ? (out[0] = 0, false) : false; }
__declspec(noinline) static const char* __fastcall fake_hostname() { return "unbound-fixture"; }
__declspec(noinline) static bool __fastcall fake_fqdn(char* out, size_t size) { return out && size ? (out[0] = 0, false) : false; }
static bool test_installed = false;
extern "C" __declspec(dllexport) DWORD WINAPI SteamSDA_TestHooks(DeviceData* d) {
    if (!d || !valid_profile(*d) || MH_Initialize() != MH_OK) return 1;
    saved = *d;
    void* targets[] = { reinterpret_cast<void*>(fake_guid), reinterpret_cast<void*>(fake_network),
        reinterpret_cast<void*>(fake_fallback), reinterpret_cast<void*>(fake_disk),
        reinterpret_cast<void*>(fake_hostname), reinterpret_cast<void*>(fake_fqdn) };
    void* hooks[] = { reinterpret_cast<void*>(get_guid), reinterpret_cast<void*>(get_network),
        reinterpret_cast<void*>(get_fallback), reinterpret_cast<void*>(get_disk),
        reinterpret_cast<void*>(get_hostname), reinterpret_cast<void*>(get_fqdn) };
    for (size_t i = 0; i < 6; ++i)
        if (MH_CreateHook(targets[i], hooks[i], nullptr) != MH_OK) return 2;
    if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) return 3;
    test_installed = true;
    return 0;
}
extern "C" __declspec(dllexport) DWORD WINAPI SteamSDA_Probe() {
    // Volatile function pointers prevent compiler folding of synthetic calls.
    Guid volatile guid = fake_guid;
    Network volatile network = fake_network;
    Fallback volatile fallback = fake_fallback;
    Disk volatile disk = fake_disk;
    Hostname volatile host = fake_hostname;
    Fqdn volatile fqdn = fake_fqdn;
    char g[128]{}, d[512]{}, f[512]{};
    std::array<unsigned long long, 3> n{0, 0, 0x123456789abcdef0ull};
    if (!test_installed || !guid(reinterpret_cast<unsigned char*>(g), sizeof(g)) || std::strcmp(g, saved.guid)) return 1;
    if (network(n.data()) || fallback(n.data(), 1) || std::memcmp(n.data(), saved.network, 16) || n[2] != 0x123456789abcdef0ull) return 2;
    if (!disk(reinterpret_cast<unsigned char*>(d), sizeof(d)) || std::strcmp(d, saved.disk)) return 3;
    if (std::strcmp(host(), saved.hostname) || fqdn(f, sizeof(f)) != (saved.fqdn[0] != 0) || std::strcmp(f, saved.fqdn)) return 4;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI SteamSDA_Install(DeviceData* d) {
    if (!d || !valid_profile(*d)) return 1;
    // Never attach to a running/preinitialised Steam client.
    if (GetModuleHandleW(L"steamclient64.dll")) return 3;
    saved = *d;
    if (!load(*d) || MH_Initialize() != MH_OK) return 2;
    std::array<void*, 6> targets{
        reinterpret_cast<void*>(at<Guid>(guid_rva)), reinterpret_cast<void*>(at<Network>(network_rva)),
        reinterpret_cast<void*>(at<Fallback>(fallback_rva)), reinterpret_cast<void*>(at<Disk>(disk_rva)),
        reinterpret_cast<void*>(GetProcAddress(tier, "GetLocalHostname")),
        reinterpret_cast<void*>(GetProcAddress(tier, "BGetLocalFQDN"))};
    const std::array<void*, 6> hooks{
        reinterpret_cast<void*>(&get_guid), reinterpret_cast<void*>(&get_network),
        reinterpret_cast<void*>(&get_fallback), reinterpret_cast<void*>(&get_disk),
        reinterpret_cast<void*>(&get_hostname), reinterpret_cast<void*>(&get_fqdn)};
    for (size_t i = 0; i < targets.size(); ++i) {
        if (!targets[i] || MH_CreateHook(targets[i], hooks[i], nullptr) != MH_OK) return 4;
    }
    if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) return 5;
    // Exercise the patched entry points, not just the detour implementations.
    char guid[128]{}, disk[512]{}, fqdn[512]{};
    std::array<unsigned long long, 2> network{};
    if (!at<Guid>(guid_rva)(reinterpret_cast<unsigned char*>(guid), sizeof(guid)) || std::strcmp(guid, saved.guid)) return 6;
    if (at<Network>(network_rva)(network.data()) || std::memcmp(network.data(), saved.network, 16)) return 6;
    if (at<Fallback>(fallback_rva)(network.data(), 1) || std::memcmp(network.data(), saved.network, 16)) return 6;
    if (!at<Disk>(disk_rva)(reinterpret_cast<unsigned char*>(disk), sizeof(disk)) || std::strcmp(disk, saved.disk)) return 6;
    if (std::strcmp(reinterpret_cast<Hostname>(targets[4])(), saved.hostname)) return 6;
    if (reinterpret_cast<Fqdn>(targets[5])(fqdn, sizeof(fqdn)) != (saved.fqdn[0] != 0) || std::strcmp(fqdn, saved.fqdn)) return 6;
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(instance);
    return TRUE;
}
