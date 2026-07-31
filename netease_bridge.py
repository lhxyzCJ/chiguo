#!/usr/bin/env python3
# ============================================================
# netease_bridge.py — 网易云音乐 API 桥接
#
# 零新依赖 (stdlib only: urllib, json, os, time, base64)。
# 完全解耦于 chiguo_daemon，可独立运行测试。
#
# 用法:
#   python3 netease_bridge.py --login      交互式二维码登录
#   python3 netease_bridge.py --test       测试 API 连通性 + 登录状态
#   python3 netease_bridge.py --daily      获取每日推荐歌曲
#   python3 netease_bridge.py --refresh    强制刷新缓存
#
# 文件:
#   netease_cookie.txt   持久化登录 cookie (--login 自动生成)
#   netease_cache.json   每日推荐缓存 (--daily 自动生成)
# ============================================================

import json
import os
import sys
import time
import copy
import base64
import urllib.request
import urllib.error
import argparse
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
API_BASE = os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netease_cookie.txt")
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "netease_cache.json")
CACHE_TTL_HOURS = 6  # 同一天内缓存有效
RECENT_PLAY_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recent_play_cache.json")


# ── HTTP helpers ────────────────────────────────────────────

# 模块级重试策略(测试/策略层注入;默认 1 次 / 2.0 秒)
_API_RETRY_COUNT = 1
_API_RETRY_BACKOFF = 2.0


def set_api_retry_policy(retry_count: int, backoff_seconds: float) -> None:
    """策略层(NeteaseService)启动时注入重试参数;越界值钳制为非负。"""
    global _API_RETRY_COUNT, _API_RETRY_BACKOFF
    _API_RETRY_COUNT = max(0, int(retry_count))
    _API_RETRY_BACKOFF = max(0.0, float(backoff_seconds))


def _api_get(path, cookie=None, timeout=10):
    """HTTP GET to API, return parsed JSON or None on failure.
    瞬时失败(URLError 非 HTTPError / HTTPError code>=500)→ 重试 _API_RETRY_COUNT 次,
    间隔 _API_RETRY_BACKOFF 秒;非瞬时(HTTPError 4xx / JSON 解析 / 其他)→ 直接返回 None。
    全部重试耗尽 → 打印 error 并返回 None。
    """
    url = f"{API_BASE}{path}"
    headers = {}
    # 会话凭据放 Cookie header，不进 URL query：
    # 1) query 字符串会进访问日志（MUSIC_U 明文泄露）
    # 2) 本地 NeteaseCloudMusicApi v4.32.0 server.js:164-169 解析 req.headers.cookie
    #    （query 参数 `cookie=` 只是 override 层，本桥接不传则 header 生效）
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(_API_RETRY_COUNT + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:  # HTTPError 是 URLError 子类,必须在前
            if e.code >= 500 and attempt < _API_RETRY_COUNT:
                time.sleep(_API_RETRY_BACKOFF)
                continue
            print(f"[error] API HTTP {e.code}: {e.reason}", file=sys.stderr)
            return None
        except urllib.error.URLError as e:
            if attempt < _API_RETRY_COUNT:
                time.sleep(_API_RETRY_BACKOFF)
                continue
            print(f"[error] API unreachable after {_API_RETRY_COUNT + 1} attempts: {e}", file=sys.stderr)
            return None
        except json.JSONDecodeError as e:
            print(f"[error] Bad JSON response: {e}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[error] HTTP failure: {e}", file=sys.stderr)
            return None


# ── Cookie management ───────────────────────────────────────

def _clean_cookie(raw):
    """Extract clean key=value pairs from raw cookie string.
    Handles both Set-Cookie format (with Path=/ Expires=...)
    and simple format (MUSIC_U=xxx; __csrf=xxx).
    Only keeps MUSIC_U and __csrf — what the API needs.
    """
    # Split by the ";;" separator used in response cookie field,
    # or split by ";" for standard format
    segments = raw.replace(";;", ";").split(";")
    wanted = {}
    for seg in segments:
        seg = seg.strip()
        if "=" not in seg:
            continue
        key, val = seg.split("=", 1)
        k = key.strip()
        # 大小写不敏感匹配，并按标准名（MUSIC_U / __CSRF）归一化存储，
        # 避免响应给 "__CSRF" 大写时漏存（原实现查小写 "__csrf"）
        if k.upper() in ("MUSIC_U", "__CSRF"):
            wanted[k.upper()] = val.strip()
    if not wanted.get("MUSIC_U"):
        return None  # No MUSIC_U = not logged in
    parts = [f"MUSIC_U={wanted['MUSIC_U']}"]
    if wanted.get("__CSRF"):
        # 输出统一小写 __csrf：NeteaseCloudMusicApi request.js 用 cookie['__csrf'] 取 csrf_token
        parts.append(f"__csrf={wanted['__CSRF']}")
    return "; ".join(parts)


def _load_cookie():
    """Read persisted cookie from file. Returns clean cookie string or None."""
    if not os.path.exists(COOKIE_FILE):
        return None
    try:
        with open(COOKIE_FILE) as f:
            raw = f.read().strip()
        if not raw:
            return None
        # Try to clean it — handles both raw and already-clean formats
        cleaned = _clean_cookie(raw)
        return cleaned
    except Exception:
        return None


def _save_cookie(raw):
    """Persist raw cookie to file (stores original for debugging)."""
    with open(COOKIE_FILE, "w") as f:
        f.write(raw)
    os.chmod(COOKIE_FILE, 0o600)
    print(f"[ok] Cookie saved to {COOKIE_FILE}")



# ── QR login flow ───────────────────────────────────────────

def login_qr_flow():
    """Interactive QR code login. Blocks until user scans."""
    print("=" * 56)
    print("  网易云音乐 二维码登录")
    print("=" * 56)

    # Step 1: Get unikey
    resp = _api_get("/login/qr/key")
    if not resp or resp.get("code") != 200:
        print("[error] 获取 QR key 失败", file=sys.stderr)
        return False
    unikey = resp["data"]["unikey"]
    print(f"[1/3] QR key: {unikey}")

    # Step 2: Generate QR code
    resp = _api_get(f"/login/qr/create?key={unikey}&qrimg=true")
    if not resp or resp.get("code") != 200:
        print("[error] 生成二维码失败", file=sys.stderr)
        return False
    qr_data = resp["data"]["qrimg"]
    print(f"[2/3] 二维码已生成")

    # Save QR image
    qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "netease_qr.png")
    if "," in qr_data:
        b64_data = qr_data.split(",", 1)[1]
        with open(qr_path, "wb") as f:
            f.write(base64.b64decode(b64_data))
        print(f"      二维码图片: {qr_path}")
        print(f"      (请用任意图片查看器打开，或 scp 到本地扫描)")

    # Try terminal ASCII QR if possible
    try:
        _print_qr_terminal(unikey)
    except Exception:
        pass

    # Step 3: Poll for scan
    print(f"[3/3] 等待扫码... (打开网易云音乐 APP → 扫一扫)")
    max_wait = 180  # 3 minutes
    interval = 2
    waited = 0
    last_status = None
    while waited < max_wait:
        resp = _api_get(f"/login/qr/check?key={unikey}&timestamp={int(time.time()*1000)}")
        if not resp:
            time.sleep(interval)
            waited += interval
            continue

        code = resp.get("code")
        # 800: expired, 801: waiting, 802: scanned pending confirm, 803: success
        if code == 800:
            print("[error] 二维码已过期，请重新运行 --login", file=sys.stderr)
            return False
        elif code == 801 and code != last_status:
            print("  等待扫码...")
        elif code == 802 and code != last_status:
            print("  已扫码，请在手机上确认授权...")
        elif code == 803:
            print("  授权成功！")

            # Extract cookie from response
            cookie_str = resp.get("cookie", "")
            if not cookie_str:
                print("[error] 登录失败: 响应中无 cookie 字段，无法完成认证", file=sys.stderr)
                return False

            # Persist
            _save_cookie(cookie_str)
            print("✓ 登录成功！")
            return True
        elif code != last_status:
            print(f"  状态码: {code}, message: {resp.get('message', '?')}")

        last_status = code
        time.sleep(interval)
        waited += interval

    print("[error] 登录超时", file=sys.stderr)
    return False


def _print_qr_terminal(unikey):
    """Generate simple ASCII QR via external library or display URL."""
    # If qrcode-terminal package is available in node, use it
    import subprocess
    try:
        result = subprocess.run(
            ["node", "-e", f"""
                const QRCode = require('qrcode');
                QRCode.toString('https://music.163.com/login?codekey={unikey}',
                    {{type: 'terminal', small: true}}, (err, str) => {{
                        if (err) process.exit(1);
                        console.log(str);
                    }});
            """],
            cwd=os.environ.get("NETEASE_QR_CWD", "/opt/netease-api"),
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout:
            print(result.stdout)
    except Exception:
        pass


# ── Cache management ────────────────────────────────────────

def _load_cache():
    """Read daily songs cache. Returns dict or None."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        cached_date = data.get("date", "")
        today = datetime.now(CST).strftime("%Y-%m-%d")
        if cached_date != today:
            return None  # expired (different day)
        fetched_at = data.get("fetched_at", "")
        if fetched_at:
            try:
                ftime = datetime.fromisoformat(fetched_at)
                age_hours = (datetime.now(CST) - ftime).total_seconds() / 3600
                if age_hours > CACHE_TTL_HOURS:
                    return None  # expired (TTL)
            except (ValueError, TypeError):
                pass
        return data
    except Exception:  # JSONDecodeError 是 Exception 子类，原 (json.JSONDecodeError, Exception) 冗余
        return None


def _save_cache(songs):
    """Persist daily songs to cache file."""
    today = datetime.now(CST).strftime("%Y-%m-%d")
    fetched_at = datetime.now(CST).isoformat()
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "date": today,
            "fetched_at": fetched_at,
            "songs": songs,
        }, f, ensure_ascii=False, indent=2)
    print(f"[ok] 缓存已保存: {len(songs)} 首歌 → {CACHE_FILE}")


# ── Core API ────────────────────────────────────────────────

def check_health():
    """Check API server alive + login status."""
    cookie = _load_cookie()

    # Check server reachable
    status = _api_get("/login/status", cookie=cookie)
    if status is None:
        return {"api_alive": False, "logged_in": False, "profile": None}

    # v9 审计 F-3:响应结构异常(status/data/account/profile 非 dict)按未登录降级,
    # 不抛 AttributeError(安全默认:api 可达但视为未登录)
    if not isinstance(status, dict):
        print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
        return {"api_alive": True, "logged_in": False, "profile": None}

    # Parse login status
    data = status.get("data", {})
    if not isinstance(data, dict):
        print("[error] 响应结构异常: data 非 dict", file=sys.stderr)
        return {"api_alive": True, "logged_in": False, "profile": None}

    account = data.get("account", {}) or {}
    if not isinstance(account, dict):
        account = {}
    profile = data.get("profile", {}) or {}
    if not isinstance(profile, dict):
        profile = {}

    logged_in = account.get("id", 0) > 0
    return {
        "api_alive": True,
        "logged_in": logged_in,
        "user_id": account.get("id"),
        "nickname": profile.get("nickname", ""),
        "vip_type": data.get("vipType", 0),
    }


def fetch_daily_songs(limit=10, force_refresh=False):
    """Fetch daily recommended songs. Uses cache unless force_refresh.
    Returns list of simplified song dicts, or None on failure.
    """
    # Check cache first
    if not force_refresh:
        cached = _load_cache()
        if cached and cached.get("songs"):
            print(f"[cache] 使用缓存 ({cached['date']}, {len(cached['songs'])} 首)", file=sys.stderr)
            return cached["songs"]

    # Fetch from API
    cookie = _load_cookie()
    if not cookie:
        print("[error] 未登录，请先执行: python3 netease_bridge.py --login", file=sys.stderr)
        return None

    resp = _api_get(f"/recommend/songs?timestamp={int(time.time()*1000)}", cookie=cookie)
    if resp is None:
        print("[warn] API 不可达，尝试使用过期缓存", file=sys.stderr)
        cached = _load_cache()
        return cached.get("songs") if cached else None

    if not isinstance(resp, dict):
        # v9 审计 F-3:非 dict 响应(如 list)→ 视为解析失败,不抛 AttributeError
        print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
        return None

    if resp.get("code") != 200:
        print(f"[error] API 返回错误: code={resp.get('code')} message={resp.get('message','?')}", file=sys.stderr)
        if resp.get("code") == 301:
            print("[hint] 可能需要重新登录: python3 netease_bridge.py --login", file=sys.stderr)
        return None

    raw_songs = resp.get("data", {}).get("dailySongs", [])
    if not raw_songs:
        print("[warn] 每日推荐为空 (可能是新账号或非日推更新时间)", file=sys.stderr)
        return []

    # Simplify to only fields we need
    songs = []
    for s in raw_songs[:limit]:
        if not isinstance(s, dict):
            continue  # 非 dict 条目 → 过滤
        raw_artists = s.get("ar")
        raw_artists = raw_artists if isinstance(raw_artists, list) else []
        artists = [ar.get("name", "") for ar in raw_artists if isinstance(ar, dict)]
        artist_str = "/".join(a for a in artists if a) if artists else "未知"
        album = s.get("al")
        album = album if isinstance(album, dict) else {}
        song_id = s.get("id")
        if not isinstance(song_id, int):
            continue  # 无合法 id → 过滤(share_url 依赖它)
        song = {
            "id": song_id,
            "name": s.get("name", ""),
            "artists": artist_str,
            "album": album.get("name", ""),
            "pic_url": album.get("picUrl", ""),
            "dt_ms": s.get("dt", 0) if isinstance(s.get("dt"), int) else 0,
            "fee": s.get("fee", 0) if isinstance(s.get("fee"), int) else 0,
            "share_url": f"https://music.163.com/song?id={song_id}",
        }
        songs.append(song)

    # Save to cache
    _save_cache(songs)
    return songs


def refresh_cache():
    """Force refresh the daily songs cache."""
    print("[refresh] 强制刷新缓存...", file=sys.stderr)
    return fetch_daily_songs(force_refresh=True)


# ── Recent play (近一周播放记录) ─────────────────────────────

def _parse_cst(iso_str):
    """Parse fetched_at ISO string, pad naive timestamps with CST."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)  # 旧格式无 tz → 按 CST 补齐
    return dt


def _load_recent_play_cache(cache_file):
    """Read recent play cache. Returns {"fetched_at": aware dt, "plays": [...]}
    or None if missing/corrupt/unusable structure."""
    if not os.path.exists(cache_file):
        return None
    try:
        with open(cache_file) as f:
            data = json.load(f)
    except Exception:  # JSONDecodeError 是 Exception 子类
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("plays"), list):
        return None
    fetched_at = _parse_cst(data.get("fetched_at"))
    if fetched_at is None:
        return None
    return {"fetched_at": fetched_at, "plays": data["plays"]}


def _save_recent_play_cache(cache_file, plays, fetched_at):
    """Atomic write (tmp → os.replace); failure only warns, never blocks."""
    tmp = f"{cache_file}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"fetched_at": fetched_at.isoformat(), "plays": plays},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, cache_file)
    except Exception as e:
        print(f"[warn] 播放记录缓存写入失败: {e}", file=sys.stderr)


def fetch_recent_play(limit: int = 20, ttl_minutes: int = 15,
                      now: datetime | None = None,
                      cache_file: str | None = None) -> list[dict] | None:
    """GET /user/record?type=1&limit=N(近一周播放记录)。

    返回简化条目 [{"playTime": epoch_ms int, "name", "artist"}...]:
    - 缓存命中(now - fetched_at <= ttl_minutes)直接返回缓存副本,不发请求
    - API 失败/未登录/解析失败 → None(不缓存失败)
    - 非法 playTime/非 dict 条目 → 逐条过滤(不整体失败)
    """
    cache_file = cache_file or RECENT_PLAY_CACHE_FILE
    if now is None:
        now = datetime.now(CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)  # 测试可能传 naive,按 CST 补齐

    cached = _load_recent_play_cache(cache_file)
    if cached:
        age = now - cached["fetched_at"]
        if timedelta(0) <= age <= timedelta(minutes=ttl_minutes):  # 负年龄(时钟回拨/篡改)→ 不命中
            print(f"[cache] 播放记录缓存命中 ({len(cached['plays'])} 条)", file=sys.stderr)
            return copy.deepcopy(cached["plays"])  # 副本,防调用方篡改缓存

    cookie = _load_cookie()
    if not cookie:
        print("[error] 未登录，请先执行: python3 netease_bridge.py --login", file=sys.stderr)
        return None

    resp = _api_get(f"/user/record?type=1&limit={limit}", cookie=cookie)
    if resp is None:
        print("[warn] API 不可达，播放记录获取失败", file=sys.stderr)
        return None
    if not isinstance(resp, dict):
        print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
        return None

    if resp.get("code") != 200:
        print(f"[error] API 返回错误: code={resp.get('code')} message={resp.get('message','?')}", file=sys.stderr)
        if resp.get("code") == 301:
            print("[hint] 可能需要重新登录: python3 netease_bridge.py --login", file=sys.stderr)
        return None

    data = resp.get("data")
    if not isinstance(data, dict):
        print("[error] 响应结构异常: data 非 dict", file=sys.stderr)
        return None
    raw_list = data.get("list")
    if not isinstance(raw_list, list):
        print("[error] 响应结构异常: data.list 缺失或非列表", file=sys.stderr)
        return None

    plays = []
    for item in raw_list[:limit]:
        if not isinstance(item, dict):
            continue  # 非 dict 条目 → 过滤
        pt = item.get("playTime")
        if not isinstance(pt, int):
            continue  # 非法 playTime(非 int/缺失)→ 过滤该条
        song = item.get("song")
        song = song if isinstance(song, dict) else {}  # 非 dict song → 空(不崩溃)
        name = song.get("name", "")
        artists = song.get("artists") or []
        artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else "未知"
        plays.append({"playTime": pt, "name": name, "artist": artist or "未知"})

    _save_recent_play_cache(cache_file, plays, now)
    return plays



# ── Display ──────────────────────────────────────────────────

def print_daily_songs(songs, verbose=False):
    """Pretty-print daily song recommendations."""
    if songs is None:
        print("✗ 获取失败 — API 不可达或未登录")
        return

    if not songs:
        print("(空) 今日每日推荐无数据")
        return

    print(f"\n今日推荐 ({len(songs)} 首):")
    print("-" * 56)
    for i, s in enumerate(songs, 1):
        fee_tag = ""
        if s.get("fee", 0) == 1:
            fee_tag = " [VIP]"
        elif s.get("fee", 0) == 4:
            fee_tag = " [付费]"

        dur = s.get("dt_ms", 0) // 1000
        mm, ss = divmod(dur, 60)
        dur_str = f"{mm}:{ss:02d}"

        print(f"  {i:2d}. 《{s['name']}》 — {s['artists']}  [{dur_str}]{fee_tag}")
        if verbose:
            print(f"      专辑: {s.get('album', '?')}")
            print(f"      ID: {s['id']}  链接: {s.get('share_url', '')}")
            if s.get('pic_url'):
                print(f"      封面: {s['pic_url']}")


# ── CLI ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="网易云音乐 API 桥接 (NeteaseCloudMusicApi)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python3 netease_bridge.py --login      首次登录 (二维码)
  python3 netease_bridge.py --test       检查 API 状态 + 登录
  python3 netease_bridge.py --daily      获取每日推荐
  python3 netease_bridge.py --daily -v   详细模式 (含封面/链接)
  python3 netease_bridge.py --refresh    强制刷新缓存
        """,
    )
    parser.add_argument("--login", action="store_true", help="二维码登录流程")
    parser.add_argument("--test", action="store_true", help="测试 API 连通性 + 登录状态")
    parser.add_argument("--daily", action="store_true", help="获取每日推荐歌曲")
    parser.add_argument("--refresh", action="store_true", help="强制刷新每日推荐缓存")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出 (供脚本消费)")

    args = parser.parse_args()

    # Default action: if no flags, show help
    if not any([args.login, args.test, args.daily, args.refresh]):
        parser.print_help()
        return

    # --login
    if args.login:
        success = login_qr_flow()
        if not success:
            sys.exit(1)
        # After login, show status
        health = check_health()
        if health["logged_in"]:
            print(f"  用户: {health['nickname']} (ID: {health['user_id']})")
        return

    # --test
    if args.test:
        health = check_health()
        if args.json:
            print(json.dumps(health, ensure_ascii=False, indent=2))
        else:
            print(f"API 服务: {'✓ 可达' if health['api_alive'] else '✗ 不可达'}")
            print(f"登录状态: {'✓ 已登录' if health['logged_in'] else '✗ 未登录'}")
            if health.get("nickname"):
                print(f"用户: {health['nickname']} (ID: {health['user_id']})")
                print(f"VIP: {health.get('vip_type', 0)}")
        return

    # --daily
    if args.daily:
        songs = fetch_daily_songs()
        if args.json:
            print(json.dumps(songs, ensure_ascii=False, indent=2))
        else:
            print_daily_songs(songs, verbose=args.verbose)
        if songs is None:
            sys.exit(1)
        return

    # --refresh
    if args.refresh:
        songs = refresh_cache()
        if songs is None:
            print("[error] 刷新失败", file=sys.stderr)
            sys.exit(1)
        print(f"✓ 刷新成功: {len(songs)} 首歌")
        print_daily_songs(songs, verbose=args.verbose)


if __name__ == "__main__":
    main()
