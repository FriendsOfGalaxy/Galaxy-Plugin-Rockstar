from galaxy.http import HttpClient

from consts import USER_AGENT

import aiohttp
import dataclasses
import dateutil.tz
import datetime
import logging as log
import pickle
import re
import requests
import traceback

from time import time
from yarl import URL


@dataclasses.dataclass
class Token(object):
    _token = None
    _expires = None

    def set_token(self, token, expiration):
        self._token, self._expires = token, expiration

    def get_token(self):
        return self._token

    def get_expiration(self):
        return self._expires

    @property
    def expired(self):
        return self._expires <= time()


class CookieJar(aiohttp.CookieJar):
    def __init__(self):
        super().__init__()
        self._cookies_updated_callback = None

    def set_cookies_updated_callback(self, callback):
        self._cookies_updated_callback = callback

    def update_cookies(self, cookies, url=URL()):
        super().update_cookies(cookies, url)
        if cookies and self._cookies_updated_callback:
            self._cookies_updated_callback(list(self))


class AuthenticatedHttpClient(HttpClient):
    def __init__(self, store_credentials):
        self._debug_always_refresh = False  # Set this to True if you are debugging ScAuthTokenData refreshing.
        self._store_credentials = store_credentials
        self.bearer = None
        # The refresh token here is the RMT cookie. The other refresh token is the rsso cookie. The RMT cookie is blank
        # for users not using two-factor authentication.
        self.refresh_token = Token()
        self._fingerprint = None
        self.user = None
        local_time_zone = dateutil.tz.tzlocal()
        self._utc_offset = local_time_zone.utcoffset(datetime.datetime.now(local_time_zone)).total_seconds() / 60
        self._current_session = None
        self._cookie_jar = CookieJar()
        self._cookie_jar_ros = CookieJar()
        self._auth_lost_callback = None
        self._current_auth_token = None
        self._first_auth = True
        super().__init__(cookie_jar=self._cookie_jar)

    def get_credentials(self):
        creds = self.user
        # It might seem strange to store the entire session object in hexadecimal, rather than just the session's
        # cookies. However, keeping the session object intact is necessary in order to allow ScAuthTokenData to be
        # successfully authenticated. My guess is that the ScAuthTokenData uses some form of browser fingerprinting,
        # as using a value from Chrome on Firefox returned an error. Likewise, creating a new session object, rather
        # than reimplementing the old session object, returns an error when using a correct ScAuthTokenData value, even
        # if the two sessions have equivalent cookies.
        creds['session_object'] = pickle.dumps(self._current_session).hex()
        creds['current_auth_token'] = self._current_auth_token
        creds['refresh_token'] = pickle.dumps(self.refresh_token).hex()
        creds['fingerprint'] = self._fingerprint
        return creds

    def set_cookies_updated_callback(self, callback):
        self._cookie_jar.set_cookies_updated_callback(callback)

    def update_cookie(self, cookie):
        # I believe that the cookie beginning with rsso gets a different name occasionally, so we need to delete the old
        # rsso cookie using regular expressions if we want to ensure that the refresh token can continue to be obtained.
        if re.search("^rsso", cookie['name']):
            for old_cookie in self._current_session.cookies:
                old_rsso_search = re.search("^rsso", old_cookie.name)
                if old_rsso_search:
                    del self._current_session.cookies[old_rsso_search.string]
        del self._current_session.cookies[cookie['name']]
        self._current_session.cookies.set(**cookie)

    def set_auth_lost_callback(self, callback):
        self._auth_lost_callback = callback

    def is_authenticated(self):
        return self.user is not None and self._auth_lost_callback is None

    def set_current_auth_token(self, token):
        self._current_auth_token = token

    def get_current_auth_token(self):
        return self._current_auth_token

    def get_named_cookie(self, cookie_name):
        return self._current_session.cookies[cookie_name]

    def get_rockstar_id(self):
        return self.user["rockstar_id"]

    def set_refresh_token(self, token):
        expiration_time = time() + (3600 * 24 * 365 * 20)
        self.refresh_token.set_token(token, expiration_time)

    def set_refresh_token_absolute(self, token):
        self.refresh_token = token

    def get_refresh_token(self):
        if self.refresh_token.expired:
            log.debug("ROCKSTAR_REFRESH_EXPIRED: The refresh token has expired!")
            self.refresh_token.set_token(None, None)
        return self.refresh_token.get_token()

    def set_fingerprint(self, fingerprint):
        self._fingerprint = fingerprint

    def is_fingerprint_defined(self):
        return self._fingerprint is not None

    def create_session(self, stored_credentials):
        if stored_credentials is None:
            self._current_session = requests.Session()
            self._current_session.max_redirects = 300
        elif self._current_session is None:
            self._current_session = pickle.loads(bytes.fromhex(stored_credentials['session_object']))
            self._current_session.cookies.clear()

    # Side Note: The following method is meant to ensure that the access (bearer) token continues to remain relevant.
    async def do_request(self, method, *args, **kwargs):
        try:
            return await self.request(method, *args, **kwargs)
        except Exception as e:
            log.warning(f"WARNING: The request failed with exception {repr(e)}. Attempting to refresh credentials...")
            self.set_auth_lost_callback(True)
            await self.authenticate()
            return await self.request(method, *args, **kwargs)

    async def get_json_from_request_strict(self, url, include_default_headers=True, additional_headers=None):
        headers = additional_headers if additional_headers is not None else {}
        if include_default_headers:
            headers["Authorization"] = f"Bearer {await self._get_bearer()}"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["User-Agent"] = USER_AGENT
        try:
            s = requests.Session()
            resp = s.get(url, headers=headers)
            return resp.json()
        except Exception as e:
            log.warning(f"WARNING: The request failed with exception {repr(e)}. Attempting to refresh credentials...")
            self.set_auth_lost_callback(True)
            await self.authenticate()
            return await self.get_json_from_request_strict(url, include_default_headers, additional_headers)

    async def get_bearer_from_cookie_jar(self):
        morsel_list = self._cookie_jar.__iter__()
        cookies = {}
        for morsel in morsel_list:
            cookies[morsel.key] = morsel.value
        log.debug(cookies)
        return cookies['BearerToken']

    async def get_cookies_for_headers(self):
        cookie_string = ""
        for key, value in self._current_session.cookies.get_dict().items():
            cookie_string += "" + str(key) + "=" + str(value) + ";"
            # log.debug("ROCKSTAR_CURR_COOKIE: " + cookie_string)
        return cookie_string[:len(cookie_string) - 1]

    async def _get_user_json(self, message=None):
        try:
            old_auth = self._current_session.cookies['ScAuthTokenData']
            log.debug(f"ROCKSTAR_OLD_AUTH: {str(old_auth)[:5]}***{str(old_auth[-3:])}")
            headers = {
                "accept": "application/json, text/plain, */*",
                "connection": "keep-alive",
                "cookie": "ScAuthTokenData=" + self._current_auth_token,
                "host": "www.rockstargames.com",
                "referer": "https://www.rockstargames.com",
                "user-agent": USER_AGENT
            }
            resp = self._current_session.get(r"https://www.rockstargames.com/auth/get-user.json", headers=headers,
                                             allow_redirects=False, timeout=5)
            new_auth = self._current_session.cookies['ScAuthTokenData']
            log.debug(f"ROCKSTAR_NEW_AUTH {str(new_auth)[:5]}***{str(new_auth[-3:])}")
            self._current_auth_token = new_auth
            if new_auth != old_auth:
                log.warning("ROCKSTAR_AUTH_CHANGE: The ScAuthTokenData value has changed!")
                if self.user is not None:
                    self._store_credentials(self.get_credentials())
            return resp.json()
        except Exception as e:
            if message is not None:
                log.error(message)
            else:
                log.error("ROCKSTAR_USER_JSON_ERROR: The request to get the get-user.json file resulted in this"
                          " exception: " + repr(e))
            traceback.print_exc()
            raise

    async def _get_bearer(self):
        try:
            resp_json = await self._get_user_json()
            log.debug("ROCKSTAR_AUTH_JSON: " + str(resp_json))
            new_bearer = resp_json["user"]["auth_token"]["access_token"]
            self.bearer = new_bearer
            self.refresh = resp_json["user"]["auth_token"]["refresh_token"]
            return new_bearer
        except Exception as e:
            log.error("ERROR: The request to refresh credentials resulted in this exception: " + repr(e))
            raise

    async def get_played_games(self):
        try:
            resp_json = await self._get_user_json()
            return_list = []
            played_games = resp_json["user"]["games_played"]
            for game in played_games:
                if game["platform"] == "PC":
                    return_list.append(game["id"])
            return return_list
        except Exception as e:
            log.error("ROCKSTAR_PLAYED_GAMES_ERROR: The request to scrape the user's played games resulted in "
                      "this exception: " + repr(e))
            raise

    async def refresh_credentials(self):
        # It seems like the Rockstar website connects to https://signin.rockstargames.com/connect/cors/check/rsg via a
        # POST request in order to re-authenticate the user. This request uses a fingerprint as form data.

        # This POST request then returns a message with a code, which is then sent as a request payload to
        # https://www.rockstargames.com/auth/login.json in the form of {code: "{code}"}. Note that {code} includes
        # its own set of quotation marks, so it looks like {code: ""{Numbers/Letters}""}.

        # Finally, this last request updates the cookies that are used for further authentication.
        try:
            url = "https://signin.rockstargames.com/connect/cors/check/rsg"
            rsso_cookie = {}
            rsso_name = None
            for cookie in self._current_session.cookies:
                if re.search("^rsso", cookie.name):
                    rsso_name = cookie.name
                    rsso_cookie[cookie.name] = cookie.value
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Cookie": "RMT=" + self.get_refresh_token() + "; " + rsso_name + "=" + rsso_cookie[rsso_name],
                "Content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Host": "signin.rockstargames.com",
                "Origin": "https://www.rockstargames.com",
                "User-Agent": USER_AGENT,
                "X-Requested-With": "XMLHttpRequest"
            }
            data = {"fingerprint": self._fingerprint}
            refresh_resp = self._current_session.post(url, data=data, headers=headers, timeout=5)
            refresh_code = refresh_resp.text
            log.debug("ROCKSTAR_REFRESH_CODE: Got code " + refresh_code + "!")
            # We need to set the new refresh token here, if it is updated.
            if "RMT" in self._current_session.cookies:
                self.set_refresh_token(self._current_session.cookies['RMT'])
            else:
                log.debug("ROCKSTAR_RMT_MISSING: The RMT cookie is missing, presumably because the user has not enabled"
                          " two-factor authentication. Proceeding anyways...")
                self.set_refresh_token('')
            # The Social Club API will not grant the user a new ScAuthTokenData token if they already have one that is
            # relevant, so when refreshing the credentials, the old token is deleted here.
            old_auth = self._current_session.cookies['ScAuthTokenData']
            del self._current_session.cookies['ScAuthTokenData']
            self._current_auth_token = None
            log.debug("ROCKSTAR_OLD_AUTH_REFRESH: " + old_auth)
            url = "https://www.rockstargames.com/auth/login.json"
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Cookie": "TS019978c2=" + self._current_session.cookies['TS019978c2'],
                "Content-type": "application/json",
                "Host": "www.rockstargames.com",
                "Referer": "https://www.rockstargames.com/",
                "User-Agent": USER_AGENT
            }
            data = {"code": refresh_code}
            # log.debug("ROCKSTAR_CODE: " + data['code'])
            final_request = self._current_session.post(url, json=data, headers=headers, timeout=5)
            # log.debug("ROCKSTAR_SENT_CODE: " + str(final_request.request.body))
            final_json = final_request.json()
            log.debug("ROCKSTAR_REFRESH_JSON: " + str(final_json))
            new_auth = self._current_session.cookies['ScAuthTokenData']
            self._current_auth_token = new_auth
            log.debug("ROCKSTAR_NEW_AUTH_REFRESH: " + new_auth)
            if old_auth != new_auth:
                log.debug("ROCKSTAR_REFRESH_SUCCESS: The user has been successfully re-authenticated!")
        except Exception as e:
            log.debug("ROCKSTAR_REFRESH_FAILURE: The attempt to re-authenticate the user has failed with the exception "
                      + repr(e) + ". Logging the user out...")
            traceback.print_exc()
            raise

    async def authenticate(self):
        if self._auth_lost_callback or self._debug_always_refresh:
            # We need to refresh the credentials.
            await self.refresh_credentials()
            self._auth_lost_callback = None
        self.bearer = await self._get_bearer()
        log.debug("ROCKSTAR_HTTP_CHECK: Got bearer token: " + self.bearer)

        # With the bearer token, we can now access the profile information.

        url = "https://scapi.rockstargames.com/profile/getbasicprofile"
        headers = {
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,"
                       "application/signed-exchange;application/json;v=b3"),
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US, en;q=0.9",
            "Authorization": f"Bearer {self.bearer}",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "dnt": "1",
            "Host": "scapi.rockstargames.com",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": USER_AGENT
        }
        try:
            s = requests.Session()
            s.trust_env = False
            resp_user = s.get(url, headers=headers)
            resp_user_text = resp_user.json()
        except Exception as e:
            log.error(
                "ERROR: There was a problem with getting the user information with the token. Exception: " + repr(e))
            traceback.print_exc()
            raise
        log.debug(resp_user_text)
        working_dict = resp_user_text['accounts'][0]['rockstarAccount']  # The returned json is a nightmare.
        display_name = working_dict['displayName']
        rockstar_id = working_dict['rockstarId']
        log.debug("ROCKSTAR_HTTP_CHECK: Got display name: " + display_name + " / Got Rockstar ID: " + str(rockstar_id))
        self.user = {"display_name": display_name, "rockstar_id": rockstar_id}
        log.debug("ROCKSTAR_STORE_CREDENTIALS: Preparing to store credentials...")
        # log.debug(self.get_credentials()) - Reduce Console Spam (Enable this if you need to.)
        self._store_credentials(self.get_credentials())
        return self.user
