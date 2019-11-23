import sys

from config_parser import init_config_options


class NoLogFoundException(Exception):
    pass


class NoGamesInLogException(Exception):
    pass


ARE_ACHIEVEMENTS_IMPLEMENTED = False

CONFIG_OPTIONS = init_config_options()

LOG_SENSITIVE_DATA = CONFIG_OPTIONS['log_sensitive_data'].get()

MANIFEST_URL = r"https://gamedownloads-rockstargames-com.akamaized.net/public/title_metadata.json"

IS_WINDOWS = (sys.platform == 'win32')

ROCKSTAR_LAUNCHERPATCHER_EXE = "LauncherPatcher.exe"
ROCKSTAR_LAUNCHER_EXE = "Launcher.exe"  # It's a terribly generic name for a launcher.

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/76.0.3809.132 "
              "Safari/537.36")

WINDOWS_UNINSTALL_KEY = "SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"

AUTH_PARAMS = {
    "window_title": "Login to Rockstar Games Social Club",
    "window_width": 700,
    "window_height": 600,
    "start_uri": "https://www.rockstargames.com/auth/scauth-login",
    "end_uri_regex": r"https://scapi.rockstargames.com/profile/getbasicprofile"
                    # r"https://www.rockstargames.com/auth/get-user.json.*"
}
