"""Token renewal, account ownership checks and persistence policy."""
import time
from steam_sda.domain.accounts import claims, metadata, token_valid
from steam_sda.domain.errors import SteamError, AuthError
from .ports import AccountRepository, AuthenticationGateway


class AuthenticationService:
    def __init__(self, vault: AccountRepository, gateway: AuthenticationGateway, devices=None):
        self.vault, self.gateway = vault, gateway
        self.devices = devices

    def authenticate(self, sid, force=False, password_override=None):
        row = self.vault.get(sid)
        if not row:
            raise SteamError('Сначала импортируйте maFile или настройте вход аккаунта.')
        if self.devices:
            self.devices.ensure(row)
            row = self.vault.get(sid)
        session = row.get('session', {})
        mafile = row.get('mafile', {})
        mobile = mafile.get('Session') or {}
        refresh = session.get('refresh_token', '')
        expires = claims(refresh).get('exp', 0)
        password = row.get('password', '') if password_override is None else password_override
        can_relogin = bool(password or token_valid(mobile.get('RefreshToken', ''), sid, 'mobile', True)
                           or token_valid(mobile.get('AccessToken', ''), sid, 'mobile'))
        request = {**metadata(row), 'refresh_token': refresh, 'password': password,
                   'shared_secret': mafile.get('shared_secret', ''), 'guard_data': session.get('guard_data', ''),
                   'mobile_refresh_token': mobile.get('RefreshToken', ''), 'mobile_access_token': mobile.get('AccessToken', ''),
                   'force_new': bool(can_relogin and (force or type(expires) is not int or expires < time.time() + 7 * 86400))}
        if row.get('hwid'):
            request['hwid'] = row['hwid']
        response = self.gateway.authenticate(request)
        if response.get('ok') is not True:
            code = response.get('error', '')
            raise AuthError(code)
        if (response.get('steam_id') != sid or str(response.get('login', '')).casefold() != row['login'].casefold()
                or not token_valid(response.get('refresh_token', ''), sid, 'client', True)
                or not token_valid(response.get('access_token', ''), sid, 'client')):
            raise SteamError('SteamKit вернул сессию другого владельца или неподходящего типа.')
        # Read again so settings edited before this worker started are retained.
        row = self.vault.get(sid)
        row['session'] = {key: response.get(key, '') for key in ('refresh_token', 'access_token', 'guard_data')}
        row['last_check'], row['next_check'], row['auth_error'] = time.time(), time.time() + 3600, ''
        self.vault.save(row)
        return row
