# HN1 Load Retimer

Removes loading screens and retimes a run for HN1.

## Download

Grab the latest release from the [Releases](../../releases) page it includes
the compiled exe and all dependencies. No Python installation needed.

## Running from source

1. Install [Python 3.9+](https://www.python.org/downloads/)
2. Install dependencies:
   ```
   pip install -U numpy pillow scipy customtkinter
   ```
3. Download the required binaries into `dependencies/`:
   - [ffmpeg.exe](https://ffmpeg.org/download.html) (Windows build)
   - [ffprobe.exe](https://ffmpeg.org/download.html) (comes with ffmpeg)
   - [yt-dlp.exe](https://github.com/yt-dlp/yt-dlp/releases)
   - [deno.exe](https://deno.land/)
4. Run:
   ```
   python retime.py
   ```

## Usage

- Paste a YouTube link or browse for a local video file
- Choose **Fullscreen run** or **Windowed run**
  - Windowed: click *Select game area* and drag a box around the game window
- Set start and end times (or paste from clipboard)
- Click **Retime**

Downloaded videos are saved to a `Videos/` folder next to the exe.

## License

MIT
