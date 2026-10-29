"""Deterministic file curation: which downloaded files belong in the media library."""
import re
from pathlib import PurePosixPath

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".wmv", ".flv", ".webm", ".mpg", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup"}
MIB = 1024 * 1024
# A promotional name alone is not enough: some releases prefix the real film with a site tag.
AD_SIZE_RATIO = 0.3
EXTRA_SIZE_RATIO = 0.5
AD_RE = re.compile(
    r"https?://|www\.|(?<![a-z0-9])[a-z0-9-]{2,}\.(?:com|net|org|cc|tv|me|xyz|top|vip|cn|info|club|site|app|la|io|co)(?![a-z0-9])"
    r"|更多.{0,16}(?:资源|电影|影视|剧集|高清|原盘|下载)|请访问|访问.{0,8}(?:网站|官网)|(?:最新|永久|备用)(?:地址|网址|域名)"
    r"|公众号|扫码|加群|q群|微信|免费(?:下载|观看)|无水印|广告", re.I)
EXTRA_RE = re.compile(
    r"(?<![a-z0-9])(?:sample|trailers?|teasers?|featurettes?|behind[ ._-]?the[ ._-]?scenes|deleted[ ._-]?scenes?"
    r"|making[ ._-]?of|bloopers?|nc[ ._-]?op\d*|nc[ ._-]?ed\d*|creditless|menu\d*|pv\d*|cm\d*|yokoku|preview|bonus|extras?)"
    r"(?![a-z])|预告|予告|花絮|特典|彩蛋|片花|样片|幕后|特辑", re.I)
EXTRA_DIRS = re.compile(r"^(?:extras?|featurettes?|samples?|trailers?|bonus|sps?|menus?|cds?|scans?|特典|映像特典|花絮|预告)$", re.I)
PROMOTIONAL_NOTE = "文件名含推广信息，按体积判断为正片"
ROLE_LABELS = {"video": "正片", "subtitle": "字幕", "ad": "广告", "extra": "花絮／特典", "sample": "样片／过小",
               "other": "非影视文件"}


def kind_of(path):
    ext = PurePosixPath(path).suffix.lower()
    return "video" if ext in VIDEO_EXTENSIONS else "subtitle" if ext in SUBTITLE_EXTENSIONS else "other"


def classify(files):
    """Return {path: {role, keep, reason}} for [{path, size}] using names, folders and relative size."""
    largest = max((f["size"] for f in files if kind_of(f["path"]) == "video"), default=0)
    result = {}
    for f in files:
        path, size = f["path"], f["size"]
        p = PurePosixPath(path)
        kind = kind_of(path)
        if kind == "other":
            result[path] = {"role": "other", "keep": False, "reason": "非影视文件，不进入媒体库"}
            continue
        if kind == "subtitle":
            result[path] = {"role": "subtitle", "keep": True, "reason": ""}
            continue
        promotional = bool(AD_RE.search(p.stem))
        if promotional and largest and size < largest * AD_SIZE_RATIO:
            result[path] = {"role": "ad", "keep": False, "reason": "文件名含网址或推广信息且体积远小于正片，判定为广告"}
        elif (EXTRA_RE.search(p.stem) or any(EXTRA_DIRS.match(part) for part in p.parts[:-1])) and size < largest * EXTRA_SIZE_RATIO:
            result[path] = {"role": "extra", "keep": False, "reason": "样片、预告、花絮或特典，不进入正片目录"}
        elif largest and (size < largest * 0.02 or (size < 30 * MIB and size < largest * 0.2)):
            result[path] = {"role": "sample", "keep": False, "reason": "体积远小于正片，疑似广告或样片"}
        else:
            result[path] = {"role": "video", "keep": True,
                            "reason": PROMOTIONAL_NOTE if promotional else ""}
    return result
