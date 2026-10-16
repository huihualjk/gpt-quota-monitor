# -*- coding: utf-8 -*-
"""
监控 Tibo (@thsottiaux) 的推特活动，检测到「新发帖」（他只会发 GPT 额度重置类推文）
时，立即通过 Server酱 推送微信通知。

原理：x.com 已对所有匿名访问关闭推文内容，但仍公开显示账号的「帖子总数」。
Tibo 一旦发布新的额度重置推文，帖子数就会 +1。监控帖子数变化即可可靠捕获事件。
数据源：r.jina.ai 网页代理（能从 GitHub 服务器抓取 x.com 页面并提取可见文本）。
"""

import os
import re
import json
import datetime
import urllib.request
import urllib.parse

STATE_FILE = "state.json"
SCREEN_NAME = "thsottiaux"
PROFILE_URL = "https://x.com/{0}".format(SCREEN_NAME)

# ---------------- 抓取帖子数 ----------------

def fetch_post_count():
    """从多个来源尝试获取 Tibo 的帖子总数，返回整数或 None。"""
    sources = [
        ("r.jina.ai", "https://r.jina.ai/https://x.com/{0}".format(SCREEN_NAME)),
        ("x.com直连", "https://x.com/{0}".format(SCREEN_NAME)),
    ]
    for name, url in sources:
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = resp.read().decode("utf-8", "ignore")

            count = extract_post_count(body)
            if count is not None:
                print("[{0}] 解析到帖子数: {1}".format(name, count))
                return count
            print("[{0}] 抓到了页面但没解析到帖子数".format(name))
        except Exception as e:
            print("[{0}] 失败: {1}".format(name, e))
    return None


def extract_post_count(body):
    """从页面文本中提取帖子总数。"""
    patterns = [
        r"([\d,]{1,12})\s*posts",
        r"([\d,]{1,12})\s*推文",
        r"([\d,]{1,12})\s*posts\b",
    ]
    for p in patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


# ---------------- 通知（Server酱 微信推送 + 可选聚合短信） ----------------

def send_serverchan(theme, text):
    """通过 Server酱 推送微信通知"""
    key = os.environ["SC_SENDKEY"]
    data = urllib.parse.urlencode({"title": theme, "desp": text}).encode()
    req = urllib.request.Request(
        "https://sctapi.ftqq.com/{0}.send".format(key),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8", "ignore"))
    if result.get("code") != 0:
        raise RuntimeError("Server酱推送失败: " + json.dumps(result, ensure_ascii=False))


def send_juhe_sms(last_count, count, now):
    """通过聚合数据短信API发送运营商短信（可选通道，未配置则跳过）。
    需要环境变量: JUHE_APPKEY / JUHE_TPL_ID / JUHE_MOBILE
    模板中可用变量: #last# (原帖子数) / #count# (新帖子数)"""
    appkey = os.environ.get("JUHE_APPKEY")
    tpl = os.environ.get("JUHE_TPL_ID")
    mobile = os.environ.get("JUHE_MOBILE")
    if not (appkey and tpl and mobile):
        print("未配置聚合短信，跳过运营商短信。")
        return
    tpl_value = urllib.parse.urlencode({"#last#": str(last_count), "#count#": str(count)})
    url = ("http://v.juhe.cn/sms/send?mobile={0}&tpl_id={1}&tpl_value={2}&key={3}"
           .format(mobile, tpl, tpl_value, appkey))
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
        if j.get("error_code") == 0:
            print("已发送运营商短信通知。")
        else:
            print("警告：聚合短信发送失败 -", json.dumps(j, ensure_ascii=False))
    except Exception as e:
        print("警告：聚合短信调用异常 -", e)


# ---------------- 状态持久化 ----------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------- 主流程 ----------------

def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state = load_state()
    last_count = state.get("last_post_count")

    count = fetch_post_count()
    if count is None:
        print("数据源: 全部失败，本次无法检查。")
        return

    state["last_post_count"] = count
    state["last_check"] = now

    if last_count is None:
        print("首次运行：记录当前帖子数 {0}，之后有新帖会通知。".format(count))
    elif count > last_count:
        diff = count - last_count
        body = (
            "**Tibo 可能刚刚发布了新的额度重置推文！**\n\n"
            "- 帖子数变化：{0} → {1}（新增 {2} 条）\n"
            "- 检测时间：{3}\n\n"
            "[点击立即查看 Tibo 主页]({4})\n\n"
            "（提醒：Tibo 只发额度重置类推文，新帖大概率就是额度刷新）"
        ).format(last_count, count, diff, now, PROFILE_URL)
        try:
            send_serverchan("【GPT额度重置】Tibo 发新推了！", body)
            print("已推送微信通知：帖子数 {0} → {1}".format(last_count, count))
            send_juhe_sms(last_count, count, now)
        except Exception as e:
            print("错误：微信推送失败 -", e)
    elif count < last_count:
        print("警告：帖子数竟然减少了（{0} → {1}），可能被删帖或解析异常，已更新基准。".format(last_count, count))
    else:
        print("帖子数无变化（{0}），本次未发现新的额度重置事件。".format(count))

    save_state(state)


if __name__ == "__main__":
    main()
