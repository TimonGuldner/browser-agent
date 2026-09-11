import asyncio
import os
import re

from agent import google_ads_v7_localhostrun as impl


def extract_actual_tunnel_url(text: str) -> str:
    text = impl.strip_ansi(text)
    # localhost.run prints the assigned free hostname on a line like:
    # <host> tunneled with tls termination, https://<host>
    for line in text.splitlines():
        if 'tunneled with tls termination' not in line.lower():
            continue
        urls = re.findall(r'https://[A-Za-z0-9][A-Za-z0-9.-]*', line)
        if urls:
            return urls[-1].rstrip('/')

    # Fallback for current free-domain families only; never pick admin/docs hosts.
    candidates = re.findall(r'https://[A-Za-z0-9][A-Za-z0-9.-]*', text)
    allowed_suffixes = ('.lhr.life', '.lhrtunnel.link', '.localhost.run')
    blocked_hosts = {
        'localhost.run', 'www.localhost.run', 'admin.localhost.run',
        'v2.admin.localhost.run', 'docs.localhost.run',
    }
    for url in reversed(candidates):
        host = url.split('://', 1)[1].lower().rstrip('.')
        if host in blocked_hosts:
            continue
        if host.endswith(allowed_suffixes):
            return url.rstrip('/')
    return ''


impl.extract_tunnel_url = extract_actual_tunnel_url


def main() -> None:
    command = os.sys.argv[1] if len(os.sys.argv) > 1 else 'start'
    if command == 'start':
        impl.start()
    elif command == 'run':
        asyncio.run(impl.run())
    else:
        raise SystemExit(f'Unknown command: {command}')


if __name__ == '__main__':
    main()
