"""问题中的明确查询日期。模糊年份、期间和冲突输入不能静默解释为今天。"""

import re
from datetime import date

from hotline.settings import ServiceError

DATE = re.compile(r"(?<!\d)(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})日?(?!\d)")


def query_date(question, explicit, today):
    selected = None
    if explicit:
        try:
            selected = date.fromisoformat(explicit)
        except ValueError as exc:
            raise ServiceError("查询日期必须为 YYYY-MM-DD") from exc
    dates = []
    for match in DATE.finditer(question):
        suffix = question[match.end() : match.end() + 6]
        # 发文日期是检索对象的属性，不自动充当业务适用日期。
        if re.match(r"(?:发布|印发|公布|颁布|出台|发布的|的通知|的文件)", suffix):
            continue
        try:
            dates.append(date(*map(int, match.groups())))
        except ValueError:
            return selected or today, "INVALID", "问题中的日期不存在，请确认具体查询日期。"
    values = set(dates)
    if len(values) > 1:
        return selected or today, "AMBIGUOUS", "问题涉及多个日期或时间段，请分别指定一个查询日期。"
    if values:
        value = dates[0]
        if selected and selected != value:
            return selected, "CONFLICT", "问题中的日期与查询日期设置不一致，请确认后重新查询。"
        return value, "QUESTION", ""
    if not selected:
        remainder = DATE.sub("", question)
        if re.search(r"\d{4}年|去年|前年|上个月|去年底|年初|年底|以前|当时", remainder):
            return today, "AMBIGUOUS", "问题包含历史时期，请补充具体查询日期，避免按今天的范围回答。"
    return selected or today, "EXPLICIT" if selected else "TODAY", ""
