"""Local browser search / fetch support (design: local_browser_search).

Importing this package never imports Playwright, opens a browser, touches the
network or calls a model. Playwright is loaded lazily inside
``mic.browser.session`` the first time a ``BrowserSession`` actually starts.
"""
