# schedule/parsing.py — 课表文本解析（纯函数,零 I/O,零依赖）

import re

# 课程名-教师 分隔符:`-`（工程CAD实训-张伟）、`- `（工程测量实训- 李娜）、
# 空格（管理学基础(理论) 王芳）。教师名限 2-4 个汉字,其后紧跟【周数】。
COURSE_PART_RE = re.compile(
    r'^(.+?)[- ]+([\u4e00-\u9fa5]{2,4})\s*【(.+?)】(.*)$'
)
# 旧正则（课程名-教师必须为横杠分隔）,仅作为回退
LEGACY_COURSE_RE = re.compile(r'^(.+?)-(.+?)【(.+?)】(.*)$')
# 合并单元格把多门课拼成一个 cell,课程间以 2+ 连续空白分隔
CELL_SPLIT_RE = re.compile(r'\s{2,}')


def parse_cell(cell: str) -> list[dict]:
    """
    解析单元格文本，返回课程列表（合并单元格可能含多门课）。
    格式: "课程名-教师【周数】教室" 或 "课程名 教师【周数】教室"
    示例: "高等数学BII(理论)-刘洋【2-17周】尚行楼"
          "工程CAD实训-张伟【19周】尚行楼304 BIM实训室4  管理学基础(理论) 王芳【2-17周】 南楼610智慧教室"
    """
    cell = cell.replace("\r", " ").replace("\n", " ").strip()
    if not cell:
        return []

    # 合并单元格：按 2+ 连续空白拆成多段，每段是一门完整课程
    parts = [p for p in CELL_SPLIT_RE.split(cell) if p]
    courses = []
    for part in parts:
        c = parse_course_part(part)
        if not c:
            c = parse_course_legacy(part)
        if c:
            courses.append(c)

    if len(parts) <= 1:
        return courses

    # 多段场景：
    # 1) 最后一段解析成功 → 正常的多课程合并，全部保留
    last = parse_course_part(parts[-1])
    if not last:
        last = parse_course_legacy(parts[-1])
    if last:
        return courses

    # 2) 失败段里含 【 → 尾部是残缺课程，丢弃（避免被吞进 location）
    if any("【" in p for p in parts[len(courses):]):
        return courses

    # 3) 尾部是纯 location 残片（如 location 内部恰有 2+ 空白导致误拆）：
    #    回退为整 cell 解析（新正则优先，保留完整课程；location 略脏可接受）
    c = parse_course_part(cell)
    if not c:
        c = parse_course_legacy(cell)
    return [c] if c else courses


def parse_course_part(part: str) -> dict | None:
    """解析单段课程文本：课程名[- ]教师【周数】地点"""
    match = COURSE_PART_RE.match(part)
    if not match:
        return None
    return make_course(*match.groups())


def parse_course_legacy(part: str) -> dict | None:
    """旧格式回退：课程名-教师【周数】地点"""
    match = LEGACY_COURSE_RE.match(part)
    if not match:
        return None
    return make_course(*match.groups())


def make_course(course_name: str, teacher: str, weeks_str: str,
                location: str) -> dict:
    """构造课程 dict（解析周数模式）"""
    return {
        "course": course_name.strip(),
        "teacher": teacher.strip(),
        "weeks": parse_weeks(weeks_str),  # set of week numbers
        "weeks_raw": weeks_str.strip(),
        "location": location.strip(),
    }


def parse_weeks(weeks_str: str) -> set[int]:
    """
    解析周数表达式 → 周数集合。
    "19周" → {19}
    "2-17周" → {2,3,...,17}
    "10-16(双)周" → {10,12,14,16}
    "2-4,6,8-10周" → {2,3,4,6,8,9,10}

    L-3 (#230, D2 记录不改)：已知局限——单/双标志(单/双/(单)/(双))是从整串提取的
    **全局标志**，作用于所有逗号分段；混合形态如 "2-4(单),6-10周" 会把 (单) 误作用到
    第二段(得到 {3,5,7,9} 少算)。已扫描真实课表 /root/xskb_1.xlsx：40 个周数字段中
    混合形态 0 个，现行写法解析全部正确 → **不改解析行为**(避免回归风险)；若将来课表
    出现混合写法再修(届时按分段独立解析标志)。
    """
    weeks = set()
    # 移除 "周" 字
    s = weeks_str.replace("周", "").replace(" ", "")

    # 处理单/双周标记：括号形式 (单)/(双) 或后缀形式 3-15单 / 3-15双
    odd_even = None
    if "(单)" in s:
        odd_even = "odd"
        s = s.replace("(单)", "")
    elif "(双)" in s:
        odd_even = "even"
        s = s.replace("(双)", "")
    elif odd_even is None and "单" in s:
        odd_even = "odd"
        s = s.replace("单", "")
    elif odd_even is None and "双" in s:
        odd_even = "even"
        s = s.replace("双", "")

    # 按逗号分割
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                lo, hi = int(lo), int(hi)
                for w in range(lo, hi + 1):
                    if odd_even == "odd" and w % 2 == 0:
                        continue
                    if odd_even == "even" and w % 2 != 0:
                        continue
                    weeks.add(w)
            except ValueError:
                pass
        else:
            try:
                weeks.add(int(part))
            except ValueError:
                pass
    return weeks
