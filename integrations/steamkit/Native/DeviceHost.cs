using System.Buffers.Binary;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using SteamKit2;
using SteamKit2.Authentication;
using SteamKit2.Internal;

using SteamSDA.Auth.Authentication;
using SteamSDA.Auth.Identity;
using SteamSDA.Auth.Native;

namespace SteamSDA.Auth.Native;

static class DeviceHost
{
    const int Size = 3476;
    const string ClientHash = "253e0a11a4415b38c78a42f7bdd6ecc426455f06c1ba6453a48090df3142dbbc";
    const string TierHash = "8559e6bd32eb3fddd95079633272866deb292211ec4f7e69aeb0ec48fdafef72";
    static string Dll => Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "device", "SteamSDADevice.dll"));
    [UnmanagedFunctionPointer(CallingConvention.Winapi)] delegate uint Export(IntPtr data);
    static void Put(byte[] data, int offset, int size, string text, Encoding encoding)
    {
        var raw = encoding.GetBytes(text);
        if (text.Contains('\0') || raw.Length > size - (encoding == Encoding.Unicode ? 2 : 1)) throw new AuthFailure("device_input");
        raw.CopyTo(data, offset);
    }
    static byte[] Data(string root, SavedDevice? profile)
    {
        var data = new byte[Size];
        BinaryPrimitives.WriteUInt32LittleEndian(data, 1);
        Put(data, 1428, 2048, root, Encoding.Unicode);
        if (profile is not null) {
            Put(data, 4, 128, profile.Guid, Encoding.UTF8);
            profile.Network.CopyTo(data, 132);
            Put(data, 148, 512, profile.Disk, Encoding.UTF8);
            Put(data, 660, 256, profile.Name, Encoding.UTF8);
            Put(data, 916, 512, profile.Fqdn, Encoding.UTF8);
        }
        return data;
    }
    static string Read(byte[] data, int offset, int size) {
        var end = Array.IndexOf(data, (byte)0, offset, size);
        if (end < offset) throw new AuthFailure("device_capture");
        return new UTF8Encoding(false, true).GetString(data, offset, end - offset);
    }
    public static void Check(string root)
    {
        if (!OperatingSystem.IsWindows() || !Environment.Is64BitProcess || !File.Exists(Dll)) throw new AuthFailure("device_version");
        foreach (var pair in new[] { ("steamclient64.dll", ClientHash), ("tier0_s64.dll", TierHash) }) {
            using var stream = File.OpenRead(Path.Combine(root, pair.Item1));
            if (Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant() != pair.Item2) throw new AuthFailure("device_version");
        }
        using var exe = new BinaryReader(File.OpenRead(Path.Combine(root, "steam.exe")));
        exe.BaseStream.Position = 60;
        var pe = exe.ReadInt32();
        exe.BaseStream.Position = pe;
        if (exe.ReadUInt32() != 0x4550 || exe.ReadUInt16() != 0x8664) throw new AuthFailure("device_version");
    }
    public static object Capture(string root)
    {
        Check(root);
        var data = Data(root, null);
        if (Local("SteamSDA_Capture", data) != 0) throw new AuthFailure("device_capture");
        return new { ok = true, hwid = new { version = 1, source = "native_collectors",
            machine_guid = Read(data, 4, 128), network_id = Convert.ToHexString(data.AsSpan(132, 16)).ToLowerInvariant(),
            disk_serial = Read(data, 148, 512), pc_name = Read(data, 660, 256), fqdn = Read(data, 916, 512) } };
    }
    static uint Local(string name, byte[] data)
    {
        var library = NativeLibrary.Load(Dll);
        var buffer = Marshal.AllocHGlobal(data.Length);
        try {
            Marshal.Copy(data, 0, buffer, data.Length);
            var result = Marshal.GetDelegateForFunctionPointer<Export>(NativeLibrary.GetExport(library, name))(buffer);
            Marshal.Copy(buffer, data, 0, data.Length);
            return result;
        }
        finally {
            Marshal.FreeHGlobal(buffer);
            // Installed detours must stay mapped until the isolated helper exits.
            if (name != "SteamSDA_Install") NativeLibrary.Free(library);
        }
    }
    public static object TestInstalled(string root, SavedDevice device)
    {
        Check(root);
        var code = Local("SteamSDA_Install", Data(root, device));
        return new { ok = code == 0, native_code = code, machine_id_sha256 = device.Fingerprint };
    }
    public static object Test(SavedDevice device)
    {
        var observations = TestObservation(device);
        var ok = Local("SteamSDA_Test", Data("C:\\SyntheticSteam", device)) == 0 && DeviceAuth.Test(device) && observations;
        bool? pipeline = null;
        var fixture = Path.Combine(AppContext.BaseDirectory, "SteamSDA.DeviceFixture.exe");
        if (File.Exists(fixture)) {
            var pid = Start(Path.GetDirectoryName(fixture)!, device, fixture, "SteamSDA_TestHooks");
            using var process = Process.GetProcessById((int)pid);
            _ = process.SafeHandle;
            pipeline = process.WaitForExit(10000) && process.ExitCode == 42;
            if (!process.HasExited) process.Kill();
            ok &= pipeline == true;
        }
        return new { ok, machine_id_sha256 = device.Fingerprint, network_bytes = device.Network.Length,
            suspended_process_test = pipeline, observation_parser_test = observations };
    }

    // Only a newly created, suspended child is modified. On any failure the job
    // destroys that child; the parent's existing Steam process is never touched.
    public static object Launch(string root, SavedDevice device)
    {
        Check(root);
        foreach (var running in Process.GetProcessesByName("steam")) {
            running.Dispose();
            throw new AuthFailure("device_running");
        }
        var pid = Start(root, device, Path.Combine(root, "steam.exe"), "SteamSDA_Install");
        return new { ok = true, hooks_ready = true, before_main = true,
            machine_id_sha256 = device.Fingerprint, steam_pid = pid };
    }

    // Only read the exported diagnostic block of our already loaded module.
    // No remote code execution, hook installation or Steam memory scan here.
    public static object Observe(string root, SavedDevice expected, int pid)
    {
        Check(root);
        if (pid <= 0) throw new AuthFailure("device_observation_process");
        using var process = Process.GetProcessById(pid);
        if (!string.Equals(process.MainModule?.FileName, Path.Combine(root, "steam.exe"), StringComparison.OrdinalIgnoreCase))
            throw new AuthFailure("device_observation_process");
        var module = process.Modules.Cast<ProcessModule>().SingleOrDefault(m => string.Equals(m.FileName, Dll, StringComparison.OrdinalIgnoreCase));
        if (module is null) throw new AuthFailure("device_observation_module");
        var local = NativeLibrary.Load(Dll);
        var handle = OpenProcess(0x0410, false, (uint)pid); // QUERY_INFORMATION | VM_READ.
        if (handle == IntPtr.Zero) { NativeLibrary.Free(local); throw new AuthFailure("device_observation_read"); }
        try {
            var offset = NativeLibrary.GetExport(local, "SteamSDA_Observations").ToInt64() - local.ToInt64();
            var address = new IntPtr(module.BaseAddress.ToInt64() + offset);
            byte[]? snapshot = null;
            for (var attempt = 0; attempt < 20; attempt++) {
                var data = new byte[3508];
                var after = new byte[4];
                if (!ReadProcessMemory(handle, address, data, (nuint)data.Length, out var read) || read != (nuint)data.Length
                    || !ReadProcessMemory(handle, new IntPtr(address.ToInt64() + 4), after, 4, out read) || read != 4)
                    throw new AuthFailure("device_observation_read");
                var sequence = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(4));
                if ((sequence & 1) == 0 && sequence == BinaryPrimitives.ReadUInt32LittleEndian(after)) { snapshot = data; break; }
                Thread.Sleep(5);
            }
            if (snapshot is null || BinaryPrimitives.ReadUInt32LittleEndian(snapshot) != 1)
                throw new AuthFailure("device_observation_read");
            return ObservationResult(snapshot, expected, pid);
        }
        finally { CloseHandle(handle); NativeLibrary.Free(local); }
    }

    static object ObservationResult(byte[] snapshot, SavedDevice expected, int pid)
    {
        const int start = 32;
        var calls = Enumerable.Range(0, 6).Select(i => BinaryPrimitives.ReadUInt32LittleEndian(snapshot.AsSpan(8 + i * 4))).ToArray();
        var guid = Read(snapshot, start + 4, 128);
        var network = snapshot.AsSpan(start + 132, 16).ToArray();
        var disk = Read(snapshot, start + 148, 512);
        var host = Read(snapshot, start + 660, 256);
        var fqdn = Read(snapshot, start + 916, 512);
        bool? Match(uint count, bool equal) => count == 0 ? null : equal;
        var guidMatch = Match(calls[0], guid == expected.Guid);
        var networkMatch = Match(calls[1] + calls[2], network.SequenceEqual(expected.Network));
        var diskMatch = Match(calls[3], disk == expected.Disk);
        var hostMatch = Match(calls[4], host == expected.Name);
        var fqdnMatch = Match(calls[5], fqdn == expected.Fqdn);
        var complete = calls[0] > 0 && calls[1] + calls[2] > 0 && calls[3] > 0;
        string? observedHash = null;
        if (complete) {
            using var raw = new MemoryStream();
            raw.Write(Encoding.ASCII.GetBytes("\0MessageObject\0"));
            foreach (var pair in new[] { ("BB3", Encoding.UTF8.GetBytes(guid)), ("FF2", network), ("3B3", Encoding.UTF8.GetBytes(disk)) })
                raw.Write(Encoding.ASCII.GetBytes("\x01" + pair.Item1 + "\0" + Convert.ToHexString(SHA1.HashData(pair.Item2)).ToLowerInvariant() + "\0"));
            raw.Write(new byte[] { 8, 8 });
            observedHash = Convert.ToHexString(SHA256.HashData(raw.ToArray())).ToLowerInvariant();
        }
        return new { ok = true, steam_pid = pid, source = "steam_process_collectors_after_resume",
            install_self_test_excluded = true, authorization_payload_observed = false,
            machine_id_inputs_observed = complete,
            machine_id_inputs_match = guidMatch == true && networkMatch == true && diskMatch == true,
            machine_id_sha256_observed = observedHash, machine_id_sha256_expected = expected.Fingerprint,
            components = new {
                machine_guid = new { calls = calls[0], matches = guidMatch },
                network_id = new { calls = (ulong)calls[1] + calls[2], network_calls = calls[1], fallback_calls = calls[2], bytes = 16, matches = networkMatch },
                disk_serial = new { calls = calls[3], matches = diskMatch },
                pc_name = new { calls = calls[4], matches = hostMatch },
                fqdn = new { calls = calls[5], matches = fqdnMatch }
            } };
    }

    static bool TestObservation(SavedDevice device)
    {
        var data = new byte[3508];
        BinaryPrimitives.WriteUInt32LittleEndian(data, 1);
        Data("C:\\SyntheticSteam", device).CopyTo(data, 32);
        var empty = JsonSerializer.SerializeToElement(ObservationResult(data, device, 1));
        if (empty.GetProperty("machine_id_inputs_observed").GetBoolean()
            || empty.GetProperty("machine_id_sha256_observed").ValueKind != JsonValueKind.Null) return false;
        for (var i = 0; i < 6; i++) BinaryPrimitives.WriteUInt32LittleEndian(data.AsSpan(8 + 4 * i), 1);
        var matching = JsonSerializer.SerializeToElement(ObservationResult(data, device, 1));
        if (!matching.GetProperty("machine_id_inputs_match").GetBoolean()
            || matching.GetProperty("machine_id_sha256_observed").GetString() != device.Fingerprint
            || matching.GetProperty("authorization_payload_observed").GetBoolean()) return false;
        data[32 + 132 + 15] ^= 1; // The last network byte must also be compared.
        var changed = JsonSerializer.SerializeToElement(ObservationResult(data, device, 1));
        return !changed.GetProperty("machine_id_inputs_match").GetBoolean()
            && changed.GetProperty("machine_id_sha256_observed").GetString() != device.Fingerprint;
    }
    static uint Start(string root, SavedDevice device, string exe, string entry)
    {
        var job = CreateJobObjectW(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new AuthFailure("device_launch");
        PROCESS_INFORMATION child = default;
        bool released = false;
        try {
            JobLimit(job, true);
            var startup = new STARTUPINFO { cb = Marshal.SizeOf<STARTUPINFO>() };
            if (!CreateProcessW(exe, new StringBuilder("\"" + exe + "\""), IntPtr.Zero, IntPtr.Zero, false,
                    0x00000004, IntPtr.Zero, root, ref startup, out child)) throw new AuthFailure("device_launch");
            if (!AssignProcessToJobObject(job, child.hProcess)) throw new AuthFailure("device_launch");
            var path = Encoding.Unicode.GetBytes(Dll + "\0");
            var remotePath = Allocate(child.hProcess, path);
            try { Remote(child.hProcess, GetProcAddress(GetModuleHandleW("kernel32.dll"), "LoadLibraryW"), remotePath); }
            finally { VirtualFreeEx(child.hProcess, remotePath, 0, 0x8000); }
            using var process = Process.GetProcessById((int)child.dwProcessId);
            var module = process.Modules.Cast<ProcessModule>().SingleOrDefault(m => string.Equals(m.FileName, Dll, StringComparison.OrdinalIgnoreCase));
            if (module is null) throw new AuthFailure("device_launch_module");
            var local = NativeLibrary.Load(Dll);
            var data = Allocate(child.hProcess, Data(root, device));
            try {
                var offset = NativeLibrary.GetExport(local, entry).ToInt64() - local.ToInt64();
                var result = Remote(child.hProcess, new IntPtr(module.BaseAddress.ToInt64() + offset), data);
                if (result == 3) throw new AuthFailure("device_late");
                if (result != 0) throw new AuthFailure("device_launch_install_" + result);
            }
            finally { VirtualFreeEx(child.hProcess, data, 0, 0x8000); NativeLibrary.Free(local); }
            // The caller owns this one suspend count. Keep the rollback job
            // attached until the primary thread has successfully resumed.
            if (ResumeThread(child.hThread) != 1) throw new AuthFailure("device_launch_resume");
            JobLimit(job, false);
            released = true;
            return child.dwProcessId;
        }
        finally {
            if (!released && child.hProcess != IntPtr.Zero) TerminateProcess(child.hProcess, 1);
            if (child.hThread != IntPtr.Zero) CloseHandle(child.hThread);
            if (child.hProcess != IntPtr.Zero) CloseHandle(child.hProcess);
            CloseHandle(job);
        }
    }
    static void JobLimit(IntPtr job, bool kill)
    {
        var limits = new byte[144]; // JOBOBJECT_EXTENDED_LIMIT_INFORMATION, Windows x64.
        BinaryPrimitives.WriteUInt32LittleEndian(limits.AsSpan(16), kill ? 0x2000u : 0u);
        if (!SetInformationJobObject(job, 9, limits, (uint)limits.Length)) throw new AuthFailure("device_launch");
    }
    static IntPtr Allocate(IntPtr process, byte[] data)
    {
        var address = VirtualAllocEx(process, IntPtr.Zero, (nuint)data.Length, 0x3000, 4);
        if (address == IntPtr.Zero) throw new AuthFailure("device_launch");
        if (!WriteProcessMemory(process, address, data, (nuint)data.Length, out var written) || written != (nuint)data.Length) {
            VirtualFreeEx(process, address, 0, 0x8000); throw new AuthFailure("device_launch");
        }
        return address;
    }
    static uint Remote(IntPtr process, IntPtr function, IntPtr data)
    {
        if (function == IntPtr.Zero) throw new AuthFailure("device_launch");
        var thread = CreateRemoteThread(process, IntPtr.Zero, 0, function, data, 0, IntPtr.Zero);
        if (thread == IntPtr.Zero) throw new AuthFailure("device_launch");
        try {
            if (WaitForSingleObject(thread, 20000) != 0 || !GetExitCodeThread(thread, out var code)) throw new AuthFailure("device_launch");
            return code;
        }
        finally { CloseHandle(thread); }
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct STARTUPINFO {
        public int cb; public IntPtr reserved, desktop, title;
        public uint x, y, xSize, ySize, xCount, yCount, fill, flags;
        public ushort show, cbReserved; public IntPtr reserved2, input, output, error;
    }
    [StructLayout(LayoutKind.Sequential)] struct PROCESS_INFORMATION {
        public IntPtr hProcess, hThread; public uint dwProcessId, dwThreadId;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool CreateProcessW(string exe, StringBuilder args, IntPtr processAttributes, IntPtr threadAttributes, bool inherit, uint flags, IntPtr env, string cwd, ref STARTUPINFO startup, out PROCESS_INFORMATION process);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr GetModuleHandleW(string name);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi)] static extern IntPtr GetProcAddress(IntPtr module, string name);
    [DllImport("kernel32.dll")] static extern IntPtr VirtualAllocEx(IntPtr process, IntPtr address, nuint size, uint type, uint protect);
    [DllImport("kernel32.dll")] static extern bool VirtualFreeEx(IntPtr process, IntPtr address, nuint size, uint type);
    [DllImport("kernel32.dll")] static extern bool WriteProcessMemory(IntPtr process, IntPtr address, byte[] data, nuint size, out nuint written);
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] data, nuint size, out nuint read);
    [DllImport("kernel32.dll")] static extern IntPtr CreateRemoteThread(IntPtr process, IntPtr attributes, nuint stack, IntPtr function, IntPtr data, uint flags, IntPtr id);
    [DllImport("kernel32.dll")] static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll")] static extern bool GetExitCodeThread(IntPtr thread, out uint code);
    [DllImport("kernel32.dll")] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] static extern IntPtr CreateJobObjectW(IntPtr attributes, string? name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int infoClass, byte[] data, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
}
