"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
import re
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup

# ----------------------------
# Retry Decorator
# ----------------------------
def retry_decorator(retries=3, min_delay=5, max_delay=10):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    if attempt < retries - 1:
                        sleep_s = random.uniform(min_delay, max_delay)
                        logger.info(
                            f"将在 {sleep_s:.2f}s 后重试 ({min_delay}-{max_delay}s 随机延迟)"
                        )
                        time.sleep(sleep_s)
            return None

        return wrapper

    return decorator


# ----------------------------
# Env & Config
# ----------------------------
os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME") or os.environ.get("USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD") or os.environ.get("PASSWORD")

BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]

# 你要的：至少浏览 5~10 “页/批次” 评论（默认）
MIN_COMMENT_PAGES = int(os.environ.get("MIN_COMMENT_PAGES", "5"))
MAX_COMMENT_PAGES = int(os.environ.get("MAX_COMMENT_PAGES", "10"))

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN")
SC3_PUSH_KEY = os.environ.get("SC3_PUSH_KEY")
WXPUSH_URL = os.environ.get("WXPUSH_URL")
WXPUSH_TOKEN = os.environ.get("WXPUSH_TOKEN")

# 访问入口
LIST_URL = "https://linux.do/latest"
HOME_FOR_COOKIE = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform.startswith("linux"):
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"
        else:
            platformIdentifier = "X11; Linux x86_64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    # ----------------------------
    # Headers
    # ----------------------------
    def _api_headers(self):
        return {
            "User-Agent": self.session.headers.get("User-Agent"),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
            "Origin": "https://linux.do",
        }

    def _html_headers(self):
        return {
            "User-Agent": self.session.headers.get("User-Agent"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": HOME_FOR_COOKIE,
        }

    # ----------------------------
    # CSRF + Login
    # ----------------------------
    def _get_csrf_token(self) -> str:
        r0 = self.session.get(
            HOME_FOR_COOKIE,
            headers=self._html_headers(),
            impersonate="chrome136",
            allow_redirects=True,
            timeout=30,
        )
        logger.info(
            f"HOME: status={r0.status_code} ct={r0.headers.get('content-type')} url={getattr(r0, 'url', None)}"
        )

        resp_csrf = self.session.get(
            CSRF_URL,
            headers=self._api_headers(),
            impersonate="chrome136",
            allow_redirects=True,
            timeout=30,
        )
        ct = (resp_csrf.headers.get("content-type") or "").lower()
        logger.info(
            f"CSRF: status={resp_csrf.status_code} ct={resp_csrf.headers.get('content-type')} url={getattr(resp_csrf, 'url', None)}"
        )

        if resp_csrf.status_code != 200 or "application/json" not in ct:
            head = (resp_csrf.text or "")[:200]
            raise RuntimeError(
                f"CSRF not JSON. status={resp_csrf.status_code}, ct={ct}, head={head}"
            )

        data = resp_csrf.json()
        csrf = data.get("csrf")
        if not csrf:
            raise RuntimeError(f"CSRF JSON missing token keys: {list(data.keys())}")
        return csrf

    def login(self):
        logger.info("开始登录")
        logger.info("获取 CSRF token...")

        try:
            csrf_token = self._get_csrf_token()
        except Exception as e:
            logger.error(f"获取 CSRF 失败：{e}")
            return False

        logger.info("正在登录...")

        headers = self._api_headers()
        headers.update(
            {
                "X-CSRF-Token": csrf_token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
        )

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "timezone": "Asia/Shanghai",
        }

        try:
            resp_login = self.session.post(
                SESSION_URL,
                data=data,
                impersonate="chrome136",
                headers=headers,
                allow_redirects=True,
                timeout=30,
            )
            logger.info(
                f"LOGIN: status={resp_login.status_code} ct={resp_login.headers.get('content-type')} url={getattr(resp_login, 'url', None)}"
            )

            ct = (resp_login.headers.get("content-type") or "").lower()
            if "application/json" not in ct:
                logger.error(f"登录返回不是 JSON，head={resp_login.text[:200]}")
                return False

            response_json = resp_login.json()
            if response_json.get("error"):
                logger.error(f"登录失败: {response_json.get('error')}")
                return False

            logger.info("登录成功!")
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False

        self.print_connect_info()

        # 同步 Cookie 到 DrissionPage
        logger.info("同步 Cookie 到 DrissionPage...")
        cookies_dict = self.session.cookies.get_dict()
        dp_cookies = []
        for name, value in cookies_dict.items():
            dp_cookies.append(
                {"name": name, "value": value, "domain": ".linux.do", "path": "/"}
            )
        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至主题列表页 /latest ...")
        self.page.get(LIST_URL)

        # Discourse 前端渲染：更稳的等待策略
        try:
            self.page.wait.ele("@id=main-outlet", timeout=25)
        except Exception:
            logger.warning("未等到 main-outlet，但继续尝试查找 topic link")

        ok = self._wait_any_topic_link(timeout=35)
        if not ok:
            logger.warning("未等到主题链接 a.raw-topic-link，输出页面信息辅助定位")
            logger.warning(f"url={self.page.url}")
            logger.warning((self.page.html or "")[:500])
            # requests 已登录成功，这里不强行失败
            return True

        logger.info("主题列表已渲染，登录&页面加载完成")
        return True

    def _wait_any_topic_link(self, timeout=30) -> bool:
        """等待 Discourse 主题标题链接出现"""
        end = time.time() + timeout
        while time.time() < end:
            try:
                links = self.page.eles("css:a.raw-topic-link")
                if links and len(links) > 0:
                    return True
            except Exception:
                pass
            time.sleep(0.8)
        return False

    # ----------------------------
    # Topic load helpers (关键：评论/回复加载)
    # ----------------------------
    def wait_topic_posts_ready(self, page, timeout=35) -> bool:
        """
        等待话题页的帖子流出现，并尽量等加载状态结束
        """
        end = time.time() + timeout

        # 等至少一个 post
        try:
            page.wait.ele("css:article.topic-post", timeout=min(20, timeout))
        except Exception:
            logger.warning("未等到 article.topic-post（帖子流），可能结构变更或被拦截")
            return False

        # 等 aria-busy / 常见 loading 消失
        while time.time() < end:
            try:
                busy = page.run_js("""
                    const ps = document.querySelector('#post-stream');
                    return ps && ps.getAttribute('aria-busy') === 'true';
                """)
                if busy:
                    time.sleep(0.4)
                    continue

                loading_visible = page.run_js("""
                    const sels = [
                      '.spinner', '.loading-container', '.loading', '.topic-loading',
                      '.composer-loading', '.discourse-spinner'
                    ];
                    return sels.some(sel => {
                      const el = document.querySelector(sel);
                      if (!el) return false;
                      const st = window.getComputedStyle(el);
                      return st && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                    });
                """)
                if loading_visible:
                    time.sleep(0.4)
                    continue

                # 稳妥：再等一下
                time.sleep(random.uniform(1.2, 2.0))
                return True
            except Exception:
                time.sleep(0.5)

        logger.warning("等待帖子流 ready 超时")
        return True  # 不硬失败，避免影响后续

    def wait_topic_progress_stable(self, page, stable_seconds=2.5, timeout=25) -> bool:
        """
        等右侧时间轴（你图里蓝点所在区域）稳定：
        判定标准：#topic-progress 的 innerText 在 stable_seconds 内不再变化
        """
        end = time.time() + timeout
        last_text = None
        stable_start = None

        while time.time() < end:
            try:
                text = page.run_js("""
                    const el = document.querySelector('#topic-progress');
                    return el ? el.innerText.trim() : null;
                """)
                if not text:
                    time.sleep(0.4)
                    continue

                if text == last_text:
                    if stable_start is None:
                        stable_start = time.time()
                    elif time.time() - stable_start >= stable_seconds:
                        return True
                else:
                    last_text = text
                    stable_start = None

                time.sleep(0.4)
            except Exception:
                time.sleep(0.4)

        return False

    def _current_post_number(self, page) -> int:
        """从右侧时间轴提取当前楼层号，如 '#2422' -> 2422；取不到返回 0"""
        try:
            s = page.run_js("""
                const el = document.querySelector('#topic-progress');
                return el ? el.innerText : '';
            """) or ""
            m = re.search(r"#(\\d+)", s)
            return int(m.group(1)) if m else 0
        except Exception:
            return 0

    def browse_replies_pages(self, page, min_pages=5, max_pages=10):
        """
        至少浏览 min_pages 页，最多 max_pages 页（按“右侧楼层号增长”计页）
        """
        # 目标页数随机化（更像真人）
        if max_pages < min_pages:
            max_pages = min_pages
        target_pages = random.randint(min_pages, max_pages)

        logger.info(f"目标：浏览评论 {target_pages} 页（批次）")

        # 1) 确保回复流 ready
        self.wait_topic_posts_ready(page, timeout=40)

        # 2) 像人停顿一下
        time.sleep(random.uniform(1.5, 3.0))

        pages_done = 0
        last_post_no = self._current_post_number(page)
        if last_post_no:
            logger.info(f"初始时间轴楼层号: #{last_post_no}")
        else:
            logger.info("初始未能读取楼层号（仍继续浏览）")

        # 防止死循环：最多尝试次数
        max_loops = target_pages * 6 + 12

        for i in range(max_loops):
            # 分段滚动（更像“翻页/刷评论”）
            scroll_distance = random.randint(900, 1400)
            logger.info(f"[loop {i+1}] 向下滚动 {scroll_distance}px 浏览评论...")
            page.run_js(f"window.scrollBy(0, {scroll_distance});")

            # 小等待让前端触发加载
            time.sleep(random.uniform(0.8, 1.6))

            # 关键：等右侧时间轴稳定（你说的蓝点不再变化/加载完成）
            self.wait_topic_progress_stable(
                page,
                stable_seconds=random.uniform(2.0, 3.2),
                timeout=25
            )

            # 读取当前楼层号
            cur_post_no = self._current_post_number(page)

            # 判断是否“翻到下一页/批次”
            if cur_post_no and last_post_no and cur_post_no > last_post_no:
                pages_done += 1
                logger.success(
                    f"✅ 已浏览第 {pages_done}/{target_pages} 页（楼层 #{last_post_no} -> #{cur_post_no}）"
                )
                last_post_no = cur_post_no

                # 翻页后多停留：像在读这一屏评论
                time.sleep(random.uniform(3.5, 8.0))
            else:
                # 没翻页也停留：像在读已加载的评论
                time.sleep(random.uniform(2.0, 5.0))

            if pages_done >= target_pages:
                logger.success("🎉 已达到目标评论页数，结束浏览")
                return True

            # 到底部退出
            try:
                at_bottom = page.run_js(
                    "return (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 5);"
                )
                if at_bottom:
                    logger.success("已到达页面底部，结束浏览")
                    return pages_done >= min_pages
            except Exception:
                pass

        logger.warning("达到最大循环次数仍未完成目标页数（可能加载慢/结构变化）")
        return pages_done >= min_pages

    # ----------------------------
    # Browse from latest list
    # ----------------------------
    def click_topic(self):
        # 确保在列表页
        if not self.page.url.startswith("https://linux.do/latest"):
            self.page.get(LIST_URL)

        if not self._wait_any_topic_link(timeout=35):
            logger.error("未找到 a.raw-topic-link（主题标题链接），可能页面未渲染完成或结构变更")
            logger.error(f"当前URL: {self.page.url}")
            logger.error((self.page.html or "")[:500])
            return False

        topic_links = self.page.eles("css:a.raw-topic-link")
        if not topic_links:
            logger.error("主题链接列表为空")
            logger.error(f"当前URL: {self.page.url}")
            logger.error((self.page.html or "")[:500])
            return False

        logger.info(f"发现 {len(topic_links)} 个主题帖，随机选择50个")
        for a in random.sample(topic_links, min(50, len(topic_links))):
            href = a.attr("href")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://linux.do" + href
            self.click_one_topic(href)

        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        new_page = self.browser.new_tab()
        try:
            new_page.get(topic_url)

            # 先等回复/评论流 ready + 时间轴稳定
            self.wait_topic_posts_ready(new_page, timeout=40)
            time.sleep(random.uniform(1.2, 2.5))
            self.wait_topic_progress_stable(new_page, stable_seconds=2.2, timeout=25)

            # 点赞（可选）
            if random.random() < 0.3:
                self.click_like(new_page)

            # ✅ 关键：至少浏览 5~10 页评论（按右侧楼层号增长计页）
            ok = self.browse_replies_pages(
                new_page,
                min_pages=MIN_COMMENT_PAGES,
                max_pages=MAX_COMMENT_PAGES
            )
            if not ok:
                logger.warning("本主题未达到最小评论页数目标（可能帖子很短/到底/加载慢）")

        finally:
            try:
                new_page.close()
            except Exception:
                pass

    # ----------------------------
    # Like
    # ----------------------------
    def click_like(self, page):
        try:
            like_button = page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                time.sleep(random.uniform(1, 2))
            else:
                logger.info("帖子可能已经点过赞了")
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")

    # ----------------------------
    # Run
    # ----------------------------
    def run(self):
        try:
            login_res = self.login()
            if not login_res:
                logger.warning("登录失败，后续任务可能无法进行")

            if BROWSE_ENABLED:
                click_topic_res = self.click_topic()
                if not click_topic_res:
                    logger.error("点击主题失败，程序终止")
                    return
                logger.info("完成浏览任务（含评论浏览）")

            self.send_notifications(BROWSE_ENABLED)
        finally:
            try:
                self.page.close()
            except Exception:
                pass
            try:
                self.browser.quit()
            except Exception:
                pass

    # ----------------------------
    # Connect info
    # ----------------------------
    def print_connect_info(self):
        logger.info("获取连接信息")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = self.session.get(
            "https://connect.linux.do/",
            headers=headers,
            impersonate="chrome136",
            allow_redirects=True,
            timeout=30,
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        info = []

        for row in rows:
            cells = row.select("td")
            if len(cells) >= 3:
                project = cells[0].text.strip()
                current = cells[1].text.strip() if cells[1].text.strip() else "0"
                requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                info.append([project, current, requirement])

        print("--------------Connect Info-----------------")
        print(tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))

    # ----------------------------
    # Notifications
    # ----------------------------
    def send_notifications(self, browse_enabled):
        status_msg = f"✅每日登录成功: {USERNAME}"
        if browse_enabled:
            status_msg += f" + 浏览任务完成(含评论{MIN_COMMENT_PAGES}-{MAX_COMMENT_PAGES}页)"

        if GOTIFY_URL and GOTIFY_TOKEN:
            try:
                response = requests.post(
                    f"{GOTIFY_URL}/message",
                    params={"token": GOTIFY_TOKEN},
                    json={"title": "LINUX DO", "message": status_msg, "priority": 1},
                    timeout=10,
                )
                response.raise_for_status()
                logger.success("消息已推送至Gotify")
            except Exception as e:
                logger.error(f"Gotify推送失败: {str(e)}")
        else:
            logger.info("未配置Gotify环境变量，跳过通知发送")

        if SC3_PUSH_KEY:
            match = re.match(r"sct(\d+)t", SC3_PUSH_KEY, re.I)
            if not match:
                logger.error("❌ SC3_PUSH_KEY格式错误，未获取到UID，无法使用Server酱³推送")
                return

            uid = match.group(1)
            url = f"https://{uid}.push.ft07.com/send/{SC3_PUSH_KEY}"
            params = {"title": "LINUX DO", "desp": status_msg}

            attempts = 5
            for attempt in range(attempts):
                try:
                    response = requests.get(url, params=params, timeout=10)
                    response.raise_for_status()
                    logger.success(f"Server酱³推送成功: {response.text}")
                    break
                except Exception as e:
                    logger.error(f"Server酱³推送失败: {str(e)}")
                    if attempt < attempts - 1:
                        sleep_time = random.randint(180, 360)
                        logger.info(f"将在 {sleep_time} 秒后重试...")
                        time.sleep(sleep_time)

        if WXPUSH_URL and WXPUSH_TOKEN:
            try:
                response = requests.post(
                    f"{WXPUSH_URL}/wxsend",
                    headers={
                        "Authorization": WXPUSH_TOKEN,
                        "Content-Type": "application/json",
                    },
                    json={"title": "LINUX DO", "content": status_msg},
                    timeout=10,
                )
                response.raise_for_status()
                logger.success(f"wxpush 推送成功: {response.text}")
            except Exception as e:
                logger.error(f"wxpush 推送失败: {str(e)}")
        else:
            logger.info("未配置 WXPUSH_URL 或 WXPUSH_TOKEN，跳过通知发送")


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("Please set LINUXDO_USERNAME/LINUXDO_PASSWORD (or USERNAME/PASSWORD)")
        raise SystemExit(1)

    l = LinuxDoBrowser()
    l.run()
