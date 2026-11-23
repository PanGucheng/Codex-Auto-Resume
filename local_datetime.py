"""Windows local time shared by both configuration surfaces."""

from datetime import datetime

from codex_client import MIN_SCHEDULE_LEAD_SECONDS


def parse_local_time(raw):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            date = datetime.strptime(raw.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError("请输入本机时间，格式为 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。")
    try:
        candidates = {date.replace(fold=fold).timestamp() for fold in (0, 1)}
        valid = [ts for ts in candidates if datetime.fromtimestamp(ts) == date]
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError("该时间超出 Windows 支持的范围。") from exc
    if len(valid) != 1:
        raise ValueError("该本机时间处于夏令时切换区间，请选用其他时间。")
    if valid[0] - datetime.now().timestamp() < MIN_SCHEDULE_LEAD_SECONDS:
        raise ValueError("发送时间至少晚于现在 20 秒。")
    return valid[0]
