import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Tuple, List, Optional
import platform
import urllib.parse

try:
    import yt_dlp  # type: ignore
    from yt_dlp.utils import DownloadError  # type: ignore
except ImportError:
    sys.stderr.write("Error: The 'yt-dlp' package is required. Install it with 'pip install yt-dlp'.\n")
    sys.exit(1)


ALLOWED_FORMATS = {"mp3", "wav", "aiff", "mp4"}
MEDIA_EXTENSIONS = {
    ".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".flv", ".avi",
    ".mov", ".wav", ".aac", ".opus", ".ogg", ".m4v",
}


def parse_time(timestr: str) -> float:
    """Convert a HH:MM:SS[.mmm] or MM:SS or SS string into seconds (float)."""
    parts = timestr.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid time format: '{timestr}'")

    try:
        parts = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"Invalid numeric value in time '{timestr}': {e}") from e

    if len(parts) == 1:  # SS
        seconds = parts[0]
    elif len(parts) == 2:  # MM:SS
        minutes, seconds = parts
        seconds = minutes * 60 + seconds
    else:  # HH:MM:SS
        hours, minutes, seconds = parts
        seconds = hours * 3600 + minutes * 60 + seconds
    return seconds


def sanitize_filename(filename: str) -> str:
    """清理文件名，移除或替换不安全的字符"""
    # 移除或替换文件名中不允许的字符
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # 移除多余的空格和点
    filename = re.sub(r'\s+', ' ', filename).strip()
    filename = filename.strip('.')
    # 限制文件名长度
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def apply_part_to_url(url: str, part: Optional[int]) -> str:
    """在 URL 中设置 Bilibili 分P 参数 ?p=N。"""
    if part is None:
        return url
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    query["p"] = [str(part)]
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query))


def resolve_video_duration(info: dict) -> Optional[float]:
    """从视频信息中解析时长，兼容分P合集。"""
    duration = info.get("duration")
    if duration is not None:
        return float(duration)

    entries = info.get("entries")
    if entries:
        first = entries[0]
        if first and first.get("duration") is not None:
            return float(first["duration"])
    return None


def site_origin(url: str) -> Optional[str]:
    """从 URL 解析站点 origin，例如 https://www.example.com。"""
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def build_ydl_opts(
    tmp_dir: Optional[Path],
    format_ext: str,
    cookies_from_browser: Optional[str],
    *,
    download: bool = True,
    noplaylist: bool = True,
    referer_url: Optional[str] = None,
) -> dict:
    """构建 yt-dlp 通用选项。"""
    if format_ext == "mp4":
        # 优先 HLS，部分站点（如 Pornhub）progressive 直链常 404/空文件
        format_spec = "best[height<=1080][protocol^=m3u8]/best[height<=1080]/best"
    else:
        format_spec = "bestaudio[protocol^=m3u8]/best[protocol^=m3u8]/bestaudio/best"

    opts = {
        "format": format_spec,
        "quiet": True,
        "no_warnings": True,
        "extractor_retries": 3,
        "fragment_retries": 3,
        "retries": 3,
        "noplaylist": noplaylist,
    }

    # 部分 CDN 要求 Referer，否则 m3u8 返回 412、progressive 返回空文件/404
    if referer_url:
        origin = site_origin(referer_url)
        if origin:
            opts["http_headers"] = {
                "Referer": f"{origin}/",
                "Origin": origin,
            }

    if cookies_from_browser and cookies_from_browser.lower() not in {"", "none", "off", "false"}:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)

    if download and tmp_dir is not None:
        opts["outtmpl"] = str(tmp_dir / "%(id)s.%(ext)s")
    return opts


def find_media_files(tmp_dir: Path) -> List[Path]:
    """在临时目录中查找已下载的媒体文件。"""
    files = []
    for path in tmp_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.endswith((".part", ".ytdl")):
            continue
        if path.suffix.lower() in MEDIA_EXTENSIONS:
            files.append(path)
    return sorted(files)


def locate_downloaded_file(tmp_dir: Path, ydl: yt_dlp.YoutubeDL, info: dict) -> str:
    """定位 yt-dlp 下载后的文件路径。"""
    candidate = ydl.prepare_filename(info)
    if os.path.exists(candidate):
        return candidate

    media_files = find_media_files(tmp_dir)
    if not media_files:
        raise RuntimeError(
            "下载未完成：临时目录中未找到媒体文件。"
            "若为 Bilibili 分P 视频，请在 URL 中添加 ?p=N 或使用 --part 指定分P。"
        )
    if len(media_files) > 1:
        names = ", ".join(f.name for f in media_files)
        raise RuntimeError(
            f"找到多个下载文件（{names}），无法确定使用哪一个。"
            "请使用 --part 指定分P，或在 URL 中添加 ?p=N。"
        )
    return str(media_files[0])


def format_ytdlp_error(error: Exception) -> str:
    """将 yt-dlp 异常转换为更易读的错误信息。"""
    message = str(error).strip()
    if "HTTP Error 412" in message:
        return (
            "站点拒绝了下载请求（HTTP 412）。"
            "可尝试：更新 yt-dlp；或使用 --cookies-from-browser 指定已打开该站点的浏览器；"
            "若已开启 cookies，可试 --cookies-from-browser none。"
        )
    if "downloaded file is empty" in message.lower() or "HTTP Error 404" in message:
        return (
            "下载文件为空或直链失效（常见于成人站点 progressive 地址）。"
            "请更新 yt-dlp 后重试；仍失败时可加 --cookies-from-browser chrome|safari，"
            "或 --cookies-from-browser none 关闭 cookies 再试。"
        )
    if "Sign in to confirm" in message:
        return (
            "YouTube 要求身份验证。"
            "请使用 --cookies-from-browser 指定已登录的浏览器（如 chrome、firefox）。"
        )
    return f"yt-dlp 下载失败: {message}"


def derive_output_path(output: str, video_title: str, ext: str) -> Path:
    """根据视频标题生成输出文件路径"""
    output_path = Path(output).expanduser().resolve()
    
    # 清理视频标题
    safe_title = sanitize_filename(video_title)
    filename = f"{safe_title}.{ext}"
    
    # 如果用户提供的是目录，在目录中创建文件
    if output_path.is_dir() or not output_path.suffix:
        output_path = output_path / filename
    else:
        # 如果用户提供的是文件路径，使用该路径但更新扩展名
        output_path = output_path.with_suffix(f'.{ext}')
        # 如果文件名不是基于视频标题的，则更新为基于标题的
        if not any(char in str(output_path.stem) for char in ['_', '-']):
            output_path = output_path.parent / filename

    # 确保父目录存在
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path


def is_retryable_download_error(error: Exception) -> bool:
    """判断是否值得去掉 cookies 后重试。"""
    message = str(error).lower()
    return any(
        token in message
        for token in (
            "downloaded file is empty",
            "http error 404",
            "http error 412",
            "unable to download video data",
        )
    )


def download_with_ydl(url: str, ydl_opts: dict, tmp_dir: Path) -> Tuple[dict, str]:
    """执行一次 yt-dlp 下载并定位文件。"""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return info, locate_downloaded_file(tmp_dir, ydl, info)


def download_media(
    url: str,
    tmp_dir: Path,
    format_ext: str,
    cookies_from_browser: Optional[str],
) -> Tuple[dict, str]:
    """下载媒体；cookies 场景下若直链失败，自动无 cookies 重试一次。"""
    ydl_opts = build_ydl_opts(tmp_dir, format_ext, cookies_from_browser, referer_url=url)
    try:
        return download_with_ydl(url, ydl_opts, tmp_dir)
    except DownloadError as first_error:
        cookies_enabled = bool(ydl_opts.get("cookiesfrombrowser"))
        if not cookies_enabled or not is_retryable_download_error(first_error):
            raise RuntimeError(format_ytdlp_error(first_error)) from first_error

        print("cookies 下载失败，尝试不使用 cookies 重试...", file=sys.stderr)
        retry_opts = build_ydl_opts(tmp_dir, format_ext, None, referer_url=url)
        try:
            return download_with_ydl(url, retry_opts, tmp_dir)
        except DownloadError as retry_error:
            raise RuntimeError(format_ytdlp_error(retry_error)) from retry_error


def download_video(
    url: str,
    tmp_dir: Path,
    fmt: str = "mp3",
    cookies_from_browser: str = "chrome",
) -> Tuple[str, str, str]:
    """Download the best audio/video stream using yt_dlp and return (file_path, video_id, video_title)."""
    info, downloaded_file = download_media(url, tmp_dir, fmt, cookies_from_browser)
    video_id = info.get("id") or "video"
    video_title = info.get("title") or "Unknown Title"
    return downloaded_file, video_id, video_title


def run_ffmpeg(input_file: str, start: float, duration: float, output_file: Path, fmt: str):
    """Invoke ffmpeg to cut the segment and convert to the requested format."""

    if fmt == "mp3":
        codec = "libmp3lame"
        extra = ["-b:a", "192k"]
        video_args = ["-vn"]  # no video
        audio_args = ["-acodec", codec]
    elif fmt == "wav":
        codec = "pcm_s16le"
        extra = ["-ac", "2", "-ar", "44100"]
        video_args = ["-vn"]  # no video
        audio_args = ["-acodec", codec]
    elif fmt == "aiff":
        codec = "pcm_s16be"
        extra = ["-ac", "2", "-ar", "44100"]
        video_args = ["-vn"]  # no video
        audio_args = ["-acodec", codec]
    elif fmt == "mp4":
        video_codec = "libx264"
        audio_codec = "aac"
        extra = ["-c:v", video_codec, "-c:a", audio_codec, "-preset", "medium", "-crf", "23"]
        video_args = []  # keep video
        audio_args = []
    else:
        raise ValueError(f"Unsupported format: {fmt}")

    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-y",  # overwrite without asking
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        input_file,
        *video_args,
        *audio_args,
        *extra,
        str(output_file),
    ]

    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        sys.stderr.write("Error: ffmpeg executable not found. Please install ffmpeg and ensure it is in your PATH.\n")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"ffmpeg failed with exit code {e.returncode}.\n")
        sys.exit(e.returncode)


def get_video_title_with_ytdlp(url: str) -> str:
    """使用yt-dlp命令行获取视频标题"""
    try:
        result = subprocess.run([
            'yt-dlp', '--get-title', '--no-warnings', url
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            title = result.stdout.strip()
            if title and 'youtube video #' not in title.lower():
                return title
        return None
    except Exception as e:
        print(f"yt-dlp获取标题失败: {e}", file=sys.stderr)
        return None


def get_video_info(url: str, cookies_from_browser: str, format_ext: str = "mp3") -> dict:
    """获取视频信息而不下载"""
    title_from_cmd = get_video_title_with_ytdlp(url)

    ydl_opts = build_ydl_opts(
        None, format_ext, cookies_from_browser, download=False, referer_url=url
    )
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as e:
        raise RuntimeError(format_ytdlp_error(e)) from e

    if title_from_cmd:
        info["title"] = title_from_cmd

    return info


def process_single_video(
    url: str,
    start: float,
    end: float,
    format_ext: str,
    output_dir: str,
    cookies_from_browser: str,
    part: Optional[int] = None,
) -> Path:
    """处理单个视频的下载和转换"""
    with tempfile.TemporaryDirectory() as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)

        url = apply_part_to_url(url, part)

        # Step 1: 先获取视频信息
        print(f"正在获取视频信息: {url}", file=sys.stderr)
        info = get_video_info(url, cookies_from_browser, format_ext)
        video_title = info.get("title") or "Unknown Title"
        duration_total = resolve_video_duration(info)

        print(f"视频标题: {video_title}", file=sys.stderr)
        if part is not None:
            print(f"分P: 第 {part} P", file=sys.stderr)

        # Step 2: 处理 start/end 默认值
        try:
            start_sec = start
            if end is not None:
                end_sec = end
            else:
                if duration_total is None:
                    raise RuntimeError(
                        "无法自动获取视频时长，请使用 --end 指定结束时间。"
                        "若为 Bilibili 分P 视频，可使用 --part 指定分P编号。"
                    )
                end_sec = float(duration_total)
        except ValueError as e:
            sys.stderr.write(str(e) + "\n")
            sys.exit(1)

        if end_sec <= start_sec:
            sys.stderr.write("Error: end time must be greater than start time.\n")
            sys.exit(1)

        duration = end_sec - start_sec

        # Step 3: 根据视频标题生成输出路径
        output_path = derive_output_path(output_dir, video_title, format_ext)

        # Step 4: 下载视频/音频（默认仅下载当前分P，不下载整个合集）
        print(f"正在下载视频: {url}", file=sys.stderr)
        _, downloaded_file = download_media(
            url, tmp_dir, format_ext, cookies_from_browser
        )

        # Step 5: Cut and convert using ffmpeg
        print(f"正在处理: {video_title}", file=sys.stderr)
        # 对于MP4格式，如果没有指定时间范围，直接复制文件
        if format_ext == "mp4" and start_sec == 0.0 and end_sec == float(duration_total):
            import shutil
            shutil.copy2(downloaded_file, output_path)
        else:
            run_ffmpeg(downloaded_file, start_sec, duration, output_path, format_ext)

        return output_path


def parse_url_list(url_list_str: str) -> List[str]:
    """解析URL列表字符串"""
    import urllib.parse
    # 移除方括号
    url_list_str = url_list_str.strip('[]')
    # 按逗号分割并清理每个URL
    urls = [url.strip() for url in url_list_str.split(',')]
    # 处理每个URL：去除反斜杠转义字符，并处理URL编码
    cleaned_urls = []
    for url in urls:
        if not url:
            continue
        # 先尝试URL解码（处理 %5C 等情况）
        try:
            url = urllib.parse.unquote(url)
        except Exception:
            pass
        # 去除反斜杠转义字符（如 \? 变成 ?，\= 变成 =，以及单独的 \）
        url = url.replace('\\?', '?').replace('\\=', '=').replace('\\', '')
        cleaned_urls.append(url)
    return cleaned_urls


def main():
    parser = argparse.ArgumentParser(
        description="Clip a segment from a YouTube or Bilibili video (or any site supported by yt-dlp) and convert it to the desired audio or video format.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", "-u", help="Video URL (YouTube, Bilibili, or any site supported by yt-dlp)")
    parser.add_argument("--list", "-l", help="List of video URLs in format [url1, url2, url3, ...]")
    parser.add_argument("--start", "-s", default=None, help="Clip start time (HH:MM:SS or MM:SS or SS[.ms]), default: 00:00:00")
    parser.add_argument("--end", "-e", default=None, help="Clip end time (HH:MM:SS or MM:SS or SS[.ms]), default: video end")
    parser.add_argument("--format", "-f", default="mp3", choices=sorted(ALLOWED_FORMATS), help="Output format (audio or video), default: mp3")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Path to the output directory or full file path where the file will be stored. Default: system Downloads directory.",
    )
    parser.add_argument(
        "--cookies-from-browser",
        default="chrome",
        help=(
            "从指定浏览器读取 cookies（chrome、firefox、safari、edge 等）。"
            "传 none 可关闭。默认: chrome"
        ),
    )
    parser.add_argument(
        "--part",
        type=int,
        default=None,
        help="Bilibili 分P 编号（如 1、2）。未指定时默认下载第 1 P。",
    )

    args = parser.parse_args()

    # 检查是否提供了URL或列表
    if not args.url and not args.list:
        parser.error("必须提供 --url 或 --list 参数")

    # 处理 output 默认值
    if args.output is not None:
        output_path_arg = args.output
    else:
        home = str(Path.home())
        sys_name = platform.system()
        if sys_name == "Windows":
            downloads = os.path.join(home, "Downloads")
        elif sys_name == "Darwin":
            downloads = os.path.join(home, "Downloads")
        else:
            # Linux: 兼容部分中文系统
            downloads = os.path.join(home, "Downloads")
            if not os.path.exists(downloads):
                downloads = os.path.join(home, "下载")
        output_path_arg = downloads

    # 处理时间参数
    try:
        start_sec = parse_time(args.start) if args.start is not None else 0.0
        end_sec = parse_time(args.end) if args.end is not None else None
    except ValueError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(1)

    # 确定要处理的URL列表
    urls_to_process = []
    if args.url:
        urls_to_process.append(args.url)
    if args.list:
        urls_to_process.extend(parse_url_list(args.list))

    # 处理每个URL
    successful_downloads = []
    failed_downloads = []

    for i, url in enumerate(urls_to_process, 1):
        try:
            print(f"\n处理第 {i}/{len(urls_to_process)} 个视频...", file=sys.stderr)
            output_path = process_single_video(
                url,
                start_sec,
                end_sec,
                args.format,
                output_path_arg,
                args.cookies_from_browser,
                part=args.part,
            )
            successful_downloads.append(output_path)
            print(f"✓ 成功: {output_path}")
        except Exception as e:
            print(f"✗ 失败: {url} - {str(e)}", file=sys.stderr)
            failed_downloads.append((url, str(e)))

    # 输出总结
    print(f"\n处理完成!")
    print(f"成功下载: {len(successful_downloads)} 个文件")
    if failed_downloads:
        print(f"失败: {len(failed_downloads)} 个文件")
        for url, error in failed_downloads:
            print(f"  - {url}: {error}")


def cli():
    """命令行入口点函数"""
    main()


if __name__ == "__main__":
    main()