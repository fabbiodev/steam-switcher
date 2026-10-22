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

sealed class Connection : IDisposable
{
    public SteamClient Client { get; }
    readonly SavedDevice? device;
    readonly CallbackManager callbacks;
    readonly TaskCompletionSource connected = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly TaskCompletionSource<SteamUser.LoggedOnCallback> loggedOn = new(TaskCreationOptions.RunContinuationsAsynchronously);
    readonly CancellationTokenSource cancel = new();
    readonly Task pump;
    public Connection(SavedDevice? device)
    {
        this.device = device;
        Client = device is null ? new SteamClient() : new SteamClient(SteamConfiguration.Create(b => b.WithMachineInfoProvider(device)));
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
        Client.GetHandler<SteamUser>()!.LogOn(new() { Username = login, AccessToken = token, MachineName = device?.Name, ShouldRememberPassword = true, LoginID = (uint)RandomNumberGenerator.GetInt32(1, int.MaxValue) });
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
