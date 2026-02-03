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

MAX_TOPICS = int(os.environ.get("MAX_TOPICS", "50"))

MIN_COMMENT_PAGES = int(os.environ.get("MIN_COMMENT_PAGES", "5"))
MAX_COMMENT_PAGES = int(os.environ.get("MAX_COMMENT_PAGES", "10"))
PAGE_GROW = int(os.environ.get("PAGE_GROW", "10"))

LIKE_PROB = float(os.environ.get("LIKE_PROB", "0.3"))
MAX_LOOP_FACTOR = float(os.environ.get("MAX_LOOP_FACTOR", "8"))

# 蓝点约 5 秒才消失
MIN_READ_STAY = float(os.environ.get("MIN_READ_STAY", "5"))
READ_STATE_TIMEOUT = float(os.environ.get("READ_STATE_TIMEOUT", "20"))

# 接近底部触发加载：等待楼层增长的最长时间（秒）
NEAR_BOTTOM_WAIT_TIMEOUT = float(os.environ.get("NEAR_BOTTOM_WAIT_TIMEOUT", "22"))

# 每次翻页后：最多尝试阅读多少个“仍有蓝点”的楼层（找不到就跳过）
MAX_UNREAD_READS_PER_PAGE = int(os.environ.get("MAX_UNREAD_READS_PER_PAGE", "2"))

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN")
SC3_PUSH_KEY = os.environ.get("SC3_PUSH_KEY")
WXPUSH_URL = os.environ.get("WXPUSH_URL")
WXPUSH_TOKEN = os.environ.get("WXPUSH_TOKEN")

LIST_URL = "https://linux.do/latest"
HOME_FOR_COOKIE = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"

POST_CONTENT_CSS = "div.post__regular.regular.post__contents.contents"


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
    # URL normalize
    # ----------------------------
    def normalize_topic_url(self, url: str) -> str:
        try:
            if re.search(r"/\d+(\?.*)?$", url):
                return url
            if url.endswith("/"):
                return url + "1"
            return url + "/1"
        except Exception:
            return url

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
    # Posts ready
    # ----------------------------
    def wait_topic_posts_ready(self, page, timeout=60) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            try:
                ok = page.run_js(
                    f"""
                    const posts = Array.from(document.querySelectorAll('[id^="post_"]'));
                    if (!posts.length) return false;
                    for (const p of posts) {{
                      const c = p.querySelector('{POST_CONTENT_CSS}');
                      if (!c) continue;
                      const t = (c.innerText || c.textContent || '').trim();
                      if (t.length > 0) return true;
                    }}
                    return false;
                    """
                )
                if ok:
                    time.sleep(random.uniform(0.6, 1.2))
                    return True
            except Exception:
                pass
            time.sleep(0.5)

        logger.warning("未等到任何 post 正文渲染完成（可能结构变化/加载慢）")
        return False

    def _max_post_number_in_dom(self, page) -> int:
        try:
            return int(
                page.run_js(
                    """
                    let maxN = 0;
                    document.querySelectorAll('[id^="post_"]').forEach(el => {
                      const m = el.id.match(/^post_(\\d+)$/);
                      if (m) maxN = Math.max(maxN, parseInt(m[1], 10));
                    });
                    return maxN;
                    """
                )
                or 0
            )
        except Exception:
            return 0

    def _post_count_in_dom(self, page) -> int:
        try:
            return int(
                page.run_js(
                    """
                    return document.querySelectorAll('[id^="post_"]').length;
                    """
                )
                or 0
            )
        except Exception:
            return 0

    # ----------------------------
    # Near-bottom trigger loading
    # ----------------------------
    def scroll_near_bottom(self, page):
        try:
            page.run_js(
                """
                const h = document.body.scrollHeight;
                const ratio = 0.90 + Math.random() * 0.05;
                window.scrollTo(0, Math.floor(h * ratio));
                """
            )
        except Exception:
            pass

    def wait_max_post_grow(self, page, last_max_no: int, need_grow: int, timeout=22) -> int:
        end = time.time() + timeout
        best = last_max_no
        while time.time() < end:
            cur = self._max_post_number_in_dom(page)
            if cur > best:
                best = cur
            if cur - last_max_no >= need_grow:
                return cur
            time.sleep(0.6)
        return best

    # ----------------------------
    # Read-state / Blue-dot
    # ----------------------------
    def _post_is_read(self, page, post_id: int) -> bool:
        try:
            return bool(
                page.run_js(
                    """
                    const pid = arguments[0];
                    const root = document.querySelector(`#post_${pid}`);
                    if (!root) return false;

                    const rs = root.querySelector('.topic-meta-data .post-infos .read-state');
                    if (!rs) return false;

                    return rs.classList.contains('read');
                    """,
                    post_id,
                )
            )
        except Exception:
            return False

    def wait_blue_dot_gone(self, page, post_id: int, min_stay=5.0, timeout=20.0) -> bool:
        try:
            page.run_js(
                """
                const pid = arguments[0];
                const el = document.querySelector(`#post_${pid}`);
                if (el) el.scrollIntoView({behavior:'instant', block:'center'});
                """,
                post_id,
            )
        except Exception:
            pass

        time.sleep(min_stay)

        if self._post_is_read(page, post_id):
            return True

        end = time.time() + timeout
        while time.time() < end:
            if self._post_is_read(page, post_id):
                return True
            time.sleep(0.5)
        return False

    # ----------------------------
    # Only read unread posts (仍有蓝点) —— KEY
    # ----------------------------
    def pick_unread_post_ids(self, page, limit=2):
        """
        从当前 DOM 中找“仍有蓝点”的楼层（read-state 不含 read）
        返回 post_id 列表（随机抽样）
        """
        try:
            ids = page.run_js(
                """
                const out = [];
                document.querySelectorAll('[id^="post_"]').forEach(root => {
                  const m = root.id.match(/^post_(\\d+)$/);
                  if (!m) return;
                  const rs = root.querySelector('.topic-meta-data .post-infos .read-state');
                  if (!rs) return;
                  if (!rs.classList.contains('read')) out.push(parseInt(m[1], 10));
                });

                // shuffle
                for (let i = out.length - 1; i > 0; i--) {
                  const j = Math.floor(Math.random() * (i + 1));
                  [out[i], out[j]] = [out[j], out[i]];
                }
                return out;
                """
            )
            if not ids:
                return []
            ids = [int(x) for x in ids if str(x).isdigit()]
            if limit and len(ids) > limit:
                ids = ids[:limit]
            return ids
        except Exception:
            return []

    def read_only_unread_posts(self, page, max_reads=2):
        """
        只读“仍有蓝点”的楼层；已读楼层跳过
        找不到未读楼层就不强行停留
        """
        unread_ids = self.pick_unread_post_ids(page, limit=max_reads)
        if not unread_ids:
            logger.info("本页未发现“仍有蓝点”的楼层（可能都已读），跳过有效阅读")
            return 0

        read_ok = 0
        for pid in unread_ids:
            logger.info(f"👀 阅读未读楼层 post_{pid}（停留≥{MIN_READ_STAY}s，等待 read-state=read）")
            ok = self.wait_blue_dot_gone(
                page, pid, min_stay=MIN_READ_STAY, timeout=READ_STATE_TIMEOUT
            )
            if ok:
                read_ok += 1
                logger.success(f"✅ post_{pid} 已变为 read")
            else:
                logger.warning(f"⚠️ post_{pid} 等待 read 超时（但已停留≥{MIN_READ_STAY}s）")
        return read_ok

    # ----------------------------
    # Browse replies
    # ----------------------------
    def browse_replies_pages(self, page, min_pages=5, max_pages=10):
        if max_pages < min_pages:
            max_pages = min_pages
        target_pages = random.randint(min_pages, max_pages)
        logger.info(f"目标：浏览评论 {target_pages} 页（按楼层号增长计，PAGE_GROW={PAGE_GROW}）")

        self.wait_topic_posts_ready(page, timeout=60)

        pages_done = 0
        last_max_no = self._max_post_number_in_dom(page)
        last_cnt = self._post_count_in_dom(page)
        logger.info(f"初始：max_post_no={last_max_no}, dom_posts={last_cnt}")

        max_loops = int(target_pages * MAX_LOOP_FACTOR + 16)

        for i in range(max_loops):
            logger.info(f"[loop {i+1}] 滚到接近底部以触发加载更多评论...")
            self.scroll_near_bottom(page)

            cur_max_no = self.wait_max_post_grow(
                page,
                last_max_no,
                need_grow=PAGE_GROW,
                timeout=NEAR_BOTTOM_WAIT_TIMEOUT,
            )
            cur_cnt = self._post_count_in_dom(page)

            if cur_max_no - last_max_no >= PAGE_GROW:
                pages_done += 1
                logger.success(
                    f"✅ 第 {pages_done}/{target_pages} 页：max_post_no {last_max_no} -> {cur_max_no}（dom_posts={cur_cnt}）"
                )
                last_max_no = cur_max_no
                last_cnt = cur_cnt

                # ✅ 只读仍有蓝点的楼层（最多 MAX_UNREAD_READS_PER_PAGE 个）
                self.read_only_unread_posts(page, max_reads=MAX_UNREAD_READS_PER_PAGE)

                time.sleep(random.uniform(0.6, 1.8))
            else:
                time.sleep(random.uniform(1.2, 2.8))

            if pages_done >= target_pages:
                logger.success("🎉 已达到目标评论页数，结束浏览")
                return True

            try:
                at_bottom = page.run_js(
                    "return (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 5);"
                )
            except Exception:
                at_bottom = False

            if at_bottom:
                logger.success("已到达页面底部，结束浏览")
                if cur_max_no <= (min_pages * PAGE_GROW + 5):
                    logger.info(f"主题较短（max_post_no≈{cur_max_no}），放宽最小页数要求，视为完成")
                    return True
                return pages_done >= min_pages

        logger.warning("达到最大循环次数仍未完成目标页数（可能加载慢/主题很短）")
        return pages_done >= min_pages

    # ----------------------------
    # Topic list -> open topics
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
            fixed_url = self.normalize_topic_url(topic_url)
            new_page.get(fixed_url)

            self.wait_topic_posts_ready(new_page, timeout=60)
            time.sleep(random.uniform(1.0, 2.0))

            if random.random() < LIKE_PROB:
                self.click_like(new_page)

            ok = self.browse_replies_pages(
                new_page,
                min_pages=MIN_COMMENT_PAGES,
                max_pages=MAX_COMMENT_PAGES,
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
                f" + 浏览任务完成(话题<= {MAX_TOPICS} 个, 评论{MIN_COMMENT_PAGES}-{MAX_COMMENT_PAGES}页, "
                f"PAGE_GROW={PAGE_GROW}, 只读蓝点楼层<= {MAX_UNREAD_READS_PER_PAGE}/页, "
                f"MIN_READ_STAY={MIN_READ_STAY}s, near-bottom=on)"
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


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("Please set LINUXDO_USERNAME/LINUXDO_PASSWORD (or USERNAME/PASSWORD)")
        raise SystemExit(1)

    l = LinuxDoBrowser()
    l.run()
