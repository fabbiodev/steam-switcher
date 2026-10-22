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

sealed class SavedDevice : IMachineInfoProvider
{
    public string Guid { get; }
    public byte[] Network { get; }
    public string Disk { get; }
    public string Name { get; }
    public string Fqdn { get; }
    static string Field(JsonElement p, string key, int limit, bool empty = false)
    {
        var text = p.GetProperty(key).GetString() ?? "";
        if ((!empty && text.Length == 0) || Encoding.UTF8.GetByteCount(text) >= limit || text.Any(char.IsControl))
            throw new AuthFailure("device_input");
        return text;
    }
    public SavedDevice(JsonElement p)
    {
        if (p.GetProperty("version").GetInt32() != 1) throw new AuthFailure("device_input");
        Guid = Field(p, "machine_guid", 128);
        Disk = Field(p, "disk_serial", 512);
        Name = Field(p, "pc_name", 256);
        Fqdn = Field(p, "fqdn", 512, true);
        Network = Convert.FromHexString(p.GetProperty("network_id").GetString()!);
        if (Network.Length != 16) throw new AuthFailure("device_input");
    }
    public byte[] GetMachineGuid() => Encoding.UTF8.GetBytes(Guid);
    public byte[] GetMacAddress() => (byte[])Network.Clone();
    public byte[] GetDiskId() => Encoding.UTF8.GetBytes(Disk);
    public byte[] MachineId()
    {
        using var stream = new MemoryStream();
        stream.Write(Encoding.ASCII.GetBytes("\0MessageObject\0"));
        foreach (var pair in new[] { ("BB3", GetMachineGuid()), ("FF2", GetMacAddress()), ("3B3", GetDiskId()) })
            stream.Write(Encoding.ASCII.GetBytes("\x01" + pair.Item1 + "\0" + Convert.ToHexString(SHA1.HashData(pair.Item2)).ToLowerInvariant() + "\0"));
        stream.Write(new byte[] { 8, 8 });
        return stream.ToArray();
    }
    public string Fingerprint => Convert.ToHexString(SHA256.HashData(MachineId())).ToLowerInvariant();
    public CAuthentication_DeviceDetails Details(AuthSessionDetails details) => new() {
        device_friendly_name = Name, platform_type = details.PlatformType,
        os_type = (int)details.ClientOSType, machine_id = MachineId()
    };
}

// SteamKit 3.4.0 doesn't populate DeviceDetails.machine_id in QR/credentials.
// Use its typed service and polling sessions, adding the persisted identity.
static class DeviceAuth
{
    static T Session<T>(SteamClient client, IAuthenticator? guard, object response) =>
        (T)Activator.CreateInstance(typeof(T), BindingFlags.Instance | BindingFlags.NonPublic,
            null, new object?[] { client.Authentication, guard, response }, null)!;
    public static async Task<QrAuthSession> QR(SteamClient client, AuthSessionDetails details, SavedDevice? device)
    {
        if (device is null) return await client.Authentication.BeginAuthSessionViaQRAsync(details);
        var service = client.GetHandler<SteamUnifiedMessages>()!.CreateService<SteamKit2.Internal.Authentication>();
        var response = await service.BeginAuthSessionViaQR(new() { website_id = details.WebsiteID, device_details = device.Details(details) });
        if (response.Result != EResult.OK) throw new AuthFailure("steam_" + (int)response.Result);
        return Session<QrAuthSession>(client, details.Authenticator, response.Body);
    }
    public static async Task<CredentialsAuthSession> Credentials(SteamClient client, AuthSessionDetails details, SavedDevice? device)
    {
        if (device is null) return await client.Authentication.BeginAuthSessionViaCredentialsAsync(details);
        var service = client.GetHandler<SteamUnifiedMessages>()!.CreateService<SteamKit2.Internal.Authentication>();
        var key = await service.GetPasswordRSAPublicKey(new() { account_name = details.Username });
        if (key.Result != EResult.OK) throw new AuthFailure("steam_" + (int)key.Result);
        using var rsa = RSA.Create();
        rsa.ImportParameters(new RSAParameters { Modulus = Convert.FromHexString(key.Body.publickey_mod), Exponent = Convert.FromHexString(key.Body.publickey_exp) });
        var response = await service.BeginAuthSessionViaCredentials(new() {
            account_name = details.Username, website_id = details.WebsiteID, guard_data = details.GuardData,
            persistence = details.IsPersistentSession ? ESessionPersistence.k_ESessionPersistence_Persistent : ESessionPersistence.k_ESessionPersistence_Ephemeral,
            encrypted_password = Convert.ToBase64String(rsa.Encrypt(Encoding.UTF8.GetBytes(details.Password!), RSAEncryptionPadding.Pkcs1)),
            encryption_timestamp = key.Body.timestamp, device_details = device.Details(details)
        });
        if (response.Result != EResult.OK) throw new AuthFailure("steam_" + (int)response.Result);
        return Session<CredentialsAuthSession>(client, details.Authenticator, response.Body);
    }
    public static bool Test(SavedDevice device)
    {
        var config = SteamConfiguration.Create(b => b.WithMachineInfoProvider(device));
        var client = new SteamClient(config); // No connection/network.
        var qr = Session<QrAuthSession>(client, null, new CAuthentication_BeginAuthSessionViaQR_Response { challenge_url = "https://s.team/q/1/1", interval = 1 });
        var credentials = Session<CredentialsAuthSession>(client, null, new CAuthentication_BeginAuthSessionViaCredentials_Response { steamid = 76561198000000001, interval = 1 });
        // Verify the actual SteamKit serializer, not a duplicate hash implementation.
        var hardware = typeof(SteamClient).Assembly.GetType("SteamKit2.HardwareUtils")!;
        var raw = (byte[])hardware.GetMethod("GetMachineID", BindingFlags.Static | BindingFlags.Public)!.Invoke(null, new object[] { device })!;
        return raw.SequenceEqual(device.MachineId()) && qr.ChallengeURL.EndsWith("/1/1") && credentials.SteamID.ConvertToUInt64() == 76561198000000001;
    }
}

static class DeviceHost
{
    const int Size = 3476;
    const string ClientHash = "253e0a11a4415b38c78a42f7bdd6ecc426455f06c1ba6453a48090df3142dbbc";
    const string TierHash = "8559e6bd32eb3fddd95079633272866deb292211ec4f7e69aeb0ec48fdafef72";
    static string Dll => Path.Combine(AppContext.BaseDirectory, "SteamSDADevice.dll");
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
        var ok = Local("SteamSDA_Test", Data("C:\\SyntheticSteam", device)) == 0 && DeviceAuth.Test(device);
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
            suspended_process_test = pipeline };
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
