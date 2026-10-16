# -*- coding: utf-8 -*-
"""
监控 Tibo (@thsottiaux) 的推特，检测到「GPT 额度重置」类新推文时，
立即通过 QQ 邮箱 SMTP 发送邮件通知。

设计要点：
- 纯 Python 标准库实现，无需安装任何第三方库
- 抓取策略（按顺序尝试，第一个成功就用它）：
    1. 直接抓 x.com 个人主页，解析内嵌 JSON 提取推文（在 GitHub 美服上最可靠）
    2. Nitter 实例的 RSS（免费镜像，部分可用）
    3. RSSHub 公共实例（社区镜像，部分可用）
- 关键词命中才发邮件（避免他的闲聊也轰炸邮箱）
- 用 state.json 记录已见过的推文 ID，防止同一条推文重复发信
- 运行所需配置全部来自环境变量（在 GitHub Secrets 中配置，不写死在代码里）
"""
import os
import re
import json
import ssl
import html
import smtplib
import urllib.request
from email.mime.text import MIMEText
from email.header import Header

# 要监控的账号
HANDLE = "thsottiaux"

# 命中任一关键词即视为「额度重置」事件（中文在前，英文在后）
KEYWORDS = ["重置", "额度", "reset", "quota", "credit", "credits", "token", "refresh"]

# 策略 2：Nitter 实例 RSS（按顺序尝试）
NITTER_SOURCES = [
    "https://nitter.net/{}/rss",
    "https://xcancel.com/{}/rss",
    "https://nitter.poast.org/{}/rss",
    "https://nitter.privacyredirect.com/{}/rss",
    "https://nitter.catsarch.com/{}/rss",
]

# 策略 3：RSSHub 公共实例（按顺序尝试）
RSSHUB_SOURCES = [
    "https://rsshub.rssforever.com/twitter/user/{}",
    "https://rsshub.pseudoyu.com/twitter/user/{}",
]

STATE_FILE = "state.json"
MAX_KNOWN = 200  # state 里最多保留的推文 ID 数量
FETCH_TIMEOUT = 15
MAX_TWEETS = 20  # 每次最多解析多少条最新推文


def http_get(url, timeout=FETCH_TIMEOUT):
    """抓取网页内容"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


# ---------------- 策略 1：x.com 页面内嵌 JSON 解析 ----------------

def walk_tweets(obj, out):
    """递归遍历 x.com 内嵌 JSON，找出所有 Tweet 对象"""
    if isinstance(obj, dict):
        if obj.get("__typename") == "Tweet" and obj.get("rest_id") and obj.get("legacy"):
            legacy = obj["legacy"]
            out.append({
                "id": obj["rest_id"],
                "text": legacy.get("full_text", ""),
                "created_at": legacy.get("created_at", ""),
            })
        for v in obj.values():
            walk_tweets(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_tweets(v, out)


def extract_from_xcom(html_text):
    """从 x.com 个人主页 HTML 里提取推文列表"""
    tweets = []
    # 页面里有多段 type="application/json" 的内嵌数据
    for block in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html_text, re.S):
        try:
            walk_tweets(json.loads(block), tweets)
        except Exception:
            continue
    if not tweets:
        # 兜底：直接正则抓 full_text + rest_id
        for rest_id, full_text in re.findall(
            r'"rest_id":"(\d{15,20})"[^}]{0,500}?"full_text":"((?:\\.|[^"\\])*)"', html_text
        ):
            tweets.append({"id": rest_id, "text": full_text, "created_at": ""})
    # 去重 + 按时间倒序 + 限制条数
    seen, uniq = set(), []
    for t in tweets:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    uniq.sort(key=lambda t: t["created_at"], reverse=True)
    return uniq[:MAX_TWEETS]


# ---------------- 策略 2/3：RSS 解析 ----------------

def parse_rss_items(xml):
    """极简 RSS 解析：提取每个 <item> 的标题、链接、时间"""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if title and link:
            items.append({
                "id": extract_tweet_id(html.unescape(link.group(1)).strip()),
                "text": html.unescape(re.sub(r"<.*?>", "", title.group(1))).strip(),
                "created_at": html.unescape(pub.group(1)).strip() if pub else "",
            })
    return items


def extract_tweet_id(link):
    """从推文链接里取出推文 ID（去重用）"""
    tid = link.rstrip("/").split("/")[-1]
    return tid.split("?")[0]


# ---------------- 状态持久化 ----------------

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_ids": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------- 邮件通知 ----------------

def send_mail(theme, text):
    """通过 QQ 邮箱 SMTP 发送邮件"""
    sender = os.environ["QQ_MAIL"]          # 发件邮箱（QQ 邮箱地址）
    auth = os.environ["QQ_AUTH_CODE"]       # QQ 邮箱授权码
    to = os.environ.get("QQ_MAIL_TO", sender)  # 收件邮箱，默认同发件邮箱

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(theme, "utf-8")
    msg["From"] = sender
    msg["To"] = to

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ctx, timeout=30) as server:
        server.login(sender, auth)
        server.sendmail(sender, [to], msg.as_string())


# ---------------- 主流程 ----------------

def fetch_tweets():
    """按策略顺序抓取推文，返回 (数据源说明, 推文列表)"""
    # 策略 1：x.com 个人主页
    try:
        html_text = http_get("https://x.com/{}".format(HANDLE))
        tweets = extract_from_xcom(html_text)
        if tweets:
            return "x.com 直接抓取", tweets
        print("[x.com] 抓到了页面但没解析出推文，换下一策略")
    except Exception as e:
        print("[x.com] 失败:", type(e).__name__, e)

    # 策略 2：Nitter RSS
    for tmpl in NITTER_SOURCES:
        try:
            items = parse_rss_items(http_get(tmpl.format(HANDLE)))
            if items:
                return "Nitter: " + tmpl, items
        except Exception as e:
            print("[Nitter]", tmpl, "失败:", type(e).__name__, e)

    # 策略 3：RSSHub
    for tmpl in RSSHUB_SOURCES:
        try:
            items = parse_rss_items(http_get(tmpl.format(HANDLE)))
            if items:
                return "RSSHub: " + tmpl, items
        except Exception as e:
            print("[RSSHub]", tmpl, "失败:", type(e).__name__, e)

    return None, []


def main():
    state = load_state()
    known = set(state.get("last_ids", []))
    source, tweets = fetch_tweets()

    print("数据源:", source or "全部失败")

    if not tweets:
        print("错误：所有数据源都不可用，本次无法检查。")
        state["last_ids"] = []
        save_state(state)
        return

    new_hits = []
    for t in tweets:
        if t["id"] in known:
            continue
        text = (t["text"] or "").lower()
        if any(k in text for k in KEYWORDS):
            new_hits.append(t)
        known.add(t["id"])

    if new_hits:
        lines = ["Tibo 发布了新的额度重置相关推文：", ""]
        for h in new_hits:
            lines.append("- 推文：" + h["text"])
            lines.append("- 链接：https://x.com/{}/status/{}".format(HANDLE, h["id"]))
            if h["created_at"]:
                lines.append("- 时间：" + h["created_at"])
            lines.append("")
        body = "\n".join(lines)
        try:
            send_mail("【GPT额度重置】Tibo 有新动作！", body)
            print("已发送邮件，命中", len(new_hits), "条")
        except Exception as e:
            print("错误：邮件发送失败 -", e)
    else:
        print("本次未发现新的额度重置事件。")

    # 更新去重状态（只保留最近的 ID，防止文件无限膨胀）
    state["last_ids"] = list(known)[-MAX_KNOWN:]
    save_state(state)
    print("状态已更新，共记录推文", len(state["last_ids"]), "条")


if __name__ == "__main__":
    main()
