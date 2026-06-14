"""
文本智能解析引擎 - 支持自由格式和结构化格式
自动识别：日期、报销人、品类、金额
"""

import re
from datetime import datetime


def parse_expense_text(text: str) -> list[dict]:
    """
    解析报账文本，返回结构化记录列表。
    每条记录: {date, person, category, amount, remark}

    支持格式示例：
    1. 结构化: "2024-06-01 张三 餐饮 128.5 团建聚餐"
    2. 自由格式: "张三6月1号请客吃饭花了128.5"
    3. 多行批量: 每行一条记录
    4. "6.1 李四 打车 35"
    5. "王五 - 快递费 12元 - 6月3日"
    """
    if not text or not text.strip():
        return []

    results = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    for line in lines:
        parsed = _parse_single_line(line)
        if parsed:
            results.append(parsed)

    return results


def _parse_single_line(line: str) -> dict | None:
    """解析单行文本"""
    # 尝试结构化格式：日期 人名 品类 金额 备注
    structured = _try_structured(line)
    if structured:
        return structured

    # 尝试冒号格式：人名：品名 金额（品名直出不归类）
    colon = _try_colon_format(line)
    if colon:
        return colon

    # 尝试分隔符格式：人名 - 品类 金额 - 日期
    dashed = _try_dashed_format(line)
    if dashed:
        return dashed

    # 自由格式解析
    free = _try_free_format(line)
    if free:
        return free

    return None


def _try_structured(line: str) -> dict | None:
    """结构化: 2024-06-01 张三 餐饮 128.5 备注"""
    # 日期 人名 品类 金额 备注
    pattern = re.compile(
        r'^(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2})\s+'
        r'(\S+?)\s+'
        r'(\S+?)\s+'
        r'([\d,.]+)\s*'
        r'(.*?)$'
    )
    m = pattern.match(line)
    if m:
        date_str = _normalize_date(m.group(1))
        if date_str is None:
            return None
        person = m.group(2)
        amount_str = m.group(4).replace(",", "")
        remark = m.group(5).strip()
        # 剥离金额后紧跟的"元/¥/￥"
        remark = re.sub(r'^[元¥￥]\s*', '', remark)

        try:
            amount = float(amount_str)
        except ValueError:
            return None

        # 第三字段直出，不做归类
        category = m.group(3)

        return {
            "date": date_str,
            "person": person,
            "category": category,
            "amount": amount,
            "remark": remark
        }
    return None


def _try_colon_format(line: str) -> dict | None:
    """人名：品名 金额 — 品名直接提取，不做归类"""
    m = re.match(r'^([\u4e00-\u9fa5]{2,4})\s*[:：]\s*(.+)$', line)
    if not m:
        return None

    person = m.group(1)
    rest = m.group(2).strip()

    # 从 rest 中提取金额
    amount = _extract_amount_from_line(rest)
    if not amount:
        return None

    # 去掉金额部分和元/¥符号，剩余就是品名
    item_name = _strip_amount(rest, amount)

    date_str = _extract_date_from_line(line) or datetime.now().strftime("%Y-%m-%d")

    return {
        "date": date_str,
        "person": person,
        "category": item_name if item_name else "其他",
        "amount": amount,
        "remark": ""
    }


def _strip_amount(text: str, amount: float) -> str:
    """从文本中剥离金额部分，返回剩余内容"""
    # 整数显示时去掉 .0
    amount_str = str(int(amount)) if amount == int(amount) else str(amount)

    # 移除金额的各种写法：80元 / ¥80 / 80.00 等
    text = re.sub(re.escape(amount_str) + r'(?:\.0{1,2})?\s*元?', '', text)
    text = re.sub(r'[¥￥]\s*' + re.escape(amount_str) + r'(?:\.0{1,2})?', '', text)
    text = re.sub(r'\b' + re.escape(amount_str) + r'(?:\.0{1,2})?\s*元?\b', '', text)

    return text.strip().lstrip('-–—').strip()


def _extract_item_name(text: str, person: str, amount: float) -> str:
    """从原文中去掉人名、金额、日期、常见动词，剩余为品名"""
    result = text

    # 去掉人名
    result = result.replace(person, "", 1)

    # 去掉金额
    result = _strip_amount(result, amount)

    # 去掉日期
    result = re.sub(r'\d{1,2}月\d{1,2}[号日]', '', result)
    result = re.sub(r'\d{4}[-/.]\d{1,2}[-/.]\d{1,2}', '', result)

    # 去掉常见动词（长词优先，避免"购买"被"买"部分匹配）
    verbs = ['请客', '花了钱', '支付', '付款', '购买', '购入', '垫付', 
             '请', '花了', '花', '付了', '付', '买了', '买', '用了', '用',
             '给', '垫', '交了', '交']
    for v in sorted(verbs, key=len, reverse=True):
        result = result.replace(v, "")

    # 清理多余符号和空格
    result = re.sub(r'\s+', ' ', result)
    result = result.strip(' -–—:：，,。.、\t')

    return result if result else ""


def _try_dashed_format(line: str) -> dict | None:
    """分隔符格式: 张三 - 餐饮 128元 - 6月3日"""
    parts = re.split(r'\s*[-|]\s*', line)
    if len(parts) < 2:
        return None

    date_str = _extract_date_from_line(line)
    amount = _extract_amount_from_line(line)
    person = _extract_person_from_parts(parts)

    if not amount or not person:
        return None

    # 品名直出：从原文去掉人名和金额
    item_name = _extract_item_name(line, person, amount)

    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    return {
        "date": date_str,
        "person": person,
        "category": item_name if item_name else "其他",
        "amount": amount,
        "remark": ""
    }


def _try_free_format(line: str) -> dict | None:
    """自由格式解析：张三请客吃饭花了128.5元"""
    amount = _extract_amount_from_line(line)
    if not amount:
        return None

    date_str = _extract_date_from_line(line) or datetime.now().strftime("%Y-%m-%d")
    person = _extract_person_from_line(line)

    if not person:
        return None

    # 品名直出：从原文去掉人名和金额
    item_name = _extract_item_name(line, person, amount)

    return {
        "date": date_str,
        "person": person,
        "category": item_name if item_name else "其他",
        "amount": amount,
        "remark": ""
    }


def _extract_amount_from_line(text: str) -> float | None:
    """从文本中提取金额"""
    # 匹配 128.5元 / ¥128.5 / 128元 / 128.5 等
    patterns = [
        r'(?:¥|￥)\s*([\d,]+\.?\d*)',
        r'([\d,]+\.?\d*)\s*元',
        r'(?:金额|花费|用了|花了|付了|支付|付款|共计|合计)[:：]?\s*([\d,]+\.?\d*)',
        r'(?:金额|花费|用了|花了|付了|支付|付款|共计|合计)[:：]?\s*(?:¥|￥)\s*([\d,]+\.?\d*)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue

    # 最后尝试：独立的数字（有风险，放最后）
    numbers = re.findall(r'(?<!\d)(\d{2,6}\.?\d{0,2})(?!\d)', text)
    for n in numbers:
        try:
            val = float(n)
            if 0.5 <= val <= 100000:
                # 避免把日期当金额
                if _is_likely_date(n):
                    continue
                return val
        except ValueError:
            continue
    return None


def _is_likely_date(text: str) -> bool:
    """判断是否像日期"""
    return bool(re.match(r'^\d{4}\d{2}\d{2}$', text)) or bool(re.match(r'^\d{2}\d{2}$', text))


def _extract_date_from_line(text: str) -> str | None:
    """从文本中提取日期"""
    patterns = [
        r'(?<!\d)(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})(?!\d)',     # 2024-06-01
        r'(?<!\d)(\d{1,2}[-/.]\d{1,2})(?!\d)',                # 6-1 或 06/01
        r'(?<!\d)(\d{1,2})月(\d{1,2})[号日]',                  # 6月1号
        r'(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})[号日]?',       # 2024年6月1日
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            result = _normalize_date(m.group(0))
            if result:
                return result
    return None


def _normalize_date(date_str: str) -> str | None:
    """标准化日期为 YYYY-MM-DD，无效日期返回 None"""
    date_str = date_str.replace("/", "-").replace(".", "-")
    parts = re.findall(r'\d+', date_str)
    if len(parts) == 3:
        y, m, d = parts
        y = y.zfill(4) if len(y) == 2 else y
        m_int, d_int = int(m), int(d)
        if 1 <= m_int <= 12 and 1 <= d_int <= 31:
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        return None
    elif len(parts) == 2:
        m, d = parts
        m_int, d_int = int(m), int(d)
        if 1 <= m_int <= 12 and 1 <= d_int <= 31:
            y = datetime.now().year
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        return None
    return None


def _extract_person_from_line(text: str) -> str | None:
    """从文本中提取人名（支持中文名和英文/拼音名）"""
    # 0. 行首英文/拼音人名 + 空格/中文分隔 (如 "wangcheng 购买...")
    m = re.match(r'^([a-zA-Z][a-zA-Z0-9_]{1,19})\s', text)
    if m:
        return m.group(1)

    # 1. 最高优先级：行首人名 + 冒号（中英文）分隔
    m = re.match(r'^([\u4e00-\u9fa5]{2,4})\s*[:：]\s*', text)
    if m:
        return m.group(1)

    # 2. 行首的2-4字中文名 + 空格/连字符/数字
    m = re.match(r'^([\u4e00-\u9fa5]{2,4})[\s\-:：\d]', text)
    if m:
        return m.group(1)

    # 3. 人名后紧跟日期模式：张三6月1号
    m = re.search(r'([\u4e00-\u9fa5]{2,4})(?:\d{1,2}[月./-]|\d{4}[年./-])', text)
    if m:
        return m.group(1)

    # 4. 常见动词前缀：垫/付/买/请/发/寄/交/花/购
    m = re.search(r'([\u4e00-\u9fa5]{2,4})(?:垫|付|买|请|帮|代|淘|订|购|发|寄|花|交)', text)
    if m:
        return m.group(1)

    # 5. @或给 + 人名
    m = re.search(r'(?:@|给)([\u4e00-\u9fa5]{2,4})', text)
    if m:
        return m.group(1)

    # 6. 行中英文/拼音人名 + 空格 + 中文动词 (如 "xxx 购买 ...")
    m = re.search(r'\b([a-zA-Z][a-zA-Z0-9_]{1,19})\s+[\u4e00-\u9fa5]', text)
    if m:
        return m.group(1)

    return None


def _extract_person_from_parts(parts: list[str]) -> str | None:
    """从分隔的parts中提取人名"""
    for p in parts:
        p = p.strip()
        m = re.match(r'^([\u4e00-\u9fa5]{2,4})$', p)
        if m:
            return m.group(1)
    return None



