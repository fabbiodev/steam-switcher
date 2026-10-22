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

namespace SteamSDA.Auth.Authentication;

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
