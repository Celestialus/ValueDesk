<div align="center">

# ValueDesk

**Paste any URL and view its data in a clean desktop app, no browser needed.**

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## Overview

ValueDesk is a lightweight Python desktop application that fetches a web page, detects the useful data on it (tables, item lists, values) and shows it in a searchable, sortable table, all without opening a web browser.

It was built to quickly check value-list websites such as Blox Fruits value sites, but it works with any page that contains tabular or list-style data.

## Screenshots

> Add your screenshots here, for example:
>
> ![Main window](screenshots/main.png)

## Features

- **One-step fetching**: paste a URL, press Enter or click Fetch
- **Automatic data detection**: finds HTML tables and repeated list/card elements
- **Search and sort**: live filter box and click-to-sort column headers
- **Saved URLs**: keep your favorite pages in a dropdown for quick access
- **Auto-refresh**: re-fetch at a set interval and highlight changes (green = increased, red = decreased)
- **Export**: save the current table as CSV or JSON
- **Local cache**: previous results appear instantly while fresh data loads
- **JavaScript fallback**: uses a headless browser (no visible window) for pages that load content dynamically
- **Responsive UI**: network requests run in the background, so the app never freezes
- **Clear error messages**: invalid URLs, connection problems and blocked requests are explained instead of crashing the app

## Installation

### Option 1: Download the app (Windows)

1. Go to the [Releases](../../releases) page.
2. Download the latest `ValueDesk.exe`.
3. Double-click to run. No Python required.

### Option 2: Run from source

**Requirements:** Python 3.10 or newer

```bash
# Clone the repository
git clone https://github.com/Celestialus/ValueDesk.git
cd ValueDesk

# Install dependencies
pip install -r requirements.txt

# (Optional) Install the headless browser for JavaScript-heavy sites
playwright install chromium

# Run the app
python app.py
```

## Usage

1. Launch the app.
2. Paste a URL into the input box.
3. Press **Enter** or click **Fetch**.
4. Use the search box to filter results and click a column header to sort.
5. Click **Save** to add the URL to your favorites.
6. Enable **Auto-refresh** to monitor changes over time.
7. Use **Export** to save the data as CSV or JSON.

## Build your own .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --collect-all customtkinter --name ValueDesk app.py
```

The executable will be created in the `dist/` folder.

> If you use the Playwright fallback, the browser files are not bundled in the `.exe`. Run `playwright install chromium` once on the machine that will run the app.

## Troubleshooting

| Problem | Solution |
|---|---|
| Page returns no data | The site may load content with JavaScript. Make sure Chromium is installed with `playwright install chromium`. |
| `403` or `429` errors | The site is blocking or rate-limiting requests. Wait a while and increase the refresh interval. |
| Antivirus flags the `.exe` | This is a common false positive with PyInstaller builds. You can build it yourself from source. |
| App opens slowly | One-file executables unpack on first launch. Build with `--onedir` for faster startup. |

## Tech stack

- [Python](https://www.python.org/)
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) for the interface
- [Requests](https://requests.readthedocs.io/) for fetching pages
- [Beautiful Soup](https://www.crummy.com/software/BeautifulSoup/) and [pandas](https://pandas.pydata.org/) for parsing
- [Playwright](https://playwright.dev/python/) for the headless browser fallback

## Disclaimer

This tool is intended for personal use. Please respect the terms of service and `robots.txt` of the websites you access, and avoid sending excessive requests. The author is not responsible for how this tool is used.

## Contributing

Issues and pull requests are welcome. If you find a site that does not parse correctly, open an issue with the URL and a description of what you expected to see.

## License

Distributed under the MIT License. See [LICENSE](LICENSE) for details.
