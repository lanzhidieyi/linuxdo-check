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

# 每次运行最多进入多少个话题帖
MAX_TOPICS = int(os.environ.get("MAX_TOPICS", "50"))

# 每个话题至少/最多浏览多少“页/批次”评论
MIN_COMMENT_PAGES = int(os.environ.get("MIN_COMMENT_PAGES", "5"))
MAX_COMMENT_PAGES = int(os.environ.get("MAX_COMMENT_PAGES", "10"))

# 用“帖子节点增长”计页时，每增长多少个帖子算 1 页（可调小/大）
PAGE_POST_GROW = int(os.environ.get("PAGE_POST_GROW", "10"))

# 你提供的：评论内容 XPath（用于确认评论真实渲染完成）
COMMENT_XPATH = os.environ.get(
    "COMMENT_XPATH",
    "/html/body/section/div[1]/div[4]/div[2]/div[3]/div[3]/div[3]/section/div[1]/div[2]/div[4]/article/div/div[2]/div[2]",
)

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

        # Discourse 前端渲染等待
        try:
            self.page.wait.ele("@id=main-outlet", timeout=25)
        except Exception:
            logger.warning("未等到 main-outlet，但继续尝试查找 topic link")

        ok = self._wait_any_topic_link(timeout=35)
        if not ok:
            logger.warning("未等到主题链接 a.raw-topic-link，输出页面信息辅助定位")
            logger.warning(f"url={self.page.url}")
            logger.warning((self.page.html or "")[:500])
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
    # XPath helpers (用于确认评论真实渲染)
    # ----------------------------
    def _xpath_exists(self, page, xpath: str) -> bool:
        try:
            return bool(
                page.run_js(
                    r"""
                const xp = arguments[0];
                const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                return !!n;
            """,
                    xpath,
                )
            )
        except Exception:
            return False

    def _xpath_visible(self, page, xpath: str) -> bool:
        try:
            return bool(
                page.run_js(
                    r"""
                const xp = arguments[0];
                const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!n) return false;
                const r = n.getBoundingClientRect();
                const style = window.getComputedStyle(n);
                return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
            """,
                    xpath,
                )
            )
        except Exception:
            return False

    def _xpath_text_len(self, page, xpath: str) -> int:
        try:
            return int(
                page.run_js(
                    r"""
                const xp = arguments[0];
                const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                if (!n) return 0;
                return (n.innerText || n.textContent || '').trim().length;
            """,
                    xpath,
                )
                or 0
            )
        except Exception:
            return 0

    def wait_comment_loaded_by_xpath(self, page, xpath: str, timeout=45) -> bool:
        """
        等评论区域真正加载：
        - XPath 节点存在
        - 可见
        - 文本非空（避免只是壳）
        """
        end = time.time() + timeout
        while time.time() < end:
            if self._xpath_exists(page, xpath) and self._xpath_visible(page, xpath):
                if self._xpath_text_len(page, xpath) > 0:
                    return True
            time.sleep(0.5)
        return False

    # ----------------------------
    # Timeline helpers (蓝点区域)
    # ----------------------------
    def _topic_progress_text(self, page) -> str:
        try:
            return (
                page.run_js(
                    """
                const a = document.querySelector('#topic-progress');
                if (a) return a.innerText.trim();
                const b = document.querySelector('.topic-timeline .current-post');
                if (b) return b.innerText.trim();
                const c = document.querySelector('.timeline-container .current-post');
                if (c) return c.innerText.trim();
                return '';
            """
                )
                or ""
            ).strip()
        except Exception:
            return ""

    def _current_post_number(self, page) -> int:
        try:
            s = self._topic_progress_text(page)
            m = re.search(r"#(\\d+)", s)
            return int(m.group(1)) if m else 0
        except Exception:
            return 0

    def wait_topic_progress_stable(self, page, stable_seconds=2.5, timeout=25) -> bool:
        """
        等右侧时间轴（蓝点区域）稳定：文本 stable_seconds 内不再变化
        """
        end = time.time() + timeout
        last_text = None
        stable_start = None

        while time.time() < end:
            text = self._topic_progress_text(page)
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

        return False

    # ----------------------------
    # Count posts as fallback
    # ----------------------------
    def _topic_article_count(self, page) -> int:
        try:
            return int(
                page.run_js(
                    r"""
                const ps = document.querySelector('#post-stream') || document;
                let n = ps.querySelectorAll('article').length;
                if (n) return n;
                n = ps.querySelectorAll('.topic-post, .post').length;
                return n || 0;
            """
                )
                or 0
            )
        except Exception:
            return 0

    # ----------------------------
    # Wait topic ready (用 XPath 为主)
    # ----------------------------
    def wait_topic_posts_ready(self, page, timeout=50) -> bool:
        """
        linux.do 实测：用评论内容 XPath 判断最稳
        """
        ok = self.wait_comment_loaded_by_xpath(page, COMMENT_XPATH, timeout=timeout)
        if not ok:
            logger.warning("未等到评论内容 XPath（可能结构变化/被拦截/页面未渲染）")
            return False

        # 给前端状态更新一点时间
        time.sleep(random.uniform(1.0, 2.0))
        return True

    # ----------------------------
    # Browse replies pages (5-10)
    # ----------------------------
    def browse_replies_pages(self, page, min_pages=5, max_pages=10):
        """
        至少浏览 min_pages 页，最多 max_pages 页
        计页策略：
          1) 优先：右侧楼层号增长（#2422 这种）
          2) fallback：帖子节点数量每增长 PAGE_POST_GROW 记 1 页
        短帖策略：到底且总量很少时，不算失败
        """
        if max_pages < min_pages:
            max_pages = min_pages
        target_pages = random.randint(min_pages, max_pages)
        logger.info(f"目标：浏览评论 {target_pages} 页（批次）")

        ready = self.wait_topic_posts_ready(page, timeout=55)
        if not ready:
            logger.warning("帖子流未确认 ready，但继续尝试滚动浏览（不中断）")

        time.sleep(random.uniform(1.2, 2.5))

        pages_done = 0
        last_post_no = self._current_post_number(page)
        last_cnt = self._topic_article_count(page)

        if last_post_no:
            logger.info(f"初始楼层号: #{last_post_no}")
        else:
            logger.info(f"初始未读到楼层号，fallback 用帖子数计页；初始帖子数={last_cnt}")

        max_loops = target_pages * 7 + 14
        for i in range(max_loops):
            scroll_distance = random.randint(900, 1500)
            logger.info(f"[loop {i+1}] 向下滚动 {scroll_distance}px 浏览评论...")
            page.run_js(f"window.scrollBy(0, {scroll_distance});")

            time.sleep(random.uniform(0.8, 1.6))
            self.wait_topic_progress_stable(
                page,
                stable_seconds=random.uniform(1.8, 3.0),
                timeout=25
            )

            # 判断到底
            try:
                at_bottom = page.run_js(
                    "return (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 5);"
                )
            except Exception:
                at_bottom = False

            # 1) 楼层号计页（优先）
            cur_post_no = self._current_post_number(page)
            if cur_post_no and last_post_no and cur_post_no > last_post_no:
                pages_done += 1
                logger.success(
                    f"✅ 已浏览第 {pages_done}/{target_pages} 页（楼层 #{last_post_no} -> #{cur_post_no}）"
                )
                last_post_no = cur_post_no
                time.sleep(random.uniform(3.5, 8.0))
            else:
                # 2) fallback：帖子数增长计页
                cur_cnt = self._topic_article_count(page)
                if cur_cnt - last_cnt >= PAGE_POST_GROW:
                    pages_done += 1
                    logger.success(
                        f"✅ 已浏览第 {pages_done}/{target_pages} 页（帖子数 {last_cnt} -> {cur_cnt}）"
                    )
                    last_cnt = cur_cnt
                    time.sleep(random.uniform(3.0, 7.0))
                else:
                    time.sleep(random.uniform(2.0, 5.0))

            if pages_done >= target_pages:
                logger.success("🎉 已达到目标评论页数，结束浏览")
                return True

            if at_bottom:
                total_cnt = self._topic_article_count(page)
                logger.success("已到达页面底部，结束浏览")

                # 短帖容错：总量太少就放宽最小页数
                if total_cnt <= (min_pages * PAGE_POST_GROW + 5):
                    logger.info(f"该主题较短（总帖子数={total_cnt}），放宽最小页数要求，视为完成")
                    return True
                return pages_done >= min_pages

        logger.warning("达到最大循环次数仍未完成目标页数（可能加载慢/结构变化）")
        return pages_done >= min_pages

    # ----------------------------
    # Browse from latest list
    # ----------------------------
    def click_topic(self):
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

        count = min(MAX_TOPICS, len(topic_links))
        logger.info(f"发现 {len(topic_links)} 个主题帖，随机选择 {count} 个进行浏览")

        for a in random.sample(topic_links, count):
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

            # 先等评论真实渲染 + 时间轴稳定
            self.wait_topic_posts_ready(new_page, timeout=55)
            time.sleep(random.uniform(1.0, 2.0))
            self.wait_topic_progress_stable(new_page, stable_seconds=2.2, timeout=25)

            # 点赞（可选）
            if random.random() < 0.3:
                self.click_like(new_page)

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
            status_msg += (
                f" + 浏览任务完成(话题<= {MAX_TOPICS} 个, 评论{MIN_COMMENT_PAGES}-{MAX_COMMENT_PAGES}页)"
            )

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
