"""侧栏浏览器放 H.264 / AAC 视频：转码成 WebM 再交给页面。

PySide6 自带的 QtWebEngine 不带专有编解码器（实测 canPlayType：H.264、AAC、HEVC 都是空，
VP8 / VP9 / AV1 / Opus / MP3 能放）。网页里的 mp4 广告、演示视频一律「播不了」，模型对着
「暂时没接通」反复点播放（2026-11-24 网页小游戏实测，十来轮）。

做法：请求拦截器（`MediaInterceptor`）把页面里**直接给文件的** mp4 / m4v / mov / m4a / aac
（媒体类请求，按地址后缀认）改道到灵犀自己的 `lxmedia:` 协议；协议处理器（`MediaHandler`）
在后台线程下载原文件（带上页面地址做 Referer），用 imageio-ffmpeg 自带的 ffmpeg 转成
WebM（VP8 + Opus），转好后把文件交回给页面。转好的按地址缓存在本进程的临时目录里，
关灵犀时删掉。

管不到的：
- 分段推流（MSE，B 站主站、YouTube 这类）——数据是页面脚本自己拉、自己喂给解码器的，
  拦不到也没法换格式；那些站点会自己判断不支持 H.264 再换格式或提示。
- 没装 imageio-ffmpeg（可选依赖）时不拦，和以前一样播不了。

`lxmedia:` 注册成安全协议（https 页面里用不算混合内容）、允许跨域读取（页面用 fetch / 带
crossorigin 的 <video> 也能拿到）。协议要在创建任何 QtWebEngine 对象之前注册
（`register_scheme`，page._get_profile 第一次建 profile 前调）。
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse

import shiboken6
from PySide6.QtCore import QObject, QUrl, Signal

from ..paths import logger

SCHEME = b"lxmedia"
# 按地址后缀认要转的：只拦这几种（H.264 / AAC 最常见的容器）。webm / mp3 / ogg 原样放行
_EXTS = (".mp4", ".m4v", ".mov", ".m4a", ".aac")
_MAX_BYTES = 300 * 1024 * 1024          # 原文件最大 300MB（再大的多半是正片，不在这里转）
_DOWNLOAD_TIMEOUT_S = 60
_TRANSCODE_TIMEOUT_S = 180

_registered = {"done": False}
_cache_dir = {"path": None}
_lock = threading.Lock()
_inflight: dict = {}                    # 缓存键 → threading.Event（同一个文件只转一次）


_ffmpeg = {"exe": None}


def ffmpeg_exe() -> str:
    """imageio-ffmpeg 自带的 ffmpeg（带 libvpx / libopus）；没装返回 ""。

    直接找它包里 binaries 目录下的那个文件，不用 imageio_ffmpeg.get_ffmpeg_exe()：后者要先跑一次
    `ffmpeg -version` 验证、而且不给子进程 stdin——Windows 上父进程没有有效的标准句柄时
    （打包后的窗口程序、pytest 捕获输出时）这一步就失败，还把失败缓存下来，转码整个不生效。"""
    if _ffmpeg["exe"] is None:
        exe = ""
        try:
            import glob
            import imageio_ffmpeg
            found = sorted(glob.glob(os.path.join(os.path.dirname(imageio_ffmpeg.__file__),
                                                  "binaries", "ffmpeg*")))
            exe = next((f for f in found if os.path.isfile(f) and not f.endswith(".md")), "")
        except Exception:
            exe = ""
        _ffmpeg["exe"] = exe
    return _ffmpeg["exe"]


def register_scheme():
    """登记 lxmedia: 协议。必须在创建任何 QtWebEngine 对象（profile / page）之前调，晚了被忽略。"""
    if _registered["done"]:
        return
    from PySide6.QtWebEngineCore import QWebEngineUrlScheme
    s = QWebEngineUrlScheme(SCHEME)
    s.setSyntax(QWebEngineUrlScheme.Syntax.Path)
    s.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.CorsEnabled)
    QWebEngineUrlScheme.registerScheme(s)
    _registered["done"] = True


def wants_transcode(url: QUrl) -> bool:
    """这个媒体地址要不要转：http(s)、路径以 mp4 / m4v / mov / m4a / aac 结尾。"""
    if url.scheme() not in ("http", "https"):
        return False
    return url.path().lower().endswith(_EXTS)


def media_url(original: str, referer: str = "") -> QUrl:
    """原地址 → lxmedia: 地址（原地址和页面地址放在查询参数里）。"""
    q = urllib.parse.urlencode({"u": original, "r": referer})
    return QUrl(f"{SCHEME.decode()}:/v?{q}")


def _parse(url: QUrl):
    q = urllib.parse.parse_qs(url.query(QUrl.ComponentFormattingOption.FullyEncoded))
    return (q.get("u") or [""])[0], (q.get("r") or [""])[0]


def _cache_root() -> str:
    with _lock:
        if _cache_dir["path"] is None:
            _cache_dir["path"] = tempfile.mkdtemp(prefix="lingxi-media-")
            atexit.register(shutil.rmtree, _cache_dir["path"], True)
        return _cache_dir["path"]


def _cache_path(original: str) -> str:
    return os.path.join(_cache_root(), hashlib.sha1(original.encode("utf-8")).hexdigest() + ".webm")


def transcode(original: str, referer: str = "") -> str:
    """下载 original、转成 WebM，返回转好的文件路径（有缓存直接用）。失败抛 RuntimeError。
    后台线程里调（下载 + 转码要几秒）。"""
    out = _cache_path(original)
    with _lock:
        if os.path.exists(out):
            return out
        ev = _inflight.get(out)
        owner = ev is None
        if owner:
            ev = _inflight[out] = threading.Event()
    if not owner:
        ev.wait(_DOWNLOAD_TIMEOUT_S + _TRANSCODE_TIMEOUT_S)
        if os.path.exists(out):
            return out
        raise RuntimeError("同一个视频的另一次转码失败了")
    try:
        exe = ffmpeg_exe()
        if not exe:
            raise RuntimeError("没装 imageio-ffmpeg，转不了")
        src = out[:-5] + ".src"
        _download(original, referer, src)
        part = out + ".part"
        # 宽度压到 854 以内、用最快的实时档：一段 37 秒的 1280×720 广告 5.4s → 1.3s（实测），
        # 网页里的视频多半显示在一个小框里，看不出差别
        cmd = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
               "-vf", "scale='min(854,iw)':-2",
               "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "16", "-b:v", "2M",
               "-c:a", "libopus", "-b:a", "96k", "-f", "webm", part]
        r = subprocess.run(cmd, capture_output=True, timeout=_TRANSCODE_TIMEOUT_S,
                           stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            os.remove(src)
        except OSError:
            pass
        if r.returncode != 0 or not os.path.exists(part):
            err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg 转码失败：{err or r.returncode}")
        os.replace(part, out)
        return out
    finally:
        with _lock:
            _inflight.pop(out, None)
        ev.set()


def _download(url: str, referer: str, dest: str):
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    if referer:
        headers["Referer"] = referer
    with requests.get(url, headers=headers, stream=True, timeout=_DOWNLOAD_TIMEOUT_S) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"下载视频失败：HTTP {resp.status_code}")
        size = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                size += len(chunk)
                if size > _MAX_BYTES:
                    raise RuntimeError("视频文件太大（超过 300MB），不转码")
                f.write(chunk)


def _interceptor_class():
    from PySide6.QtWebEngineCore import QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor

    class MediaInterceptor(QWebEngineUrlRequestInterceptor):
        """页面里的媒体请求（<video> / <audio> 直接给的文件）是 H.264 / AAC 容器就改道去转码。"""

        def interceptRequest(self, info):
            if info.resourceType() != QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMedia:
                return
            url = info.requestUrl()
            if not wants_transcode(url) or not ffmpeg_exe():
                return
            info.redirect(media_url(url.toString(), info.firstPartyUrl().toString()))

    return MediaInterceptor


class _Done(QObject):
    finished = Signal(object, str, str)     # (job, 文件路径, 错误)


def _handler_class():
    from PySide6.QtCore import QFile, QIODevice
    from PySide6.QtWebEngineCore import QWebEngineUrlRequestJob, QWebEngineUrlSchemeHandler

    class MediaHandler(QWebEngineUrlSchemeHandler):
        """lxmedia: 请求：后台下载 + 转码，转好后在主线程把文件交给页面。"""

        def __init__(self, parent=None):
            super().__init__(parent)
            self._done = _Done(self)
            self._done.finished.connect(self._reply)
            self._jobs = set()           # 主线程持有 job 的 Python 包装，转码期间别被回收

        def requestStarted(self, job):
            original, referer = _parse(job.requestUrl())
            if not original.startswith(("http://", "https://")):
                job.fail(QWebEngineUrlRequestJob.Error.UrlInvalid)
                return
            self._jobs.add(job)

            def work():
                try:
                    path, err = transcode(original, referer), ""
                except Exception as e:
                    path, err = "", str(e)
                    logger.warning(f"[浏览器] 视频转码失败 {original}: {e}")
                self._done.finished.emit(job, path, err)

            threading.Thread(target=work, daemon=True, name="lingxi-media").start()

        def _reply(self, job, path, err):
            self._jobs.discard(job)
            if not shiboken6.isValid(job):
                return                    # 页面已经不要了（换页 / 关标签）
            if err or not path:
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
                return
            f = QFile(path, job)          # 跟着 job 走：job 销毁时文件句柄一起关
            if not f.open(QIODevice.OpenModeFlag.ReadOnly):
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
                return
            job.reply(b"video/webm", f)

    return MediaHandler


def install(profile):
    """给 profile 装上拦截器和 lxmedia: 处理器（page._get_profile 里调一次）。"""
    interceptor = _interceptor_class()(profile)
    handler = _handler_class()(profile)
    profile.setUrlRequestInterceptor(interceptor)
    profile.installUrlSchemeHandler(SCHEME, handler)
    return interceptor, handler
