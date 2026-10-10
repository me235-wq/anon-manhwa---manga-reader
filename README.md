# anon-manhwa---manga-reader

A small, local, web-based reader for manhwa/webtoons stored as CBZ or ZIP files. It does one thing: read. No downloads, no extensions, no database, and nothing to install beyond Python.

## Features

- Continuous vertical webtoon scrolling that stays smooth on long chapters
- Library grid with search and a "Continue reading" row
- Chapter list per series with read marks and newest/oldest sorting
- Saves your exact scroll position per series and resumes where you left off
- Adjustable reading width, with the setting remembered
- Works on desktop and phone (same Wi-Fi)

## Requirements

- Python 3.8 or newer
- Nothing else. The server uses only the Python standard library.
- Optional: `pip install pillow` creates small cover thumbnails so the library page loads faster.

## Quick start

1. Put `manhwa_reader.py` in a folder.
2. Create a `library` folder next to it and add your series (see below).
3. Run:

   ```
   python manhwa_reader.py
   ```

4. Your browser opens at <http://localhost:8000>. If it doesn't, open that address yourself.

Stop the server with `Ctrl+C`.

## Library layout

One folder per series, one `.cbz` or `.zip` per chapter:

```
library/
  Solo Leveling/
    cover.jpg            (optional)
    Chapter 1.cbz
    Chapter 2.cbz
    Chapter 10.cbz
  Tower of God/
    ch1.cbz
    ch2.cbz
```

- **Chapter order:** Names are sorted naturally, so `Chapter 2` comes before `Chapter 10`.
- **Page order:** Images inside each CBZ are sorted the same way. Supported formats: JPG, PNG, WebP, GIF, AVIF.
- **Covers:** A file named `cover`, `folder`, `poster` or `thumbnail` (any supported image type) in the series folder is used as the cover. Otherwise the first page of the first chapter is used.
- **New files:** Add chapters at any time and refresh the browser. No restart or rescan needed.

## Options

| Option | Description |
| --- | --- |
| `--lib PATH` | Use a different library folder (default: `library` next to the script) |
| `--port N` | Use a different port (default: `8000`) |
| `--lan` | Allow other devices on your network to connect, such as a phone or tablet |
| `--no-open` | Do not open the browser automatically |

Examples:

```
python manhwa_reader.py --lib "D:\Manhwa"
python manhwa_reader.py --port 8080
python manhwa_reader.py --lan
```

### Reading on your phone

1. Start the server with `--lan`.
2. Open the `Phone:` address printed in the terminal (for example `http://192.168.1.20:8000`) on a device connected to the same Wi-Fi.

`--lan` makes the reader reachable by anyone on that network, so use it only on networks you trust.

## Using the reader

- **Scroll** to read. Tap or click an image to show or hide the top bar. The bar also hides when you scroll down and returns when you scroll up.
- **‹ and ›** in the top bar jump to the previous and next chapter. The **Next chapter** button at the end of a chapter does the same.
- **⚙** opens the width slider.
- If an image fails to load, tap it to retry.
- On a series page, the main button continues from where you stopped. If you finished a chapter, it opens the next one.

## Your data

- **Reading progress** is stored in `progress.json`, next to `manhwa_reader.py`. Delete it to reset all progress. Back it up if you want to keep it.
- **Reading width** is stored in your browser.
- Your CBZ files are never modified.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Could not start on port 8000" | Another program is using that port. Run with `--port 8080`. |
| Library is empty | Chapters must be inside a series folder, not directly in `library`, and must end in `.cbz` or `.zip`. |
| "Not a valid CBZ/ZIP" error | The file is corrupt or not really a ZIP. Re-download or re-create it. |
| Phone can't connect | Use `--lan`, make sure both devices are on the same Wi-Fi, and allow Python through your firewall if asked. |
| Wrong page order | Rename images inside the archive so they sort correctly (`001.jpg`, `002.jpg`, ...). |
| Pages with no known size jump slightly | Some uncommon image formats (such as AVIF) don't report their size up front, so the page height is set once the image loads. |

## How it stays fast

- The server reads each image's width and height from its file header, so the browser can reserve the right space before anything loads. The scrollbar doesn't jump.
- Only images near the screen are loaded. Images far away are released to keep memory use low on very long chapters.
- Images are served directly from the CBZ and cached by the browser, so scrolling back up is instant.

## Limitations

- Webtoon-style vertical scrolling only (no paged or left/right mode).
- CBZ and ZIP only (no loose image folders, RAR/CBR or PDF).
- Intended for personal use on your own machine or home network. There is no login.
