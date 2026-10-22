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

namespace SteamSDA.Auth.Identity;

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
