"""Anti-crawl layer for the job-discovery skill.

旁路增强：新增的登录 + 反爬能力，与现有 browse.py 业务逻辑完全隔离。
只新增文件；现有 scripts/、adapters/、references/ 一概不动。

用法入口见 scripts/login.py / check_login.py / crawl.py / anti_crawl_selftest.py。
合规边界见 references/anti-crawl-guide.md：不破解验证码、不注入 JS 绕过风控，
滑块/验证码一律人工协作；礼貌限速为默认纪律。
"""

__version__ = "1.0.0"
