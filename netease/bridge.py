#!/usr/bin/env python3
# ============================================================
# netease/bridge.py — 网易云音乐 API 桥接（数据面）
#
# 零新依赖 (stdlib only: urllib, json, os, time, base64)。
# NeteaseBridge 实例化：构造注入 base_dir/api_base/重试/cookie 路径；
# 无模块级全局状态。运行时文件统一锚定 <base_dir>/netease/。
#
# 用法:
#   uv run python -m netease.bridge --login      交互式二维码登录
#   uv run python -m netease.bridge --test       测试 API 连通性 + 登录状态
#   uv run python -m netease.bridge --daily      获取每日推荐歌曲
#   uv run python -m netease.bridge --refresh    强制刷新缓存
#   (等价: python3 netease/bridge.py --login)
#
# 文件:
#   ~/.chiguo/auth/netease_cookie.txt(优先) → <base_dir>/netease/netease_cookie.txt
#   <base_dir>/netease/netease_cache.json         每日推荐缓存
#   <base_dir>/netease/recent_play_cache.json     播放记录缓存
#   <base_dir>/netease/netease_qr.png             登录二维码
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
from pathlib import Path

CST = timezone(timedelta(hours=8))


class NeteaseBridge:
    """网易云数据面：HTTP 桥接。构造注入路径与重试；零模块级全局状态。"""

    def __init__(self, base_dir: str | os.PathLike, *,
                 api_base: str | None = None,
                 retry_count: int = 1,
                 retry_backoff: float = 2.0,
                 cookie_path: str | os.PathLike | None = None):
        base = Path(base_dir)
        self.data_dir = base / "netease"
        self._cookie_path_override = Path(cookie_path) if cookie_path else None
        self.cache_file = self.data_dir / "netease_cache.json"
        self.recent_play_cache_file = self.data_dir / "recent_play_cache.json"
        self.qr_path = self.data_dir / "netease_qr.png"
        self.api_base = api_base or os.environ.get("NETEASE_API_BASE", "http://localhost:3000")
        self.retry_count = max(0, int(retry_count))      # 钳制非负
        self.retry_backoff = max(0.0, float(retry_backoff))

    # ── Cookie ───────────────────────────────────────────────

    def _cookie_file(self) -> str:
        """cookie 解析链（调用时解析）:显式注入(测试) → 集中认证目录 → <data_dir>/netease_cookie.txt"""
        if self._cookie_path_override:
            return str(self._cookie_path_override)
        auth_cookie = os.path.expanduser("~/.chiguo/auth/netease_cookie.txt")
        if os.path.isfile(auth_cookie):
            return auth_cookie
        return str(self.data_dir / "netease_cookie.txt")

    @staticmethod
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

    def _load_cookie(self):
        """Read persisted cookie from file. Returns clean cookie string or None."""
        path = self._cookie_file()
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                raw = f.read().strip()
            if not raw:
                return None
            return self._clean_cookie(raw)
        except Exception:
            return None

    def _save_cookie(self, raw):
        """Persist raw cookie to file (stores original for debugging).
        权限 0600 一步到位：os.open(O_CREAT, 0o600) 落盘即 0600，无先写后 chmod 的泄露窗口。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._cookie_file()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(raw)
        print(f"[ok] Cookie saved to {path}", file=sys.stderr)

    # ── HTTP ─────────────────────────────────────────────────

    def _api_get(self, path, cookie=None, timeout=10):
        """HTTP GET to API, return parsed JSON or None on failure.
        瞬时失败(URLError 非 HTTPError / HTTPError code>=500)→ 重试 self.retry_count 次,
        间隔 self.retry_backoff 秒;非瞬时(HTTPError 4xx / JSON 解析 / 其他)→ 直接返回 None。
        全部重试耗尽 → 打印 error 并返回 None。
        """
        url = f"{self.api_base}{path}"
        headers = {}
        # 会话凭据放 Cookie header，不进 URL query：
        # 1) query 字符串会进访问日志（MUSIC_U 明文泄露）
        # 2) 本地 NeteaseCloudMusicApi 系服务解析 req.headers.cookie
        #    （query 参数 `cookie=` 只是 override 层，本桥接不传则 header 生效）
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(url, headers=headers)
        for attempt in range(self.retry_count + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:  # HTTPError 是 URLError 子类,必须在前
                if e.code >= 500 and attempt < self.retry_count:
                    time.sleep(self.retry_backoff)
                    continue
                print(f"[error] API HTTP {e.code}: {e.reason}", file=sys.stderr)
                return None
            except urllib.error.URLError as e:
                if attempt < self.retry_count:
                    time.sleep(self.retry_backoff)
                    continue
                print(f"[error] API unreachable after {self.retry_count + 1} attempts: {e}", file=sys.stderr)
                return None
            except json.JSONDecodeError as e:
                print(f"[error] Bad JSON response: {e}", file=sys.stderr)
                return None
            except Exception as e:
                print(f"[error] HTTP failure: {e}", file=sys.stderr)
                return None

    # ── QR login flow ────────────────────────────────────────

    def login(self):
        """Interactive QR code login. Blocks until user scans."""
        print("=" * 56)
        print("  网易云音乐 二维码登录")
        print("=" * 56)

        resp = self._api_get("/login/qr/key")
        if not resp or resp.get("code") != 200:
            print("[error] 获取 QR key 失败", file=sys.stderr)
            return False
        key_data = resp.get("data")
        if not isinstance(key_data, dict) or not isinstance(key_data.get("unikey"), str):
            print("[error] 获取 QR key 失败: 响应结构异常", file=sys.stderr)
            return False
        unikey = key_data["unikey"]
        print(f"[1/3] QR key: {unikey}")

        resp = self._api_get(f"/login/qr/create?key={unikey}&qrimg=true")
        if not resp or resp.get("code") != 200:
            print("[error] 生成二维码失败", file=sys.stderr)
            return False
        create_data = resp.get("data")
        if not isinstance(create_data, dict) or not isinstance(create_data.get("qrimg"), str):
            print("[error] 生成二维码失败: 响应结构异常", file=sys.stderr)
            return False
        qr_data = create_data["qrimg"]
        print(f"[2/3] 二维码已生成")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        if "," in qr_data:
            b64_data = qr_data.split(",", 1)[1]
            try:
                with open(self.qr_path, "wb") as f:
                    f.write(base64.b64decode(b64_data))
                print(f"      二维码图片: {self.qr_path}")
                print(f"      (请用任意图片查看器打开，或 scp 到本地扫描)")
            except Exception as e:
                print(f"[warn] 二维码图片保存失败: {e}", file=sys.stderr)

        try:
            self._print_qr_terminal(unikey)
        except Exception:
            pass

        print(f"[3/3] 等待扫码... (打开网易云音乐 APP → 扫一扫)")
        max_wait = 180  # 3 minutes
        interval = 2
        waited = 0
        last_status = None
        while waited < max_wait:
            resp = self._api_get(f"/login/qr/check?key={unikey}&timestamp={int(time.time()*1000)}")
            if not isinstance(resp, dict):
                time.sleep(interval)
                waited += interval
                continue

            code = resp.get("code")
            if code == 800:
                print("[error] 二维码已过期，请重新运行 --login", file=sys.stderr)
                return False
            elif code == 801 and code != last_status:
                print("  等待扫码...")
            elif code == 802 and code != last_status:
                print("  已扫码，请在手机上确认授权...")
            elif code == 803:
                print("  授权成功！")

                cookie_str = resp.get("cookie") or (resp.get("data") or {}).get("cookie")
                if not cookie_str:
                    print("[error] 登录失败: 响应中无 cookie 字段，无法完成认证", file=sys.stderr)
                    return False

                self._save_cookie(cookie_str)
                print("✓ 登录成功！")
                return True
            elif code != last_status:
                print(f"  状态码: {code}, message: {resp.get('message', '?')}")

            last_status = code
            time.sleep(interval)
            waited += interval

        print("[error] 登录超时", file=sys.stderr)
        return False

    def _print_qr_terminal(self, unikey):
        """Generate simple ASCII QR via external library or display URL."""
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

    # ── Cache ────────────────────────────────────────────────

    def _load_cache(self):
        """Read daily songs cache. Returns dict or None."""
        if not os.path.exists(self.cache_file):
            return None
        try:
            with open(self.cache_file) as f:
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
        except Exception:
            return None

    def _save_cache(self, songs):
        """Persist daily songs to cache file (atomic tmp → os.replace;失败仅 warn)。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(CST).strftime("%Y-%m-%d")
        fetched_at = datetime.now(CST).isoformat()
        tmp = f"{self.cache_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({
                    "date": today,
                    "fetched_at": fetched_at,
                    "songs": songs,
                }, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.cache_file)
        except Exception as e:
            print(f"[warn] 缓存写入失败: {e}", file=sys.stderr)
            return
        print(f"[ok] 缓存已保存: {len(songs)} 首歌 → {self.cache_file}", file=sys.stderr)

    # ── Core API ─────────────────────────────────────────────

    def check_health(self):
        """Check API server alive + login status."""
        cookie = self._load_cookie()

        status = self._api_get("/login/status", cookie=cookie)
        if status is None:
            return {"api_alive": False, "logged_in": False, "profile": None}

        if not isinstance(status, dict):
            print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
            return {"api_alive": True, "logged_in": False, "profile": None, "api_error": "malformed"}

        # api-enhanced 成功响应把 code/account/profile 包在 data 内（{data:{code,...}}，
        # vipType 位于 data.account）；失败响应为顶层 {code,...}（不包装）。
        # data.code 优先、顶层 code 回退，恰好覆盖这两种真实形态（并对旧格式防御性兜底）。
        data = status.get("data", {})
        if not isinstance(data, dict):
            print("[error] 响应结构异常: data 非 dict", file=sys.stderr)
            return {"api_alive": True, "logged_in": False, "profile": None, "api_error": "malformed"}
        code = data.get("code")
        if code is None:
            code = status.get("code")
        if code != 200:
            print(f"[error] API 返回错误: code={code} message={(data or status).get('message', '?')}", file=sys.stderr)
            if code == 301:
                print("[hint] 可能需要重新登录: uv run python -m netease.bridge --login", file=sys.stderr)
            return {"api_alive": True, "logged_in": False, "profile": None, "api_error": code}

        account = data.get("account") or status.get("account") or {}
        if not isinstance(account, dict):
            account = {}
        profile = data.get("profile") or status.get("profile") or {}
        if not isinstance(profile, dict):
            profile = {}

        logged_in = account.get("id", 0) > 0
        return {
            "api_alive": True,
            "logged_in": logged_in,
            "user_id": account.get("id"),
            "nickname": profile.get("nickname", ""),
            "vip_type": account.get("vipType", data.get("vipType", status.get("vipType", 0))),
        }

    def fetch_daily_songs(self, limit=10, force_refresh=False):
        """Fetch daily recommended songs. Uses cache unless force_refresh.
        Returns list of simplified song dicts, or None on failure.
        """
        if not force_refresh:
            cached = self._load_cache()
            if cached is not None:  # 缓存存在（含空 songs）即命中；None=无缓存才重新拉取
                songs = cached.get("songs") or []
                print(f"[cache] 使用缓存 ({cached.get('date', '?')}, {len(songs)} 首)", file=sys.stderr)
                return songs

        cookie = self._load_cookie()
        if not cookie:
            print("[error] 未登录，请先执行: uv run python -m netease.bridge --login", file=sys.stderr)
            return None

        resp = self._api_get(f"/recommend/songs?timestamp={int(time.time()*1000)}", cookie=cookie)
        if resp is None:
            print("[warn] API 不可达，尝试使用仍在 TTL 内的缓存", file=sys.stderr)
            cached = self._load_cache()
            return cached.get("songs") if cached else None

        if not isinstance(resp, dict):
            print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
            return None

        if resp.get("code") != 200:
            print(f"[error] API 返回错误: code={resp.get('code')} message={resp.get('message','?')}", file=sys.stderr)
            if resp.get("code") == 301:
                print("[hint] 可能需要重新登录: uv run python -m netease.bridge --login", file=sys.stderr)
            return None

        data = resp.get("data")
        if not isinstance(data, dict):
            print("[error] 响应结构异常: data 非 dict", file=sys.stderr)
            return None
        raw_songs = data.get("dailySongs")
        if not isinstance(raw_songs, list):
            print("[error] 响应结构异常: data.dailySongs 缺失或非列表", file=sys.stderr)
            return None

        songs = []
        for s in raw_songs[:limit]:
            if not isinstance(s, dict):
                continue
            raw_artists = s.get("ar")
            raw_artists = raw_artists if isinstance(raw_artists, list) else []
            artists = [ar.get("name", "") for ar in raw_artists if isinstance(ar, dict)]
            artist_str = "/".join(a for a in artists if a) if artists else "未知"
            album = s.get("al")
            album = album if isinstance(album, dict) else {}
            song_id = s.get("id")
            if not isinstance(song_id, int):
                continue
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

        if not songs:
            print("[warn] 每日推荐为空 (可能是新账号或非日推更新时间)", file=sys.stderr)

        self._save_cache(songs)
        return songs

    def refresh_cache(self):
        """Force refresh the daily songs cache."""
        print("[refresh] 强制刷新缓存...", file=sys.stderr)
        return self.fetch_daily_songs(force_refresh=True)

    # ── Recent play (近一周播放记录) ─────────────────────────

    @staticmethod
    def _parse_cst(iso_str):
        """Parse fetched_at ISO string, pad naive timestamps with CST."""
        try:
            dt = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)  # 旧格式无 tz → 按 CST 补齐
        return dt

    def _load_recent_play_cache(self, cache_file):
        """Read recent play cache. Returns {"fetched_at": aware dt, "plays": [...]}
        or None if missing/corrupt/unusable structure."""
        if not os.path.exists(cache_file):
            return None
        try:
            with open(cache_file) as f:
                data = json.load(f)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("plays"), list):
            return None
        fetched_at = self._parse_cst(data.get("fetched_at"))
        if fetched_at is None:
            return None
        return {"fetched_at": fetched_at, "plays": data["plays"]}

    def _save_recent_play_cache(self, cache_file, plays, fetched_at):
        """Atomic write (tmp → os.replace); failure only warns, never blocks."""
        tmp = f"{cache_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"fetched_at": fetched_at.isoformat(), "plays": plays},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, cache_file)
        except Exception as e:
            print(f"[warn] 播放记录缓存写入失败: {e}", file=sys.stderr)

    def fetch_recent_play(self, limit: int = 20, ttl_minutes: int = 15,
                          now: datetime | None = None,
                          cache_file: str | None = None) -> list[dict] | None:
        """GET /user/record?type=1&limit=N(近一周播放记录)。

        返回简化条目 [{"playTime": epoch_ms int, "name", "artist"}...]:
        - 缓存命中(now - fetched_at <= ttl_minutes)直接返回缓存副本,不发请求
        - API 失败/未登录/解析失败 → None(不缓存失败)
        - 非法 playTime/非 dict 条目 → 逐条过滤(不整体失败)
        cache_file=None → 默认 <data_dir>/recent_play_cache.json。
        """
        if cache_file is None:
            cache_file = str(self.recent_play_cache_file)
        if now is None:
            now = datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)  # 测试可能传 naive,按 CST 补齐

        cached = self._load_recent_play_cache(cache_file)
        if cached:
            age = now - cached["fetched_at"]
            if timedelta(0) <= age <= timedelta(minutes=ttl_minutes):  # 负年龄(时钟回拨/篡改)→ 不命中
                print(f"[cache] 播放记录缓存命中 ({len(cached['plays'])} 条)", file=sys.stderr)
                return copy.deepcopy(cached["plays"])  # 副本,防调用方篡改缓存

        cookie = self._load_cookie()
        if not cookie:
            print("[error] 未登录，请先执行: uv run python -m netease.bridge --login", file=sys.stderr)
            return None

        resp = self._api_get(f"/user/record?type=1&limit={limit}", cookie=cookie)
        if resp is None:
            print("[warn] API 不可达，播放记录获取失败", file=sys.stderr)
            return None
        if not isinstance(resp, dict):
            print("[error] 响应结构异常: resp 非 dict", file=sys.stderr)
            return None

        if resp.get("code") != 200:
            print(f"[error] API 返回错误: code={resp.get('code')} message={resp.get('message','?')}", file=sys.stderr)
            if resp.get("code") == 301:
                print("[hint] 可能需要重新登录: uv run python -m netease.bridge --login", file=sys.stderr)
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
                continue
            pt = item.get("playTime")
            if not isinstance(pt, int):
                continue
            song = item.get("song")
            song = song if isinstance(song, dict) else {}
            name = song.get("name", "")
            artists = song.get("artists") or []
            artist = artists[0].get("name") if artists and isinstance(artists[0], dict) else "未知"
            plays.append({"playTime": pt, "name": name, "artist": artist or "未知"})

        self._save_recent_play_cache(cache_file, plays, now)
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

CACHE_TTL_HOURS = 6  # 同一天内缓存有效（模块级常量，供 _load_cache 使用）


def main():
    parser = argparse.ArgumentParser(
        description="网易云音乐 API 桥接 (NeteaseCloudMusicApi 系)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  uv run python -m netease.bridge --login      首次登录 (二维码)
  uv run python -m netease.bridge --test       检查 API 状态 + 登录
  uv run python -m netease.bridge --daily      获取每日推荐
  uv run python -m netease.bridge --daily -v   详细模式 (含封面/链接)
  uv run python -m netease.bridge --refresh    强制刷新缓存
        """,
    )
    parser.add_argument("--login", action="store_true", help="二维码登录流程")
    parser.add_argument("--test", action="store_true", help="测试 API 连通性 + 登录状态")
    parser.add_argument("--daily", action="store_true", help="获取每日推荐歌曲")
    parser.add_argument("--refresh", action="store_true", help="强制刷新每日推荐缓存")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细输出")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出 (供脚本消费)")

    args = parser.parse_args()

    if not any([args.login, args.test, args.daily, args.refresh]):
        parser.print_help()
        return

    base = Path(__file__).resolve().parent.parent   # 项目根
    b = NeteaseBridge(base)

    if args.login:
        success = b.login()
        if not success:
            sys.exit(1)
        health = b.check_health()
        if health["logged_in"]:
            print(f"  用户: {health['nickname']} (ID: {health['user_id']})")
        return

    if args.test:
        health = b.check_health()
        if args.json:
            print(json.dumps(health, ensure_ascii=False, indent=2))
        else:
            print(f"API 服务: {'✓ 可达' if health['api_alive'] else '✗ 不可达'}")
            print(f"登录状态: {'✓ 已登录' if health['logged_in'] else '✗ 未登录'}")
            if health.get("nickname"):
                print(f"用户: {health['nickname']} (ID: {health['user_id']})")
                print(f"VIP: {health.get('vip_type', 0)}")
        return

    if args.daily:
        songs = b.fetch_daily_songs()
        if args.json:
            print(json.dumps(songs, ensure_ascii=False, indent=2))
        else:
            print_daily_songs(songs, verbose=args.verbose)
        if songs is None:
            sys.exit(1)
        return

    if args.refresh:
        songs = b.refresh_cache()
        if songs is None:
            print("[error] 刷新失败", file=sys.stderr)
            sys.exit(1)
        print(f"✓ 刷新成功: {len(songs)} 首歌")
        print_daily_songs(songs, verbose=args.verbose)


if __name__ == "__main__":
    main()
