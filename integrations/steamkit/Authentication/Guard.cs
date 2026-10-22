using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using SteamKit2;
using SteamKit2.Authentication;
using SteamKit2.Internal;

using SteamSDA.Auth.Authentication;
using SteamSDA.Auth.Identity;
using SteamSDA.Auth.Native;

namespace SteamSDA.Auth.Authentication;

sealed class Guard(string secret) : IAuthenticator
{
    public Task<bool> AcceptDeviceConfirmationAsync() => Task.FromResult(false);
    public Task<string> GetEmailCodeAsync(string email, bool previousCodeWasIncorrect) => throw new AuthFailure("needs_email_guard");
    public Task<string> GetDeviceCodeAsync(bool previousCodeWasIncorrect)
    {
        if (previousCodeWasIncorrect || string.IsNullOrEmpty(secret)) throw new AuthFailure("needs_device_guard");
        return Task.FromResult(Code(secret, DateTimeOffset.UtcNow.ToUnixTimeSeconds()));
    }
    public static string Code(string secret, long seconds)
    {
        var timestamp = new byte[8];
        BinaryPrimitives.WriteInt64BigEndian(timestamp, seconds / 30);
        var digest = HMACSHA1.HashData(Convert.FromBase64String(secret), timestamp);
        var offset = digest[^1] & 15;
        var number = BinaryPrimitives.ReadUInt32BigEndian(digest.AsSpan(offset)) & 0x7fffffff;
        const string alphabet = "23456789BCDFGHJKMNPQRTVWXY";
        var result = new StringBuilder(5);
        for (var i = 0; i < 5; i++) { result.Append(alphabet[(int)(number % 26)]); number /= 26; }
        return result.ToString();
    }
}
