"""从 Apple 官方直取 macOS 恢复镜像。

协议实现源于开源项目 1401（nullmoth）的 p1401/apple.py，它本身改写自 OpenCorePkg 的
macrecovery.py（Copyright (c) 2019 vit9696, BSD-3-Clause）。这里保留它真正有价值的两个安全设计：

  1. chunklist 必须用 Apple EFI ROM key 验签；签名方式为 2（裸 sha256，谁都能算）直接拒收；
  2. 镜像边下边按 chunk 校验 sha256，中途断网用 Range 续传，续传的字节同样要过校验;
     且整个流 renormalize 完才把 .part 改名为正式文件 —— 半成品永远不会看起来"已完成"。

工具本身不托管、不镜像、不附带任何 Apple 文件，全部走 http://osrecovery.apple.com。
"""
import hashlib
import http.client
import os
import random
import socket
import ssl
import string
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60
MLB_ZERO = "00000000000000000"      # 通用主板序列号，Apple 会返回该机型最新的恢复镜像
UA = "InternetRecovery/1.0"

# Darwin 内核主版本号 -> (macOS 名称, board-id)。注意 Darwin 版本号比 macOS 版本号小 1：
# macOS 26 Tahoe 的内核是 Darwin 25。board-id 沿用 Dortania 安装指南推荐的那些。
TARGETS = {
    25: ("macOS 26 Tahoe", "Mac-CFF7D910A743CAAF"),
    24: ("macOS 15 Sequoia", "Mac-937A206F2EE63C01"),
    23: ("macOS 14 Sonoma", "Mac-827FAC58A8FDFA22"),
    22: ("macOS 13 Ventura", "Mac-4B682C642B45593E"),
    21: ("macOS 12 Monterey", "Mac-FFE5EF870D7BA81A"),
    20: ("macOS 11 Big Sur", "Mac-42FD25EABCABB274"),
    19: ("macOS 10.15 Catalina", "Mac-00BE6ED71E35EB86"),
}
MACOS_NAME = {25: "26 Tahoe", 24: "15 Sequoia", 23: "14 Sonoma", 22: "13 Ventura",
              21: "12 Monterey", 20: "11 Big Sur", 19: "10.15 Catalina"}

APPLE_EFI_ROM_KEY_1 = int(
    "C3E748CAD9CD384329E10E25A91E43E1A762FF529ADE578C935BDDF9B13F2179D4855E6FC89E9E29CA12517D17DFA1EDCE0BEBF0EA7B461FFE"
    "61D94E2BDF72C196F89ACD3536B644064014DAE25A15DB6BB0852ECBD120916318D1CCDEA3C84C92ED743FC176D0BACA920D3FCF3158AFF731"
    "F88CE0623182A8ED67E650515F75745909F07D415F55FC15A35654D118C55A462D37A3ACDA08612F3F3F6571761EFCCBCC299AEE99B3A4FD62"
    "12CCFFF5EF37A2C334E871191F7E1C31960E010A54E86FA3F62E6D6905E1CD57732410A3EB0C6B4DEFDABE9F59BF1618758C751CD56CEF851D"
    "1C0EAA1C558E37AC108DA9089863D20E2E7E4BF475EC66FE6B3EFDCF", 16)

HEADER = struct.Struct("<4sIBBBxQQQ")
CHUNK = struct.Struct("<I32s")

OPEN_TRIES = 4      # 瞬时故障重试次数
RESUMES = 8         # 允许的续传次数


class RecoveryError(RuntimeError):
    pass


def ssl_context():
    """优先用系统证书库，拿不到再退化到 urllib 默认策略。"""
    try:
        import certifi  # type: ignore
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    return ctx


_CTX = None
_opener = None


def _get_opener():
    global _CTX, _opener
    if _opener is None:
        _CTX = ssl_context()
        _opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
    return _opener


def _transient(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (500, 502, 503, 504)
    return isinstance(e, (urllib.error.URLError, http.client.HTTPException,
                          ConnectionError, TimeoutError, socket.timeout))


def _open(url, headers, data=None, sleep=None):
    req = urllib.request.Request(url, headers={"Connection": "close", "User-Agent": UA, **headers}, data=data)
    last = None
    for attempt in range(OPEN_TRIES):
        try:
            return _get_opener().open(req, timeout=TIMEOUT)
        except Exception as e:
            last = e
            if attempt + 1 < OPEN_TRIES and _transient(e):
                (sleep or time.sleep)(2 ** (attempt + 1))
                continue
            break
    raise RecoveryError(f"{url}: {type(last).__name__}: {last}") from last


def _hex(n):
    return "".join(random.choices(string.hexdigits[:16].upper(), k=n))


def session():
    r = _open("http://osrecovery.apple.com/", {"Host": "osrecovery.apple.com"})
    try:
        for k, v in r.headers.items():
            if k.lower() == "set-cookie":
                for c in v.split("; "):
                    if c.startswith("session="):
                        return c
    finally:
        try:
            r.close()
        except Exception:
            pass
    raise RecoveryError("Apple 未返回 session cookie")


def image_info(darwin_major, sess=None):
    """问 Apple 这个 macOS 版本对应哪个恢复镜像，只取元数据不下载。"""
    if darwin_major not in TARGETS:
        raise RecoveryError(f"没有 Darwin {darwin_major} 对应的恢复镜像")
    name, board = TARGETS[darwin_major]
    post = {"cid": _hex(16), "sn": MLB_ZERO, "bid": board, "k": _hex(64), "fg": _hex(64), "os": "latest"}
    r = _open("http://osrecovery.apple.com/InstallationPayload/RecoveryImage",
              {"Host": "osrecovery.apple.com", "Cookie": sess or session(), "Content-Type": "text/plain"},
              "\n".join(f"{k}={v}" for k, v in post.items()).encode())
    try:
        info = dict(l.split(": ", 1) for l in r.read().decode().splitlines() if ": " in l)
    finally:
        try:
            r.close()
        except Exception:
            pass
    missing = [k for k in ("AP", "AU", "AH", "AT", "CU", "CH", "CT") if k not in info]
    if missing:
        raise RecoveryError(f"Apple 对 {board} 的应答缺少字段 {missing}")
    return {"name": name, "board": board, "product": info["AP"], "image_url": info["AU"],
            "image_token": info["AT"], "chunklist_url": info["CU"], "chunklist_token": info["CT"]}


def _asset(url, token, extra=None):
    return _open(url, {"Host": urllib.parse.urlparse(url).hostname,
                       "Cookie": "AssetToken=" + token, **(extra or {})})


def parse_chunklist(data):
    """验签并返回 [(chunk_size, sha256), ...]。任何一步不对就抛错，决不带病往下走。"""
    if len(data) < HEADER.size:
        raise RecoveryError("chunklist 被截断")
    magic, hsize, ver, cmethod, smethod, count, coff, soff = HEADER.unpack_from(data)
    if (magic, hsize, ver, cmethod, coff) != (b"CNKL", HEADER.size, 1, 1, HEADER.size) or count <= 0 \
            or soff != coff + CHUNK.size * count:
        raise RecoveryError(f"不是合法的 Apple chunklist (magic={magic!r} version={ver})")
    if smethod != 1:
        # 方式 2 是任何人都能算的裸 sha256，证明不了这玩意是 Apple 签的
        raise RecoveryError(f"chunklist 签名方式 {smethod} 未经 Apple 签名，拒绝")
    body, sig = data[:soff], data[soff:]
    if len(sig) != 256:
        raise RecoveryError(f"chunklist 签名长度异常：{len(sig)}")
    digest = hashlib.sha256(body).digest()
    want = int("1" + "f" * 404 + "003031300d060960864801650304020105000420" + "0" * 64, 16) | int.from_bytes(digest, "big")
    if pow(int.from_bytes(sig, "little"), 0x10001, APPLE_EFI_ROM_KEY_1) != want:
        raise RecoveryError("chunklist 签名无法通过 Apple EFI ROM key 校验，拒绝")
    return [CHUNK.unpack_from(data, coff + i * CHUNK.size) for i in range(count)]


def fetch_chunklist(info):
    r = _asset(info["chunklist_url"], info["chunklist_token"])
    try:
        return r.read()
    finally:
        try:
            r.close()
        except Exception:
            pass


def probe(darwin_major, sess=None):
    """轻量连通性体检：取元数据 + chunklist + 第 0 个 chunk，不真下载几个 GB。"""
    info = image_info(darwin_major, sess)
    chunks = parse_chunklist(fetch_chunklist(info))
    size0, sha0 = chunks[0]
    r = _asset(info["image_url"], info["image_token"], {"Range": f"bytes=0-{size0 - 1}"})
    try:
        c0 = _read_exact(r, size0)
    finally:
        try:
            r.close()
        except Exception:
            pass
    return {**info, "chunks": len(chunks), "bytes": sum(s for s, _ in chunks),
            "chunk0_ok": hashlib.sha256(c0).digest() == sha0}


def _resume(info, offset, sleep=time.sleep):
    sleep(2)
    r = _asset(info["image_url"], info["image_token"], {"Range": f"bytes={offset}-"})
    got = (r.headers.get("Content-Range") or "") if getattr(r, "headers", None) else ""
    if getattr(r, "status", None) != 206 or not got.startswith(f"bytes {offset}-"):
        try:
            r.close()
        except Exception:
            pass
        raise RecoveryError(f"Apple 服务器拒绝从 {offset} 字节续传")
    return r


def _read_exact(r, n):
    parts, got = [], 0
    while got < n:
        b = r.read(min(n - got, 1 << 20))
        if not b:
            raise RecoveryError(f"数据流在 {got}/{n} 字节处中断")
        parts.append(b)
        got += len(b)
    return b"".join(parts)


def download(info, dest_root, progress=None, cancel=None):
    """把恢复镜像流式写入 <dest_root>/com.apple.recovery.boot/。

    这是 Windows 下唯一可行的安装器布局：FAT32 里放 com.apple.recovery.boot，
    macOS 恢复系统启动后从 Apple 服务器拉取完整安装包，我们不需要外挂任何 Apple 文件。
    """
    dest = os.path.join(dest_root, "com.apple.recovery.boot")
    os.makedirs(dest, exist_ok=True)
    cl = fetch_chunklist(info)
    chunks = parse_chunklist(cl)
    total = sum(s for s, _ in chunks)
    img = os.path.join(dest, os.path.basename(urllib.parse.urlparse(info["image_url"]).path))
    cnk = os.path.splitext(img)[0] + ".chunklist"
    done = 0
    resumes = 0
    if os.path.exists(img + ".part"):
        os.remove(img + ".part")
    if os.path.exists(cnk + ".part"):
        os.remove(cnk + ".part")

    r = _asset(info["image_url"], info["image_token"])
    last_report = 0.0
    try:
        with open(img + ".part", "wb") as fh:
            for i, (size, sha) in enumerate(chunks, 1):
                if cancel and cancel():
                    raise RecoveryError("已取消下载")
                while True:
                    try:
                        buf = _read_exact(r, size)
                        break
                    except (RecoveryError, OSError, http.client.HTTPException) as e:
                        # 断流了就从最后一个校验通过的 chunk 往后要。
                        # 每个字节仍然对着 Apple 的签名 chunklist 校验，续传改不了镜像内容。
                        resumes += 1
                        if resumes > RESUMES:
                            raise RecoveryError(f"{e}（已续传 {RESUMES} 次仍失败）") from None
                        try:
                            r.close()
                        except Exception:
                            pass
                        r = _resume(info, done)
                if hashlib.sha256(buf).digest() != sha:
                    raise RecoveryError(f"第 {i}/{len(chunks)} 个分片未通过 Apple 的哈希校验，已中止")
                fh.write(buf)
                done += size
                now = time.time()
                if progress and (now - last_report > 0.15 or i == len(chunks)):
                    last_report = now
                    progress(done, total)
            if r.read(1):
                raise RecoveryError("镜像比它的签名 chunklist 更大，拒绝")
    finally:
        try:
            r.close()
        except Exception:
            pass

    with open(cnk + ".part", "wb") as fh:
        fh.write(cl)
    # 两个 replace 之间挂着:.part 名字;只有全部分片校验通过后才换成正式文件
    os.replace(img + ".part", img)
    os.replace(cnk + ".part", cnk)
    return {"image": img, "chunklist": cnk, "bytes": total, "chunks": len(chunks)}
