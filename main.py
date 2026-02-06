"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
import re
import tempfile
from pathlib import Path

from loguru import logger
from tabulate import tabulate
from bs4 import BeautifulSoup
from curl_cffi import requests

from DrissionPage import ChromiumOptions, Chromium


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
# ⚠️ 不要 pop DISPLAY：你在 Actions + Xvfb 里需要它
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME") or os.environ.get("USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD") or os.environ.get("PASSWORD")

BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]

# ✅ GitHub Actions + Xvfb 建议：HEADLESS=false
# （workflow 会强制给 HEADLESS=false）
HEADLESS = os.environ.get("HEADLESS", "false").strip().lower() not in ["false", "0", "off"]

# 每次运行最多进入多少个话题帖
MAX_TOPICS = int(os.environ.get("MAX_TOPICS", "50"))

# 每个话题至少/最多浏览多少“页/批次”评论
MIN_COMMENT_PAGES = int(os.environ.get("MIN_COMMENT_PAGES", "5"))
MAX_COMMENT_PAGES = int(os.environ.get("MAX_COMMENT_PAGES", "10"))

# “翻一页评论”的判定：最大楼层号增长多少算 1 页（建议 8~15；默认 10）
PAGE_GROW = int(os.environ.get("PAGE_GROW", "10"))

# 点赞概率（0~1）
LIKE_PROB = float(os.environ.get("LIKE_PROB", "0.3"))

# 大步滚动距离（推进楼层增长）
SCROLL_MIN = int(os.environ.get("SCROLL_MIN", "1000"))
SCROLL_MAX = int(os.environ.get("SCROLL_MAX", "1600"))

# 阅读式小步滚动（借鉴 bookmarklet 节奏）
READ_STEP_MIN = int(os.environ.get("READ_STEP_MIN", "14"))
READ_STEP_MAX = int(os.environ.get("READ_STEP_MAX", "60"))
READ_DELAY_MIN = float(os.environ.get("READ_DELAY_MIN", "0.52"))
READ_DELAY_MAX = float(os.environ.get("READ_DELAY_MAX", "0.98"))

# 每个话题最多滚动循环次数倍率（避免死循环）
MAX_LOOP_FACTOR = float(os.environ.get("MAX_LOOP_FACTOR", "10"))

# ✅ 写死默认（也可 env 覆盖）
MIN_READ_STAY = float(os.environ.get("MIN_READ_STAY", "5"))
READ_STATE_TIMEOUT = float(os.environ.get("READ_STATE_TIMEOUT", "20"))

# 接近底部判定阈值
NEAR_BOTTOM_GAP = int(os.environ.get("NEAR_BOTTOM_GAP", "140"))
BOTTOM_EXTRA_STAY_MIN = float(os.environ.get("BOTTOM_EXTRA_STAY_MIN", "6"))
BOTTOM_EXTRA_STAY_MAX = float(os.environ.get("BOTTOM_EXTRA_STAY_MAX", "12"))

# Chrome 路径（Actions 下建议用 /usr/bin/google-chrome）
CHROME_PATH = os.environ.get("CHROME_PATH", "/usr/bin/google-chrome")

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

# 你提供的帖子结构关键选择器（用于确认评论/回复已渲染）
POST_CONTENT_CSS = "div.post__regular.regular.post__contents.contents"


def _rand_port():
    # 避免 9222 冲突：随机选一个高位端口
    return random.randint(20000, 45000)


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

        # ✅ 每次运行独立 user-data-dir，避免 Actions 并发/残留导致端口或 profile 冲突
        self._profile_dir = Path(tempfile.mkdtemp(prefix="linuxdo_profile_")).resolve()
        self._debug_port = _rand_port()

        co = ChromiumOptions().incognito(True)

        # ✅ 指定 Chrome 路径（Actions 很关键）
        # DrissionPage 版本不同方法名可能不同：做兼容
        if hasattr(co, "set_browser_path"):
            co.set_browser_path(CHROME_PATH)
        elif hasattr(co, "set_paths"):
            try:
                co.set_paths(browser_path=CHROME_PATH)
            except Exception:
                pass

        # ✅ user-data-dir + remote-debugging-port：彻底绕开 9222 冲突
        co.set_argument(f"--user-data-dir={str(self._profile_dir)}")
        co.set_argument(f"--remote-debugging-port={self._debug_port}")

        # Linux Actions 常用参数
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-gpu")

        # ✅ HEADLESS=false + Xvfb：更像“真实浏览器”
        # DrissionPage 的 headless(True/False) 语义：True=无头
        # 我们用 env HEADLESS=false => 这里 headless(False)
        co.headless(HEADLESS)

        # 尽量避免后台节流（有助于前端自己触发 /topics/timings）
        co.set_argument("--disable-background-timer-throttling")
        co.set_argument("--disable-backgrounding-occluded-windows")
        co.set_argument("--disable-renderer-backgrounding")

        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/130.0.0.0 Safari/537.36"
        )

        # ✅ 关键：显式告诉 DrissionPage 连接的端口（不同版本方法名不同）
        if hasattr(co, "set_local_port"):
            co.set_local_port(self._debug_port)
        elif hasattr(co, "set_port"):
            try:
                co.set_port(self._debug_port)
            except Exception:
                pass

        logger.info(f"Chrome: path={CHROME_PATH}, headless={HEADLESS}, port={self._debug_port}")
        logger.info(f"Chrome profile: {self._profile_dir}")

        # ✅ 启动浏览器
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()

        # requests 会话（用于登录 / connect info）
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
        self.session.get(
            HOME_FOR_COOKIE,
            headers=self._html_headers(),
            impersonate="chrome136",
            allow_redirects=True,
            timeout=30,
        )

        resp_csrf = self.session.get(
            CSRF_URL,
            headers=self._api_headers(),
            impersonate="chrome136",
            allow_redirects=True,
            timeout=30,
        )
        ct = (resp_csrf.headers.get("content-type") or "").lower()
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

        data = {"login": USERNAME, "password": PASSWORD, "timezone": "Asia/Shanghai"}

        try:
            resp_login = self.session.post(
                SESSION_URL,
                data=data,
                impersonate="chrome136",
                headers=headers,
                allow_redirects=True,
                timeout=30,
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
        dp_cookies = [
            {"name": name, "value": value, "domain": ".linux.do", "path": "/"}
            for name, value in cookies_dict.items()
        ]
        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至主题列表页 /latest ...")
        self.page.get(LIST_URL)

        try:
            self.page.wait.ele("@id=main-outlet", timeout=25)
        except Exception:
            logger.warning("未等到 main-outlet，但继续尝试查找 topic link")

        ok = self._wait_any_topic_link(timeout=35)
        if not ok:
            logger.warning("未等到主题链接 a.raw-topic-link")
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
    # Topic/Posts helpers
    # ----------------------------
    def _post_count_in_dom(self, page) -> int:
        try:
            # ✅ 引号完全安全：不会再出现 SyntaxError
            js = "return document.querySelectorAll('[id^=\"post_\"]').length;"
            return int(page.run_js(js) or 0)
        except Exception:
            return 0

    def _max_post_number_in_dom(self, page) -> int:
        try:
            js = r"""
            let maxN = 0;
            document.querySelectorAll('[id^="post_"]').forEach(el => {
              const m = el.id.match(/^post_(\d+)$/);
              if (m) maxN = Math.max(maxN, parseInt(m[1], 10));
            });
            return maxN;
            """
            return int(page.run_js(js) or 0)
        except Exception:
            return 0

    def wait_topic_posts_ready(self, page, timeout=60) -> bool:
        """
        ✅ 不再依赖 #post_1
        只要存在任意 post_数字 且正文区域有文本 => ready
        """
        end = time.time() + timeout
        while time.time() < end:
            try:
                js = f"""
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
                ok = page.run_js(js)
                if ok:
                    cnt = self._post_count_in_dom(page)
                    mx = self._max_post_number_in_dom(page)
                    mn = int(
                        page.run_js(
                            r"""
                            let minN = 999999;
                            document.querySelectorAll('[id^="post_"]').forEach(el=>{
                              const m = el.id.match(/^post_(\d+)$/);
                              if (m) minN = Math.min(minN, parseInt(m[1],10));
                            });
                            return (minN===999999)?0:minN;
                            """
                        )
                        or 0
                    )
                    logger.info(f"帖子流已渲染：dom_posts={cnt} range=post_{mn}..post_{mx}")
                    time.sleep(random.uniform(0.8, 1.6))
                    return True
            except Exception:
                pass
            time.sleep(0.6)

        logger.warning("未等到帖子流渲染完成（可能结构变化/加载慢/被拦截）")
        return False

    # ----------------------------
    # Blue-dot / read-state helpers
    # ----------------------------
    def _post_has_blue_dot(self, page, post_id: int) -> bool:
        """
        蓝点判断：存在 .read-state 且不包含 class 'read' => 未读
        """
        try:
            js = r"""
            const pid = arguments[0];
            const root = document.querySelector(`#post_${pid}`);
            if (!root) return false;
            const rs = root.querySelector('.topic-meta-data .read-state');
            if (!rs) return false;
            return !rs.classList.contains('read');
            """
            return bool(page.run_js(js, post_id))
        except Exception:
            return False

    def _post_is_read(self, page, post_id: int) -> bool:
        try:
            js = r"""
            const pid = arguments[0];
            const root = document.querySelector(`#post_${pid}`);
            if (!root) return false;
            const rs = root.querySelector('.topic-meta-data .read-state');
            if (!rs) return false;
            return rs.classList.contains('read');
            """
            return bool(page.run_js(js, post_id))
        except Exception:
            return False

    def _list_visible_posts_in_viewport(self, page):
        """
        返回视口内出现的 post_id 列表（按出现顺序）
        """
        try:
            js = r"""
            const els = Array.from(document.querySelectorAll('[id^="post_"]'));
            const v = [];
            for (const el of els) {
              const r = el.getBoundingClientRect();
              if (r.bottom < 0 || r.top > window.innerHeight) continue;
              const m = el.id.match(/^post_(\d+)$/);
              if (m) v.push(parseInt(m[1], 10));
            }
            return v;
            """
            ids = page.run_js(js)
            if not ids:
                return []
            return [int(x) for x in ids]
        except Exception:
            return []

    # ----------------------------
    # Human-like active stay (核心：让前端自己发 /topics/timings)
    # ----------------------------
    def _active_stay(self, page, seconds: float):
        """
        不是纯 sleep：小步滚动 + 随机节奏 + focus/mousemove/scroll event
        目标：像真人一样，让 Discourse 前端自然触发 /topics/timings 计阅读
        """
        end = time.time() + seconds
        while time.time() < end:
            step = random.randint(READ_STEP_MIN, READ_STEP_MAX)
            delay = random.uniform(READ_DELAY_MIN, READ_DELAY_MAX)
            try:
                page.run_js(
                    r"""
                    try { window.focus(); } catch(e) {}
                    try {
                      const ev = new MouseEvent('mousemove', {
                        clientX: 80 + Math.random()*600,
                        clientY: 80 + Math.random()*600
                      });
                      document.dispatchEvent(ev);
                    } catch(e) {}
                    try {
                      window.scrollBy(0, arguments[0]);
                      window.dispatchEvent(new Event('scroll'));
                    } catch(e) {}
                    """,
                    step,
                )
            except Exception:
                pass
            time.sleep(delay)

    def _read_post_like_human(self, page, post_id: int):
        """
        只读未读（蓝点）楼层：
        - 滚到楼层中间
        - 停留 >= MIN_READ_STAY（停留期间持续触发 scroll/mousemove/focus）
        - 最后检查 read-state.read 是否出现（不出现也不强求：以“触发timings”为主）
        """
        try:
            page.run_js(
                r"""
                const pid = arguments[0];
                const el = document.querySelector(`#post_${pid}`);
                if (el) el.scrollIntoView({behavior:'instant', block:'center'});
                """,
                post_id,
            )
        except Exception:
            pass

        stay = max(MIN_READ_STAY, random.uniform(MIN_READ_STAY, MIN_READ_STAY + 4.5))
        logger.info(f"👀 阅读未读楼层 post_{post_id}（停留≈{stay:.1f}s）")
        self._active_stay(page, stay)

        # 给 read-state 一个补充时间窗口
        if self._post_is_read(page, post_id):
            return True

        end = time.time() + READ_STATE_TIMEOUT
        while time.time() < end:
            if self._post_is_read(page, post_id):
                return True
            time.sleep(0.6)

        logger.warning(
            f"⚠️ post_{post_id} 停留已达阈值但蓝点未消失（read-state.read 未出现，可能前端状态延迟/风控/显示不同步）"
        )
        return False

    # ----------------------------
    # Near-bottom
    # ----------------------------
    def _near_bottom(self, page, gap=140) -> bool:
        try:
            js = r"""
            const d = document.documentElement;
            const y = window.scrollY || d.scrollTop || 0;
            const maxY = Math.max(0, d.scrollHeight - window.innerHeight);
            return (maxY - y) <= arguments[0];
            """
            return bool(page.run_js(js, gap))
        except Exception:
            return False

    # ----------------------------
    # Browse replies (5-10 pages) + 只读蓝点楼层
    # ----------------------------
    def browse_replies_pages(self, page, min_pages=5, max_pages=10):
        if max_pages < min_pages:
            max_pages = min_pages
        target_pages = random.randint(min_pages, max_pages)
        logger.info(f"目标：浏览评论 {target_pages} 页（按 PAGE_GROW={PAGE_GROW} 计页）")

        self.wait_topic_posts_ready(page, timeout=60)

        pages_done = 0
        last_max_no = self._max_post_number_in_dom(page)
        last_cnt = self._post_count_in_dom(page)
        logger.info(f"初始：max_post_no={last_max_no}, dom_posts={last_cnt}")

        max_loops = int(target_pages * MAX_LOOP_FACTOR + 20)

        # 避免同一楼层反复读
        seen_read_attempts = set()

        for i in range(max_loops):
            # 1) 大步滚动推进
            scroll_distance = random.randint(SCROLL_MIN, SCROLL_MAX)
            logger.info(f"[loop {i+1}] 向下滚动 {scroll_distance}px 浏览评论...")
            try:
                page.run_js("window.scrollBy(0, arguments[0]);", scroll_distance)
            except Exception:
                pass

            # 2) 等待渲染
            time.sleep(random.uniform(1.2, 2.0))

            # 3) 视口内只读蓝点楼层（最多 1~3 个）
            vp = self._list_visible_posts_in_viewport(page)
            unread = [pid for pid in vp if self._post_has_blue_dot(page, pid)]
            unread = [pid for pid in unread if pid not in seen_read_attempts]

            if unread:
                k = min(len(unread), random.randint(1, 3))
                for pid in unread[:k]:
                    seen_read_attempts.add(pid)
                    self._read_post_like_human(page, pid)

            # 4) “翻页”判断（按 max_post_no 增长）
            cur_max_no = self._max_post_number_in_dom(page)
            cur_cnt = self._post_count_in_dom(page)

            if cur_max_no - last_max_no >= PAGE_GROW:
                pages_done += 1
                logger.success(
                    f"✅ 第 {pages_done}/{target_pages} 页：max_post_no {last_max_no} -> {cur_max_no}（dom_posts={cur_cnt}）"
                )
                last_max_no = cur_max_no
                last_cnt = cur_cnt

            # 5) near-bottom：额外停留 + 小步滚动，促发“加载更多 + timings 上报”
            if self._near_bottom(page, gap=NEAR_BOTTOM_GAP):
                extra = random.uniform(BOTTOM_EXTRA_STAY_MIN, BOTTOM_EXTRA_STAY_MAX)
                logger.info(
                    f"[loop {i+1}] 接近底部（gap<={NEAR_BOTTOM_GAP}px），额外停留≈{extra:.1f}s"
                )
                self._active_stay(page, extra)

            # 6) 达标退出
            if pages_done >= target_pages:
                logger.success("🎉 已达到目标评论页数，结束浏览")
                return True

            # 7) 强到底判断
            try:
                at_bottom = page.run_js(
                    "return (window.scrollY + window.innerHeight) >= (document.body.scrollHeight - 5);"
                )
            except Exception:
                at_bottom = False

            if at_bottom:
                logger.success("已到达页面底部，结束浏览")
                # 短帖容错：楼层总量不足时不算失败
                if cur_max_no <= (min_pages * PAGE_GROW + 5):
                    logger.info(f"主题较短（max_post_no≈{cur_max_no}），放宽最小页数要求，视为完成")
                    return True
                return pages_done >= min_pages

        logger.warning("达到最大循环次数仍未完成目标页数（可能加载慢/主题很短/被拦截）")
        return pages_done >= min_pages

    # ----------------------------
    # Browse from latest list
    # ----------------------------
    def click_topic(self):
        if not self.page.url.startswith("https://linux.do/latest"):
            self.page.get(LIST_URL)

        if not self._wait_any_topic_link(timeout=35):
            logger.error("未找到 a.raw-topic-link（主题标题链接）")
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

            self.wait_topic_posts_ready(new_page, timeout=60)
            time.sleep(random.uniform(1.0, 2.0))

            # 点赞（可选）
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
        headers = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
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
                f"PAGE_GROW={PAGE_GROW}, MIN_READ_STAY={MIN_READ_STAY}s, READ_STATE_TIMEOUT={READ_STATE_TIMEOUT}s, "
                f"HEADLESS={HEADLESS}, port={self._debug_port})"
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
                    headers={"Authorization": WXPUSH_TOKEN, "Content-Type": "application/json"},
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
            try:
                # 清理 profile
                for _ in range(3):
                    try:
                        if self._profile_dir.exists():
                            for p in self._profile_dir.rglob("*"):
                                try:
                                    p.chmod(0o777)
                                except Exception:
                                    pass
                            # 递归删除
                            import shutil
                            shutil.rmtree(self._profile_dir, ignore_errors=True)
                        break
                    except Exception:
                        time.sleep(0.5)
            except Exception:
                pass


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("Please set LINUXDO_USERNAME/LINUXDO_PASSWORD (or USERNAME/PASSWORD)")
        raise SystemExit(1)

    l = LinuxDoBrowser()
    l.run()
