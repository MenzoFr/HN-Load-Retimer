# HN1 Load Retimer

Removes loading screens and retimes a run for HN1 or/and HN2.

## Download

Grab the latest release from the [Releases](../../releases) page it includes
the compiled exe and all dependencies. No Python installation needed.

## Running from source

1. Install [Python 3.9+](https://www.python.org/downloads/)
2. Download this repo
3. Install dependencies:
   ```
   pip install -U numpy pillow scipy customtkinter
   ```
4. Run:
   ```
   python retime.py
   ```

## Building from source

1. Install [Python 3.9+](https://www.python.org/downloads/)
2. Install dependencies:
   ```
   pip install -U numpy pillow scipy customtkinter pyinstaller
   ```
3. Place all dependency files into `dependencies/`:
   - `1.png`, `2.png`, `3.png`, `icon.ico`
   - `ffmpeg.exe`, `ffprobe.exe`, `yt-dlp.exe`, `deno.exe`
4. Build:
   ```
   pyinstaller --noconsole --onefile --icon=dependencies/icon.ico retime.py
   ```
5. The compiled exe will be at `dist/retime.exe`. Place it next to the `dependencies/` folder to use.

## Binaries (.exe dependencies)

This project requires external binaries for video downloading, processing, and frame detection. While pre-compiled versions are included in the repository, you can download the official, up-to-date versions directly from the sources below:

| Dependency | Purpose | Download Link |
| :--- | :--- | :--- |
| **`ffmpeg.exe`** | Video processing & frame extraction | [FFmpeg Official Download](https://ffmpeg.org/download.html) (Windows Build) |
| **`ffprobe.exe`** | Video metadata analysis | *Included with the FFmpeg download* |
| **`yt-dlp.exe`** | Video downloading | [yt-dlp Releases](https://github.com/yt-dlp/yt-dlp/releases) |
| **`deno.exe`** | JavaScript/TypeScript runtime | [Deno Official Site](https://deno.land/) |

## Usage

1. **Paste a YouTube link** or **browse for a local video file**
2. Choose your run type:
   - **Fullscreen run :** analyses the entire frame (for runs where the game takes the entire screen)
   - **Windowed run :** click *Select game area* and drag a box around the game window to ignore the desktop/background/bands
3. **Set your start and end times :** you can type them manually or click **Paste** to paste from clipboard
4. Click **Retime**

### Finding timestamps

You can get precise frame-accurate timestamps from:

- **YouTube** pause the video, use **`,`** to step back a frame and **`;`** to step forward. On the correct frame, right-click the video → **Copy debug info**, then paste it into the tool.
- **Frame-by-frame sites** like [somewes.com/frame-count](https://somewes.com/frame-count/) copy the "Video Time" and paste it directly.

Accepted formats: `1:32`, `0:01:32.500`, `92.5`, or YouTube debug info JSON.

## Notes

- The tool detects loading screens by matching template images (`1.png` / `2.png` / `3.png`) against every frame of the video.
- **Known limitations:** Latest Patch PC runs and Mobile runs are not supported.
- **Performance:** Retime speed depends entirely on your CPU. The video must be downloaded first, so internet speed also factors in.
- Downloaded videos are saved to a `Videos/` folder next to the exe.

## License

MIT
