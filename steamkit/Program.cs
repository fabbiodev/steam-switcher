using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using SteamKit2;
using SteamKit2.Authentication;
using SteamKit2.Internal;

// Private IPC only: credentials arrive on stdin, never command-line arguments.
try
{
    if (args.Contains("--self-test"))
    {
        var code = Guard.Code(Convert.ToBase64String(new byte[20]), 1700000000);
        Console.WriteLine(JsonSerializer.Serialize(new { ok = code.Length == 5, backend = "SteamKit2", version = typeof(SteamClient).Assembly.GetName().Version?.ToString() }));
        return;
    }
    var input = await Console.In.ReadLineAsync();
    if (input is null || input.Length > 100000) throw new AuthFailure("invalid_input");
    using var document = JsonDocument.Parse(input);
    var request = document.RootElement;
    var sid = Text(request, "steam_id");
    var login = Text(request, "login");
    if (!ulong.TryParse(sid, out var id) || id <= 76561197960265728UL || id > 76561202255233023UL || !Regex.IsMatch(login, "^[a-zA-Z0-9_]{1,64}$"))
        throw new AuthFailure("invalid_input");
    var refresh = Text(request, "refresh_token");
    var password = Text(request, "password");
    var secret = Text(request, "shared_secret");
    var forceNew = request.TryGetProperty("force_new", out var force) && force.ValueKind == JsonValueKind.True;
    var guardData = Text(request, "guard_data");
    object? result = null;
    if (!forceNew && Token(refresh, sid, "client", true))
    {
        try
        {
            using var connection = new Connection();
            await connection.Connect();
            await connection.LogOn(login, refresh, id);
            var updated = await connection.Client.Authentication.GenerateAccessTokenForAppAsync(new SteamID(id), refresh, allowRenewal: false).WaitAsync(TimeSpan.FromSeconds(25));
            result = Result(sid, login, updated.AccessToken, string.IsNullOrEmpty(updated.RefreshToken) ? refresh : updated.RefreshToken, guardData);
        }
        catch (AuthenticationException) { /* An invalid durable token can be replaced using the owner's authenticator. */ }
        catch (AuthFailure e) when (e.Code == "token_rejected") { }
    }
    if (result is null)
    {
        var mobileRefresh = Text(request, "mobile_refresh_token");
        var mobileAccess = Text(request, "mobile_access_token");
        if (secret.Length > 0 && (Token(mobileRefresh, sid, "mobile", true) || Token(mobileAccess, sid, "mobile", false)))
        {
            try
            {
                using var api = WebAPI.GetAsyncInterface("IAuthenticationService");
                api.Timeout = TimeSpan.FromSeconds(20);
                if (!Token(mobileAccess, sid, "mobile", false, 120))
                {
                    var response = await api.CallProtobufAsync<CAuthentication_AccessToken_GenerateForApp_Response, CAuthentication_AccessToken_GenerateForApp_Request>(HttpMethod.Post, "GenerateAccessTokenForApp",
                        new() { refresh_token = mobileRefresh, steamid = id });
                    if (response.Result != EResult.OK) throw new AuthFailure("mobile_token_rejected");
                    mobileAccess = response.Body.access_token;
                }
                if (!Token(mobileAccess, sid, "mobile", false)) throw new AuthFailure("mobile_token_rejected");
                using var connection = new Connection();
                await connection.Connect();
                var qr = await connection.Client.Authentication.BeginAuthSessionViaQRAsync(new AuthSessionDetails { IsPersistentSession = true, DeviceFriendlyName = "Steam SDA", PlatformType = EAuthTokenPlatformType.k_EAuthTokenPlatformType_SteamClient });
                var match = Regex.Match(qr.ChallengeURL, @"^https://s\.team/q/(\d{1,5})/(\d{1,20})$");
                if (!match.Success) throw new AuthFailure("unexpected_challenge");
                var version = ushort.Parse(match.Groups[1].Value);
                var clientId = ulong.Parse(match.Groups[2].Value);
                var info = await api.CallProtobufAsync<CAuthentication_GetAuthSessionInfo_Response, CAuthentication_GetAuthSessionInfo_Request>(HttpMethod.Post, "GetAuthSessionInfo",
                    new() { client_id = clientId }, extraArgs: new() { ["access_token"] = mobileAccess });
                if (info.Result != EResult.OK) throw new AuthFailure("mobile_token_rejected");
                if (info.Body.platform_type != EAuthTokenPlatformType.k_EAuthTokenPlatformType_SteamClient || (info.Body.version != 0 && info.Body.version != version))
                    throw new AuthFailure("unexpected_challenge");
                // Approve only the QR session created above, never an external QR.
                var signed = new byte[18];
                BinaryPrimitives.WriteUInt16LittleEndian(signed, version);
                BinaryPrimitives.WriteUInt64LittleEndian(signed.AsSpan(2), clientId);
                BinaryPrimitives.WriteUInt64LittleEndian(signed.AsSpan(10), id);
                var signature = HMACSHA256.HashData(Convert.FromBase64String(secret), signed);
                var approval = await api.CallProtobufAsync<CAuthentication_UpdateAuthSessionWithMobileConfirmation_Response, CAuthentication_UpdateAuthSessionWithMobileConfirmation_Request>(HttpMethod.Post, "UpdateAuthSessionWithMobileConfirmation",
                    new() { version = version, client_id = clientId, steamid = id, signature = signature, confirm = true },
                    extraArgs: new() { ["access_token"] = mobileAccess });
                if (approval.Result != EResult.OK) throw new AuthFailure("mobile_token_rejected");
                var authenticated = await qr.PollingWaitForResultAsync().WaitAsync(TimeSpan.FromSeconds(35));
                if (!authenticated.AccountName.Equals(login, StringComparison.OrdinalIgnoreCase) || !Token(authenticated.RefreshToken, sid, "client", true)) throw new AuthFailure("owner_mismatch");
                await connection.LogOn(login, authenticated.RefreshToken, id);
                var verified = await connection.Client.Authentication.GenerateAccessTokenForAppAsync(new SteamID(id), authenticated.RefreshToken).WaitAsync(TimeSpan.FromSeconds(25));
                result = Result(sid, login, verified.AccessToken, authenticated.RefreshToken, authenticated.NewGuardData ?? guardData);
            }
            catch (AuthFailure e) when (e.Code == "mobile_token_rejected" && password.Length > 0) { }
            catch (SteamKitWebRequestException e) when (password.Length > 0 && e.StatusCode is System.Net.HttpStatusCode.Unauthorized or System.Net.HttpStatusCode.Forbidden) { }
        }
        if (result is null && password.Length > 0)
        {
            using var connection = new Connection();
            await connection.Connect();
            var auth = await connection.Client.Authentication.BeginAuthSessionViaCredentialsAsync(new AuthSessionDetails {
                Username = login, Password = password, IsPersistentSession = true,
                GuardData = guardData, DeviceFriendlyName = "Steam SDA",
                Authenticator = new Guard(secret)
            }).WaitAsync(TimeSpan.FromSeconds(25));
            var authenticated = await auth.PollingWaitForResultAsync().WaitAsync(TimeSpan.FromSeconds(35));
            if (!authenticated.AccountName.Equals(login, StringComparison.OrdinalIgnoreCase) || !Token(authenticated.RefreshToken, sid, "client", true)) throw new AuthFailure("owner_mismatch");
            await connection.LogOn(login, authenticated.RefreshToken, id);
            var verified = await connection.Client.Authentication.GenerateAccessTokenForAppAsync(new SteamID(id), authenticated.RefreshToken).WaitAsync(TimeSpan.FromSeconds(25));
            result = Result(sid, login, verified.AccessToken, authenticated.RefreshToken, authenticated.NewGuardData ?? guardData);
        }
        if (result is null) throw new AuthFailure("needs_credentials");
    }
    Console.WriteLine(JsonSerializer.Serialize(result));
}
catch (AuthFailure e) { Console.WriteLine(JsonSerializer.Serialize(new { ok = false, error = e.Code })); }
catch (AuthenticationException e) { Console.WriteLine(JsonSerializer.Serialize(new { ok = false, error = "steam_" + (int)e.Result })); }
catch (SteamKitWebRequestException e) when (e.StatusCode is System.Net.HttpStatusCode.Unauthorized or System.Net.HttpStatusCode.Forbidden) { Console.WriteLine("{\"ok\":false,\"error\":\"mobile_token_rejected\"}"); }
catch (SteamKitWebRequestException e) when ((int)e.StatusCode == 429) { Console.WriteLine("{\"ok\":false,\"error\":\"steam_84\"}"); }
catch (TimeoutException) { Console.WriteLine("{\"ok\":false,\"error\":\"timeout\"}"); }
catch (Exception) { Console.WriteLine("{\"ok\":false,\"error\":\"unavailable\"}"); }

static string Text(JsonElement element, string key) => element.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : "";
static object Result(string sid, string login, string access, string refresh, string guard) => new { ok = true, steam_id = sid, login, access_token = access, refresh_token = refresh, guard_data = guard };
static bool Token(string token, string sid, string audience, bool refresh, int margin = 0)
{
    try
    {
        if (token.Length > 32768) return false;
        var parts = token.Split('.');
        if (parts.Length != 3) return false;
        var raw = parts[1].Replace('-', '+').Replace('_', '/');
        using var doc = JsonDocument.Parse(Convert.FromBase64String(raw.PadRight((raw.Length + 3) / 4 * 4, '=')));
        var data = doc.RootElement;
        var aud = data.GetProperty("aud").EnumerateArray().Select(x => x.GetString()).ToArray();
        return data.GetProperty("sub").GetString() == sid && aud.Contains(audience) && aud.Contains("derive") == refresh && data.GetProperty("exp").GetInt64() > DateTimeOffset.UtcNow.ToUnixTimeSeconds() + margin;
    }
    catch { return false; }
}

sealed class AuthFailure(string code) : Exception { public string Code { get; } = code; }

sealed class Connection : IDisposable
{
    public SteamClient Client { get; } = new();
    readonly CallbackManager callbacks;
    readonly TaskCompletionSource connected = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly TaskCompletionSource<SteamUser.LoggedOnCallback> loggedOn = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly CancellationTokenSource cancel = new();
    readonly Task pump;
    public Connection()
    {
        callbacks = new(Client);
        callbacks.Subscribe<SteamClient.ConnectedCallback>(_ => connected.TrySetResult());
        callbacks.Subscribe<SteamUser.LoggedOnCallback>(x => loggedOn.TrySetResult(x));
        callbacks.Subscribe<SteamClient.DisconnectedCallback>(_ => {
            connected.TrySetException(new AuthFailure("disconnected"));
            loggedOn.TrySetException(new AuthFailure("disconnected"));
        });
        pump = Task.Run(() => { while (!cancel.IsCancellationRequested) callbacks.RunWaitCallbacks(TimeSpan.FromMilliseconds(100)); });
    }
    public async Task Connect() { Client.Connect(); await connected.Task.WaitAsync(TimeSpan.FromSeconds(25)); }
    public async Task LogOn(string login, string token, ulong expected)
    {
        Client.GetHandler<SteamUser>()!.LogOn(new() { Username = login, AccessToken = token, ShouldRememberPassword = true, LoginID = (uint)RandomNumberGenerator.GetInt32(1, int.MaxValue) });
        var response = await loggedOn.Task.WaitAsync(TimeSpan.FromSeconds(25));
        if (response.Result != EResult.OK)
        {
            if (response.Result is EResult.InvalidPassword or EResult.AccessDenied or EResult.Expired or EResult.Revoked)
                throw new AuthFailure("token_rejected");
            throw new AuthFailure("steam_" + (int)response.Result);
        }
        if (response.ClientSteamID?.ConvertToUInt64() != expected) throw new AuthFailure("owner_mismatch");
    }
    public void Dispose() { Client.Disconnect(); cancel.Cancel(); pump.Wait(TimeSpan.FromSeconds(2)); cancel.Dispose(); }
}

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
