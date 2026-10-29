import ipaddress
import json
import re

from .core import Problem


def normalize_host(host):
    host = host.strip().lower().rstrip('.')
    try:
        return str(ipaddress.ip_address(host.removeprefix('[').removesuffix(']')))
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError:
            raise Problem('请输入有效的域名或 IP，不包含协议、端口或路径', 422) from None
        if len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                      for label in host.split('.')):
            raise Problem('请输入有效的域名或 IP，不包含协议、端口或路径', 422)
        return host


def normalize_public_hosts(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise Problem('公网域名/IP 列表必须是文本，且不超过 2048 个字符', 422)
    return ','.join(dict.fromkeys(normalize_host(host) for host in re.split(r'[,\n]', value) if host.strip()))


def host_allowed(host, settings):
    try:
        host = normalize_host(host)
    except Problem:
        return False
    if host in {'localhost', 'testserver'}:
        return True
    try:
        if ipaddress.ip_address(host).is_private:
            return True
    except ValueError:
        pass
    return settings['public_access_enabled'] is True and host in settings['public_access_hosts'].split(',')


def migrate_public_access(store, legacy_hosts):
    if not legacy_hosts.strip():
        return
    with store.connect() as db:
        if db.execute("SELECT 1 FROM settings WHERE key='public_access_enabled'").fetchone():
            return
        hosts = normalize_public_hosts(legacy_hosts)
        if not hosts:
            return
        db.execute("INSERT INTO settings VALUES ('public_access_enabled', 'true')")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('public_access_hosts', ?)", (json.dumps(hosts),))
