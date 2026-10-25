"""One-command build for Windows:   py build.py

1. installs the Python packages
2. builds dist\\Sahm\\Sahm.exe with PyInstaller
3. downloads yt-dlp, FFmpeg and Deno (cached in vendor_tools\\) and puts them in dist\\Sahm\\tools
4. makes dist\\Sahm-Portable.zip (no-install version)
5. builds dist\\Sahm-Setup.exe with Inno Setup (if installed)
"""
import os
import sys
import shutil
import zipfile
import subprocess
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(ROOT, "vendor_tools")
DIST_TOOLS = os.path.join(ROOT, "dist", "Sahm", "tools")

TOOLS = {
    "yt-dlp.exe": ("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe", None),
    "ffmpeg.exe": ("https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
                   "ffmpeg-master-latest-win64-gpl.zip", ("ffmpeg.exe", "ffprobe.exe")),
    "deno.exe": ("https://github.com/denoland/deno/releases/latest/download/"
                 "deno-x86_64-pc-windows-msvc.zip", ("deno.exe",)),
}

NOTICE = """These programs are included unmodified to provide video downloads:

yt-dlp  - Unlicense (public domain)            https://github.com/yt-dlp/yt-dlp
FFmpeg  - GPL v3, source code available at     https://github.com/yt-dlp/FFmpeg-Builds
                                               https://ffmpeg.org/download.html
Deno    - MIT license                          https://github.com/denoland/deno
"""


def step(msg):
    print("\n=== " + msg, flush=True)


def run(args):
    subprocess.check_call(args, cwd=ROOT)


def download(url, dest):
    def hook(blocks, bs, total):
        if total > 0:
            pct = min(100, blocks * bs * 100 // total)
            print(f"\r    {os.path.basename(dest)}: {pct}%  of {total / 1e6:.0f} MB", end="", flush=True)
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp, hook)
    os.replace(tmp, dest)
    print()


def fetch_tools():
    os.makedirs(VENDOR, exist_ok=True)
    for main_exe, (url, members) in TOOLS.items():
        if os.path.exists(os.path.join(VENDOR, main_exe)):
            print(f"    {main_exe}: already downloaded")
            continue
        if members is None:
            download(url, os.path.join(VENDOR, main_exe))
            continue
        zpath = os.path.join(VENDOR, os.path.basename(url))
        download(url, zpath)
        with zipfile.ZipFile(zpath) as z:
            for m in z.namelist():
                base = os.path.basename(m).lower()
                if base in members:
                    with z.open(m) as src, open(os.path.join(VENDOR, base), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        os.remove(zpath)
    with open(os.path.join(VENDOR, "THIRD-PARTY-NOTICES.txt"), "w") as f:
        f.write(NOTICE)


def find_iscc():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        if base:
            p = os.path.join(base, "Inno Setup 6", "ISCC.exe")
            if os.path.exists(p):
                return p
    return shutil.which("ISCC")


def main():
    py = sys.executable
    step("Installing Python packages")
    run([py, "-m", "pip", "install", "-r", "requirements.txt"])

    step("Building Sahm.exe")
    run([py, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "Sahm",
         "--icon", os.path.join("app", "assets", "icon.ico"),
         "--add-data", os.path.join("app", "assets") + os.pathsep + "assets",
         "--paths", "app", os.path.join("app", "main.py")])

    step("Downloading video tools (yt-dlp, FFmpeg, Deno) - first build only")
    fetch_tools()
    os.makedirs(DIST_TOOLS, exist_ok=True)
    for f in os.listdir(VENDOR):
        shutil.copy2(os.path.join(VENDOR, f), os.path.join(DIST_TOOLS, f))

    step("Making the portable zip")
    portable = os.path.join(ROOT, "dist", "Sahm-Portable")
    shutil.make_archive(portable, "zip", root_dir=os.path.join(ROOT, "dist"), base_dir="Sahm")
    print("    dist\\Sahm-Portable.zip")

    iscc = find_iscc()
    if iscc:
        step("Building the installer")
        run([iscc, "installer.iss"])
        print("\nDONE!  Installer: dist\\Sahm-Setup.exe")
    else:
        print("\nDONE!  App: dist\\Sahm\\Sahm.exe")
        print("Install Inno Setup 6 (https://jrsoftware.org/isdl.php) and run again to get Sahm-Setup.exe")


if __name__ == "__main__":
    main()
