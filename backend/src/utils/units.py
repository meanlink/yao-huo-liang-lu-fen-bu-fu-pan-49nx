"""电量单位与时间显示：结账相关的共用格式化。"""
from datetime import datetime, timezone


def kwh(milli):
    return round((milli or 0) / 1000.0, 3)


def fmt_time(ts):
    if ts is None:
        return ''
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
