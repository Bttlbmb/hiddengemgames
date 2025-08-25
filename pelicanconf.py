from datetime import datetime
CURRENTYEAR = datetime.now().year

AUTHOR = "Max"
SITENAME = "Hidden Gem Games"
SITEURL = ""                 # set only in publishconf.py

PATH = "content"
ARTICLE_PATHS = ["", "posts"]

MENUITEMS = [
    ("About", "/pages/about.html"),   # adjust path if your About page differs
    ("Past Games", "/archives.html"), # Pelican’s built-in archive page
]

TIMEZONE = "Europe/Berlin"
DEFAULT_LANG = "en"
THEME = "notmyidea"          # built-in, simple

DEFAULT_PAGINATION = 10

THEME = "themes/hgg"
