"""Local administration; never start workers or make network requests."""

import argparse
import getpass
import json
import secrets

from .core import Problem, Vault


def initialize_password(vault, prompt=getpass.getpass):
    if vault.get("password"):
        raise Problem("访问密码已设置，请使用网页登录或修改密码", 409)
    password = prompt("设置 MediaLink 访问密码（8–200 字符）: ")
    confirmation = prompt("再次输入密码: ")
    if not 8 <= len(password) <= 200 or password != confirmation:
        raise Problem("密码长度不符合要求或两次输入不一致", 422)
    # Import the same hash function used by the HTTP login, without creating an app.
    from .app import password_hash
    salt = secrets.token_hex(16)
    vault.set("password", json.dumps({"salt": salt, "hash": password_hash(password, salt)}))


def main():
    parser = argparse.ArgumentParser(description="MediaLink 本地管理（不会启动服务或入库任务）")
    parser.add_argument("command", choices=["init-password"])
    parser.parse_args()
    try:
        initialize_password(Vault())
    except (Problem, EOFError, KeyboardInterrupt) as exc:
        parser.exit(1, (exc.message if isinstance(exc, Problem) else "已取消密码初始化") + "\n")
    print("访问密码已设置，请打开 MediaLink 网页登录。")


if __name__ == "__main__":
    main()
