"""一次性登录：打开抖音，等你扫码，登录态存进 chrome-profile 供后续复用。"""
import time

from playwright.sync_api import sync_playwright

from core import export_cookies, is_logged_in, launch_browser, load_config, setup_logger

WAIT_SECONDS = 300


def main():
    log = setup_logger("login")
    cfg = load_config()
    log.info("启动 Chrome，准备登录抖音")

    with sync_playwright() as p:
        ctx = launch_browser(p, cfg, log)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if is_logged_in(ctx.cookies()):
            log.info("检测到已有登录态，无需重复扫码")
            export_cookies(ctx.cookies())
            ctx.close()
            return

        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        print("\n" + "=" * 56)
        print("  请在弹出的 Chrome 窗口中，用抖音 App 扫码登录")
        print("  登录成功后本窗口会自动关闭，无需其他操作")
        print(f"  最长等待 {WAIT_SECONDS // 60} 分钟")
        print("=" * 56 + "\n")

        deadline = time.time() + WAIT_SECONDS
        ok = False
        while time.time() < deadline:
            page.wait_for_timeout(2000)
            if is_logged_in(ctx.cookies()):
                ok = True
                break
            # 有些账号是手机号登录而非扫码，也允许
            if page.url.startswith("https://www.douyin.com/") and is_logged_in(ctx.cookies()):
                ok = True
                break

        if not ok:
            log.error("等待超时，未检测到登录态")
            print("\n[失败] 未检测到登录。请重跑本脚本，并在窗口内完成扫码。")
            ctx.close()
            raise SystemExit(1)

        export_cookies(ctx.cookies())
        log.info("登录成功，登录态已保存")
        print("\n[成功] 登录态已保存，之后每天的自动任务都会复用它。")
        ctx.close()


if __name__ == "__main__":
    main()
