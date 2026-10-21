"""纯标准库把 verify_build 截的 32-bit BMP 转成 PNG（无 PIL 依赖）。"""
import struct
import zlib
import sys

src = sys.argv[1] if len(sys.argv) > 1 else r"E:\BaiduSyncdisk\ai测试\黑苹果\GlassHack\_shot\glasscheck.bmp"
dst = sys.argv[2] if len(sys.argv) > 1 and len(sys.argv) > 2 else src[:-4] + ".png"

with open(src, "rb") as f:
    data = f.read()

# 解析 BMP 头（14 + 40）
assert data[:2] == b"BM", "not BMP"
pixels_off = struct.unpack("<I", data[10:14])[0]
bih = data[14:54]
_, w, h, _, _, _, _, _, _, _, _ = struct.unpack("<IiiHHIIiiII", bih[:40])
# 像素：BGRA，top-down（高为负时）
stride = w * 4
raw = data[pixels_off:]

# 组装 PNG 扫描线（RGB，丢弃 alpha）
out = bytearray()
for y in range(abs(h)):
    row = raw[y * stride:(y + 1) * stride]
    line = bytearray()
    for x in range(w):
        b, g, r, a = row[x * 4:x * 4 + 4]
        line += bytes((r, g, b))
    out += b"\x00" + line  # filter type 0

def chunk(tag, body):
    c = tag + body
    return struct.pack("<I", len(body)) + c + struct.pack("<I", zlib.crc32(c) & 0xffffffff)

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, abs(h), 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(out), 9))
png += chunk(b"IEND", b"")

with open(dst, "wb") as f:
    f.write(png)
print(f"wrote {dst}  ({w}x{abs(h)}, {len(png)} bytes)")
