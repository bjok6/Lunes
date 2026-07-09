#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import platform
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ================== 配置 ==================
BETADASH_LOGIN_URL = "https://betadash.lunes.host/login"
OUTPUT_DIR = Path("output/screenshots")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("lunes-keep")


# ================== 辅助函数 ==================
def is_linux() -> bool:
    return platform.system().lower() == "linux"


def mask_url(url: str) -> str:
    return re.sub(r'/servers/\d+', '/servers/***', url)


def mask_email(email: str) -> str:
    if '@' not in email:
        return email[:1] + "***"
    local, domain = email.split('@', 1)
    masked_local = local[:1] + "***" if local else "***"
    if '.' in domain:
        parts = domain.split('.')
        tld = parts[-1]
        first_char = domain[0]
        masked_domain = f"{first_char}***.{tld}" if len(parts) > 1 else f"{first_char}***"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


def mask_server_id(server_id: str) -> str:
    if not server_id or server_id == "Unknown":
        return server_id
    if len(server_id) <= 2:
        return server_id + "***"
    return server_id[:2] + "***"


def setup_display():
    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            logger.info("虚拟显示已启动")
            return display
        except Exception as e:
            logger.error(f"虚拟显示启动失败: {e}")
            sys.exit(1)
    return None


def screenshot_path(name: str) -> str:
    return str(OUTPUT_DIR / f"{datetime.now().strftime('%H%M%S')}-{name}.png")


def safe_screenshot(sb, path: str):
    try:
        sb.save_screenshot(path)
        logger.info(f"📸 截图 → {Path(path).name}")
    except Exception as e:
        logger.warning(f"截图失败: {e}")


def notify_telegram(email: str, ok: bool, msg: str = "", screenshot_file: str = None):
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return

        status = "✅ 保活成功" if ok else "❌ 保活失败"
        lines = [status, "", f"账号：{email}"]
        if msg:
            lines.append(f"信息：{msg}")
        lines.append(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append("Lunes Host Auto Keep Alive")
        text = "\n".join(lines)

        if screenshot_file and Path(screenshot_file).exists():
            with open(screenshot_file, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": text},
                    files={"photo": f},
                    timeout=60
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                timeout=30
            )
    except Exception as e:
        logger.warning(f"Telegram 通知失败: {e}")


def check_and_exit_on_rate_limit(sb, email: str) -> None:
    try:
        page_source = sb.get_page_source()
        if "Too Many Requests" in page_source or "20 per 1 hour" in page_source:
            logger.error("⛔ 检测到速率限制: Too Many Requests / 20 per 1 hour")
            sp = screenshot_path("rate-limit")
            safe_screenshot(sb, sp)

            notify_telegram(
                email=email,
                ok=False,
                msg="IP 已被限制（Too Many Requests, 20/h），脚本已停止",
                screenshot_file=sp
            )
            sys.exit(1)
    except Exception:
        pass


def parse_single_account() -> tuple[str, str]:
    raw = os.environ.get("LUNES", "").strip()
    if not raw:
        logger.error("未设置环境变量 LUNES，请设置 LUNES=邮箱-----密码")
        sys.exit(1)

    try:
        parts = raw.split("-----")
        if len(parts) >= 2:
            email = parts[0].strip()
            password = parts[1].strip()
            if email and password:
                logger.info(f"读取到账号: {mask_email(email)}")
                return email, password
            else:
                logger.error("LUNES 中邮箱或密码为空")
                sys.exit(1)
        else:
            logger.error(f"LUNES 格式错误，期望 '邮箱-----密码'，实际: {raw}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"解析 LUNES 失败: {e}")
        sys.exit(1)


# ================== Cloudflare 处理 ==================
def is_cloudflare_interstitial(sb) -> bool:
    try:
        has_login_form = sb.execute_script('''
            return !!(document.querySelector('input#email')
                   || document.querySelector('input[name="email"]')
                   || document.querySelector('form[action*="login"]'));
        ''')
        if has_login_form:
            return False

        has_dashboard = sb.execute_script('''
            return !!(document.querySelector('a.server-card')
                   || document.querySelector('.dashboard')
                   || document.querySelector('.sidebar'));
        ''')
        if has_dashboard:
            return False

        page_source = sb.get_page_source()
        title = sb.get_title().lower() if sb.get_title() else ""

        strong_indicators = [
            "Just a moment",
            "Verify you are human",
            "Checking your browser",
            "Checking if the site connection is secure",
        ]
        for indicator in strong_indicators:
            if indicator in page_source:
                return True

        if "just a moment" in title or "attention required" in title:
            return True

        body_text_len = sb.execute_script('''
            return (document.body && document.body.innerText)
                ? document.body.innerText.trim().length : 0;
        ''')
        if body_text_len < 100 and "challenges.cloudflare.com" in page_source:
            return True

        return False
    except:
        return False


def bypass_cloudflare_interstitial(sb, email: str, max_attempts: int = 3) -> bool:
    logger.info("检测到 Cloudflare 整页挑战，尝试绕过...")
    for attempt in range(max_attempts):
        logger.info(f"CF 绕过尝试 {attempt + 1}/{max_attempts}")
        try:
            sb.uc_gui_click_captcha()
            time.sleep(6)
            check_and_exit_on_rate_limit(sb, email)
            if not is_cloudflare_interstitial(sb):
                logger.info("✅ Cloudflare 挑战已通过")
                return True
        except Exception as e:
            logger.warning(f"CF 绕过尝试 {attempt + 1} 失败: {e}")
        time.sleep(3)

    logger.info("尝试刷新页面重新加载...")
    try:
        sb.uc_open_with_reconnect(BETADASH_LOGIN_URL, reconnect_time=10)
        time.sleep(5)
        check_and_exit_on_rate_limit(sb, email)
        if not is_cloudflare_interstitial(sb):
            logger.info("✅ 刷新后 Cloudflare 挑战已消失")
            return True
    except:
        pass
    return False


def wait_for_turnstile_success(sb, timeout: int = 30) -> bool:
    logger.info("等待 Turnstile 验证...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            success = sb.execute_script('''
                var resp = document.querySelector('input[name="cf-turnstile-response"]');
                if (resp && resp.value && resp.value.length > 20) return true;
                var grecap = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (grecap && grecap.value && grecap.value.length > 20) return true;
                var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe && iframe.getAttribute("data-state") === "solved") return true;
                return false;
            ''')
            if success:
                logger.info("✅ Turnstile 验证成功")
                return True
        except:
            pass
        time.sleep(1)
    logger.warning("⏰ Turnstile 验证超时")
    return False


# ================== 登录流程 ==================
def clear_browser_state(sb):
    """清除浏览器 Cookie、localStorage、sessionStorage"""
    try:
        sb.execute_script('''
            try { window.localStorage.clear(); } catch(e) {}
            try { window.sessionStorage.clear(); } catch(e) {}
        ''')
    except:
        pass
    try:
        sb.delete_all_cookies()
    except:
        pass
    logger.info("🧹 浏览器状态已清理")


def handle_initial_page(sb, email: str) -> Optional[str]:
    clear_browser_state(sb)

    logger.info("访问登录页...")
    sb.uc_open_with_reconnect(BETADASH_LOGIN_URL, reconnect_time=8)
    time.sleep(4)

    check_and_exit_on_rate_limit(sb, email)

    sp = screenshot_path("01-initial")
    safe_screenshot(sb, sp)

    current_url = sb.get_current_url()
    logger.info(f"当前URL: {mask_url(current_url)}")

    if "/login" not in current_url and current_url != BETADASH_LOGIN_URL:
        logger.info("页面未到达登录页，尝试强制导航...")
        clear_browser_state(sb)
        sb.uc_open_with_reconnect(BETADASH_LOGIN_URL, reconnect_time=8)
        time.sleep(4)
        check_and_exit_on_rate_limit(sb, email)
        current_url = sb.get_current_url()
        logger.info(f"强制导航后URL: {mask_url(current_url)}")

    if is_cloudflare_interstitial(sb):
        logger.info("检测到 Cloudflare 整页挑战")
        if not bypass_cloudflare_interstitial(sb, email):
            sp = screenshot_path("02-cf-failed")
            safe_screenshot(sb, sp)
            return None
        time.sleep(3)
        current_url = sb.get_current_url()
        if "/login" not in current_url:
            logger.info("✅ CF 通过后已自动登录")
            return "already_logged"

    logger.info("等待登录表单...")
    for wait_round in range(3):
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
            logger.info("✅ 找到登录表单")
            return "need_login"
        except TimeoutException:
            logger.info(f"第 {wait_round + 1} 次等待表单超时，检查页面状态...")
            check_and_exit_on_rate_limit(sb, email)
            if is_cloudflare_interstitial(sb):
                logger.info("表单等待期间出现 CF 挑战")
                bypass_cloudflare_interstitial(sb, email, max_attempts=2)
                time.sleep(3)
            else:
                time.sleep(3)

    sp = screenshot_path("02-no-form")
    safe_screenshot(sb, sp)
    logger.error("未找到登录表单")
    return None


def fill_and_submit(sb, email: str, password: str) -> bool:
    """填写登录表单并提交"""
    logger.info("填写登录信息...")

    try:
        sb.clear('input#email')
    except:
        pass
    sb.type('input#email', email)
    time.sleep(0.5)

    try:
        sb.clear('input#password')
    except:
        pass
    sb.type('input#password', password)
    time.sleep(0.5)

    sp = screenshot_path("03-form-filled")
    safe_screenshot(sb, sp)

    logger.info("处理 Turnstile 验证码...")
    already_done = wait_for_turnstile_success(sb, timeout=5)
    if already_done:
        logger.info("Turnstile 已自动完成，直接提交")
    else:
        logger.info("Turnstile 未自动完成，尝试点击...")
        for click_attempt in range(3):
            try:
                sb.uc_gui_click_captcha()
                logger.info(f"点击验证码第 {click_attempt + 1} 次")
            except Exception as e:
                logger.warning(f"点击验证码异常 (第 {click_attempt + 1} 次): {e}")
            time.sleep(2)
            if wait_for_turnstile_success(sb, timeout=10):
                break
        else:
            logger.warning("Turnstile 多次尝试后仍未通过，尝试强制提交...")

    sp = screenshot_path("04-before-submit")
    safe_screenshot(sb, sp)

    logger.info("提交登录...")
    submitted = False
    for selector in ['button.submit-btn', 'button[type="submit"]']:
        try:
            sb.click(selector)
            submitted = True
            logger.info(f"通过 {selector} 提交")
            break
        except:
            continue

    if not submitted:
        try:
            sb.execute_script('document.querySelector("form").submit()')
            submitted = True
            logger.info("通过 JS 提交")
        except:
            logger.error("所有提交方式都失败")
            return False

    logger.info("等待登录结果...")
    time.sleep(6)

    check_and_exit_on_rate_limit(sb, email)

    current_url = sb.get_current_url()
    logger.info(f"登录后URL: {mask_url(current_url)}")

    if "/login" in current_url:
        try:
            error_msg = sb.execute_script('''
                var flash = document.querySelector('.flash-message, .error-message, .alert');
                return flash ? flash.innerText.trim() : '';
            ''')
            if error_msg:
                logger.error(f"登录错误提示: {error_msg}")
        except:
            pass
        sp = screenshot_path("05-login-failed")
        safe_screenshot(sb, sp)
        logger.error("登录失败 - 仍在登录页")
        return False

    logger.info("✅ 登录成功！")
    return True


def _extract_server_id(url: str) -> str:
    match = re.search(r'/servers/(\d+)', url)
    return match.group(1) if match else "Unknown"


def navigate_to_server(sb, email: str) -> tuple[bool, str, Optional[str]]:
    time.sleep(3)

    sp_dash = screenshot_path("06-dashboard")
    safe_screenshot(sb, sp_dash)

    check_and_exit_on_rate_limit(sb, email)

    current_url = sb.get_current_url()
    logger.info(f"仪表盘URL: {mask_url(current_url)}")

    if "/servers/" in current_url:
        logger.info("✅ 已在服务器详情页")
        server_id = _extract_server_id(current_url)
        logger.info(f"服务器卡片 ID: {mask_server_id(server_id)}")
        return True, server_id, sp_dash

    found_selector = None
    primary_selector = 'a.server-card'
    alt_selectors = [
        'a[href*="/servers/"]',
        '.server-card',
        'div.server-card',
        'a[class*="server"]',
    ]

    try:
        sb.wait_for_element_visible(primary_selector, timeout=15)
        found_selector = primary_selector
        logger.info("✅ 找到服务器卡片")
    except TimeoutException:
        for sel in alt_selectors:
            try:
                sb.wait_for_element_visible(sel, timeout=5)
                found_selector = sel
                logger.info(f"✅ 通过备选选择器找到服务器: {sel}")
                break
            except:
                continue

    if found_selector is None:
        sp = screenshot_path("07-no-server")
        safe_screenshot(sb, sp)

        try:
            body_text = sb.execute_script(
                "return document.body ? document.body.innerText.trim() : '';"
            )
            no_server_hints = [
                "no server", "no servers", "没有服务器", "create a server",
                "get started", "deploy", "no instance", "no instances",
            ]
            body_lower = body_text.lower()
            is_no_server = any(hint in body_lower for hint in no_server_hints)

            if is_no_server:
                logger.warning("⚠️  该账号下没有服务器（可能已被删除或未创建）")
                return False, "NO_SERVER", sp
            else:
                logger.error(f"未找到服务器卡片，页面内容片段: {body_text[:200]}")
                return False, "PAGE_ERROR", sp
        except Exception as e:
            logger.error(f"读取页面内容失败: {e}")
            return False, "PAGE_ERROR", sp

    try:
        href = sb.execute_script(f"return document.querySelector('{found_selector}').getAttribute('href') || '';")
        card_id = _extract_server_id(href) if href else "Unknown"
    except:
        card_id = "Unknown"
    logger.info(f"服务器卡片 ID: {mask_server_id(card_id)}")

    logger.info("进入服务器详情页...")
    try:
        sb.click(found_selector)
    except:
        try:
            sb.execute_script(f'''
                var card = document.querySelector('{found_selector}');
                if (card) card.click();
            ''')
        except:
            logger.error("无法点击服务器卡片")
            return False, "CLICK_ERROR", sp_dash

    time.sleep(5)
    current_url = sb.get_current_url()
    logger.info(f"详情页URL: {mask_url(current_url)}")

    sp_detail = screenshot_path("08-server-detail")
    safe_screenshot(sb, sp_detail)

    if "/servers/" in current_url:
        server_id = _extract_server_id(current_url)
        logger.info("✅ 成功进入服务器详情页")
        return True, server_id, sp_detail
    else:
        logger.error("未能进入服务器详情页")
        return False, "NAV_ERROR", sp_detail


# ================== 主登录函数 ==================
def betadash_login(
    email: str,
    password: str,
    proxy: Optional[str] = None,
    max_retries: int = 1,
) -> dict:
    """执行登录并保活，仅在未触发速率限制时运行一次"""
    result = {"success": False, "message": "", "screenshot": None}

    logger.info("=" * 50)
    logger.info(f"Betadash 保活 | 邮箱: {mask_email(email)}")
    logger.info("=" * 50)

    for attempt in range(max_retries):
        if attempt > 0:
            logger.warning("已进行过一次尝试，不再重试以避免触发速率限制")
            break

        try:
            sb_kwargs = dict(
                uc=True,
                test=True,
                locale="en",
                headed=not is_linux(),
                user_data_dir=None,
                chromium_arg="--disable-blink-features=AutomationControlled",
            )
            if proxy:
                sb_kwargs["proxy"] = proxy

            with SB(**sb_kwargs) as sb:
                init_status = handle_initial_page(sb, email)
                if init_status is None:
                    result["message"] = "Cloudflare 绕过失败或登录表单未找到"
                    result["screenshot"] = screenshot_path("02-cf-failed")
                    logger.warning(result["message"])
                    break

                if init_status == "need_login":
                    login_ok = fill_and_submit(sb, email, password)
                    if not login_ok:
                        result["message"] = "登录失败（凭据错误或提交异常）"
                        result["screenshot"] = screenshot_path("05-login-failed")
                        logger.warning(result["message"])
                        break

                nav_ok, server_id, screenshot = navigate_to_server(sb, email)

                if nav_ok:
                    result.update(
                        success=True,
                        message=f"服务器: {server_id}",
                        screenshot=screenshot,
                    )
                    logger.info("✅ 保活成功！")
                    return result

                if server_id == "NO_SERVER":
                    result.update(
                        success=True,
                        message="登录成功，但该账号下没有服务器（可能已被删除）",
                        screenshot=screenshot,
                    )
                    logger.warning("⚠️  账号保活成功（无服务器），请确认服务器状态")
                    return result
                else:
                    result.update(
                        message=f"导航失败: {server_id}",
                        screenshot=screenshot,
                    )
                    logger.error(f"导航失败 (code={server_id})，不再重试")
                    return result

        except Exception as e:
            logger.exception(f"执行异常: {e}")
            result["message"] = str(e)

    return result


# ================== 主程序 ==================
def main():
    email, password = parse_single_account()
    proxy = os.environ.get("PROXY_SERVER")
    display = setup_display()

    try:
        result = betadash_login(email, password, proxy, max_retries=1)

        notify_telegram(
            email=email,
            ok=result["success"],
            msg=result["message"],
            screenshot_file=result["screenshot"],
        )

        if result["success"]:
            logger.info("✅ 保活流程完成")
            sys.exit(0)
        else:
            logger.error("❌ 保活失败")
            sys.exit(1)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
